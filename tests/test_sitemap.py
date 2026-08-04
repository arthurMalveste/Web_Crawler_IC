"""Descoberta por sitemap e seu orcamento.

O caso que motivou o orcamento e real: o Liferay do ESA Cosmos publica um
indice com centenas de sub-sitemaps de UMA URL cada (um por layout de pagina).
Segui-lo consumiu todo o tempo de execucao antes de o rastreamento comecar.
"""

from pathlib import Path

import httpx
import pytest
import respx

from crawler.core.fetcher import Config, Fetcher
from crawler.engine.sitemap import SitemapBudget, discover_sitemaps, iter_sitemap_urls

ROOT = Path(__file__).resolve().parent.parent

SM = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{}</urlset>'
IDX = '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{}</sitemapindex>'


@pytest.fixture
def fetcher():
    cfg = Config.load(ROOT / "config" / "domains.yaml")
    cfg.defaults.update({"respect_robots": False, "rate": 0.0, "retries": 1})
    for d in cfg.domains.values():
        d.update({"respect_robots": False, "rate": 0.0})
    f = Fetcher(cfg)
    yield f
    f.close()


@respx.mock
def test_le_urlset_simples(fetcher):
    corpo = SM.format("".join(f"<url><loc>https://ex.org/{i}</loc></url>" for i in range(5)))
    respx.get("https://ex.org/sitemap.xml").mock(return_value=httpx.Response(200, text=corpo))
    assert len(list(iter_sitemap_urls(fetcher, "https://ex.org/sitemap.xml"))) == 5


@respx.mock
def test_segue_indice_de_sitemaps(fetcher):
    respx.get("https://ex.org/sitemap.xml").mock(
        return_value=httpx.Response(
            200,
            text=IDX.format(
                "".join(f"<sitemap><loc>https://ex.org/sm{i}.xml</loc></sitemap>" for i in range(3))
            ),
        )
    )
    for i in range(3):
        respx.get(f"https://ex.org/sm{i}.xml").mock(
            return_value=httpx.Response(
                200,
                text=SM.format("".join(f"<url><loc>https://ex.org/{i}-{j}</loc></url>" for j in range(10))),
            )
        )
    assert len(list(iter_sitemap_urls(fetcher, "https://ex.org/sitemap.xml"))) == 30


@respx.mock
def test_abandona_indice_degenerado(fetcher):
    """Caso ESA Cosmos: centenas de sub-sitemaps com 1 URL cada.

    Seguir tudo custaria centenas de requisicoes para poucas URLs. O crawler
    precisa desistir e cair para BFS, nao gastar o orcamento inteiro ali.
    """
    respx.get("https://liferay.org/sitemap.xml").mock(
        return_value=httpx.Response(
            200,
            text=IDX.format(
                "".join(f"<sitemap><loc>https://liferay.org/sm{i}.xml</loc></sitemap>" for i in range(400))
            ),
        )
    )
    chamadas = {"n": 0}

    def um_url(request):
        chamadas["n"] += 1
        return httpx.Response(200, text=SM.format("<url><loc>https://liferay.org/p</loc></url>"))

    respx.get(url__regex=r"https://liferay\.org/sm\d+\.xml").mock(side_effect=um_url)

    budget = SitemapBudget()
    list(iter_sitemap_urls(fetcher, "https://liferay.org/sitemap.xml", budget=budget))

    assert budget.abandonado, "indice degenerado deveria ter sido abandonado"
    assert chamadas["n"] < 30, f"gastou {chamadas['n']} requisicoes num sitemap improdutivo"


@respx.mock
def test_orcamento_limita_mesmo_com_bom_rendimento(fetcher):
    respx.get("https://ex.org/sitemap.xml").mock(
        return_value=httpx.Response(
            200,
            text=IDX.format(
                "".join(f"<sitemap><loc>https://ex.org/sm{i}.xml</loc></sitemap>" for i in range(500))
            ),
        )
    )
    respx.get(url__regex=r"https://ex\.org/sm\d+\.xml").mock(
        return_value=httpx.Response(
            200, text=SM.format("".join(f"<url><loc>https://ex.org/{j}</loc></url>" for j in range(50)))
        )
    )
    b = SitemapBudget(max_requisicoes=10)
    list(iter_sitemap_urls(fetcher, "https://ex.org/sitemap.xml", budget=b))
    assert b.requisicoes <= 10


@respx.mock
def test_xml_invalido_nao_quebra(fetcher):
    respx.get("https://ex.org/sitemap.xml").mock(
        return_value=httpx.Response(200, text="<urlset><loc>quebrado")
    )
    assert list(iter_sitemap_urls(fetcher, "https://ex.org/sitemap.xml")) == []


@respx.mock
def test_descobre_sitemap_declarado_no_robots(fetcher):
    respx.get("https://ex.org/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nSitemap: https://ex.org/mapa.xml\n")
    )
    assert discover_sitemaps(fetcher, "https://ex.org/pagina")[0] == "https://ex.org/mapa.xml"
