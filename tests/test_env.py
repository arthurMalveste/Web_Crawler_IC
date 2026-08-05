"""Testes da configuracao por ambiente.

O bug que originou este modulo foi silencioso: mover a pasta do projeto deixava
o `data_root` do YAML apontando para um lugar inexistente, e a coleta seguia
como se estivesse tudo bem, gravando num corpus vazio. Os testes abaixo cobrem
principalmente as formas de falhar em silencio.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from crawler.core.env import (
    ROOT,
    ConfigAusente,
    carregar_dotenv,
    expandir,
    expandir_arvore,
    resolver_caminho,
)
from crawler.core.fetcher import Config


@pytest.fixture
def ambiente_limpo(monkeypatch):
    """Isola o teste do `.env` real da maquina de quem roda a suite."""
    for chave in ("CONOPS_DATA_ROOT", "CONOPS_CONTACT_EMAIL", "TESTE_VAR"):
        monkeypatch.delenv(chave, raising=False)
    return monkeypatch


class TestLeituraDotenv:
    def test_le_pares_chave_valor(self, tmp_path, ambiente_limpo):
        env = tmp_path / ".env"
        env.write_text("TESTE_VAR=abc\n", encoding="utf-8")
        assert carregar_dotenv(env) == 1
        assert os.environ["TESTE_VAR"] == "abc"

    def test_ignora_comentarios_e_linhas_vazias(self, tmp_path, ambiente_limpo):
        env = tmp_path / ".env"
        env.write_text("# comentario\n\n   \nTESTE_VAR=abc\n", encoding="utf-8")
        assert carregar_dotenv(env) == 1

    def test_tolera_export_e_aspas(self, tmp_path, ambiente_limpo):
        env = tmp_path / ".env"
        env.write_text('export TESTE_VAR="com espaco"\n', encoding="utf-8")
        carregar_dotenv(env)
        assert os.environ["TESTE_VAR"] == "com espaco"

    def test_cerquilha_no_valor_e_preservada(self, tmp_path, ambiente_limpo):
        """Um `#` pode ser parte de um caminho — nao e comentario no fim da linha."""
        env = tmp_path / ".env"
        env.write_text("TESTE_VAR=D:/IC/Corpus#2\n", encoding="utf-8")
        carregar_dotenv(env)
        assert os.environ["TESTE_VAR"] == "D:/IC/Corpus#2"

    def test_ambiente_vence_dotenv(self, tmp_path, ambiente_limpo):
        """Precedencia que permite a um CI sobrepor sem editar arquivo."""
        ambiente_limpo.setenv("TESTE_VAR", "do ambiente")
        env = tmp_path / ".env"
        env.write_text("TESTE_VAR=do arquivo\n", encoding="utf-8")
        assert carregar_dotenv(env) == 0
        assert os.environ["TESTE_VAR"] == "do ambiente"

    def test_arquivo_ausente_nao_e_erro(self, tmp_path):
        # Quem usa o layout padrao nao precisa de `.env`.
        assert carregar_dotenv(tmp_path / "nao-existe") == 0


class TestExpansao:
    def test_substitui_variavel(self, ambiente_limpo):
        ambiente_limpo.setenv("TESTE_VAR", "valor")
        assert expandir("x=${TESTE_VAR}") == "x=valor"

    def test_usa_default_quando_ausente(self, ambiente_limpo):
        assert expandir("${TESTE_VAR:-padrao}") == "padrao"

    def test_variavel_vazia_cai_no_default(self, ambiente_limpo):
        # `CONOPS_DATA_ROOT=` em branco no .env nao deve virar caminho vazio.
        ambiente_limpo.setenv("TESTE_VAR", "")
        assert expandir("${TESTE_VAR:-padrao}") == "padrao"

    def test_ausente_sem_default_falha_alto(self, ambiente_limpo):
        """Falhar aqui e melhor que criar uma pasta chamada `${TESTE_VAR}`."""
        with pytest.raises(ConfigAusente, match="TESTE_VAR"):
            expandir("${TESTE_VAR}")

    def test_percorre_dicionarios_e_listas(self, ambiente_limpo):
        ambiente_limpo.setenv("TESTE_VAR", "v")
        arvore = {"a": ["${TESTE_VAR}", 2], "b": {"c": "${TESTE_VAR}"}, "d": True}
        assert expandir_arvore(arvore) == {"a": ["v", 2], "b": {"c": "v"}, "d": True}


class TestResolucaoDeCaminho:
    def test_absoluto_passa_intacto(self):
        assert resolver_caminho("D:/IC/ConOpsCorpus") == Path("D:/IC/ConOpsCorpus")

    def test_relativo_ancora_no_repositorio(self, tmp_path, monkeypatch):
        """Nao no diretorio corrente: a mesma config tem que valer de qualquer
        lugar de onde o comando seja chamado."""
        monkeypatch.chdir(tmp_path)
        assert resolver_caminho("../ConOpsCorpus") == (ROOT.parent / "ConOpsCorpus").resolve()


class TestConfigIntegrada:
    def test_data_root_vem_do_ambiente(self, ambiente_limpo, tmp_path):
        ambiente_limpo.setenv("CONOPS_DATA_ROOT", str(tmp_path))
        cfg = Config.load(ROOT / "config" / "domains.yaml")
        assert cfg.data_root == tmp_path

    def test_domains_yaml_nao_tem_caminho_absoluto(self):
        """O caminho de UMA maquina nao pode voltar para um arquivo versionado
        — foi exatamente isso que quebrou a coleta ao mover a pasta."""
        texto = (ROOT / "config" / "domains.yaml").read_text(encoding="utf-8")
        linha = next(ln for ln in texto.splitlines() if ln.startswith("data_root:"))
        assert "${" in linha, "data_root deve referenciar variavel de ambiente"

    def test_contato_do_user_agent_e_configuravel(self, ambiente_limpo):
        ambiente_limpo.setenv("CONOPS_CONTACT_EMAIL", "fulano@unicamp.br")
        cfg = Config.load(ROOT / "config" / "domains.yaml")
        assert "fulano@unicamp.br" in cfg.policy_for("ntrs.nasa.gov").user_agent
