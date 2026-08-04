"""Testes do nucleo: fetcher, frontier, store e pipeline.

O foco esta nas propriedades que o plano trata como nao-negociaveis —
identidade por conteudo, idempotencia, respeito a politica — porque sao elas que
sustentam a reprodutibilidade da Etapa 1.
"""

from pathlib import Path

import httpx
import pytest
import respx

from crawler.core.fetcher import BlockedByPolicy, Config, Fetcher, NotADocument, sniff_kind
from crawler.core.frontier import (
    STATUS_DISCOVERED,
    STATUS_DUPLICATE,
    STATUS_STORED,
    Frontier,
)
from crawler.core.pipeline import Pipeline
from crawler.core.prefilter import Lexicon
from crawler.core.record import DocumentRecord, canonical_url
from crawler.core.store import Store

ROOT = Path(__file__).resolve().parent.parent
PDF = b"%PDF-1.7\n" + b"x" * 400 + b"\n%%EOF"


@pytest.fixture
def config():
    cfg = Config.load(ROOT / "config" / "domains.yaml")
    cfg.defaults.update({"respect_robots": False, "rate": 0.0, "retries": 2, "backoff_base_s": 0.0})
    for d in cfg.domains.values():
        d.update({"respect_robots": False, "rate": 0.0})
    return cfg


@pytest.fixture
def fetcher(config):
    f = Fetcher(config)
    yield f
    f.close()


# ------------------------------------------------------------------ deteccao


class TestMagicBytes:
    """Servidores governamentais erram Content-Type com frequencia; a decisao
    tem de vir dos bytes."""

    def test_pdf_reconhecido(self):
        assert sniff_kind(PDF, "application/octet-stream") == "pdf"

    def test_html_disfarcado_de_pdf_e_recusado(self):
        """Caso real: pagina de erro devolvida com HTTP 200 e Content-Type PDF."""
        corpo = b"<!DOCTYPE html><html><body>Not Found</body></html>"
        assert sniff_kind(corpo, "application/pdf") == "html"

    def test_content_type_pdf_sem_magic_nao_vira_pdf(self):
        assert sniff_kind(b"\x00\x01\x02lixo binario", "application/pdf") == "unknown"

    def test_office_moderno(self):
        assert sniff_kind(b"PK\x03\x04abc", None) == "zip"


class TestPoliticaDeFetch:
    @respx.mock
    def test_blocklist_impede_acesso(self, fetcher):
        """dms.cosmos.esa.int exige autenticacao: nao se tenta, por decisao."""
        rota = respx.get(url__startswith="https://dms.cosmos.esa.int").mock(
            return_value=httpx.Response(200, content=PDF)
        )
        with pytest.raises(BlockedByPolicy, match="blocklist"):
            fetcher.fetch("https://dms.cosmos.esa.int/doc/1.pdf")
        assert not rota.called, "a requisicao nao pode nem sair"

    @respx.mock
    def test_teto_de_tamanho_por_content_length(self, fetcher):
        respx.get("https://ntrs.nasa.gov/big.pdf").mock(
            return_value=httpx.Response(
                200, content=PDF, headers={"Content-Length": str(200 * 1024 * 1024)}
            )
        )
        with pytest.raises(BlockedByPolicy, match="teto"):
            fetcher.fetch("https://ntrs.nasa.gov/big.pdf")

    @respx.mock
    def test_teto_aplicado_mesmo_sem_content_length(self, config):
        """Alguns relatorios da NASA passam de 300 MB e o header pode faltar.

        Resposta em chunks (transfer-encoding: chunked) nao traz Content-Length,
        entao o teto so pode ser aplicado durante a leitura do corpo.
        """
        config.defaults["max_file_mb"] = 0.001
        config.domains["ntrs.nasa.gov"]["max_file_mb"] = 0.001
        with Fetcher(config) as f:
            respx.get("https://ntrs.nasa.gov/x.pdf").mock(
                return_value=httpx.Response(
                    200, content=iter([b"%PDF-", *[b"y" * 8192] * 8])
                )
            )
            with pytest.raises(BlockedByPolicy, match="durante o download"):
                f.fetch("https://ntrs.nasa.gov/x.pdf")

    @respx.mock
    def test_304_nao_rebaixa_conteudo(self, fetcher):
        respx.get("https://ntrs.nasa.gov/a.pdf").mock(return_value=httpx.Response(304))
        res = fetcher.fetch("https://ntrs.nasa.gov/a.pdf", etag='"abc"')
        assert res.from_cache and res.body is None

    @respx.mock
    def test_envia_cabecalhos_condicionais(self, fetcher):
        capturado = {}

        def responder(request: httpx.Request) -> httpx.Response:
            capturado.update(request.headers)
            return httpx.Response(200, content=PDF)

        respx.get("https://ntrs.nasa.gov/a.pdf").mock(side_effect=responder)
        fetcher.fetch("https://ntrs.nasa.gov/a.pdf", etag='"abc"', last_modified="Thu, 06 Aug 2020 21:54:17 GMT")
        assert capturado["if-none-match"] == '"abc"'
        assert capturado["if-modified-since"].startswith("Thu, 06 Aug 2020")

    @respx.mock
    def test_user_agent_identificado(self, fetcher):
        capturado = {}

        def responder(request: httpx.Request) -> httpx.Response:
            capturado["ua"] = request.headers["user-agent"]
            return httpx.Response(200, content=PDF)

        respx.get("https://ntrs.nasa.gov/a.pdf").mock(side_effect=responder)
        fetcher.fetch("https://ntrs.nasa.gov/a.pdf")
        assert "UNICAMP" in capturado["ua"] and "mailto:" in capturado["ua"]

    @respx.mock
    def test_retry_em_429_e_depois_sucesso(self, fetcher):
        respostas = [
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, content=PDF),
        ]
        respx.get("https://ntrs.nasa.gov/a.pdf").mock(side_effect=respostas)
        res = fetcher.fetch("https://ntrs.nasa.gov/a.pdf")
        assert res.ok and res.kind == "pdf"

    @respx.mock
    def test_expect_document_recusa_html(self, fetcher):
        respx.get("https://ntrs.nasa.gov/a.pdf").mock(
            return_value=httpx.Response(200, content=b"<html>erro</html>")
        )
        with pytest.raises(NotADocument):
            fetcher.fetch("https://ntrs.nasa.gov/a.pdf", expect_document=True)


class TestCanonicalizacaoDeUrl:
    def test_normaliza_host_e_esquema(self):
        assert canonical_url("HTTPS://NTRS.NASA.GOV/a/") == "https://ntrs.nasa.gov/a"

    def test_preserva_query(self):
        """No Liferay (Cosmos) o `?t=` faz parte do endereco; remove-lo quebra."""
        u = "https://www.cosmos.esa.int/documents/1/2/x/uuid?t=1699999"
        assert canonical_url(u).endswith("?t=1699999")


# ----------------------------------------------------------------- pipeline


@pytest.fixture
def ambiente(tmp_path, fetcher):
    store = Store(tmp_path)
    frontier = Frontier(tmp_path / "f.sqlite")
    lex = Lexicon.load(ROOT / "config" / "lexicon.yaml")
    yield Pipeline(frontier, store, fetcher, lex), frontier, store
    frontier.close()


def make_rec(source: str, sid: str, url: str, titulo: str = "Concept of Operations for X"):
    return DocumentRecord(
        source=source,
        source_id=sid,
        title=titulo,
        landing_url=f"https://example.org/{sid}",
        candidate_urls=[url],
    )


class FakeAdapter:
    name = "fake"

    def __init__(self, recs):
        self._recs = recs

    def discover(self):
        yield from self._recs


class TestIdempotencia:
    @respx.mock
    def test_coletar_duas_vezes_nao_duplica(self, ambiente):
        pipeline, frontier, _ = ambiente
        respx.get("https://ntrs.nasa.gov/a.pdf").mock(
            return_value=httpx.Response(200, content=PDF)
        )
        recs = [make_rec("ntrs", "1", "https://ntrs.nasa.gov/a.pdf")]

        pipeline.discover(FakeAdapter(recs))
        pipeline.harvest()
        segunda = pipeline.discover(FakeAdapter(recs))

        assert segunda.novos == 0 and segunda.ja_conhecidos == 1
        assert frontier.counts_by("status")[STATUS_STORED] == 1

    @respx.mock
    def test_retomada_apos_interrupcao(self, ambiente):
        """A fila guarda o estado: so o que ficou pendente e refeito."""
        pipeline, frontier, _ = ambiente
        respx.get(url__startswith="https://ntrs.nasa.gov/").mock(
            side_effect=lambda r: httpx.Response(200, content=PDF + str(r.url).encode())
        )
        recs = [make_rec("ntrs", str(i), f"https://ntrs.nasa.gov/{i}.pdf") for i in range(5)]
        pipeline.discover(FakeAdapter(recs))

        pipeline.harvest(limit=2)
        assert len(list(frontier.pending())) == 3

        pipeline.harvest()
        assert list(frontier.pending()) == []


class TestDeduplicacaoPorConteudo:
    @respx.mock
    def test_mesmo_pdf_em_duas_fontes_vira_um_arquivo(self, ambiente):
        """O mesmo ConOps vindo da FAA e do ROSA P nao pode contar duas vezes —
        sem isso as metricas inflam artificialmente."""
        pipeline, frontier, store = ambiente
        respx.get("https://ntrs.nasa.gov/a.pdf").mock(return_value=httpx.Response(200, content=PDF))
        respx.get("https://rosap.ntl.bts.gov/b.pdf").mock(return_value=httpx.Response(200, content=PDF))

        pipeline.discover(
            FakeAdapter(
                [
                    make_rec("ntrs", "1", "https://ntrs.nasa.gov/a.pdf"),
                    make_rec("rosap", "2", "https://rosap.ntl.bts.gov/b.pdf"),
                ]
            )
        )
        st = pipeline.harvest()

        assert st.armazenados == 1 and st.duplicados == 1
        arquivos = list((store.raw).rglob("*.pdf"))
        assert len(arquivos) == 1, "o conteudo identico deveria ocupar um unico arquivo"

        dup = frontier.duplicate_stats()
        assert dup["conteudos_unicos"] == 1
        assert dup["conteudos_em_multiplas_fontes"] == 1
        assert dup["sobreposicao_por_par"] == {"ntrs x rosap": 1}

    @respx.mock
    def test_url_nao_e_chave_de_identidade(self, ambiente):
        """No Liferay a URL muda quando o documento e reeditado; so o SHA-256
        identifica o conteudo."""
        pipeline, frontier, store = ambiente
        respx.get(url__startswith="https://www.cosmos.esa.int/").mock(
            return_value=httpx.Response(200, content=PDF)
        )
        pipeline.discover(
            FakeAdapter(
                [
                    make_rec("cosmos", "1", "https://www.cosmos.esa.int/documents/1/2/x/uuid?t=111"),
                    make_rec("cosmos", "2", "https://www.cosmos.esa.int/documents/1/2/x/uuid?t=222"),
                ]
            )
        )
        pipeline.harvest()
        assert len(list(store.raw.rglob("*.pdf"))) == 1


class TestConformidade:
    @respx.mock
    def test_export_control_nao_e_baixado(self, ambiente):
        """ITAR/EAR marcado -> o registro nem entra na fila de download."""
        pipeline, frontier, store = ambiente
        rota = respx.get("https://ntrs.nasa.gov/itar.pdf").mock(
            return_value=httpx.Response(200, content=PDF)
        )
        r = make_rec("ntrs", "9", "https://ntrs.nasa.gov/itar.pdf")
        r.export_control = True
        r.export_control_reason = "itar=YES"

        st = pipeline.discover(FakeAdapter([r]))
        pipeline.harvest()

        assert st.export_control == 1
        assert not rota.called
        assert "export_control" in store.rejects_path.read_text(encoding="utf-8")

    @respx.mock
    def test_rejeicoes_sao_registradas_com_motivo(self, ambiente):
        pipeline, _, store = ambiente
        respx.get("https://ntrs.nasa.gov/x.pdf").mock(
            return_value=httpx.Response(200, content=b"<html>404</html>")
        )
        pipeline.discover(FakeAdapter([make_rec("ntrs", "1", "https://ntrs.nasa.gov/x.pdf")]))
        st = pipeline.harvest()
        assert st.falhos == 1
        assert "nao_documento" in store.rejects_path.read_text(encoding="utf-8")


class TestPreferenciaPorTextoExtraido:
    @respx.mock
    def test_txt_do_ntrs_dispensa_a_etapa_2(self, ambiente):
        pipeline, frontier, store = ambiente
        respx.get("https://ntrs.nasa.gov/a.txt").mock(
            return_value=httpx.Response(200, content=b"HRP-48020\n\nIMPACT Concept of Operations\n")
        )
        rec = make_rec("ntrs", "1", "https://ntrs.nasa.gov/a.txt")
        rec.candidate_urls.append("https://ntrs.nasa.gov/a.pdf")
        pipeline.discover(FakeAdapter([rec]))
        st = pipeline.harvest()

        assert st.texto_ja_extraido == 1
        assert list(store.text.rglob("*.txt")), "o texto deve ir para data/text/"


class TestAmostragemDaClasseNegativa:
    @respx.mock
    def test_negativos_sao_coletados(self, ambiente):
        """Sem classe negativa nao ha precisao, recall nem F1 na Etapa 5."""
        pipeline, frontier, _ = ambiente
        recs = [
            make_rec("ntrs", str(i), f"https://ntrs.nasa.gov/{i}.pdf", titulo="Thermal Analysis of Panels")
            for i in range(400)
        ]
        pipeline.discover(FakeAdapter(recs))
        faixas = frontier.counts_by("tier")
        assert faixas.get("negative_sample", 0) > 0

    @respx.mock
    def test_flag_desliga_a_amostragem(self, tmp_path, fetcher):
        frontier = Frontier(tmp_path / "g.sqlite")
        p = Pipeline(
            frontier,
            Store(tmp_path),
            fetcher,
            Lexicon.load(ROOT / "config" / "lexicon.yaml"),
            collect_negatives=False,
        )
        recs = [
            make_rec("ntrs", str(i), f"https://x/{i}.pdf", titulo="Thermal Analysis") for i in range(200)
        ]
        p.discover(FakeAdapter(recs))
        assert frontier.counts_by("tier").get("negative_sample", 0) == 0
        frontier.close()
