"""Testes dos adaptadores contra fixtures gravadas do servico real.

As fixtures foram capturadas em 2026-08-04 de `ntrs.nasa.gov` e
`rosap.ntl.bts.gov`. Os testes nao tocam a rede — reproduzir a coleta nao pode
depender de os servidores estarem no ar nem do estado do acervo.
"""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from crawler.adapters.ntrs import HARD_CAP, NTRSAdapter
from crawler.adapters.rosap import RosaPAdapter, _as_oai_datetime
from crawler.core.fetcher import Config, Fetcher

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONFIG = Path(__file__).resolve().parent.parent / "config" / "domains.yaml"


@pytest.fixture
def fetcher(tmp_path):
    cfg = Config.load(CONFIG)
    # robots.txt fora do teste: e rede, e ja e coberto pelos testes do fetcher.
    for d in cfg.domains.values():
        d["respect_robots"] = False
    cfg.defaults["respect_robots"] = False
    cfg.defaults["rate"] = 0.0
    for d in cfg.domains.values():
        d["rate"] = 0.0
    f = Fetcher(cfg)
    yield f
    f.close()


# --------------------------------------------------------------------- NTRS


@pytest.fixture(scope="module")
def ntrs_payload():
    return json.loads((FIXTURES / "ntrs_search.json").read_text(encoding="utf-8"))


class TestNTRSConversao:
    def test_registros_convertem(self, fetcher, ntrs_payload):
        recs = [NTRSAdapter._to_record(h) for h in ntrs_payload["results"]]
        assert len(recs) == 20
        assert all(r.source == "ntrs" for r in recs)
        assert all(r.landing_url.startswith("https://ntrs.nasa.gov/citations/") for r in recs)

    def test_texto_extraido_vem_antes_do_pdf(self, fetcher, ntrs_payload):
        """A NASA entrega .txt pronto. Preferi-lo dispensa a Etapa 2 para esse
        documento — e o ganho de maior impacto no custo do pipeline."""
        alvo = next(h for h in ntrs_payload["results"] if str(h["id"]) == "20200001712")
        rec = NTRSAdapter._to_record(alvo)
        assert rec.candidate_urls[0].endswith(".txt")
        assert any(u.endswith(".pdf") for u in rec.candidate_urls)

    def test_links_relativos_viram_absolutos(self, fetcher, ntrs_payload):
        """A API devolve `/api/citations/...`, nao URL completa."""
        for h in ntrs_payload["results"]:
            for u in NTRSAdapter._to_record(h).candidate_urls:
                assert u.startswith("https://ntrs.nasa.gov/")

    def test_export_control_detectado(self, fetcher):
        hit = {
            "id": 1,
            "title": "Restrito",
            "exportControl": {"itar": "YES", "ear": "NO", "isExportControl": "YES"},
        }
        rec = NTRSAdapter._to_record(hit)
        assert rec.export_control
        assert "itar=YES" in rec.export_control_reason

    def test_sem_export_control_passa(self, fetcher, ntrs_payload):
        alvo = next(h for h in ntrs_payload["results"] if str(h["id"]) == "20200001712")
        assert NTRSAdapter._to_record(alvo).export_control is False

    def test_metadados_ricos_preservados(self, fetcher, ntrs_payload):
        alvo = next(h for h in ntrs_payload["results"] if str(h["id"]) == "20200001712")
        rec = NTRSAdapter._to_record(alvo)
        assert rec.organization == "Johnson Space Center"
        assert rec.doc_type == "CONFERENCE_PAPER"
        assert rec.subject_terms
        assert rec.raw_metadata  # auditoria: o JSON original fica guardado


class TestNTRSPaginacao:
    @respx.mock
    def test_usa_page_from_e_nao_from(self, fetcher):
        """`from` e silenciosamente ignorado pela API real; so `page.from` pagina.

        Com `from`, o servidor devolveria a primeira pagina para sempre — laco
        infinito colhendo registros repetidos. Este teste trava a correcao.
        """
        vistos: list[dict] = []

        def responder(request: httpx.Request) -> httpx.Response:
            q = parse_qs(urlsplit(str(request.url)).query)
            vistos.append(q)
            page_from = int(q.get("page.from", ["0"])[0])
            size = int(q.get("page.size", ["500"])[0])
            total = 7
            restante = max(0, total - page_from)
            n = min(size, restante)
            return httpx.Response(
                200,
                json={
                    "stats": {"total": total},
                    "results": [{"id": page_from + i, "title": f"doc {page_from+i}"} for i in range(n)],
                },
            )

        respx.get(url__startswith="https://ntrs.nasa.gov/api/citations/search").mock(
            side_effect=responder
        )

        adapter = NTRSAdapter(fetcher, terms=["conops"], year_start=2020, year_end=2020, page_size=3)
        ids = [r.source_id for r in adapter.discover()]

        assert ids == ["0", "1", "2", "3", "4", "5", "6"], "paginacao nao avancou"
        assert all("page.from" in q for q in vistos)
        assert not any("from" in q and "page.from" not in q for q in vistos)

    @respx.mock
    def test_nunca_estoura_o_teto_de_10k(self, fetcher):
        """page.from + page.size > 10000 -> HTTP 400 na API real."""
        pedidos: list[tuple[int, int]] = []

        def responder(request: httpx.Request) -> httpx.Response:
            q = parse_qs(urlsplit(str(request.url)).query)
            pf = int(q.get("page.from", ["0"])[0])
            ps = int(q.get("page.size", ["500"])[0])
            pedidos.append((pf, ps))
            if pf + ps > HARD_CAP:
                return httpx.Response(400, json={"error": "window too large"})
            return httpx.Response(
                200,
                json={
                    "stats": {"total": 50_000},
                    "results": [{"id": pf + i, "title": "x"} for i in range(ps)],
                },
            )

        respx.get(url__startswith="https://ntrs.nasa.gov/api/citations/search").mock(
            side_effect=responder
        )

        adapter = NTRSAdapter(fetcher, terms=["nasa"], year_start=2020, year_end=2020, page_size=500)
        # year_start == year_end: nao ha como particionar mais, entao o walk
        # tem de parar sozinho no teto em vez de tomar 400.
        list(adapter.discover())
        assert pedidos, "nenhuma requisicao feita"
        assert all(pf + ps <= HARD_CAP for pf, ps in pedidos), pedidos

    @respx.mock
    def test_particiona_ate_caber_no_teto(self, fetcher):
        """Consulta com >10k resultados precisa ser quebrada, senao a cauda
        se perde em silencio — perda de recall que nao aparece em log de erro.

        A propriedade a garantir nao e "particionar por ano", e sim: toda
        particao efetivamente percorrida cabe abaixo do teto. Quantos anos ela
        cobre e consequencia da densidade do acervo, nao do algoritmo.
        """
        percorridas: dict[tuple[str, str], int] = {}
        DENSIDADE = 4000  # registros por ano no acervo simulado

        def responder(request: httpx.Request) -> httpx.Response:
            q = parse_qs(urlsplit(str(request.url)).query)
            gte, lte = q["published.gte"][0], q["published.lte"][0]
            anos = int(lte[:4]) - int(gte[:4]) + 1
            total = DENSIDADE * anos
            ps = int(q.get("page.size", ["500"])[0])
            pf = int(q.get("page.from", ["0"])[0])
            if ps > 1:  # page.size=1 e so a sondagem de total do particionador
                percorridas[(gte, lte)] = total
            n = min(ps, max(0, min(total, HARD_CAP) - pf))
            return httpx.Response(
                200,
                json={"stats": {"total": total}, "results": [{"id": f"{gte}-{pf+i}"} for i in range(n)]},
            )

        respx.get(url__startswith="https://ntrs.nasa.gov/api/citations/search").mock(
            side_effect=responder
        )

        # 2000-2015 = 16 anos x 4000 = 64.000 registros, muito acima do teto.
        adapter = NTRSAdapter(fetcher, terms=["conops"], year_start=2000, year_end=2015, page_size=500)
        recs = list(adapter.discover())

        assert percorridas, "nenhuma particao percorrida"
        assert all(t < HARD_CAP for t in percorridas.values()), percorridas
        # Cobertura completa: nenhum ano do intervalo pode ficar de fora.
        cobertos = set()
        for gte, lte in percorridas:
            cobertos.update(range(int(gte[:4]), int(lte[:4]) + 1))
        assert cobertos == set(range(2000, 2016)), sorted(cobertos)
        assert len(recs) == 16 * DENSIDADE


# -------------------------------------------------------------------- ROSA P


class TestRosaPDatas:
    def test_data_curta_e_expandida(self):
        """O endpoint exige YYYY-MM-DDThh:mm:ssZ; `2025-01-01` devolve
        `badArgument: Error parsing date` no servico real."""
        assert _as_oai_datetime("2025-01-01") == "2025-01-01T00:00:00Z"

    def test_datetime_completo_preservado(self):
        assert _as_oai_datetime("2026-08-01T12:30:00Z") == "2026-08-01T12:30:00Z"

    def test_none_passa(self):
        assert _as_oai_datetime(None) is None


class TestRosaPConversao:
    @respx.mock
    def test_extrai_dublin_core_da_fixture(self, fetcher):
        xml = (FIXTURES / "rosap_listrecords.xml").read_text(encoding="utf-8")
        respx.get(url__startswith="https://rosap.ntl.bts.gov/fedora/oai").mock(
            return_value=httpx.Response(200, text=xml)
        )
        recs = list(RosaPAdapter(fetcher, max_pages=1).discover())

        assert len(recs) == 100
        r = recs[0]
        assert r.source == "rosap"
        assert r.source_id == "dot:92801"
        assert r.landing_url == "https://rosap.ntl.bts.gov/view/dot/92801"
        assert r.candidate_urls == [
            "https://rosap.ntl.bts.gov/view/dot/92801/dot_92801_DS1.pdf"
        ]
        assert r.rights == "Public Domain"
        assert r.subject_terms  # dc:subject e multivalorado
        assert r.abstract and "bridge" in r.abstract.lower()
        assert r.export_control is False

    @respx.mock
    def test_resumption_token_nao_leva_outros_parametros(self, fetcher):
        """O protocolo OAI-PMH proibe combinar resumptionToken com metadataPrefix
        ou from/until; o servidor responde badArgument."""
        xml = (FIXTURES / "rosap_listrecords.xml").read_text(encoding="utf-8")
        queries: list[dict] = []

        def responder(request: httpx.Request) -> httpx.Response:
            q = parse_qs(urlsplit(str(request.url)).query)
            queries.append(q)
            return httpx.Response(200, text=xml)

        respx.get(url__startswith="https://rosap.ntl.bts.gov/fedora/oai").mock(
            side_effect=responder
        )
        list(RosaPAdapter(fetcher, from_date="2026-08-01", max_pages=3).discover())

        assert len(queries) == 3
        assert "from" in queries[0] and "metadataPrefix" in queries[0]
        for q in queries[1:]:
            assert "resumptionToken" in q
            assert "metadataPrefix" not in q
            assert "from" not in q

    @respx.mock
    def test_erro_oai_nao_vira_registro(self, fetcher):
        erro = """<?xml version="1.0"?>
        <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
          <error code="badArgument">Error parsing date.</error>
        </OAI-PMH>"""
        respx.get(url__startswith="https://rosap.ntl.bts.gov/fedora/oai").mock(
            return_value=httpx.Response(200, text=erro)
        )
        assert list(RosaPAdapter(fetcher).discover()) == []
