"""Testes do pre-filtro lexico.

Os casos vem de titulos reais observados nos acervos, nao de exemplos
inventados: e o que garante que o lexico funciona sobre o vocabulario que as
fontes de fato usam.
"""

from pathlib import Path

import pytest

from crawler.core.prefilter import Lexicon, normalize
from crawler.core.record import TIER_NEGATIVE, TIER_STRONG, TIER_WEAK, DocumentRecord

CONFIG = Path(__file__).resolve().parent.parent / "config" / "lexicon.yaml"


@pytest.fixture(scope="module")
def lex() -> Lexicon:
    return Lexicon.load(CONFIG)


def promove_a_strong(lex: Lexicon, br) -> bool:
    """A faixa final depende da variante; o SINAL nao.

    Na variante penalizada, um documento sem vocabulario de safety nao chega a
    `strong` por mais forte que seja o sinal de ConOps — e' exatamente o efeito
    que essa branch existe para medir. Os testes abaixo carregam o YAML da
    branch em que rodam, entao afirmam incondicionalmente o que e' invariante
    (`strong_in_title`, `matched_terms`) e condicionam so a faixa.

    A semantica da porta em si nao depende deste helper: `TestPortaDeSafety`
    monta os dois lexicos a mao e roda igual nas tres branches.
    """
    return br.has_safety or not lex.require_safety


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
        assert br.strong_in_title
        if promove_a_strong(lex, br):
            assert br.tier == TIER_STRONG


class TestFaixaWeak:
    def test_sinal_apenas_no_abstract(self, lex):
        br = lex.score_text(
            "Airspace Modernization Study",
            "This study documents the concept of operations for the future system.",
        )
        assert "concept of operations" in br.matched_terms
        assert not br.strong_in_title
        if promove_a_strong(lex, br):
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


class TestJargaoDeSafetyPSAS:
    def test_jargao_de_safety_vai_para_revisao_manual(self, lex):
        """Acervo PSAS: denso em terminologia STPA, mas nao sao ConOps.

        MUDANCA DELIBERADA (2026-08-26). Ate aqui este caso era `negative`:
        `safety_terms` existia no YAML mas nenhum codigo lia. Com `safety_core`
        pontuando, o mesmo documento soma 5.5 (3.0 de "stpa" no titulo + 2.0 de
        dois termos no corpo + 0.5 de "hazard analysis") e vai para `weak`.

        `weak` significa REVISAO MANUAL, nao "aceito como ConOps" — e por isso
        que os pesos de safety ficam abaixo de `strong_min`: sozinhos nunca
        promovem a `strong`. O custo esperado disso e' o acervo PSAS entrar em
        peso na faixa de revisao; e' exatamente o que o experimento A vs B
        pretende medir.
        """
        r = rec(
            "STPA Applied to Automotive Steering",
            "Hazard analysis using the safety control structure and unsafe control actions.",
        )
        br = lex.score_record(r)
        assert br.has_safety
        assert br.tier == TIER_WEAK
        assert not br.strong_in_title

    def test_safety_sozinho_nunca_promove_a_strong(self, lex):
        """Mesmo saturado de vocabulario STPA, sem sinal de ConOps nao passa."""
        br = lex.score_text(
            "STPA and STAMP for Rail Signalling",
            "Unsafe control actions, loss scenarios, system level hazards and "
            "safety constraints derived from the hierarchical control structure. "
            "Fault tree analysis and FMEA were used for comparison.",
        )
        assert br.tier == TIER_WEAK


class TestFronteiraDePalavra:
    """O casamento e' `f" {termo} " in padded`: singular nao casa com plural.

    Foi assim que `justification for change` passou a vida sem casar com o
    cabecalho real da norma. Ambos os casos aqui sao regressoes de termos que a
    ISO 29148:2018 escreve de forma diferente da de 2011.
    """

    def test_plural_do_cabecalho_a_2_5_2(self, lex):
        br = lex.score_text(None, "Section 3 covers justification for changes.")
        assert "justification for changes" in br.matched_terms

    def test_singular_do_annex_b(self, lex):
        # Annex B, B.2: "Concept of operation content" — sem o "s".
        br = lex.score_text("Concept of operation content", None)
        assert "concept of operation" in br.matched_terms
        assert "concept of operations" not in br.matched_terms
        assert br.strong_in_title
        if promove_a_strong(lex, br):
            assert br.tier == TIER_STRONG


class TestPortaDeCoocorrencia:
    """`structural_generic`: nada vale ate `min_hits` termos distintos casarem.

    Os cabecalhos do Annex B sao "Purpose", "Scope", "Security", "Compliance".
    Sem a porta, um unico acerto passaria de `weak_min` e quase todo documento
    tecnico entraria no corpus.
    """

    @staticmethod
    def _lex(min_hits: int) -> Lexicon:
        return Lexicon(
            {
                "structural_generic": {
                    "min_hits": min_hits,
                    "weight": 2.0,
                    "terms": ["purpose", "scope", "security", "compliance", "governance"],
                },
                "thresholds": {"strong_min_score": 10.0, "weak_min_score": 2.5},
            }
        )

    def test_abaixo_da_porta_nao_pontua(self):
        br = self._lex(5).score_text("Relatorio", "purpose, scope and security of the platform")
        assert br.score == 0.0
        assert br.structural_generic_hits == 0
        assert br.tier == TIER_NEGATIVE

    def test_abaixo_da_porta_nao_suja_matched_terms(self):
        """Os parciais nao podem vazar para a auditoria: se "purpose" e "scope"
        aparecessem em `matched_terms` sem terem pontuado, a inspecao manual
        perderia o unico sinal que usa para atribuir um documento a um termo."""
        br = self._lex(5).score_text("Relatorio", "purpose, scope and security")
        assert br.matched_terms == []

    def test_na_porta_pontua_todos_os_acertos(self):
        br = self._lex(5).score_text(None, "purpose scope security compliance governance")
        assert br.structural_generic_hits == 5
        assert br.score == pytest.approx(10.0)


class TestPortaDeSafety:
    """A UNICA diferenca entre as branches aditiva e penalizada.

    O codigo e' identico nas duas; muda `safety_gate.require` no YAML. Estes
    testes sao o que sustenta atribuir a diferenca de corpus exclusivamente a
    penalidade.
    """

    @staticmethod
    def _lex(require: bool) -> Lexicon:
        return Lexicon(
            {
                "strong_terms": ["concept of operations"],
                "safety_core": ["stpa"],
                "safety_gate": {"require": require, "penalty": -4.0, "scope": "core_or_domain"},
                "thresholds": {"strong_min_score": 10.0, "weak_min_score": 2.5},
            }
        )

    def test_aditiva_nao_penaliza_ausencia(self):
        br = self._lex(False).score_text("System X Concept of Operations", None)
        assert br.has_safety is False
        assert br.score == pytest.approx(10.0)
        assert br.tier == TIER_STRONG

    def test_penalizada_barra_ate_o_atalho_booleano(self):
        """Sem o gate na primeira regra de faixa, um termo forte no titulo
        viraria `strong` apesar da penalidade — e a branch B nao mediria nada,
        porque `strong_t and not adv_t` ignora o score por completo."""
        br = self._lex(True).score_text("System X Concept of Operations", None)
        assert br.has_safety is False
        assert br.score == pytest.approx(6.0)  # 10.0 - 4.0
        assert br.tier != TIER_STRONG

    def test_penalizada_preserva_quem_tem_safety(self):
        br = self._lex(True).score_text("STPA-based Concept of Operations for Rail", None)
        assert br.has_safety is True
        assert br.score == pytest.approx(13.0)
        assert br.tier == TIER_STRONG

    def test_escopo_core_only_ignora_safety_classica(self):
        """`scope: core_only` exige STPA propriamente dito; "hazard analysis"
        sozinho nao abre a porta. E' a sub-variante B2."""
        lex = Lexicon(
            {
                "strong_terms": ["concept of operations"],
                "safety_core": ["stpa"],
                "safety_domain": ["hazard analysis"],
                "safety_gate": {"require": True, "penalty": -4.0, "scope": "core_only"},
                "thresholds": {"strong_min_score": 10.0, "weak_min_score": 2.5},
            }
        )
        br = lex.score_text("Concept of Operations", "Includes a hazard analysis.")
        assert br.has_safety is False


class TestInvarianteDaVariante:
    """As branches do experimento compartilham TODO o codigo e diferem so pelo
    YAML. Estes testes rodam identicos nas tres e travam essa propriedade — se
    algum deles precisasse ser diferente por branch, a comparacao entre os
    corpora deixaria de ser atribuivel apenas ao lexico.
    """

    def test_variante_coerente_com_a_porta(self, lex):
        """`variant` e' o rotulo gravado junto do corpus; `require` e' o que de
        fato muda a pontuacao. Se divergirem, um corpus sai com identidade
        errada e a analise posterior fica inauditavel."""
        assert lex.variant in ("additive", "penalizing")
        assert lex.require_safety is (lex.variant == "penalizing")

    def test_nenhum_peso_novo_e_negativo(self, lex):
        """A penalidade e' o UNICO mecanismo de subtracao introduzido, e vive em
        `safety_gate.penalty` — nao nos pesos. Um peso negativo aqui rebaixaria
        documentos nas TRES variantes, e a diferenca entre elas deixaria de
        isolar a porta."""
        assert all(
            peso >= 0
            for peso in (
                lex.w_safety_core_title,
                lex.w_safety_core_body,
                lex.w_safety_dom_title,
                lex.w_safety_dom_body,
                lex.w_struct_generic,
                lex.w_safety_support,
            )
        )

    def test_escopo_da_porta_e_conhecido(self, lex):
        assert lex.gate_scope in ("core_or_domain", "core_only")
        assert lex.penalty_no_safety < 0
