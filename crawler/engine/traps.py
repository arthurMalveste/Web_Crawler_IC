"""Deteccao de armadilhas de rastreamento.

Um crawler ingenuo apontado para um portal institucional nao termina. As
armadilhas abaixo nao sao hipoteticas — sao os padroes que efetivamente
aparecem nos sites da lista de fontes (Drupal da FAA, Liferay da ESA, WordPress
do PSAS e da DTIC):

  - BUSCA FACETADA: cada combinacao de filtros gera uma URL distinta. Um portal
    com 8 facetas produz milhares de paginas com o mesmo conteudo.
  - CALENDARIOS: `?month=2026-08` tem sempre um link para o mes seguinte. Nao ha
    fundo.
  - IDENTIFICADORES DE SESSAO: a mesma pagina com `?sid=` diferente parece nova
    a cada visita, e a fronteira cresce sem limite.
  - CAMINHOS REPETIDOS: `/a/b/a/b/a/b/...` surge de links relativos mal formados
    e produz profundidade infinita.

A politica aqui e conservadora: na duvida, seguir. O objetivo e cortar o que e
comprovadamente circular, nao adivinhar relevancia — isso e trabalho do
LinkScorer.
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Parametros que nao mudam o conteudo. Removidos antes do dedupe, para que a
#: mesma pagina com rastreamento de campanha diferente nao conte como nova.
PARAMS_IRRELEVANTES = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid",
        "sid", "sessionid", "session_id", "phpsessid", "jsessionid",
        "returnurl", "return_url", "redirect", "ref", "referrer",
        "print", "output", "display",
    }
)

#: Parametros que indicam navegacao facetada/ordenacao — conteudo repetido.
PARAMS_FACETA = frozenset(
    {
        "sort", "sort_by", "sortby", "order", "orderby", "dir",
        "view", "layout", "mode", "theme",
        "month", "year", "day", "week", "date", "calendar",
        "filter", "facet", "refine",
    }
)

_ANO = re.compile(r"^(19|20)\d{2}$")

#: Padroes de CAMINHO que, quando aparecem so' depois de um redirecionamento
#: (a URL final difere da pedida), indicam pagina de manutencao/erro servida
#: com HTTP 200 — e nao o conteudo real. Achado real (2026-08-12):
#: apps.dtic.mil redireciona `/sitemap.xml` e `/sti/citations/` para
#: `/landingpage/maint.html` durante manutencao; o `httpx.Client` segue o
#: redirecionamento sozinho (`follow_redirects=True`), entao sem esta
#: checagem o crawler registra "pagina visitada com sucesso" para uma pagina
#: que na verdade nao tem nada do que foi pedido — silenciosamente zerando a
#: fonte inteira (visto ao vivo: `vistos: 0` em toda execucao de discover ja
#: registrada para o DTIC, sem NENHUM erro nos logs).
#:
#: Deliberadamente ESTREITO (nomes de pagina especificos, nao palavras soltas
#: como "maintenance"): um crawler de aviacao/defesa encontra "manutencao" o
#: tempo todo em conteudo LEGITIMO (manuais de manutencao de aeronave, por
#: exemplo) — um substring solto geraria falsos positivos reais.
_PADROES_PAGINA_SUSPEITA = re.compile(
    r"/landingpage/maint|/under-?maintenance|/site-unavailable|/error(?:page)?\.html?$",
    re.I,
)


def pagina_redirecionada_suspeita(pedida: str, final: str) -> str | None:
    """Devolve o motivo se `final` (URL apos redirecionamentos) parece uma
    pagina de manutencao/erro substituindo o conteudo pedido em `pedida`, ou
    None se o redirecionamento parece normal (ou nao houve nenhum).

    So dispara quando o CAMINHO realmente mudou — normalizacoes inofensivas
    (www., barra final, http->https) nao tem `pedida` e `final` com caminhos
    diferentes e por isso nunca casam aqui.
    """
    p_pedida = urlsplit(pedida)
    p_final = urlsplit(final)
    if p_final.path.rstrip("/") == p_pedida.path.rstrip("/") and p_final.netloc.lower() == p_pedida.netloc.lower():
        return None
    if _PADROES_PAGINA_SUSPEITA.search(p_final.path):
        return f"redirecionado para pagina de manutencao/erro: {final}"
    return None


def canonicalize(url: str, *, drop_facets: bool = False) -> str:
    """Remove ruido de query e normaliza, para dedupe da fronteira.

    ATENCAO: nao remove a query inteira. No Liferay da ESA o `?t=<timestamp>` e
    parte do endereco e sem ele o servidor recusa o download.
    """
    parts = urlsplit(url)
    pares = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in PARAMS_IRRELEVANTES
        and not (drop_facets and k.lower() in PARAMS_FACETA)
    ]
    caminho = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), caminho, urlencode(sorted(pares)), "")
    )


def is_trap(url: str, *, max_repeticoes: int = 3, max_segmentos: int = 12, max_params: int = 6) -> str | None:
    """Devolve o motivo se a URL parece armadilha, ou None se parece legitima."""
    parts = urlsplit(url)
    segmentos = [s for s in parts.path.split("/") if s]

    if len(segmentos) > max_segmentos:
        return f"caminho profundo demais ({len(segmentos)} segmentos)"

    # /a/b/a/b/a/b — link relativo mal formado gera profundidade infinita.
    if segmentos:
        mais_comum, n = Counter(segmentos).most_common(1)[0]
        if n > max_repeticoes:
            return f"segmento repetido {n}x: {mais_comum!r}"

    pares = parse_qsl(parts.query, keep_blank_values=True)
    if len(pares) > max_params:
        return f"query com {len(pares)} parametros (busca facetada)"

    chaves = {k.lower() for k, _ in pares}
    if len(chaves & PARAMS_FACETA) >= 2:
        return "combinacao de facetas"

    # Calendario: um parametro de mes/ano sempre tem link para o proximo.
    for k, v in pares:
        if k.lower() in ("month", "week", "day") and v:
            return "navegacao de calendario"
        if k.lower() == "year" and _ANO.match(v or ""):
            return "navegacao de calendario"

    return None


class HostBudget:
    """Teto de paginas por host.

    O orcamento do SourceSpec vale para a fonte inteira; este vale por host e
    impede que um unico dominio consuma tudo quando o escopo permite varios.

    `.allow()` e' ler-e-incrementar — nao atomico por si so. Com o rastreamento
    paralelo, varias threads do MESMO `Crawler` chamam isso ao mesmo tempo;
    sem o lock, duas poderiam ler a mesma contagem antes de qualquer uma
    incrementar e as duas passariam, furando o teto. Um `Lock` simples (nao por
    host) basta: todas as threads aqui pertencem ao mesmo `Crawler`, entao a
    contencao e' pequena e a operacao e' so um `dict`+comparacao.
    """

    def __init__(self, max_por_host: int):
        self.max_por_host = max_por_host
        self._contagem: Counter[str] = Counter()
        self._lock = threading.Lock()

    def allow(self, url: str) -> bool:
        host = urlsplit(url).netloc.lower()
        with self._lock:
            if self._contagem[host] >= self.max_por_host:
                return False
            self._contagem[host] += 1
            return True

    @property
    def counts(self) -> dict[str, int]:
        return dict(self._contagem)
