"""Descoberta por sitemap — sempre tentada antes do BFS.

Um sitemap entrega a lista de URLs do site em uma requisicao, ja curada pelo
proprio administrador. Quando existe, torna o BFS desnecessario: em vez de N
requisicoes HTML para descobrir os endereços, faz-se uma.

A DTIC publica instrucao literal para usa-lo:
"To index DTIC's collection of unclassified and unlimited Technical Reports,
point your crawler to the sitemap at https://apps.dtic.mil/sitemap.xml".

Trata indices de sitemap (sitemap de sitemaps), que e como sites grandes
particionam a listagem.
"""

from __future__ import annotations

import gzip
from typing import Iterator
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

import structlog

from .traps import pagina_redirecionada_suspeita

log = structlog.get_logger(__name__)

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

#: Profundidade maxima de indices aninhados. Protege contra um sitemap que
#: aponta para si mesmo.
MAX_NIVEIS = 3

#: Teto de requisicoes gastas em sitemaps, e rendimento minimo para continuar.
#:
#: Nem todo sitemap compensa. O Liferay da ESA publica um indice com centenas de
#: sub-sitemaps de UMA URL cada (um por layout de pagina): segui-lo custa
#: centenas de requisicoes para obter o que uma unica pagina de listagem daria.
#: Um sitemap assim e pior que inutil — consome o orcamento inteiro antes de o
#: rastreamento comecar. Quando o rendimento cai abaixo do limiar, desiste-se e
#: cai-se para o BFS, que e o comportamento correto.
MAX_REQUISICOES_SITEMAP = 30
RENDIMENTO_MINIMO = 3.0  # URLs por requisicao


class SitemapBudget:
    def __init__(self, max_requisicoes: int = MAX_REQUISICOES_SITEMAP):
        self.max_requisicoes = max_requisicoes
        self.requisicoes = 0
        self.urls = 0
        self.abandonado = False

    def gastar(self) -> bool:
        if self.requisicoes >= self.max_requisicoes or self.abandonado:
            return False
        self.requisicoes += 1
        return True

    def avaliar(self) -> None:
        """Desiste quando o sitemap nao esta pagando o que custa."""
        if self.requisicoes >= 8 and self.urls / self.requisicoes < RENDIMENTO_MINIMO:
            self.abandonado = True
            log.warning(
                "sitemap.abandonado",
                motivo="rendimento baixo demais",
                requisicoes=self.requisicoes,
                urls=self.urls,
                acao="caindo para BFS",
            )


def discover_sitemaps(fetcher, base_url: str) -> list[str]:
    """Localiza sitemaps: primeiro o declarado em robots.txt, depois o padrao."""
    parts = urlsplit(base_url)
    raiz = f"{parts.scheme}://{parts.netloc}"
    encontrados: list[str] = []

    try:
        texto = fetcher.get_text(f"{raiz}/robots.txt")
        for linha in texto.splitlines():
            if linha.lower().startswith("sitemap:"):
                url = linha.split(":", 1)[1].strip()
                if url:
                    encontrados.append(url)
    except Exception as exc:  # robots ausente nao e erro
        log.debug("sitemap.robots_indisponivel", host=parts.netloc, error=str(exc))

    padrao = f"{raiz}/sitemap.xml"
    if padrao not in encontrados:
        encontrados.append(padrao)
    return encontrados


def iter_sitemap_urls(
    fetcher, sitemap_url: str, nivel: int = 0, budget: SitemapBudget | None = None
) -> Iterator[str]:
    """Percorre um sitemap (ou indice de sitemaps) e emite as URLs."""
    if nivel >= MAX_NIVEIS:
        log.warning("sitemap.aninhamento_excessivo", url=sitemap_url)
        return

    budget = budget or SitemapBudget()
    if not budget.gastar():
        return

    try:
        res = fetcher.fetch(sitemap_url, accept="application/xml,text/xml")
    except Exception as exc:
        log.info("sitemap.indisponivel", url=sitemap_url, error=str(exc))
        return
    if not res.ok:
        log.info("sitemap.indisponivel", url=sitemap_url, status=res.status)
        return

    # ACHADO REAL (2026-08-12): o httpx segue redirecionamentos sozinho, entao
    # um sitemap redirecionado para uma pagina de manutencao (apps.dtic.mil,
    # verificado ao vivo) chega aqui como HTTP 200 com corpo HTML — o
    # `ET.fromstring()` abaixo ia falhar mesmo, mas so' com um
    # "sitemap.xml_invalido" generico, sem indicar a causa real. Deixa
    # explicito enquanto ja se sabe.
    motivo = pagina_redirecionada_suspeita(sitemap_url, res.url)
    if motivo:
        log.warning("sitemap.redirecionado_para_pagina_suspeita", url=sitemap_url, motivo=motivo)
        return

    corpo = res.body
    if sitemap_url.endswith(".gz") or corpo[:2] == b"\x1f\x8b":
        try:
            corpo = gzip.decompress(corpo)
        except OSError as exc:
            log.warning("sitemap.gzip_invalido", url=sitemap_url, error=str(exc))
            return

    try:
        raiz = ET.fromstring(corpo)
    except ET.ParseError as exc:
        log.warning("sitemap.xml_invalido", url=sitemap_url, error=str(exc))
        return

    tag = raiz.tag.rsplit("}", 1)[-1]

    if tag == "sitemapindex":
        filhos = [
            (loc.text or "").strip() for loc in raiz.findall(".//sm:sitemap/sm:loc", NS)
        ]
        log.info("sitemap.indice", url=sitemap_url, sub_sitemaps=len(filhos))
        for filho in filhos:
            if not filho:
                continue
            if budget.abandonado or budget.requisicoes >= budget.max_requisicoes:
                log.warning(
                    "sitemap.orcamento_esgotado",
                    seguidos=budget.requisicoes,
                    de=len(filhos),
                    urls=budget.urls,
                )
                return
            yield from iter_sitemap_urls(fetcher, urljoin(sitemap_url, filho), nivel + 1, budget)
            budget.avaliar()
        return

    n = 0
    for loc in raiz.findall(".//sm:loc", NS):
        url = (loc.text or "").strip()
        if url:
            n += 1
            yield url
    budget.urls += n
    log.debug("sitemap.lido", url=sitemap_url, urls=n)
