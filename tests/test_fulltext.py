"""Deteccao de formato no endpoint `fulltext` do NTRS.

Os dois casos abaixo vem de corpos reais que fizeram a primeira coleta rejeitar
14 arquivos legitimos. Servem de regressao: o `fulltext` e o achado que barateia
a Etapa 2 inteira, entao descarta-lo por engano custa caro.
"""

from pathlib import Path

import httpx
import pytest
import respx

from crawler.core.fetcher import Config, Fetcher, sniff_kind
from crawler.core.frontier import Frontier
from crawler.core.pipeline import Pipeline
from crawler.core.prefilter import Lexicon
from crawler.core.record import DocumentRecord
from crawler.core.store import Store

ROOT = Path(__file__).resolve().parent.parent

# Corpo real de .../20220011194/downloads/SSC22-Final.docx.txt
TEXTO_COM_COLCHETE = b"\n\n\n\n\n\n[Paper Number]\n\nSSC22-III-09\nSCIENCE CONOPS FOR APPLICATION\n"

# Corpo real de .../20230015722/downloads/RenalPanel-4-HRP%20risk.pdf.txt
TIKA_XHTML = (
    b'<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
    b'<meta name="pdf:PDFVersion" content="1.7">\n</head>\n'
    b"<body><p>Evolution of Spaceflight Renal Stone Risks</p></body></html>"
)


class TestDeteccaoDeFormato:
    def test_texto_iniciado_por_colchete_nao_e_json(self):
        """`[Paper Number]` comeca com `[` mas nao faz parse como JSON."""
        assert sniff_kind(TEXTO_COM_COLCHETE, "text/plain; charset=utf-8") == "text"

    def test_json_de_verdade_ainda_e_json(self):
        assert sniff_kind(b'[{"id": 1}]', "text/plain") == "json"
        assert sniff_kind(b'{"stats": {"total": 5}}', None) == "json"

    def test_xhtml_do_tika_e_html(self):
        assert sniff_kind(TIKA_XHTML, "text/plain; charset=utf-8") == "html"

    def test_pdf_prevalece_sobre_content_type_errado(self):
        """A regra original continua valendo: os bytes mandam nos binarios."""
        assert sniff_kind(b"%PDF-1.7\nconteudo", "text/html") == "pdf"

    def test_html_anunciado_como_pdf_e_pego(self):
        assert sniff_kind(b"<!DOCTYPE html><html>404</html>", "application/pdf") == "html"


@pytest.fixture
def ambiente(tmp_path):
    cfg = Config.load(ROOT / "config" / "domains.yaml")
    cfg.defaults.update({"respect_robots": False, "rate": 0.0, "retries": 1})
    for d in cfg.domains.values():
        d.update({"respect_robots": False, "rate": 0.0})
    fetcher = Fetcher(cfg)
    frontier = Frontier(tmp_path / "f.sqlite")
    store = Store(tmp_path)
    lex = Lexicon.load(ROOT / "config" / "lexicon.yaml")
    yield Pipeline(frontier, store, fetcher, lex), frontier, store
    frontier.close()
    fetcher.close()


class FakeAdapter:
    name = "fake"

    def __init__(self, recs):
        self._recs = recs

    def discover(self):
        yield from self._recs


def rec_fulltext(sid: str, url: str) -> DocumentRecord:
    return DocumentRecord(
        source="ntrs",
        source_id=sid,
        title="Concept of Operations for X",
        landing_url=f"https://ntrs.nasa.gov/citations/{sid}",
        candidate_urls=[url],
    )


class TestAceitacaoDeFulltext:
    @respx.mock
    def test_texto_com_colchete_e_aceito(self, ambiente):
        pipeline, _, store = ambiente
        respx.get("https://ntrs.nasa.gov/api/citations/1/downloads/a.txt").mock(
            return_value=httpx.Response(
                200, content=TEXTO_COM_COLCHETE, headers={"Content-Type": "text/plain"}
            )
        )
        pipeline.discover(
            FakeAdapter([rec_fulltext("1", "https://ntrs.nasa.gov/api/citations/1/downloads/a.txt")])
        )
        st = pipeline.harvest()
        assert st.armazenados == 1 and st.falhos == 0
        assert st.texto_ja_extraido == 1

    @respx.mock
    def test_xhtml_do_tika_e_aceito_e_sinalizado(self, ambiente):
        """E texto extraido legitimo — mas a Etapa 2 precisa saber do invólucro."""
        import json

        pipeline, _, store = ambiente
        respx.get("https://ntrs.nasa.gov/api/citations/2/downloads/b.txt").mock(
            return_value=httpx.Response(
                200, content=TIKA_XHTML, headers={"Content-Type": "text/plain"}
            )
        )
        pipeline.discover(
            FakeAdapter([rec_fulltext("2", "https://ntrs.nasa.gov/api/citations/2/downloads/b.txt")])
        )
        st = pipeline.harvest()

        assert st.armazenados == 1 and st.falhos == 0
        entrada = json.loads(store.manifest_path.read_text(encoding="utf-8").splitlines()[-1])
        assert entrada["text_already_extracted"] is True
        assert entrada["text_wrapper"] == "html", "a Etapa 2 precisa saber que ha XHTML a remover"

    @respx.mock
    def test_binario_ilegivel_em_endpoint_de_texto_e_recusado(self, ambiente):
        pipeline, _, store = ambiente
        respx.get("https://ntrs.nasa.gov/api/citations/3/downloads/c.txt").mock(
            return_value=httpx.Response(200, content=b"\x00\x01\x02\xff\xfe lixo")
        )
        pipeline.discover(
            FakeAdapter([rec_fulltext("3", "https://ntrs.nasa.gov/api/citations/3/downloads/c.txt")])
        )
        st = pipeline.harvest()
        assert st.armazenados == 0 and st.falhos == 1
        assert "fulltext_ilegivel" in store.rejects_path.read_text(encoding="utf-8")

    @respx.mock
    def test_cai_para_o_pdf_quando_o_fulltext_falha(self, ambiente):
        """candidate_urls e ordem de preferencia, nao lista de alternativas
        equivalentes: falhando o .txt, o .pdf ainda salva o documento."""
        pipeline, _, store = ambiente
        respx.get("https://ntrs.nasa.gov/api/citations/4/downloads/d.txt").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://ntrs.nasa.gov/api/citations/4/downloads/d.pdf").mock(
            return_value=httpx.Response(200, content=b"%PDF-1.7\n" + b"x" * 300 + b"\n%%EOF")
        )
        r = rec_fulltext("4", "https://ntrs.nasa.gov/api/citations/4/downloads/d.txt")
        r.candidate_urls.append("https://ntrs.nasa.gov/api/citations/4/downloads/d.pdf")
        pipeline.discover(FakeAdapter([r]))
        st = pipeline.harvest()

        assert st.armazenados == 1 and st.falhos == 0
        assert st.texto_ja_extraido == 0
        assert list(store.raw.rglob("*.pdf"))
