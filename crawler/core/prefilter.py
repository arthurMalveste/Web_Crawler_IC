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
from dataclasses import dataclass, field
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
    #: Acertos na assinatura estrutural COM PORTA (Annex B da 29148:2018 e os
    #: cabecalhos genericos do Annex A). Vale 0 quando a porta nao abriu, mesmo
    #: que alguns termos tenham casado — ver `_gated_score`.
    structural_generic_hits: int = 0
    #: Vocabulario STPA/safety encontrado (core + domain + supporting).
    safety_hits: list[str] = field(default_factory=list)
    #: Houve sinal de safety segundo o escopo configurado em `safety_gate.scope`.
    #: E' o que a branch penalizada consulta; na aditiva e' so' informativo.
    has_safety: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "tier": self.tier,
            "matched_terms": self.matched_terms,
            "strong_in_title": self.strong_in_title,
            "adversarial_in_title": self.adversarial_in_title,
            "structural_hits": self.structural_hits,
            "structural_generic_hits": self.structural_generic_hits,
            "safety_hits": self.safety_hits,
            "has_safety": self.has_safety,
        }


def _gated(cfg_block: Any) -> tuple[list[str], int, float]:
    """Le um bloco de categoria COM PORTA: `{min_hits, weight, terms}`.

    Existe porque `lexicon.yaml` sempre afirmou que os cabecalhos de secao valem
    "porque raramente aparecem JUNTOS fora de um documento de concepcao de
    verdade" — mas o codigo pontuava cada acerto isoladamente. Para os
    cabecalhos do Annex B da 29148:2018 ("Purpose", "Scope", "Security",
    "Compliance") isso seria fatal: um unico acerto ja passa de `weak_min` e
    quase todo documento tecnico tem um deles. A porta implementa a regra que o
    comentario descrevia.
    """
    b = cfg_block or {}
    return (
        [normalize(t) for t in b.get("terms", [])],
        int(b.get("min_hits", 4)),
        float(b.get("weight", 2.0)),
    )


class Lexicon:
    def __init__(self, cfg: dict[str, Any]):
        self.version = cfg.get("version", 0)
        #: `additive` (so soma) ou `penalizing` (soma e penaliza ausencia de
        #: safety). Identifica qual variante produziu um corpus.
        self.variant = cfg.get("variant", "additive")
        self.strong = [normalize(t) for t in cfg.get("strong_terms", [])]
        self.moderate = [normalize(t) for t in cfg.get("moderate_terms", [])]
        self.structural = [normalize(t) for t in cfg.get("structural_signature", [])]
        self.adversarial = [normalize(t) for t in cfg.get("adversarial_markers", [])]
        self.safety_core = [normalize(t) for t in cfg.get("safety_core", [])]
        self.safety_domain = [normalize(t) for t in cfg.get("safety_domain", [])]

        self.struct_generic, self.struct_generic_min, self.w_struct_generic = _gated(
            cfg.get("structural_generic")
        )
        self.safety_support, self.safety_support_min, self.w_safety_support = _gated(
            cfg.get("safety_supporting")
        )

        s = cfg.get("scoring", {})
        self.w_strong_title = s.get("weight_strong_title", 10.0)
        self.w_strong_body = s.get("weight_strong_body", 3.0)
        self.w_mod_title = s.get("weight_moderate_title", 2.0)
        self.w_mod_body = s.get("weight_moderate_body", 0.5)
        self.w_structural = s.get("weight_structural", 4.0)
        self.w_safety_core_title = s.get("weight_safety_core_title", 3.0)
        self.w_safety_core_body = s.get("weight_safety_core_body", 1.0)
        self.w_safety_dom_title = s.get("weight_safety_domain_title", 1.5)
        self.w_safety_dom_body = s.get("weight_safety_domain_body", 0.5)
        self.p_adv_title = s.get("penalty_adversarial_title", -6.0)
        self.p_adv_body = s.get("penalty_adversarial_body", -1.5)

        # Porta de safety. `require: false` na branch aditiva torna este bloco
        # inteiro inerte — o comportamento fica identico ao anterior a ele, que
        # e' o que sustenta atribuir a diferenca A->B so' a penalidade.
        sf = cfg.get("safety_gate", {})
        self.require_safety = bool(sf.get("require", False))
        self.penalty_no_safety = float(sf.get("penalty", -4.0))
        self.gate_scope = sf.get("scope", "core_or_domain")

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

    def _gated_score(
        self, nt: str, nb: str, terms: list[str], min_hits: int, weight: float
    ) -> tuple[float, list[str]]:
        """Pontua uma categoria com porta: nada vale ate' `min_hits` casarem.

        Abaixo da porta devolve tambem lista VAZIA de acertos, de proposito: os
        termos parciais nao podem ir para `matched_terms`. Se fossem, o
        manifesto encheria de "purpose" e "scope" que nao pontuaram nada, e a
        auditoria manual perderia o unico sinal que ela usa para atribuir um
        documento a um termo.
        """
        hits = sorted(set(self._find(nt, terms) + self._find(nb, terms)))
        if len(hits) < min_hits:
            return 0.0, []
        return len(hits) * weight, hits

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

        sc_t = self._find(nt, self.safety_core)
        sc_b = self._find(nb, self.safety_core)
        sd_t = self._find(nt, self.safety_domain)
        sd_b = self._find(nb, self.safety_domain)

        gen_score, gen_hits = self._gated_score(
            nt, nb, self.struct_generic, self.struct_generic_min, self.w_struct_generic
        )
        sup_score, sup_hits = self._gated_score(
            nt, nb, self.safety_support, self.safety_support_min, self.w_safety_support
        )

        # Duas parcelas, e a separacao importa para a regra de faixa mais
        # abaixo: `conops` mede o quanto o documento parece um ConOps, `safety`
        # mede o quanto ele parece ser de dominio critico. So a primeira pode
        # promover a `strong`.
        conops = (
            len(strong_t) * self.w_strong_title
            + len(strong_b) * self.w_strong_body
            + len(mod_t) * self.w_mod_title
            + len(mod_b) * self.w_mod_body
            + len(set(struct)) * self.w_structural
            + gen_score
            + len(adv_t) * self.p_adv_title
            + len(adv_b) * self.p_adv_body
        )
        safety = (
            len(sc_t) * self.w_safety_core_title
            + len(sc_b) * self.w_safety_core_body
            + len(sd_t) * self.w_safety_dom_title
            + len(sd_b) * self.w_safety_dom_body
            + sup_score
        )
        score = conops + safety

        safety_hits = sorted(set(sc_t + sc_b + sd_t + sd_b + sup_hits))
        has_safety = bool(sc_t or sc_b) or (
            self.gate_scope == "core_or_domain" and bool(sd_t or sd_b)
        )
        if self.require_safety and not has_safety:
            score += self.penalty_no_safety

        matched = sorted(
            set(strong_t + strong_b + mod_t + mod_b + struct + gen_hits + safety_hits)
        )

        # Regra de faixa. Sinal forte no titulo e a condicao dominante, mas um
        # marcador adversarial no titulo ("ConOps Review Checklist") rebaixa para
        # `weak`: o documento vai para revisao manual em vez de entrar como
        # positivo. Sao exatamente os negativos adversariais da secao 3.3.
        #
        # `gate_ok` entra nas DUAS promocoes a `strong`, nao so na primeira. A
        # primeira e' booleana e ignora o score — sem o gate ali, a variante
        # penalizada nao mediria quase nada, porque todo titulo com termo forte
        # viraria `strong` apesar da penalidade. E sem o gate na segunda, um
        # documento sem safety ainda poderia somar 10.0 por outras vias e
        # escapar. Com `require_safety: false` (variante aditiva) `gate_ok` e'
        # sempre True e a regra e' identica a de antes.
        #
        # A segunda promocao compara `conops`, nao `score`. ACHADO REAL
        # (2026-08-26, pego por teste): cada peso de safety esta abaixo de
        # `strong_min` individualmente, mas a SOMA nao. "STPA and STAMP for Rail
        # Signalling", sem uma unica palavra de ConOps, chegava a 12.5 (6.0 de
        # dois termos no titulo + 5.0 de cinco no corpo + 1.5 de safety
        # classica) e virava `strong` — ou seja, "baixar sempre, e' ConOps".
        # Com o acervo PSAS sendo denso em STPA, isso encheria a faixa `strong`
        # de literatura de safety. Safety empurra para `weak` (revisao manual)
        # a vontade; para `strong` so' com evidencia de ConOps.
        gate_ok = has_safety or not self.require_safety
        if strong_t and not adv_t and gate_ok:
            tier = TIER_STRONG
        elif conops >= self.strong_min and not adv_t and gate_ok:
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
            structural_generic_hits=len(gen_hits),
            safety_hits=safety_hits,
            has_safety=has_safety,
        )

    def score_record(self, rec: DocumentRecord) -> ScoreBreakdown:
        body = "\n".join(p for p in (rec.abstract, " ".join(rec.subject_terms)) if p)
        br = self.score_text(rec.title, body)
        rec.lexicon_score = br.score
        rec.tier = br.tier
        rec.matched_terms = br.matched_terms
        return br

