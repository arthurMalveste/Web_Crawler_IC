"""Testes do pre-filtro lexico.

Os casos vem de titulos reais observados nos acervos, nao de exemplos
inventados: e o que garante que o lexico funciona sobre o vocabulario que as
fontes de fato usam.
"""

from pathlib import Path

import pytest

from crawler.core.prefilter import Lexicon, NegativeSampler, normalize
from crawler.core.record import TIER_NEGATIVE, TIER_STRONG, TIER_WEAK, DocumentRecord

CONFIG = Path(__file__).resolve().parent.parent / "config" / "lexicon.yaml"


@pytest.fixture(scope="module")
def lex() -> Lexicon:
    return Lexicon.load(CONFIG)


def rec(title: str, abstract: str | None = None, subjects: list[str] | None = None):
    return DocumentRecord(
        source="t",
        source_id="1",
        title=title,
        abstract=abstract,
        subject_terms=subjects or [],
        landing_url="https://example.org/1",
    )


class TestNormalizacao:
    def test_remove_diacriticos(self):
        # Caso real do acervo PSAS: "Andrej_Lalis__PUB.pdf".
        assert normalize("Andrej Lališ") == "andrej lalis"

    def test_pontuacao_vira_espaco(self):
        assert normalize("CON-OPS") == "con ops"
        assert normalize("Con_Ops") == "con ops"

    def test_colapsa_espacos(self):
        assert normalize("  Concept   of  Operations ") == "concept of operations"


class TestFaixaStrong:
    @pytest.mark.parametrize(
        "titulo",
        [
            "IMPACT Concept of Operations",
            "NASA GeneLab Concept of Operations",
            "Interval Management Concept of Operations",
            "UAM ConOps 2.0",
            "Operational Concept for Urban Air Mobility",
            "Science Operations Concept for Euclid",
        ],
    )
    def test_sinal_forte_no_titulo(self, lex, titulo):
        br = lex.score_text(titulo, None)
        assert br.tier == TIER_STRONG
        assert br.strong_in_title


class TestFaixaWeak:
    def test_sinal_apenas_no_abstract(self, lex):
        br = lex.score_text(
            "Airspace Modernization Study",
            "This study documents the concept of operations for the future system.",
        )
        assert br.tier == TIER_WEAK

    def test_assinatura_estrutural_iso29148(self, lex):
        # Titulos de secao canonicos: sinal bem mais especifico que o termo solto.
        br = lex.score_text(
            "System Description Document",
            "Sections: current system or situation; justification for and nature of "
            "changes; operational scenarios; summary of impacts.",
        )
        assert br.structural_hits >= 3
        assert br.tier in (TIER_STRONG, TIER_WEAK)


class TestNegativosAdversariais:
    @pytest.mark.parametrize(
        "titulo",
        [
            "ConOps Review Checklist",
            "Concept of Operations Lessons Learned",
            "Operational Concept Working Group Meeting Minutes",
        ],
    )
    def test_marcador_adversarial_rebaixa_de_strong(self, lex, titulo):
        """Contem sinal forte mas nao e documento de concepcao.

        Vai para `weak` (revisao manual), nunca para `strong` — sao os
        negativos mais informativos do conjunto de validacao.
        """
        br = lex.score_text(titulo, None)
        assert br.adversarial_in_title
        assert br.tier != TIER_STRONG


class TestFaixaNegativa:
    @pytest.mark.parametrize(
        "titulo",
        [
            "Specification for the National Bridge Inventory Bridge Elements",
            "Thermal Analysis of Composite Panels",
            "Annual Budget Summary",
        ],
    )
    def test_sem_sinal(self, lex, titulo):
        assert lex.score_text(titulo, None).tier == TIER_NEGATIVE


class TestHardNegativesPSAS:
    def test_jargao_de_safety_nao_e_conops(self, lex):
        """Acervo PSAS: denso em terminologia STPA, mas nao sao ConOps.

        Se o classificador acertar estes, esta aprendendo o padrao ConOps de
        verdade e nao apenas detectando jargao de safety.
        """
        r = rec(
            "STPA Applied to Automotive Steering",
            "Hazard analysis using the safety control structure and unsafe control actions.",
        )
        br = lex.score_record(r)
        assert br.tier == TIER_NEGATIVE
        assert lex.is_safety_jargon(r)


class TestAmostragemNegativa:
    def test_deterministica(self, lex):
        """Reexecutar a coleta deve selecionar exatamente os mesmos negativos."""
        registros = [
            DocumentRecord(source="ntrs", source_id=str(i), title="x", landing_url="u")
            for i in range(500)
        ]
        a = NegativeSampler(lex.neg_cap, lex.neg_seed)
        b = NegativeSampler(lex.neg_cap, lex.neg_seed)
        assert [a.accept(r) for r in registros] == [b.accept(r) for r in registros]

    def test_respeita_teto_por_fonte(self, lex):
        s = NegativeSampler(cap=10, seed=1)
        registros = [
            DocumentRecord(source="ntrs", source_id=str(i), title="x", landing_url="u")
            for i in range(5000)
        ]
        assert sum(s.accept(r) for r in registros) == 10
