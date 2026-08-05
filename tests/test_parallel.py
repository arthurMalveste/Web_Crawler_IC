"""Testes do harvest paralelo por dominio.

O harvest agora dispara varias threads simultaneas — uma por documento em
processamento — e conta com o `Fetcher` para nunca deixar um unico dominio
ultrapassar o `rate`/`concurrency` que `config/domains.yaml` declara. Os
testes aqui cobrem exatamente as duas metades da mudanca:

  1. dominios DIFERENTES progridem em paralelo de verdade (o ganho de
     desempenho que motivou a mudanca);
  2. o MESMO dominio continua respeitando seu teto de taxa e concorrencia
     mesmo com varias threads mirando nele (a seguranca que evita bater num
     servidor externo mais forte do que a politica configurada permite).

Mais um bloco cobre a correcao sob concorrencia de SQLite (Frontier) e
gravacao em disco (Store): o cenario onde documentos de fontes DIFERENTES tem
o MESMO conteudo (deduplicacao) e agora e processado por threads reais ao
mesmo tempo, nao mais em sequencia.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx
import pytest
import respx

from crawler.core.concurrency import KeyedLock, KeyedSemaphore
from crawler.core.fetcher import Config, Fetcher
from crawler.core.frontier import Frontier
from crawler.core.pipeline import Pipeline
from crawler.core.prefilter import Lexicon
from crawler.core.record import DocumentRecord
from crawler.core.store import Store

ROOT = Path(__file__).resolve().parent.parent
PDF = b"%PDF-1.7\n" + b"x" * 400 + b"\n%%EOF"


# --------------------------------------------------------------- concurrency.py


class TestKeyedLock:
    def test_mesma_chave_devolve_o_mesmo_lock(self):
        locks = KeyedLock()
        assert locks.get("a") is locks.get("a")

    def test_chaves_diferentes_sao_locks_independentes(self):
        locks = KeyedLock()
        a, b = locks.get("a"), locks.get("b")
        assert a is not b
        assert a.acquire(timeout=0)
        try:
            assert b.acquire(timeout=0), "chave diferente nao pode esperar a de outra"
        finally:
            b.release()
            a.release()


class TestKeyedSemaphore:
    def test_teto_e_respeitado(self):
        sems = KeyedSemaphore()
        sem = sems.get("host", 2)
        assert sem.acquire(timeout=0)
        assert sem.acquire(timeout=0)
        assert not sem.acquire(timeout=0), "terceira aquisicao deveria bloquear"

    def test_valor_minimo_e_um(self):
        sems = KeyedSemaphore()
        sem = sems.get("host", 0)
        assert sem.acquire(timeout=0)


# ---------------------------------------------------------------- fetcher.py


def _config(domains: dict) -> Config:
    """Config isolada, sem tocar em config/domains.yaml — os testes de tempo
    precisam de valores exatos e nao devem quebrar se o YAML real mudar."""
    return Config(
        {
            "data_root": "data",
            "defaults": {
                "respect_robots": False,
                "retries": 1,
                "timeout_s": 10,
                "max_download_s": 10,
                "max_file_mb": 10,
                "user_agent": "test-bot",
            },
            "domains": domains,
        }
    )


class ProbeDeConcorrencia:
    """Conta quantas chamadas estao em voo ao mesmo tempo e guarda o pico."""

    def __init__(self, atraso_s: float = 0.05):
        self.lock = threading.Lock()
        self.em_voo = 0
        self.pico = 0
        self.inicios: list[float] = []
        self.atraso_s = atraso_s

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.em_voo += 1
            self.pico = max(self.pico, self.em_voo)
            self.inicios.append(time.monotonic())
        time.sleep(self.atraso_s)
        with self.lock:
            self.em_voo -= 1
        # Corpo distinto por URL: evita que duas respostas identicas colidam
        # no dedup por SHA-256, o que esconderia a concorrencia sob teste.
        return httpx.Response(200, content=PDF + str(request.url).encode())


class TestConcorrenciaPorHost:
    @respx.mock
    def test_teto_de_concorrencia_do_dominio_e_respeitado(self):
        """`concurrency: 2` no YAML nunca deixa 3 requisicoes em voo juntas."""
        cfg = _config({"lento.test": {"rate": 0.0, "concurrency": 2}})
        probe = ProbeDeConcorrencia(atraso_s=0.08)
        respx.get(url__startswith="https://lento.test/").mock(side_effect=probe)

        with Fetcher(cfg) as f:
            threads = [
                threading.Thread(target=f.fetch, args=(f"https://lento.test/{i}.pdf",))
                for i in range(6)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert probe.pico <= 2, f"pico de {probe.pico} requisicoes simultaneas excede concurrency=2"
        assert probe.pico >= 2, "com 6 chamadas e concurrency=2 o teto deveria ser alcancado"

    @respx.mock
    def test_taxa_minima_vale_mesmo_com_concorrencia_maior_que_um(self):
        """`concurrency` amplia quantos ficam em voo, mas nao dispensa `rate`
        entre o INICIO de requisicoes sucessivas ao mesmo host."""
        cfg = _config({"lento.test": {"rate": 0.12, "concurrency": 3}})
        probe = ProbeDeConcorrencia(atraso_s=0.01)
        respx.get(url__startswith="https://lento.test/").mock(side_effect=probe)

        with Fetcher(cfg) as f:
            threads = [
                threading.Thread(target=f.fetch, args=(f"https://lento.test/{i}.pdf",))
                for i in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        inicios = sorted(probe.inicios)
        gaps = [b - a for a, b in zip(inicios, inicios[1:])]
        assert all(g >= 0.10 for g in gaps), f"intervalos menores que o rate configurado: {gaps}"

    @respx.mock
    def test_dominios_diferentes_progridem_em_paralelo(self):
        """O proprio ganho de desempenho que motivou a mudanca: dois dominios
        com `concurrency: 1` cada um NAO se esperam entre si."""
        cfg = _config(
            {
                "a.test": {"rate": 0.0, "concurrency": 1},
                "b.test": {"rate": 0.0, "concurrency": 1},
            }
        )
        atraso = 0.15
        respx.get(url__startswith="https://a.test/").mock(
            side_effect=lambda r: (time.sleep(atraso), httpx.Response(200, content=PDF))[1]
        )
        respx.get(url__startswith="https://b.test/").mock(
            side_effect=lambda r: (time.sleep(atraso), httpx.Response(200, content=PDF))[1]
        )

        with Fetcher(cfg) as f:
            inicio = time.monotonic()
            threads = [
                threading.Thread(target=f.fetch, args=("https://a.test/x.pdf",)),
                threading.Thread(target=f.fetch, args=("https://b.test/x.pdf",)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            decorrido = time.monotonic() - inicio

        # Sequencial custaria ~2*atraso; em paralelo, ~1*atraso. Limiar no
        # meio do caminho absorve folga de agendamento do SO sem esconder uma
        # regressao para o comportamento sequencial antigo.
        assert decorrido < 1.5 * atraso, f"{decorrido:.3f}s sugere que os dominios se esperaram"


# ---------------------------------------------------------------- pipeline.py


@pytest.fixture
def lexicon():
    return Lexicon.load(ROOT / "config" / "lexicon.yaml")


def make_rec(source: str, sid: str, url: str, titulo: str = "Concept of Operations for X"):
    return DocumentRecord(
        source=source,
        source_id=sid,
        title=titulo,
        landing_url=f"https://example.org/{sid}",
        candidate_urls=[url],
    )


class FakeAdapter:
    name = "fake"

    def __init__(self, recs):
        self._recs = recs

    def discover(self):
        yield from self._recs


@pytest.fixture
def ambiente(tmp_path, lexicon):
    cfg = _config(
        {
            "ntrs.nasa.gov": {"rate": 0.0, "concurrency": 4},
            "rosap.ntl.bts.gov": {"rate": 0.0, "concurrency": 4},
            "faa.gov": {"rate": 0.0, "concurrency": 4},
        }
    )
    fetcher = Fetcher(cfg)
    store = Store(tmp_path)
    frontier = Frontier(tmp_path / "f.sqlite")
    yield Pipeline(frontier, store, fetcher, lexicon), frontier, store
    frontier.close()
    fetcher.close()


class TestSegurancaConcorrenteDoHarvest:
    @respx.mock
    def test_mesmo_conteudo_de_fontes_diferentes_sob_concorrencia_real(self, ambiente):
        """12 registros em 3 fontes, todos com o MESMO PDF — o cenario de
        deduplicacao, agora processado por threads de verdade ao mesmo tempo.

        Nao corromper aqui significa: nenhuma excecao (nem IntegrityError do
        SQLite por INSERT duplicado, nem FileNotFoundError por dois `.part`
        colidindo), exatamente um arquivo em disco, e a contagem de
        armazenados+duplicados batendo com o total.
        """
        pipeline, frontier, store = ambiente
        for host in ("ntrs.nasa.gov", "rosap.ntl.bts.gov", "faa.gov"):
            respx.get(url__startswith=f"https://{host}/").mock(
                return_value=httpx.Response(200, content=PDF)
            )

        recs = [
            make_rec(fonte, str(i), f"https://{host}/{i}.pdf")
            for i, (fonte, host) in enumerate(
                [("ntrs", "ntrs.nasa.gov"), ("rosap", "rosap.ntl.bts.gov"), ("faa", "faa.gov")] * 4
            )
        ]
        pipeline.discover(FakeAdapter(recs))
        st = pipeline.harvest()

        assert st.armazenados + st.duplicados == 12
        assert st.armazenados == 1, "conteudo identico deveria virar um unico armazenamento"
        assert len(list(store.raw.rglob("*.pdf"))) == 1
        assert not list(store.raw.rglob("*.part")), "nenhum temporario deveria sobrar"

        dup = frontier.duplicate_stats()
        assert dup["conteudos_unicos"] == 1
        # As 3 fontes distintas devem ter sido registradas como proveniencia.
        assert dup["conteudos_em_multiplas_fontes"] == 1

    @respx.mock
    def test_conteudos_distintos_sob_concorrencia_nao_se_misturam(self, ambiente):
        """20 registros, 20 conteudos DIFERENTES — nenhum deveria ser
        confundido com outro por causa de uma corrida entre threads."""
        pipeline, frontier, store = ambiente
        respx.get(url__startswith="https://ntrs.nasa.gov/").mock(
            side_effect=lambda r: httpx.Response(200, content=PDF + str(r.url).encode())
        )
        recs = [make_rec("ntrs", str(i), f"https://ntrs.nasa.gov/{i}.pdf") for i in range(20)]
        pipeline.discover(FakeAdapter(recs))
        st = pipeline.harvest()

        assert st.armazenados == 20 and st.duplicados == 0
        assert len(list(store.raw.rglob("*.pdf"))) == 20

    @respx.mock
    def test_limite_e_exato_com_paralelismo_ligado(self, ambiente):
        """`limit` e aplicado na consulta ao SQLite, antes de qualquer thread
        ser aberta — continua exato mesmo com N workers concorrentes."""
        pipeline, frontier, _ = ambiente
        respx.get(url__startswith="https://ntrs.nasa.gov/").mock(
            return_value=httpx.Response(200, content=PDF)
        )
        recs = [make_rec("ntrs", str(i), f"https://ntrs.nasa.gov/{i}.pdf") for i in range(8)]
        pipeline.discover(FakeAdapter(recs))

        st = pipeline.harvest(limit=3)
        assert st.tentados == 3
        assert len(list(pipeline.frontier.pending())) == 5

    @respx.mock
    def test_workers_customizado_e_respeitado(self, ambiente):
        pipeline, frontier, _ = ambiente
        respx.get(url__startswith="https://ntrs.nasa.gov/").mock(
            return_value=httpx.Response(200, content=PDF)
        )
        recs = [make_rec("ntrs", str(i), f"https://ntrs.nasa.gov/{i}.pdf") for i in range(5)]
        pipeline.discover(FakeAdapter(recs))

        st = pipeline.harvest(max_workers=1)
        assert st.tentados == 5 and st.armazenados + st.duplicados == 5


class TestFrontierThreadSafe:
    def test_muitas_threads_gravando_nao_perdem_atualizacao(self, tmp_path, lexicon):
        """register_content chamado por N threads com o MESMO sha: so uma
        pode "ganhar" o INSERT, as demais so acumulam proveniencia."""
        frontier = Frontier(tmp_path / "f.sqlite")
        try:
            resultados: list[bool] = []
            lock = threading.Lock()

            def registrar(i: int) -> None:
                novo = frontier.register_content("mesmosha", "raw/ab/mesmosha.pdf", "pdf", 10, f"fonte:{i}")
                with lock:
                    resultados.append(novo)

            threads = [threading.Thread(target=registrar, args=(i,)) for i in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert resultados.count(True) == 1, "exatamente uma thread deveria ter criado o registro"
            assert resultados.count(False) == 15

            conteudo = frontier.get_content("mesmosha")
            import json

            prov = json.loads(conteudo["provenance"])
            assert len(prov) == 16, "todas as 16 proveniencias deveriam ter sido acumuladas"
            assert len(set(prov)) == 16, "nenhuma proveniencia deveria ter sido perdida"
        finally:
            frontier.close()
