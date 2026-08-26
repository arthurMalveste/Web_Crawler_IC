"""Testes do CLI — cobertura nova, `crawler/cli.py` nao tinha nenhuma antes.

Foco em `discover-all`: e' orquestracao pura (percorrer fontes, isolar falha,
filtrar por --only/--skip). Testar essa logica nao exige rede nem repetir o
que `test_adapters.py`/`test_engine.py` ja cobrem sobre cada adaptador — por
isso os testes aqui trocam `make_adapter` por um adaptador falso, e so
verificam o COMPORTAMENTO DO LACO.

`data_root` sempre aponta para `tmp_path`: nenhum teste deste arquivo pode
tocar o corpus real.
"""

from __future__ import annotations

import argparse
import time

from crawler import cli
from crawler.core.record import DocumentRecord


class FakeAdapter:
    """Mesma forma de `BaseAdapter` (duck typing — os outros arquivos de teste
    tambem nao subclasseiam), com um jeito de forcar falha."""

    def __init__(
        self,
        name: str,
        recs: list[DocumentRecord] | None = None,
        erro: Exception | None = None,
        delay_s: float = 0.0,
    ):
        self.name = name
        self._recs = recs or []
        self._erro = erro
        self._delay_s = delay_s
        self.stats = None

    def discover(self):
        if self._delay_s:
            time.sleep(self._delay_s)
        if self._erro:
            raise self._erro
        yield from self._recs


def rec(fonte: str, sid: str) -> DocumentRecord:
    return DocumentRecord(
        source=fonte,
        source_id=sid,
        title="Concept of Operations de teste",
        landing_url=f"https://example.org/{fonte}/{sid}",
    )


def _args(**over) -> argparse.Namespace:
    base = dict(
        data_root=None, verbose=False,
        limit=None, only=None, skip=None, retry_failed=False, workers=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


class TestDiscoverAll:
    def test_isola_falha_de_uma_fonte(self, tmp_path, monkeypatch, capsys):
        """Uma fonte que lanca excecao (servidor fora do ar, por exemplo) nao
        pode impedir a cobertura das outras fontes configuradas."""
        adaptadores = {
            "ntrs": FakeAdapter("ntrs", recs=[rec("ntrs", "1")]),
            "rosap": FakeAdapter("rosap", erro=RuntimeError("servidor fora do ar")),
        }
        monkeypatch.setattr(cli, "make_adapter", lambda nome, *a, **kw: adaptadores[nome])

        args = _args(data_root=str(tmp_path), only=["ntrs", "rosap"])
        rc = cli.cmd_discover_all(args)

        assert rc == 0, "uma fonte falhando nao pode abortar o comando inteiro"
        saida = capsys.readouterr().out
        assert '"ntrs"' in saida and '"rosap"' in saida, "as duas fontes devem aparecer no relatorio final"
        assert '"erro"' in saida, "a falha da rosap deveria ser relatada, nao engolida em silencio"

    def test_only_restringe_as_fontes_chamadas(self, tmp_path, monkeypatch):
        chamadas: list[str] = []

        def fake_make_adapter(nome, *a, **kw):
            chamadas.append(nome)
            return FakeAdapter(nome)

        monkeypatch.setattr(cli, "make_adapter", fake_make_adapter)
        args = _args(data_root=str(tmp_path), only=["ntrs"])
        cli.cmd_discover_all(args)

        assert chamadas == ["ntrs"]

    def test_skip_remove_a_fonte_da_lista(self, tmp_path, monkeypatch):
        chamadas: list[str] = []

        def fake_make_adapter(nome, *a, **kw):
            chamadas.append(nome)
            return FakeAdapter(nome)

        monkeypatch.setattr(cli, "make_adapter", fake_make_adapter)
        args = _args(data_root=str(tmp_path), skip=["rosap"])
        cli.cmd_discover_all(args)

        assert "rosap" not in chamadas
        assert "ntrs" in chamadas, "as demais fontes continuam rodando normalmente"

    def test_roda_todas_as_fontes_configuradas_por_padrao(self, tmp_path, monkeypatch):
        """Sem --only/--skip, cobre as 7 do YAML mais ntrs, rosap, core e govuk."""
        chamadas: list[str] = []

        def fake_make_adapter(nome, *a, **kw):
            chamadas.append(nome)
            return FakeAdapter(nome)

        monkeypatch.setattr(cli, "make_adapter", fake_make_adapter)
        args = _args(data_root=str(tmp_path))
        cli.cmd_discover_all(args)

        esperado = {"ntrs", "rosap", "core", "govuk"} | {p.stem for p in cli.SOURCES_DIR.glob("*.yaml")}
        assert set(chamadas) == esperado

    def test_documentos_descobertos_entram_na_fila(self, tmp_path, monkeypatch):
        """Nao e' so orquestracao vazia — o resultado do adaptador falso
        precisa mesmo chegar no Frontier, como um adaptador real chegaria."""
        monkeypatch.setattr(
            cli, "make_adapter",
            lambda nome, *a, **kw: FakeAdapter(nome, recs=[rec(nome, "1")]),
        )
        args = _args(data_root=str(tmp_path), only=["ntrs"])
        cli.cmd_discover_all(args)

        from crawler.core.frontier import Frontier

        f = Frontier(tmp_path / "frontier.sqlite")
        try:
            assert f.counts_by("source") == {"ntrs": 1}
        finally:
            f.close()

    def test_fontes_rodam_em_paralelo_nao_em_serie(self, tmp_path, monkeypatch):
        """Decisao de 2026-08-11: fontes diferentes batem em hosts diferentes,
        entao esperar uma terminar pra' comecar a proxima nao tem justificativa
        — e' exatamente o desperdicio medido ao vivo com CORDIS/ESA Cosmos
        (819s + 1200s em serie). Trava essa propriedade: 4 fontes de 0.3s cada
        rodando em serie levariam >=1.2s; em paralelo, bem menos que isso."""
        atraso = 0.3
        fontes_falsas = {
            nome: FakeAdapter(nome, delay_s=atraso)
            for nome in ("ntrs", "rosap", "faa", "dtic")
        }
        monkeypatch.setattr(cli, "make_adapter", lambda nome, *a, **kw: fontes_falsas[nome])

        args = _args(data_root=str(tmp_path), only=list(fontes_falsas))
        inicio = time.monotonic()
        cli.cmd_discover_all(args)
        decorrido = time.monotonic() - inicio

        limite_serial = atraso * len(fontes_falsas)
        assert decorrido < limite_serial, (
            f"levou {decorrido:.2f}s para {len(fontes_falsas)} fontes de {atraso}s — "
            f"parece serial (limite serial seria >= {limite_serial:.2f}s)"
        )


class TestConsultaVersusLexico:
    """`TERMOS_LEXICO_FORTE` e `strong_terms` sao duas listas que precisam
    concordar, e ja divergiram: o MOD britanico nomeia o artefato "Operating
    Concept", e ate o achado do GOV.UK (docs/achados-api.md) o termo estava
    numa lista e nao na outra. Nada no codigo impedia isso.

    A direcao que importa e' so uma: tudo que se BUSCA precisa pontuar. O
    contrario e' legitimo — `strong_terms` tem formas redundantes de prefixo
    ("operational concept description") que seriam desperdicio de varredura.
    """

    def test_todo_termo_buscado_pontua_no_lexico(self):
        from pathlib import Path

        from crawler.core.prefilter import Lexicon, normalize

        lex = Lexicon.load(Path(cli.CONFIG_DIR) / "lexicon.yaml")
        ausentes = [t for t in cli.TERMOS_LEXICO_FORTE if normalize(t) not in lex.strong]
        assert not ausentes, (
            f"termos usados como consulta mas ausentes de strong_terms: {ausentes} — "
            "a consulta traria o que o filtro depois descarta"
        )

    def test_orcamento_de_varredura(self):
        """Cada entrada custa uma varredura completa de cada fonte de API.

        Nao da' para testar redundancia por substring: se buscar "concept of
        operation" ja traz "Concept of Operations" depende de o motor fazer
        stemming, e isso varia entre NTRS (Elasticsearch), CORE e GOV.UK. Como
        nao da' para verificar sem bater nas APIs, mantem-se as duas formas — o
        custo e' uma varredura, o risco de remover e' perder recall silenciosa-
        mente. O que DA' para travar e' o teto: a lista nao pode virar
        `strong_terms` inteiro sem alguem decidir isso de proposito.
        """
        assert len(cli.TERMOS_LEXICO_FORTE) <= 16, (
            f"{len(cli.TERMOS_LEXICO_FORTE)} termos de consulta — cada um e' uma "
            "varredura por fonte; confirme que o custo de crawl e' aceitavel"
        )
