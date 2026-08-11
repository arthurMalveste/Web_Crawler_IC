"""Pre-filtro lexico: decide, a partir do METADADO, se vale baixar o arquivo.

E aqui que se ganha ordem de grandeza. Uma consulta de metadados no NTRS traz
2000 registros em uma requisicao; baixar 2000 PDFs custaria horas e gigabytes.
O pre-filtro roda sobre title + abstract + subject_terms e classifica em tres
faixas (secao 3.3 do plano).

O mesmo `Lexicon` e reusado na Etapa 3 sobre o texto completo — por isso
`score_text` e publico e independente do `DocumentRecord`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from .record import TIER_NEGATIVE, TIER_STRONG, TIER_WEAK, DocumentRecord

_PUNCT = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")


def normalize(text: str | None) -> str:
    """minusculas + NFKD sem diacriticos + pontuacao virando espaco.

    Faz "CON-OPS", "Con Ops" e "conops" convergirem para formas comparaveis, e
    resolve o caso real do PSAS (`Andrej_Lalis__PUB`) sem quebrar o casamento.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _PUNCT.sub(" ", text.lower())
    return _WS.sub(" ", text).strip()


def _pad(text: str) -> str:
    """Envelopa em espacos para permitir casamento por limite de palavra."""
    return f" {text} "


@dataclass
class ScoreBreakdown:
    """Resultado detalhado — vai para o manifesto e sustenta a auditoria."""

    score: float
    tier: str
    matched_terms: list[str]
    strong_in_title: bool
    adversarial_in_title: bool
    structural_hits: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "tier": self.tier,
            "matched_terms": self.matched_terms,
            "strong_in_title": self.strong_in_title,
            "adversarial_in_title": self.adversarial_in_title,
            "structural_hits": self.structural_hits,
        }


class Lexicon:
    def __init__(self, cfg: dict[str, Any]):
        self.version = cfg.get("version", 0)
        self.strong = [normalize(t) for t in cfg.get("strong_terms", [])]
        self.moderate = [normalize(t) for t in cfg.get("moderate_terms", [])]
        self.structural = [normalize(t) for t in cfg.get("structural_signature", [])]
        self.adversarial = [normalize(t) for t in cfg.get("adversarial_markers", [])]
        self.safety = [normalize(t) for t in cfg.get("safety_terms", [])]

        s = cfg.get("scoring", {})
        self.w_strong_title = s.get("weight_strong_title", 10.0)
        self.w_strong_body = s.get("weight_strong_body", 3.0)
        self.w_mod_title = s.get("weight_moderate_title", 2.0)
        self.w_mod_body = s.get("weight_moderate_body", 0.5)
        self.w_structural = s.get("weight_structural", 4.0)
        self.p_adv_title = s.get("penalty_adversarial_title", -6.0)
        self.p_adv_body = s.get("penalty_adversarial_body", -1.5)

        t = cfg.get("thresholds", {})
        self.strong_min = t.get("strong_min_score", 10.0)
        self.weak_min = t.get("weak_min_score", 2.5)

    @classmethod
    def load(cls, path: str | Path) -> "Lexicon":
        with open(path, encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh))

    @staticmethod
    def _find(haystack: str, terms: Iterable[str]) -> list[str]:
        padded = _pad(haystack)
        return [t for t in terms if t and f" {t} " in padded]

    def score_text(self, title: str | None, body: str | None) -> ScoreBreakdown:
        """Pontua um par (titulo, corpo). `body` = abstract + subject terms.

        Na Etapa 3 este mesmo metodo recebe o texto completo como `body`.
        """
        nt = normalize(title)
        nb = normalize(body)

        strong_t = self._find(nt, self.strong)
        strong_b = self._find(nb, self.strong)
        mod_t = self._find(nt, self.moderate)
        mod_b = self._find(nb, self.moderate)
        adv_t = self._find(nt, self.adversarial)
        adv_b = self._find(nb, self.adversarial)
        struct = self._find(nb, self.structural) + self._find(nt, self.structural)

        score = (
            len(strong_t) * self.w_strong_title
            + len(strong_b) * self.w_strong_body
            + len(mod_t) * self.w_mod_title
            + len(mod_b) * self.w_mod_body
            + len(set(struct)) * self.w_structural
            + len(adv_t) * self.p_adv_title
            + len(adv_b) * self.p_adv_body
        )

        matched = sorted(set(strong_t + strong_b + mod_t + mod_b + struct))

        # Regra de faixa. Sinal forte no titulo e a condicao dominante, mas um
        # marcador adversarial no titulo ("ConOps Review Checklist") rebaixa para
        # `weak`: o documento vai para revisao manual em vez de entrar como
        # positivo. Sao exatamente os negativos adversariais da secao 3.3.
        if strong_t and not adv_t:
            tier = TIER_STRONG
        elif score >= self.strong_min and not adv_t:
            tier = TIER_STRONG
        elif score >= self.weak_min:
            tier = TIER_WEAK
        else:
            tier = TIER_NEGATIVE

        return ScoreBreakdown(
            score=score,
            tier=tier,
            matched_terms=matched,
            strong_in_title=bool(strong_t),
            adversarial_in_title=bool(adv_t),
            structural_hits=len(set(struct)),
        )

    def score_record(self, rec: DocumentRecord) -> ScoreBreakdown:
        body = "\n".join(p for p in (rec.abstract, " ".join(rec.subject_terms)) if p)
        br = self.score_text(rec.title, body)
        rec.lexicon_score = br.score
        rec.tier = br.tier
        rec.matched_terms = br.matched_terms
        return br

