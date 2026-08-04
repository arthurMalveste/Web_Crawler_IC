"""Rastreamento focado: pontuar um link ANTES de segui-lo.

E a implementacao do "Web Crawling semantico" citado na introducao do projeto.
A ideia vem do focused crawling (CHAKRABARTI et al., 1999): em vez de varrer o
site inteiro em largura, estima-se a relevancia de cada link a partir da
evidencia disponivel no momento da descoberta — texto da ancora, tokens da URL e
o texto ao redor do link — e expande-se primeiro o que promete mais.

Tres fontes de evidencia, em ordem de confiabilidade:

  1. TOKENS DA URL. `/uam-conops-2.0.pdf` diz mais que qualquer ancora, porque
     quem nomeia arquivo raramente mente. Sinal mais forte.
  2. TEXTO DA ANCORA. "Concept of Operations v2" e explicito; "clique aqui" nao
     informa nada.
  3. CONTEXTO AO REDOR. Util quando a ancora e pobre: uma ancora "PDF" dentro de
     um paragrafo sobre concepcao operacional vale mais que a mesma ancora numa
     lista de atas de reuniao.

O pontuador reusa o `Lexicon` da Etapa 1 — o mesmo vocabulario que decide a
faixa do documento decide a ordem do rastreamento, entao consulta e triagem nao
divergem.

EVOLUCAO PREVISTA: quando a Etapa 4 produzir os embeddings BERT, este mesmo
componente pode trocar a pontuacao lexica por similaridade vetorial entre o
contexto do link e um prototipo de ConOps, sem mexer no motor. A interface
`score()` foi desenhada para essa troca.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from ..core.prefilter import Lexicon, normalize

#: Segmentos de caminho que quase sempre indicam navegacao administrativa e nao
#: conteudo tecnico. Rebaixam a prioridade sem proibir a visita.
RUIDO_ESTRUTURAL = (
    "login",
    "signin",
    "register",
    "cart",
    "search",
    "tag",
    "category",
    "author",
    "comment",
    "feed",
    "rss",
    "print",
    "share",
    "calendar",
    "archive",
    "sitemap",
    "privacy",
    "accessibility",
    "contact",
    "careers",
    "newsroom",
    "press-release",
)

_TOKEN_SEP = re.compile(r"[^a-z0-9]+")


@dataclass
class LinkScore:
    priority: float
    matched: list[str]
    from_url: float
    from_anchor: float
    from_context: float

    def as_dict(self) -> dict[str, float | list[str]]:
        return {
            "priority": round(self.priority, 3),
            "matched": self.matched,
            "url": round(self.from_url, 3),
            "anchor": round(self.from_anchor, 3),
            "context": round(self.from_context, 3),
        }


class LinkScorer:
    """Pontua links. `enabled=False` devolve 0 para tudo — e o baseline BFS."""

    # A URL e evidencia mais confiavel que a ancora; o contexto e o mais fraco.
    PESO_URL = 1.0
    PESO_ANCORA = 0.7
    PESO_CONTEXTO = 0.25

    #: Documento vale mais que pagina: e o alvo, nao um passo intermediario.
    BONUS_DOCUMENTO = 3.0
    #: Cada nivel de profundidade custa, para o crawler nao afundar num ramo.
    PENALIDADE_PROFUNDIDADE = 0.8
    PENALIDADE_RUIDO = 4.0

    def __init__(self, lexicon: Lexicon, enabled: bool = True):
        self.lexicon = lexicon
        self.enabled = enabled

    def score(
        self,
        url: str,
        *,
        anchor: str | None = None,
        context: str | None = None,
        depth: int = 0,
        is_document: bool = False,
    ) -> LinkScore:
        if not self.enabled:
            # Baseline BFS: sem prioridade, a fronteira ordena por profundidade.
            return LinkScore(0.0, [], 0.0, 0.0, 0.0)

        texto_url = self._url_como_texto(url)
        s_url = self.lexicon.score_text(texto_url, None)
        s_anc = self.lexicon.score_text(anchor or "", None)
        s_ctx = self.lexicon.score_text("", context or "")

        prioridade = (
            s_url.score * self.PESO_URL
            + s_anc.score * self.PESO_ANCORA
            + s_ctx.score * self.PESO_CONTEXTO
        )

        if is_document:
            prioridade += self.BONUS_DOCUMENTO
        prioridade -= depth * self.PENALIDADE_PROFUNDIDADE
        if self._e_ruido_estrutural(url):
            prioridade -= self.PENALIDADE_RUIDO

        matched = sorted(set(s_url.matched_terms + s_anc.matched_terms + s_ctx.matched_terms))
        return LinkScore(
            priority=prioridade,
            matched=matched,
            from_url=s_url.score,
            from_anchor=s_anc.score,
            from_context=s_ctx.score,
        )

    # ------------------------------------------------------------------ apoio

    @staticmethod
    def _url_como_texto(url: str) -> str:
        """Converte o caminho da URL em texto pontuavel.

        `/files/Urban%20Air%20Mobility%20(UAM)%20Concept%20of%20Operations%202.0.pdf`
        vira `files urban air mobility uam concept of operations 2 0 pdf`, que o
        lexico casa normalmente. Sem o unquote, o %20 esconderia a frase inteira.
        """
        parts = urlsplit(url)
        bruto = unquote(parts.path) + " " + unquote(parts.query)
        return _TOKEN_SEP.sub(" ", bruto.lower()).strip()

    @staticmethod
    def _e_ruido_estrutural(url: str) -> bool:
        caminho = urlsplit(url).path.lower()
        segmentos = set(_TOKEN_SEP.sub(" ", caminho).split())
        return any(r.replace("-", " ") in " ".join(segmentos) or r in segmentos for r in RUIDO_ESTRUTURAL)


def contexto_do_link(texto_pagina: str, posicao: int, janela: int = 220) -> str:
    """Recorta o texto ao redor da posicao de um link."""
    ini = max(0, posicao - janela)
    return texto_pagina[ini : posicao + janela]
