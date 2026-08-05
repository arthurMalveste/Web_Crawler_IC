"""Testes do motor de rastreamento.

Nao tocam a rede: um site sintetico e servido por respx. Isso permite testar
propriedades que seriam impossiveis de provocar de forma confiavel contra
servidores reais — armadilhas de calendario, profundidade infinita, orcamento
esgotado — e mantem os testes reproduziveis.
"""

from pathlib import Path

import httpx
import pytest
import respx

from crawler.core.fetcher import Config, Fetcher
from crawler.core.prefilter import Lexicon
from crawler.engine.crawler import Crawler, _melhor_titulo, _nome_do_arquivo
from crawler.engine.extract import extract_links, extract_metadata, parse_html
from crawler.engine.linkscorer import LinkScorer
from crawler.engine.spec import STRATEGY_BFS, STRATEGY_FOCUSED, Scope, SourceSpec
from crawler.engine.traps import canonicalize, is_trap
from crawler.engine.urlfrontier import KIND_DOCUMENT, KIND_PAGE, URLFrontier

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def lexicon() -> Lexicon:
    return Lexicon.load(ROOT / "config" / "lexicon.yaml")


@pytest.fixture
def fetcher():
    cfg = Config.load(ROOT / "config" / "domains.yaml")
    cfg.defaults.update({"respect_robots": False, "rate": 0.0, "retries": 1})
    for d in cfg.domains.values():
        d.update({"respect_robots": False, "rate": 0.0})
    f = Fetcher(cfg)
    yield f
    f.close()


# ------------------------------------------------------------------- extracao


class TestExtracaoDeLinks:
    def test_resolve_relativas_e_remove_fragmento(self):
        html = """<html><body>
            <a href="/docs/a.pdf">ConOps A</a>
            <a href="pagina.html#secao">Secao</a>
            <a href="mailto:x@y.z">email</a>
            <a href="javascript:void(0)">js</a>
        </body></html>"""
        links = extract_links(parse_html(html), "https://ex.org/base/", (".pdf",))
        urls = [l.url for l in links]
        assert "https://ex.org/docs/a.pdf" in urls
        assert "https://ex.org/base/pagina.html" in urls  # fragmento removido
        assert not any("mailto" in u or "javascript" in u for u in urls)

    def test_escapa_espacos_e_nao_ascii(self):
        """Caso real do MIT PSAS: "[STAMP Mar26] STPA Practice.pdf"."""
        html = '<a href="/uploads/[STAMP Mar26] STPA Practice.pdf">Apresentacao</a>'
        links = extract_links(parse_html(html), "https://psas.scripts.mit.edu/", (".pdf",))
        assert links[0].url.count(" ") == 0, "espacos precisam ser escapados"
        assert "%20" in links[0].url

    def test_nao_escapa_duas_vezes(self):
        html = '<a href="/files/Urban%20Air%20Mobility.pdf">UAM</a>'
        links = extract_links(parse_html(html), "https://faa.gov/", (".pdf",))
        assert "%2520" not in links[0].url

    def test_marca_documento_por_extensao(self):
        html = '<a href="/a.pdf">A</a><a href="/b.html">B</a>'
        links = extract_links(parse_html(html), "https://ex.org/", (".pdf",))
        por_url = {l.url: l.is_document for l in links}
        assert por_url["https://ex.org/a.pdf"] is True
        assert por_url["https://ex.org/b.html"] is False

    def test_captura_ancora_e_contexto(self):
        html = """<p>Este documento descreve o concept of operations do sistema.
                  <a href="/a.pdf">Baixar</a></p>"""
        links = extract_links(parse_html(html), "https://ex.org/", (".pdf",))
        assert links[0].anchor == "Baixar"
        assert "concept of operations" in links[0].context.lower()


class TestExtracaoDeMetadados:
    def test_highwire_citation(self):
        """Padrao do Google Scholar — o mais completo em repositorios."""
        html = """<html><head>
            <meta name="citation_title" content="UAM Concept of Operations 2.0">
            <meta name="citation_author" content="Silva, A.">
            <meta name="citation_author" content="Nunes, P.">
            <meta name="citation_publication_date" content="2023/05/01">
            <meta name="citation_abstract" content="Operational concept for urban air mobility.">
            <meta name="citation_pdf_url" content="/files/uam.pdf">
            <meta name="citation_doi" content="10.1234/abcd">
        </head></html>"""
        md = extract_metadata(parse_html(html), "https://ex.org/item/1")
        assert md.title == "UAM Concept of Operations 2.0"
        assert md.authors == ["Silva, A.", "Nunes, P."]
        assert md.date == "2023/05/01"
        assert md.doi == "10.1234/abcd"
        assert md.pdf_url == "https://ex.org/files/uam.pdf"

    def test_dublin_core_quando_nao_ha_highwire(self):
        html = """<html><head>
            <meta name="DC.title" content="Mission Operations Concept">
            <meta name="DC.creator" content="ESA">
            <meta name="DCTERMS.abstract" content="Concept for science operations.">
            <meta name="DC.subject" content="space; operations">
        </head></html>"""
        md = extract_metadata(parse_html(html), "https://ex.org/")
        assert md.title == "Mission Operations Concept"
        assert md.authors == ["ESA"]
        assert "space" in md.subjects and "operations" in md.subjects

    def test_jsonld_schema_org(self):
        html = """<html><head><script type="application/ld+json">
            {"@type":"Report","name":"Science Operations Concept",
             "description":"Euclid mission.","datePublished":"2024-01-15",
             "author":[{"name":"Team A"}]}
        </script></head></html>"""
        md = extract_metadata(parse_html(html), "https://ex.org/")
        assert md.title == "Science Operations Concept"
        assert md.date == "2024-01-15"
        assert md.authors == ["Team A"]

    def test_cai_para_title_quando_nao_ha_metadado(self):
        md = extract_metadata(parse_html("<html><head><title>Pagina X</title></head></html>"), "https://ex.org/")
        assert md.title == "Pagina X"


# -------------------------------------------------------------- pontuacao


class TestLinkScorer:
    def test_url_e_sinal_mais_forte_que_ancora_generica(self, lexicon):
        s = LinkScorer(lexicon)
        bom = s.score("https://x.org/files/uam-conops-2.0.pdf", anchor="Download", is_document=True)
        ruim = s.score("https://x.org/files/annual-report.pdf", anchor="Download", is_document=True)
        assert bom.priority > ruim.priority

    def test_decodifica_percent_encoding_da_url(self, lexicon):
        """Sem unquote, %20 esconderia a frase inteira do lexico."""
        s = LinkScorer(lexicon)
        r = s.score(
            "https://faa.gov/files/Urban%20Air%20Mobility%20Concept%20of%20Operations.pdf",
            is_document=True,
        )
        assert "concept of operations" in r.matched
        assert r.from_url > 0

    def test_ancora_informativa_pontua(self, lexicon):
        s = LinkScorer(lexicon)
        r = s.score("https://x.org/d/123.pdf", anchor="Concept of Operations v2", is_document=True)
        assert r.from_anchor > 0

    def test_contexto_ajuda_quando_ancora_e_pobre(self, lexicon):
        s = LinkScorer(lexicon)
        com = s.score("https://x.org/d/1.pdf", anchor="PDF", context="This concept of operations describes...")
        sem = s.score("https://x.org/d/1.pdf", anchor="PDF", context="Meeting agenda for tuesday.")
        assert com.priority > sem.priority

    def test_profundidade_penaliza(self, lexicon):
        s = LinkScorer(lexicon)
        raso = s.score("https://x.org/conops.pdf", depth=1, is_document=True)
        fundo = s.score("https://x.org/conops.pdf", depth=5, is_document=True)
        assert raso.priority > fundo.priority

    def test_ruido_estrutural_rebaixa(self, lexicon):
        s = LinkScorer(lexicon)
        assert s.score("https://x.org/login").priority < s.score("https://x.org/reports").priority

    def test_desligado_e_o_baseline_bfs(self, lexicon):
        """No BFS todas as prioridades sao 0 e a fronteira ordena por profundidade."""
        s = LinkScorer(lexicon, enabled=False)
        assert s.score("https://x.org/uam-conops.pdf", is_document=True).priority == 0.0


# ------------------------------------------------------------- armadilhas


class TestArmadilhas:
    @pytest.mark.parametrize(
        "url,esperado",
        [
            ("https://x.org/a/b/a/b/a/b/a/b/c", "segmento repetido"),
            ("https://x.org/eventos?month=2026-08", "calendario"),
            ("https://x.org/e?year=2026", "calendario"),
            ("https://x.org/busca?sort=asc&filter=x&order=1&view=grid", None),
            ("https://x.org/a/b/c/d/e/f/g/h/i/j/k/l/m/n", "profundo"),
        ],
    )
    def test_detecta(self, url, esperado):
        motivo = is_trap(url)
        if esperado is None:
            assert motivo is not None  # facetas combinadas tambem sao armadilha
        else:
            assert motivo and esperado in motivo

    def test_url_legitima_passa(self):
        assert is_trap("https://www.faa.gov/sites/faa.gov/files/uam_conops_2.0.pdf") is None
        assert is_trap("https://cosmos.esa.int/web/euclid/publications") is None

    def test_canonicalizacao_remove_rastreamento(self):
        a = canonicalize("https://x.org/a?utm_source=news&id=5")
        b = canonicalize("https://x.org/a?id=5")
        assert a == b

    def test_canonicalizacao_preserva_timestamp_do_liferay(self):
        """No Liferay o `?t=` faz parte do endereco; sem ele o servidor recusa."""
        u = "https://www.cosmos.esa.int/documents/1/2/x/uuid?t=1699999"
        assert "t=1699999" in canonicalize(u)


# ---------------------------------------------------------------- fronteira


class TestURLFrontier:
    def test_ordena_por_prioridade_depois_profundidade(self, tmp_path):
        f = URLFrontier(tmp_path / "u.sqlite", "s")
        f.add("https://x.org/c", depth=1, priority=1.0)
        f.add("https://x.org/a", depth=3, priority=9.0)
        f.add("https://x.org/b", depth=2, priority=5.0)
        assert [u.url for u in f.next_batch(3)] == [
            "https://x.org/a",
            "https://x.org/b",
            "https://x.org/c",
        ]
        f.close()

    def test_bfs_cai_para_ordem_de_profundidade(self, tmp_path):
        """Com prioridade 0 em tudo, a ordenacao vira busca em largura."""
        f = URLFrontier(tmp_path / "u.sqlite", "s")
        for url, d in [("https://x.org/fundo", 3), ("https://x.org/raso", 1), ("https://x.org/meio", 2)]:
            f.add(url, depth=d, priority=0.0)
        assert [u.url for u in f.next_batch(3)] == [
            "https://x.org/raso",
            "https://x.org/meio",
            "https://x.org/fundo",
        ]
        f.close()

    def test_nao_duplica(self, tmp_path):
        f = URLFrontier(tmp_path / "u.sqlite", "s")
        assert f.add("https://x.org/a", depth=1) is True
        assert f.add("https://x.org/a", depth=1) is False
        f.close()

    def test_promove_quando_reencontrada_por_caminho_melhor(self, tmp_path):
        f = URLFrontier(tmp_path / "u.sqlite", "s")
        f.add("https://x.org/a", depth=4, priority=1.0)
        f.add("https://x.org/a", depth=1, priority=20.0)
        u = f.next_batch(1)[0]
        assert u.priority == 20.0 and u.depth == 1
        f.close()

    def test_fontes_nao_se_misturam(self, tmp_path):
        db = tmp_path / "u.sqlite"
        a, b = URLFrontier(db, "faa"), URLFrontier(db, "psas")
        a.add("https://x.org/a", depth=0)
        assert b.next_batch(1) == []
        a.close()
        b.close()

    def test_requeue_failed_reabre_so_as_falhas(self, tmp_path):
        """Falha transitoria (timeout, manutencao do site) nao pode ficar
        marcada `failed` para sempre — so `reset()` (que apaga tudo) recuperava
        antes disso existir."""
        f = URLFrontier(tmp_path / "u.sqlite", "s")
        f.add("https://x.org/ok", depth=0)
        f.add("https://x.org/falhou", depth=0)
        f.mark("https://x.org/ok", "visited")
        f.mark("https://x.org/falhou", "failed", error="timeout")

        n = f.requeue_failed()
        assert n == 1
        pendentes = {u.url for u in f.next_batch(10)}
        assert pendentes == {"https://x.org/falhou"}, "so a falha deveria voltar, nao a visitada"
        assert f.counts().get("failed", 0) == 0
        f.close()

    def test_requeue_failed_filtra_por_kind(self, tmp_path):
        f = URLFrontier(tmp_path / "u.sqlite", "s")
        f.add("https://x.org/pagina", depth=0, kind=KIND_PAGE)
        f.add("https://x.org/doc.pdf", depth=0, kind=KIND_DOCUMENT)
        f.mark("https://x.org/pagina", "failed")
        f.mark("https://x.org/doc.pdf", "failed")

        n = f.requeue_failed(kind=KIND_PAGE)
        assert n == 1
        assert f.counts() == {"pending": 1, "failed": 1}
        f.close()


# ------------------------------------------------------------------ titulos


class TestSelecaoDeTitulo:
    def test_nome_do_arquivo_quando_nao_ha_ancora(self):
        """Regressao: as sementes PDF da FAA iam para a classe negativa porque o
        titulo virava um marcador interno em vez do nome do arquivo."""
        url = "https://www.faa.gov/files/Urban%20Air%20Mobility%20%28UAM%29%20Concept%20of%20Operations%202.0_0.pdf"
        assert "Concept of Operations" in _melhor_titulo(None, None, url)

    def test_ancora_generica_perde_para_nome_do_arquivo(self):
        t = _melhor_titulo(None, "Download PDF", "https://x.org/uam-conops-2.0.pdf")
        assert "conops" in t.lower()

    def test_ancora_informativa_ganha(self):
        t = _melhor_titulo(None, "UAM Concept of Operations 2.0", "https://x.org/d/123.pdf")
        assert t == "UAM Concept of Operations 2.0"

    def test_metadado_ganha_de_tudo(self):
        assert _melhor_titulo("Titulo Oficial", "Concept of Operations", "https://x.org/a.pdf") == "Titulo Oficial"

    def test_remove_extensao_e_separadores(self):
        assert _nome_do_arquivo("https://x.org/a/uam_conops-2.pdf") == "uam conops 2"


# -------------------------------------------------------------------- motor


SITE = {
    "https://ex.org/": """<html><body>
        <a href="/conops/">Concept of Operations</a>
        <a href="/noticias/">Newsroom</a>
        <a href="/login">Entrar</a>
    </body></html>""",
    "https://ex.org/conops/": """<html><body>
        <a href="/files/uam-concept-of-operations.pdf">UAM ConOps</a>
        <a href="/files/system-operational-description.pdf">SOD</a>
    </body></html>""",
    "https://ex.org/noticias/": """<html><body>
        <a href="/files/annual-report-2024.pdf">Annual Report</a>
        <a href="/files/press-release.pdf">Press Release</a>
    </body></html>""",
}


def montar_site():
    def responder(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("#")[0]
        if url.endswith(".pdf"):
            return httpx.Response(200, content=b"%PDF-1.7\n" + b"x" * 200)
        html = SITE.get(url) or SITE.get(url.rstrip("/") + "/")
        if html is None:
            return httpx.Response(404)
        return httpx.Response(200, html=html)

    respx.get(url__startswith="https://ex.org").mock(side_effect=responder)


def spec_de_teste(strategy: str, **kw) -> SourceSpec:
    return SourceSpec(
        name="ex",
        seeds=["https://ex.org/"],
        strategy=strategy,
        sitemap="none",
        scope=Scope(allow_hosts=["ex.org"], max_depth=kw.pop("max_depth", 3), max_pages=kw.pop("max_pages", 10)),
        **kw,
    )


class TestMotor:
    @respx.mock
    def test_encontra_documentos_e_emite_registros(self, tmp_path, fetcher, lexicon):
        montar_site()
        f = URLFrontier(tmp_path / "u.sqlite", "ex")
        c = Crawler(spec_de_teste(STRATEGY_FOCUSED), fetcher, lexicon, f)
        recs = list(c.crawl())

        titulos = {r.title for r in recs}
        assert any("ConOps" in t or "conops" in t.lower() for t in titulos)
        assert c.stats.documentos_encontrados >= 4
        assert all(r.source == "ex" for r in recs)
        f.close()

    @respx.mock
    def test_respeita_escopo_de_host(self, tmp_path, fetcher, lexicon):
        respx.get(url__startswith="https://ex.org").mock(
            return_value=httpx.Response(200, html='<a href="https://outro.org/x.pdf">fora</a>')
        )
        externo = respx.get(url__startswith="https://outro.org").mock(
            return_value=httpx.Response(200, content=b"%PDF-")
        )
        f = URLFrontier(tmp_path / "u.sqlite", "ex")
        list(Crawler(spec_de_teste(STRATEGY_FOCUSED), fetcher, lexicon, f).crawl())
        assert not externo.called
        f.close()

    @respx.mock
    def test_orcamento_de_paginas_e_respeitado(self, tmp_path, fetcher, lexicon):
        def responder(request):
            n = str(request.url).rstrip("/").rsplit("/", 1)[-1] or "0"
            prox = int(n) + 1 if n.isdigit() else 1
            return httpx.Response(200, html=f'<a href="/{prox}">proxima</a>')

        respx.get(url__startswith="https://ex.org").mock(side_effect=responder)
        f = URLFrontier(tmp_path / "u.sqlite", "ex")
        c = Crawler(spec_de_teste(STRATEGY_FOCUSED, max_pages=5), fetcher, lexicon, f)
        list(c.crawl())
        assert c.stats.paginas_baixadas <= 5, "site infinito precisa parar no orcamento"
        f.close()

    @respx.mock
    def test_profundidade_limita(self, tmp_path, fetcher, lexicon):
        montar_site()
        f = URLFrontier(tmp_path / "u.sqlite", "ex")
        c = Crawler(spec_de_teste(STRATEGY_FOCUSED, max_depth=0), fetcher, lexicon, f)
        list(c.crawl())
        assert c.stats.paginas_baixadas == 1, "profundidade 0 = so a semente"
        f.close()

    @respx.mock
    def test_focado_visita_o_ramo_relevante_antes(self, tmp_path, fetcher, lexicon):
        """A propriedade central do crawler focado: com orcamento apertado, ele
        gasta as paginas no ramo que promete documentos relevantes."""
        montar_site()
        f = URLFrontier(tmp_path / "u.sqlite", "ex")
        c = Crawler(spec_de_teste(STRATEGY_FOCUSED, max_pages=2), fetcher, lexicon, f)
        list(c.crawl())
        visitadas = {
            r["url"]
            for r in f.conn.execute("SELECT url FROM urls WHERE state='visited' AND source='ex'")
        }
        assert "https://ex.org/conops" in visitadas
        assert "https://ex.org/noticias" not in visitadas
        f.close()

    @respx.mock
    def test_usa_citation_pdf_url_quando_declarado(self, tmp_path, fetcher, lexicon):
        """A pagina declarando o PDF dispensa adivinhar por padrao de link."""
        respx.get("https://ex.org/").mock(
            return_value=httpx.Response(
                200,
                html="""<html><head>
                    <meta name="citation_title" content="Operational Concept Description">
                    <meta name="citation_pdf_url" content="/files/ocd.pdf">
                </head><body></body></html>""",
            )
        )
        respx.get(url__startswith="https://ex.org/files/").mock(
            return_value=httpx.Response(200, content=b"%PDF-")
        )
        f = URLFrontier(tmp_path / "u.sqlite", "ex")
        recs = list(Crawler(spec_de_teste(STRATEGY_FOCUSED), fetcher, lexicon, f).crawl())
        assert any(r.candidate_urls == ["https://ex.org/files/ocd.pdf"] for r in recs)
        f.close()

    @respx.mock
    def test_retomada_nao_revisita(self, tmp_path, fetcher, lexicon):
        montar_site()
        db = tmp_path / "u.sqlite"
        f1 = URLFrontier(db, "ex")
        c1 = Crawler(spec_de_teste(STRATEGY_FOCUSED, max_pages=1), fetcher, lexicon, f1)
        list(c1.crawl())
        f1.close()

        f2 = URLFrontier(db, "ex")
        c2 = Crawler(spec_de_teste(STRATEGY_FOCUSED, max_pages=10), fetcher, lexicon, f2)
        list(c2.crawl())
        # A semente ja foi visitada na primeira execucao e nao volta para a fila.
        visitadas = f2.conn.execute(
            "SELECT COUNT(*) n FROM urls WHERE state='visited' AND source='ex'"
        ).fetchone()["n"]
        assert visitadas == len({u.rstrip("/") for u in SITE})
        f2.close()


class TestComparacaoDeEstrategias:
    @respx.mock
    def test_focado_supera_bfs_com_orcamento_apertado(self, tmp_path, fetcher, lexicon):
        """E o experimento que vai para o relatorio, em miniatura.

        Site com um ramo relevante e varios ramos de ruido: com orcamento que
        nao cobre o site inteiro, o crawler focado deve achar mais documentos
        relevantes por pagina baixada.

        O ramo relevante vem POR ULTIMO no HTML de proposito. Se ele viesse
        primeiro, o BFS o alcancaria por acidente de ordenacao e o teste
        passaria sem provar nada — que e exatamente o que acontecia antes desta
        correcao. Isso tambem e um resultado honesto sobre o metodo: quando o
        conteudo relevante ja esta em evidencia, pontuar links nao ajuda; o
        ganho aparece quando ele esta enterrado, que e o caso real dos portais
        institucionais.
        """
        paginas = {
            "https://ex.org/": "".join(
                [f'<a href="/lixo{i}/">Newsroom {i}</a>' for i in range(8)]
                + ['<a href="/conops/">Concept of Operations</a>']
            ),
            "https://ex.org/conops/": "".join(
                f'<a href="/files/conops-{i}-concept-of-operations.pdf">ConOps {i}</a>' for i in range(5)
            ),
            **{
                f"https://ex.org/lixo{i}/": f'<a href="/files/press-release-{i}.pdf">Press</a>'
                for i in range(8)
            },
        }

        def responder(request):
            url = str(request.url)
            if url.endswith(".pdf"):
                return httpx.Response(200, content=b"%PDF-")
            html = paginas.get(url) or paginas.get(url.rstrip("/") + "/")
            return httpx.Response(200, html=html) if html else httpx.Response(404)

        respx.get(url__startswith="https://ex.org").mock(side_effect=responder)

        taxas = {}
        for estrategia in (STRATEGY_BFS, STRATEGY_FOCUSED):
            f = URLFrontier(tmp_path / f"{estrategia}.sqlite", "ex")
            c = Crawler(spec_de_teste(estrategia, max_pages=3), fetcher, lexicon, f)
            list(c.crawl())
            taxas[estrategia] = c.stats.harvest_rate
            f.close()

        assert taxas[STRATEGY_FOCUSED] > taxas[STRATEGY_BFS], taxas


class TestDeduplicacaoDeEsquema:
    def test_http_e_https_do_mesmo_host_sao_um_recurso(self):
        """Caso real do ROSA P: a mesma pagina traz o mesmo PDF ora como
        http://, ora como https://. Contar dois gasta o orcamento duas vezes no
        mesmo arquivo e infla a metrica de documentos encontrados."""
        html = """<a href="http://rosap.ntl.bts.gov/view/dot/1/a.pdf">A</a>
                  <a href="https://rosap.ntl.bts.gov/view/dot/1/a.pdf">A de novo</a>"""
        links = extract_links(parse_html(html), "https://rosap.ntl.bts.gov/view/dot/1", (".pdf",))
        assert len(links) == 1, [l.url for l in links]
        assert links[0].url.startswith("https://")

    def test_http_de_outro_host_e_preservado(self):
        """So se alinha o esquema no MESMO host: outro dominio pode nao ter TLS."""
        html = '<a href="http://outro.org/a.pdf">A</a>'
        links = extract_links(parse_html(html), "https://ex.org/", (".pdf",))
        assert links[0].url.startswith("http://outro.org")


class TestTituloNaoPodeSerUrl:
    def test_ancora_que_e_url_perde_para_nome_do_arquivo(self):
        t = _melhor_titulo(
            None,
            "https://rosap.ntl.bts.gov/view/dot/78914/dot_78914_DS1.pdf",
            "https://rosap.ntl.bts.gov/view/dot/78914/dot_78914_DS1.pdf",
        )
        assert not t.startswith("http")
        assert t == "dot 78914 DS1"


class TestTituloNaoVazaDaPaginaDeIndice:
    @respx.mock
    def test_pagina_de_listagem_nao_da_seu_titulo_aos_documentos(self, tmp_path, fetcher, lexicon):
        """Numa pagina de indice com varios PDFs, o <title> descreve o indice.

        Sem a distincao entre landing page e pagina de listagem, os 40 PDFs de
        um indice herdariam todos o mesmo titulo — e o corpus sairia com
        metadado errado, o que contamina a Etapa 5.
        """
        respx.get("https://ex.org/").mock(
            return_value=httpx.Response(
                200,
                html="""<html><head><title>Publications Index</title></head><body>
                    <a href="/a-conops.pdf">Alpha ConOps</a>
                    <a href="/b-report.pdf">Beta Report</a>
                    <a href="/c-plan.pdf">Gamma Plan</a>
                </body></html>""",
            )
        )
        respx.get(url__regex=r"https://ex\.org/\w[\w-]*\.pdf").mock(
            return_value=httpx.Response(200, content=b"%PDF-")
        )
        f = URLFrontier(tmp_path / "u.sqlite", "ex")
        recs = list(Crawler(spec_de_teste(STRATEGY_FOCUSED), fetcher, lexicon, f).crawl())

        titulos = sorted(r.title for r in recs)
        assert titulos == ["Alpha ConOps", "Beta Report", "Gamma Plan"], titulos
        assert "Publications Index" not in titulos
        f.close()

    @respx.mock
    def test_landing_page_de_um_documento_cede_seu_metadado(self, tmp_path, fetcher, lexicon):
        """Com um unico documento, a pagina E a landing page dele."""
        respx.get("https://ex.org/").mock(
            return_value=httpx.Response(
                200,
                html="""<html><head>
                    <meta name="citation_title" content="UAM Concept of Operations 2.0">
                    <meta name="citation_author" content="FAA">
                    <meta name="citation_abstract" content="Operational concept for UAM.">
                </head><body><a href="/files/doc9.pdf">Download</a></body></html>""",
            )
        )
        respx.get(url__startswith="https://ex.org/files/").mock(
            return_value=httpx.Response(200, content=b"%PDF-")
        )
        f = URLFrontier(tmp_path / "u.sqlite", "ex")
        recs = list(Crawler(spec_de_teste(STRATEGY_FOCUSED), fetcher, lexicon, f).crawl())
        doc = next(r for r in recs if r.candidate_urls == ["https://ex.org/files/doc9.pdf"])
        assert doc.title == "UAM Concept of Operations 2.0"
        assert doc.authors == ["FAA"]
        f.close()


class TestHrefMalformado:
    def test_colchetes_no_host_nao_derrubam_o_crawl(self):
        """Visto no ESA EOF: um href com colchetes no host faz o urlsplit
        tentar interpreta-lo como IPv6 e levantar ValueError, abortando a
        coleta inteira da fonte. Um link ruim numa pagina nao pode custar isso.
        """
        html = """<body>
            <a href="http://[openid_connect_generic_auth_url]/x">Login</a>
            <a href="//[nao-e-ipv6]/y">Outro</a>
            <a href="/valido/conops.pdf">Documento valido</a>
        </body>"""
        links = extract_links(parse_html(html), "https://eof.esa.int/documentation/", (".pdf",))
        assert [l.url for l in links] == ["https://eof.esa.int/valido/conops.pdf"]

    def test_placeholders_de_template_sao_ignorados(self):
        """Nao quebram, mas cada um custaria uma requisicao e um 404 certo."""
        html = """<body>
            <a href="{{ url }}">Jinja</a>
            <a href="[auth_url]">Colchete</a>
            <a href="${link}">Shell</a>
            <a href="/real/uam-conops.pdf">Real</a>
        </body>"""
        links = extract_links(parse_html(html), "https://ex.org/", (".pdf",))
        assert [l.url for l in links] == ["https://ex.org/real/uam-conops.pdf"]
