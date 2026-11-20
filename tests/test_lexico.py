"""Testes da organizacao do lexico (config/lexicon.yaml).

Fase 0 do painel de palavras-chave (decisao de 2026-11-19/20): termos de busca
sairam do codigo para o YAML, e os termos de dominio viraram grupos com nome em
duas configuracoes independentes — `download` e `etapa3`. Os testes travam que
a reorganizacao nao mudou o comportamento e que as duas configuracoes nascem
iguais mas separadas.
"""

import argparse
from pathlib import Path

import pytest
import yaml

from crawler import cli
from crawler.core.prefilter import Lexicon, termos_dos_dominios

ROOT = Path(__file__).resolve().parent.parent
LEXICO = ROOT / "config" / "lexicon.yaml"


@pytest.fixture(scope="module")
def cfg() -> dict:
    return yaml.safe_load(LEXICO.read_text(encoding="utf-8"))


class TestTermosDeBusca:
    def test_lista_igual_a_que_estava_no_codigo(self):
        # Era `TERMOS_LEXICO_FORTE` em cli.py ate 2026-11-20.
        assert Lexicon.load(LEXICO).search_terms == [
            "concept of operations",
            "conops",
            "operational concept",
            "operations concept",
            "operating concept",
            "concept of employment",
            "mission operations concept",
        ]

    def test_busca_e_so_conops(self, cfg):
        # A descoberta nunca usa dominio: todo termo de busca e termo forte de ConOps.
        assert set(cfg["search_terms"]) <= set(cfg["strong_terms"])

    @pytest.mark.parametrize("fonte", ["govuk", "core"])
    def test_adaptador_usa_lista_do_lexico(self, fonte, monkeypatch):
        monkeypatch.setenv("CORE_API_KEY", "chave-de-teste")  # o CORE exige chave
        lex = Lexicon.load(LEXICO)
        adaptador = cli.make_adapter(fonte, None, lex, argparse.Namespace(terms=None))
        assert adaptador.terms == lex.search_terms

    def test_terms_na_linha_de_comando_substitui(self):
        lex = Lexicon.load(LEXICO)
        adaptador = cli.make_adapter("govuk", None, lex, argparse.Namespace(terms=["opscon"]))
        assert adaptador.terms == ["opscon"]


class TestTermosDosDominios:
    def test_soma_so_os_ativos(self):
        dominios = {
            "a": {"ativo": True, "fortes": ["Aircraft"], "moderados": ["flight"]},
            "b": {"ativo": False, "fortes": ["spacecraft"], "moderados": ["orbit"]},
        }
        assert termos_dos_dominios(dominios) == (["aircraft"], ["flight"])

    def test_termo_repetido_em_dois_dominios_conta_uma_vez(self):
        dominios = {
            "a": {"ativo": True, "fortes": ["UAS"], "moderados": ["mission"]},
            "b": {"ativo": True, "fortes": ["uas", "lunar"], "moderados": ["Mission"]},
        }
        assert termos_dos_dominios(dominios) == (["uas", "lunar"], ["mission"])

    def test_dominio_sem_lista_ou_vazio_nao_quebra(self):
        assert termos_dos_dominios({"a": {"ativo": True}}) == ([], [])
        assert termos_dos_dominios({}) == ([], [])


class TestConfiguracoesDeDominio:
    def test_download_comeca_desligado(self, cfg):
        # Padrao = comportamento original: baixa por ConOps, sem olhar dominio.
        assert cfg["download"]["filtrar_por_dominio"] is False

    def test_download_e_etapa3_nascem_iguais(self, cfg):
        assert cfg["download"]["dominios"] == cfg["etapa3"]["dominios"]

    def test_sao_independentes_no_yaml(self, cfg):
        # Copias, nao a mesma referencia (ancora YAML): editar uma no painel
        # nao pode mudar a outra.
        cfg["download"]["dominios"]["espacial"]["ativo"] = False
        assert cfg["etapa3"]["dominios"]["espacial"]["ativo"] is True
        cfg["download"]["dominios"]["espacial"]["ativo"] = True

    def test_stpa_e_um_dominio(self, cfg):
        stpa = cfg["etapa3"]["dominios"]["seguranca_stpa"]
        assert "stpa" in stpa["fortes"] and stpa["ativo"] is True

    def test_etapa3_tem_os_mesmos_termos_da_lista_antiga(self, cfg):
        # `domain_terms` tinha 41 fortes e 15 moderados, todos distintos.
        fortes, moderados = termos_dos_dominios(cfg["etapa3"]["dominios"])
        assert (len(fortes), len(moderados)) == (41, 15)
        assert {"stpa", "aircraft", "spacecraft"} <= set(fortes)
        assert {"hazard", "flight", "satellite"} <= set(moderados)

    def test_todo_dominio_tem_nome_e_campos(self, cfg):
        for secao in ("download", "etapa3"):
            for chave, dom in cfg[secao]["dominios"].items():
                assert dom["nome"] and isinstance(dom["ativo"], bool), (secao, chave)
                assert dom["fortes"], (secao, chave)
