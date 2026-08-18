"""Testes do painel web (`webui/`).

Não tocam rede nem o corpus real: `CONOPS_DATA_ROOT` é redirecionado para
`tmp_path` (mesmo mecanismo de `.env`/ambiente que o resto do projeto usa —
ver `tests/test_env.py`), e `subprocess.Popen` é trocado por um processo
falso — nenhum destes testes chega a chamar `crawler.cli` de verdade.

Foco: a orquestração (`jobs.py`) e a leitura/formatação (`metrics.py`), não
o coletor em si — isso já é coberto pelos outros arquivos de teste.
"""

from __future__ import annotations

import pytest

from crawler.core.frontier import STATUS_DISCOVERED, STATUS_STORED, Frontier
from crawler.core.record import TIER_NEGATIVE, TIER_STRONG, TIER_WEAK, DocumentRecord
from webui import jobs, metrics
from webui.app import app as flask_app


def _rec(source: str, sid: str, tier: str = TIER_STRONG) -> DocumentRecord:
    return DocumentRecord(
        source=source,
        source_id=sid,
        title=f"documento {sid}",
        landing_url=f"https://example.org/{source}/{sid}",
        candidate_urls=[f"https://example.org/{source}/{sid}.pdf"],
        tier=tier,
    )


class FakeProcess:
    """Substitui `subprocess.Popen`: nunca chega a rodar `crawler.cli`."""

    def __init__(self, *a, **kw):
        self.returncode = None

    def poll(self):
        return self.returncode

    def concluir(self, codigo: int = 0):
        self.returncode = codigo


@pytest.fixture
def isolado(tmp_path, monkeypatch):
    """Corpus isolado em tmp_path + logs do painel isolados também."""
    monkeypatch.setenv("CONOPS_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(jobs, "LOG_DIR", tmp_path / "webui-runs")
    jobs._current = None  # cada teste comeca sem job pendente do teste anterior
    return tmp_path


@pytest.fixture
def sem_subprocess(monkeypatch, isolado):
    """`isolado` + `subprocess.Popen` trocado por um processo falso."""
    fakes: list[FakeProcess] = []

    def fake_popen(*a, **kw):
        p = FakeProcess()
        fakes.append(p)
        return p

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    return fakes


class TestMetrics:
    def test_list_sources_inclui_api_e_crawl(self, isolado):
        """Achado real (2026-08-16): `core` e depois `govuk` foram
        implementados em `cli.py` mas esquecidos em `API_SOURCES` — o painel
        web nunca lista uma fonte de API que nao esteja nesse dict manual,
        entao "adicionar um adaptador novo" e "aparecer no painel" nao sao a
        mesma coisa. Trava aqui pra nao repetir."""
        fontes = metrics.list_sources()
        nomes = {f["nome"] for f in fontes}
        assert {"ntrs", "rosap", "core", "govuk"} <= nomes
        # toda fonte com SourceSpec (config/sources/*.yaml) tem que aparecer
        specs_no_disco = {p.stem for p in metrics.SOURCES_DIR.glob("*.yaml")}
        assert specs_no_disco <= nomes

    def test_fila_vazia_conta_zero_documentos(self, isolado):
        for f in metrics.list_sources():
            assert f["pendentes"] == 0
            assert f["armazenados"] == 0

    def test_ntrs_e_api_sem_navegacao_de_urls(self, isolado):
        ntrs = next(f for f in metrics.list_sources() if f["nome"] == "ntrs")
        assert ntrs["tipo"] == "api"
        assert ntrs["navega_urls"] is False
        hosts = {p["host"] for p in ntrs["politicas"]}
        assert "ntrs.nasa.gov" in hosts

    def test_taxa_configurada_vem_do_domains_yaml(self, isolado):
        ntrs = next(f for f in metrics.list_sources() if f["nome"] == "ntrs")
        politica = next(p for p in ntrs["politicas"] if p["host"] == "ntrs.nasa.gov")
        # ver config/domains.yaml — 500 req/15min medido, 2.0s configurado
        assert politica["rate_s"] == 2.0

    def test_is_crawl_source_distingue_api_de_yaml(self, isolado):
        assert metrics.is_crawl_source("ntrs") is False
        assert metrics.is_crawl_source("rosap") is False
        alguma_fonte_yaml = next(metrics.SOURCES_DIR.glob("*.yaml")).stem
        assert metrics.is_crawl_source(alguma_fonte_yaml) is True


class TestCruzamentoFonteStatus:
    def test_sem_banco_devolve_vazio(self, isolado):
        assert metrics.counts_by_source_and_status(isolado) == {}

    def test_cruza_fonte_e_status(self, isolado):
        with Frontier(isolado / "frontier.sqlite") as f:
            r1, r2, r3 = _rec("ntrs", "1"), _rec("ntrs", "2"), _rec("rosap", "9")
            f.add(r1, status=STATUS_DISCOVERED)
            f.add(r2, status=STATUS_DISCOVERED)
            f.add(r3, status=STATUS_DISCOVERED)
            f.mark(r1.key, STATUS_STORED)

        cruzamento = metrics.counts_by_source_and_status(isolado)
        assert cruzamento["ntrs"] == {STATUS_DISCOVERED: 1, STATUS_STORED: 1}
        assert cruzamento["rosap"] == {STATUS_DISCOVERED: 1}

    def test_list_sources_separa_pendentes_de_armazenados(self, isolado):
        with Frontier(isolado / "frontier.sqlite") as f:
            f.add(_rec("ntrs", "1"), status=STATUS_DISCOVERED)
            r2 = _rec("ntrs", "2")
            f.add(r2, status=STATUS_DISCOVERED)
            f.mark(r2.key, STATUS_STORED)

        ntrs = next(f for f in metrics.list_sources() if f["nome"] == "ntrs")
        assert ntrs["pendentes"] == 1
        assert ntrs["armazenados"] == 1


class TestPendingBySource:
    def test_filtra_pela_mesma_faixa_que_o_harvest_usaria(self, isolado):
        with Frontier(isolado / "frontier.sqlite") as f:
            f.add(_rec("ntrs", "1", tier=TIER_STRONG), status=STATUS_DISCOVERED)
            f.add(_rec("ntrs", "2", tier=TIER_WEAK), status=STATUS_DISCOVERED)
            f.add(_rec("rosap", "9", tier=TIER_NEGATIVE), status=STATUS_DISCOVERED)

        assert metrics.pending_by_source(isolado, ["strong"]) == {"ntrs": 1}
        assert metrics.pending_by_source(isolado, ["strong", "weak"]) == {"ntrs": 2}

    def test_sem_faixa_marcada_nao_filtra(self, isolado):
        """Documenta o comportamento: nenhuma faixa = TODAS entram, igual a
        `crawler.cli harvest` sem `--tier`. Não é um bug do painel."""
        with Frontier(isolado / "frontier.sqlite") as f:
            f.add(_rec("ntrs", "1", tier=TIER_STRONG), status=STATUS_DISCOVERED)
            f.add(_rec("rosap", "9", tier=TIER_NEGATIVE), status=STATUS_DISCOVERED)

        esperado = {"ntrs": 1, "rosap": 1}
        assert metrics.pending_by_source(isolado, None) == esperado
        assert metrics.pending_by_source(isolado, []) == esperado


class TestJobsUmPorVez:
    def test_recusa_segundo_job_enquanto_primeiro_roda(self, sem_subprocess):
        jobs.start_discover(["ntrs"], None)
        with pytest.raises(jobs.JobEmAndamento):
            jobs.start_discover(["rosap"], None)

    def test_libera_apos_o_processo_terminar(self, sem_subprocess):
        jobs.start_discover(["ntrs"], None)
        sem_subprocess[0].concluir(0)
        # nao levanta: o job anterior ja terminou (poll() != None)
        jobs.start_discover(["rosap"], None)

    def test_discover_sem_fontes_e_erro(self, sem_subprocess):
        with pytest.raises(ValueError):
            jobs.start_discover([], None)

    def test_status_ocioso_sem_job(self, isolado):
        assert jobs.status() == {"ativo": False}

    def test_status_reflete_job_em_andamento(self, sem_subprocess):
        jobs.start_discover(["ntrs"], None)
        s = jobs.status()
        assert s["ativo"] is True
        assert s["rodando"] is True
        assert s["tipo"] == "discover"
        assert s["fontes"] == ["ntrs"]
        assert "novos_candidatos_por_fonte" in s

    def test_status_marca_concluido_com_codigo_saida(self, sem_subprocess):
        jobs.start_discover(["ntrs"], None)
        sem_subprocess[0].concluir(1)
        s = jobs.status()
        assert s["rodando"] is False
        assert s["codigo_saida"] == 1

    def test_harvest_nao_carrega_lista_de_fontes(self, sem_subprocess):
        jobs.start_harvest(None, None)
        s = jobs.status()
        assert s["tipo"] == "harvest"
        assert s["fontes"] == []
        assert "novos_candidatos_por_fonte" not in s

    def test_harvest_mostra_resultado_por_fonte(self, sem_subprocess, isolado):
        # popula a fila ANTES do job comecar — vira a "baseline" (0 armazenados)
        with Frontier(isolado / "frontier.sqlite") as f:
            r1 = _rec("ntrs", "1")
            f.add(r1, status=STATUS_DISCOVERED)

        jobs.start_harvest(None, None)

        # simula o subprocesso baixando o documento — o processo real faria
        # isso via Pipeline.harvest(); aqui so mexemos direto no banco, o
        # mesmo arquivo que um subprocesso de verdade estaria escrevendo.
        with Frontier(isolado / "frontier.sqlite") as f:
            f.mark(r1.key, STATUS_STORED)

        s = jobs.status()
        assert s["delta_status_por_fonte"] == {"ntrs": {STATUS_STORED: 1, STATUS_DISCOVERED: -1}}

    def test_harvest_sem_mudanca_nao_lista_fonte(self, sem_subprocess, isolado):
        with Frontier(isolado / "frontier.sqlite") as f:
            f.add(_rec("ntrs", "1"), status=STATUS_DISCOVERED)
        jobs.start_harvest(None, None)
        s = jobs.status()
        assert s["delta_status_por_fonte"] == {}


class TestRotasFlask:
    @pytest.fixture
    def client(self, sem_subprocess):
        flask_app.testing = True
        return flask_app.test_client()

    def test_status_ocioso(self, client):
        r = client.get("/api/status")
        assert r.status_code == 200
        assert r.get_json() == {"ativo": False}

    def test_sources_devolve_lista(self, client):
        r = client.get("/api/sources")
        assert r.status_code == 200
        assert isinstance(r.get_json(), list)

    def test_discover_sem_fontes_devolve_400(self, client):
        r = client.post("/api/discover", json={"sources": []})
        assert r.status_code == 400
        assert "erro" in r.get_json()

    def test_discover_inicia_job(self, client):
        r = client.post("/api/discover", json={"sources": ["ntrs"], "limit": 5})
        assert r.status_code == 200
        assert "job_id" in r.get_json()

    def test_segundo_discover_concorrente_devolve_409(self, client):
        client.post("/api/discover", json={"sources": ["ntrs"]})
        r = client.post("/api/discover", json={"sources": ["rosap"]})
        assert r.status_code == 409
        assert "erro" in r.get_json()

    def test_harvest_preview_sem_pendentes(self, client):
        r = client.get("/api/harvest-preview")
        assert r.status_code == 200
        assert r.get_json() == {"total": 0, "por_fonte": {}}

    def test_harvest_preview_respeita_faixa_da_query_string(self, client, isolado):
        with Frontier(isolado / "frontier.sqlite") as f:
            f.add(_rec("ntrs", "1", tier=TIER_STRONG), status=STATUS_DISCOVERED)
            f.add(_rec("rosap", "9", tier=TIER_NEGATIVE), status=STATUS_DISCOVERED)

        r = client.get("/api/harvest-preview?tiers=strong")
        assert r.get_json() == {"total": 1, "por_fonte": {"ntrs": 1}}

        r = client.get("/api/harvest-preview")  # sem query string = sem filtro
        assert r.get_json() == {"total": 2, "por_fonte": {"ntrs": 1, "rosap": 1}}
