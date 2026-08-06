"""Orquestra execuções do coletor via subprocesso da própria CLI.

Não importa `Pipeline`/`Fetcher` aqui: cada clique na página dispara
`python -m crawler.cli ...` como processo separado — exatamente como se fosse
digitado no terminal. Isso isola o servidor web de qualquer travamento numa
coleta longa e reaproveita 100% do caminho já testado da CLI, sem duplicar
lógica de descoberta/coleta.

Um job por vez: um único operador, um único navegador — não há fila de jobs,
só um "slot" ocupado ou livre. `_lock` protege tanto a checagem de "já tem
algo rodando" quanto o disparo do subprocesso, para duas requisições
concorrentes não acabarem lançando dois processos.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import metrics

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "reports" / "webui-runs"


class JobEmAndamento(Exception):
    """Já existe uma execução em andamento."""


@dataclass
class _Job:
    id: str
    kind: str  # "discover" | "harvest"
    sources: list[str]  # só preenchido para "discover" (harvest não filtra por fonte)
    started_at: str
    process: subprocess.Popen
    log_path: Path
    baseline: dict[str, Any]  # totals() completo no instante do start
    baseline_visited: dict[str, int]  # por fonte de navegação, só para discover
    baseline_por_fonte: dict[str, dict[str, int]]  # cruzamento fonte x status no instante do start


_lock = threading.Lock()
_current: _Job | None = None


def start_discover(sources: list[str], limit: int | None) -> str:
    if not sources:
        raise ValueError("selecione ao menos uma fonte")
    cmd = [sys.executable, "-m", "crawler.cli", "discover-all", "--only", *sources]
    if limit:
        cmd += ["--limit", str(limit)]
    root = metrics.data_root()
    baseline_visited = {
        s: metrics.visited_pages(root, s) for s in sources if metrics.is_crawl_source(s)
    }
    return _start("discover", sources, cmd, root, baseline_visited)


def start_harvest(limit: int | None, tiers: list[str] | None) -> str:
    cmd = [sys.executable, "-m", "crawler.cli", "harvest"]
    if limit:
        cmd += ["--limit", str(limit)]
    if tiers:
        cmd += ["--tier", *tiers]
    root = metrics.data_root()
    return _start("harvest", [], cmd, root, {})


def _start(
    kind: str, sources: list[str], cmd: list[str], root: Path, baseline_visited: dict[str, int]
) -> str:
    global _current
    with _lock:
        if _current is not None and _current.process.poll() is None:
            raise JobEmAndamento(
                f"já tem uma coleta em andamento (job {_current.id}, {_current.kind})"
            )

        job_id = uuid.uuid4().hex[:8]
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"{job_id}.log"
        baseline = metrics.totals(root)
        baseline_por_fonte = metrics.counts_by_source_and_status(root)

        with open(log_path, "w", encoding="utf-8") as log_fh:
            process = subprocess.Popen(cmd, cwd=ROOT, stdout=log_fh, stderr=subprocess.STDOUT)

        _current = _Job(
            id=job_id,
            kind=kind,
            sources=sources,
            started_at=datetime.now(timezone.utc).isoformat(),
            process=process,
            log_path=log_path,
            baseline=baseline,
            baseline_visited=baseline_visited,
            baseline_por_fonte=baseline_por_fonte,
        )
        return job_id


def status() -> dict[str, Any]:
    with _lock:
        job = _current
    if job is None:
        return {"ativo": False}

    root = metrics.data_root()
    rodando = job.process.poll() is None
    atual = metrics.totals(root)

    delta_status = {
        k: atual["por_status"].get(k, 0) - job.baseline["por_status"].get(k, 0)
        for k in set(atual["por_status"]) | set(job.baseline["por_status"])
    }

    resultado: dict[str, Any] = {
        "ativo": True,
        "rodando": rodando,
        "job_id": job.id,
        "tipo": job.kind,
        "fontes": job.sources,
        "iniciado_em": job.started_at,
        "codigo_saida": job.process.returncode if not rodando else None,
        "delta_status": delta_status,
        "log_cauda": _tail(job.log_path),
    }

    if job.kind == "discover":
        resultado["novos_candidatos_por_fonte"] = {
            s: atual["por_fonte"].get(s, 0) - job.baseline["por_fonte"].get(s, 0)
            for s in job.sources
        }
        resultado["urls_vasculhadas_por_fonte"] = {
            s: metrics.visited_pages(root, s) - job.baseline_visited[s]
            for s in job.sources
            if s in job.baseline_visited
        }
    elif job.kind == "harvest":
        # Harvest processa fontes que nao foram escolhidas de antemao (nao ha
        # checkbox de fonte no harvest — ver plano/README) — por isso aqui a
        # lista de fontes mostradas eh descoberta NA HORA, cruzando o que
        # mudou de status desde o inicio do job, nao uma lista fixa.
        atual_por_fonte = metrics.counts_by_source_and_status(root)
        delta_por_fonte: dict[str, dict[str, int]] = {}
        for fonte in set(atual_por_fonte) | set(job.baseline_por_fonte):
            antes = job.baseline_por_fonte.get(fonte, {})
            depois = atual_por_fonte.get(fonte, {})
            delta = {
                k: depois.get(k, 0) - antes.get(k, 0) for k in set(antes) | set(depois)
            }
            delta = {k: v for k, v in delta.items() if v != 0}
            if delta:
                delta_por_fonte[fonte] = delta
        resultado["delta_status_por_fonte"] = delta_por_fonte
    return resultado


def _tail(path: Path, n: int = 40) -> list[str]:
    try:
        linhas = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    return linhas[-n:]
