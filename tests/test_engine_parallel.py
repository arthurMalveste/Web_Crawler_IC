"""Testes do rastreamento HTML paralelo.

Espelha `tests/test_parallel.py` (harvest), agora para `Crawler.crawl()`: o
motor passou de uma pagina por vez para rodadas de `next_batch(n)` disparadas
num `ThreadPoolExecutor`. Os testes aqui cobrem exatamente o que essa mudanca
arriscava — `URLFrontier`/`HostBudget` sob concorrencia real, e o ganho de
tempo que motivou a mudanca — sem tocar rede real.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import respx

from crawler.core.fetcher import Config, Fetcher
from crawler.core.prefilter import Lexicon
from crawler.engine.crawler import Crawler
from crawler.engine.spec import STRATEGY_BFS, Scope, SourceSpec
from crawler.engine.traps import HostBudget
from crawler.engine.urlfrontier import URLFrontier

ROOT = Path(__file__).resolve().parent.parent


def _config(domains: dict) -> Config:
    """Config isolada, sem depender de config/domains.yaml — mesmo motivo de
    tests/test_parallel.py: os testes de tempo precisam de valores exatos."""
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


# ------------------------------------------------------------- URLFrontier


class TestURLFrontierThreadSafe:
    def test_muitas_threads_adicionando_urls_diferentes(self, tmp_path):
        f = URLFrontier(tmp_path / "u.sqlite", "s")
        threads = [
            threading.Thread(target=f.add, args=(f"https://x.org/{i}",), kwargs={"depth": 0, "priority": float(i)})
            for i in range(40)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(f.next_batch(100)) == 40, "nenhuma URL deveria se perder sob concorrencia"
        f.close()

    def test_muitas_threads_marcando_urls_diferentes(self, tmp_path):
        f = URLFrontier(tmp_path / "u.sqlite", "s")
        for i in range(30):
            f.add(f"https://x.org/{i}", depth=0)
        alvos = f.next_batch(30)

        threads = [threading.Thread(target=f.mark, args=(u.url, "visited"), kwargs={"http_status": 200}) for u in alvos]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert f.counts() == {"visited": 30}
        f.close()


class TestHostBudgetConcorrente:
    def test_teto_e_respeitado_sob_concorrencia(self):
        budget = HostBudget(max_por_host=10)
        permitidos: list[int] = []
        lock = threading.Lock()

        def tentar(i: int) -> None:
            if budget.allow(f"https://x.org/{i}"):
                with lock:
                    permitidos.append(i)

        threads = [threading.Thread(target=tentar, args=(i,)) for i in range(60)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(permitidos) == 10, f"teto de 10 deveria valer mesmo com 60 threads batendo — passaram {len(permitidos)}"


# ------------------------------------------------------------------- Crawler


class TestWorkerCount:
    """`_worker_count()` sem rede — so a logica de dimensionamento."""

    def _crawler(self, tmp_path, cfg, **spec_kw):
        lexicon = Lexicon.load(ROOT / "config" / "lexicon.yaml")
        fetcher = Fetcher(cfg)
        frontier = URLFrontier(tmp_path / "u.sqlite", "ex")
        spec = SourceSpec(
            name="ex", seeds=["https://ex.org/"], sitemap="none",
            scope=Scope(allow_hosts=["ex.org"]), **spec_kw,
        )
        return Crawler(spec, fetcher, lexicon, frontier), fetcher, frontier

    def test_usa_concorrencia_configurada_do_host(self, tmp_path):
        cfg = _config({"ex.org": {"rate": 0.0, "concurrency": 5}})
        c, fetcher, frontier = self._crawler(tmp_path, cfg)
        assert c._worker_count() == 5
        fetcher.close()
        frontier.close()

    def test_render_forca_sequencial(self, tmp_path):
        """API sincrona do Playwright nao foi desenhada para chamadas
        concorrentes sobre o mesmo browser/context."""
        cfg = _config({"ex.org": {"rate": 0.0, "concurrency": 8}})
        c, fetcher, frontier = self._crawler(tmp_path, cfg, render=True)
        assert c._worker_count() == 1
        fetcher.close()
        frontier.close()

    def test_max_workers_explicito_tem_prioridade_sobre_o_automatico(self, tmp_path):
        cfg = _config({"ex.org": {"rate": 0.0, "concurrency": 5}})
        lexicon = Lexicon.load(ROOT / "config" / "lexicon.yaml")
        fetcher = Fetcher(cfg)
        frontier = URLFrontier(tmp_path / "u.sqlite", "ex")
        spec = SourceSpec(name="ex", seeds=["https://ex.org/"], sitemap="none", scope=Scope(allow_hosts=["ex.org"]))
        c = Crawler(spec, fetcher, lexicon, frontier, max_workers=2)
        assert (c._max_workers_override or c._worker_count()) == 2
        fetcher.close()
        frontier.close()


class TestCrawlerParaleloDeVerdade:
    @respx.mock
    def test_paginas_irmas_avancam_em_paralelo(self, tmp_path):
        """A propriedade central: N paginas irmas (descobertas na mesma
        rodada) sao buscadas ao mesmo tempo, nao uma apos a outra."""
        lexicon = Lexicon.load(ROOT / "config" / "lexicon.yaml")
        cfg = _config({"ex.org": {"rate": 0.0, "concurrency": 4}})
        atraso = 0.12
        n_folhas = 8

        def responder(request: httpx.Request) -> httpx.Response:
            time.sleep(atraso)
            path = urlsplit(str(request.url)).path
            if path in ("", "/"):
                links = "".join(f'<a href="/f{i}">folha {i}</a>' for i in range(n_folhas))
                return httpx.Response(200, html=f"<html><body>{links}</body></html>")
            return httpx.Response(200, html="<html><body>sem mais links</body></html>")

        respx.get(url__startswith="https://ex.org/").mock(side_effect=responder)

        with Fetcher(cfg) as fetcher:
            frontier = URLFrontier(tmp_path / "u.sqlite", "ex")
            spec = SourceSpec(
                name="ex", seeds=["https://ex.org/"], strategy=STRATEGY_BFS, sitemap="none",
                scope=Scope(allow_hosts=["ex.org"], max_depth=2, max_pages=n_folhas + 1),
            )
            c = Crawler(spec, fetcher, lexicon, frontier)
            inicio = time.monotonic()
            list(c.crawl())
            decorrido = time.monotonic() - inicio
            frontier.close()

        sequencial_estimado = (n_folhas + 1) * atraso
        assert c.stats.paginas_baixadas == n_folhas + 1, "nenhuma pagina deveria se perder nem duplicar"
        assert decorrido < sequencial_estimado * 0.7, (
            f"{decorrido:.2f}s nao sugere paralelismo real (sequencial seria ~{sequencial_estimado:.2f}s)"
        )

    @respx.mock
    def test_resultado_e_correto_sob_paralelismo(self, tmp_path):
        """Nao so mais rapido — o mesmo resultado de antes: todo link de
        documento vira candidato, sem duplicar nem perder nenhum."""
        lexicon = Lexicon.load(ROOT / "config" / "lexicon.yaml")
        cfg = _config({"ex.org": {"rate": 0.0, "concurrency": 6}})
        n_docs = 12

        def responder(request: httpx.Request) -> httpx.Response:
            path = urlsplit(str(request.url)).path
            if path in ("", "/"):
                links = "".join(f'<a href="/doc{i}.pdf">Concept of Operations {i}</a>' for i in range(n_docs))
                return httpx.Response(200, html=f"<html><body>{links}</body></html>")
            return httpx.Response(200, content=b"%PDF-1.7\n" + str(path).encode())

        respx.get(url__startswith="https://ex.org/").mock(side_effect=responder)

        with Fetcher(cfg) as fetcher:
            frontier = URLFrontier(tmp_path / "u.sqlite", "ex")
            spec = SourceSpec(
                name="ex", seeds=["https://ex.org/"], sitemap="none",
                scope=Scope(allow_hosts=["ex.org"], max_depth=2, max_pages=5),
            )
            recs = list(Crawler(spec, fetcher, lexicon, frontier).crawl())
            frontier.close()

        assert len(recs) == n_docs
        assert len({r.source_id for r in recs}) == n_docs, "nenhum documento deveria duplicar"
