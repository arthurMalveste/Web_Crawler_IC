"""Leituras somente-consulta para o painel web.

Nada aqui grava no coletor: reaproveita `Config`, `SourceSpec`, `Frontier` e
`URLFrontier` tais como já existem em `crawler/`. A única coisa nova é ler e
formatar para JSON — nenhum arquivo de `crawler/core`, `crawler/engine` ou
`crawler/adapters` é alterado por causa do painel.

`Frontier`/`URLFrontier` já abrem SQLite em modo WAL com `busy_timeout`
(heranca do trabalho de paralelismo — ver `crawler/core/frontier.py` e
`crawler/engine/urlfrontier.py`), então abrir aqui uma conexão de leitura
separada, em outro processo, enquanto um `discover`/`harvest` está gravando
nos mesmos arquivos, é seguro sem nenhum mecanismo novo de sincronização.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from crawler.core.fetcher import Config
from crawler.core.frontier import STATUS_DISCOVERED, STATUS_STORED, Frontier
from crawler.core.record import TIER_STRONG, TIER_WEAK
from crawler.engine.spec import SourceSpec
from crawler.engine.urlfrontier import URLFrontier

# Mesmo dicionario que crawler/cli.py usa para o `--tier` da CLI — o painel
# expoe as mesmas tres faixas ao operador, entao usa o mesmo vocabulario.
TIER_LABELS: dict[str, str] = {"strong": TIER_STRONG, "weak": TIER_WEAK}

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SOURCES_DIR = CONFIG_DIR / "sources"

# Fontes por API dedicada não têm SourceSpec (config/sources/*.yaml) — o host
# vem dos próprios adaptadores (crawler/adapters/ntrs.py, rosap.py, core_api.py,
# govuk.py) e das entradas correspondentes em config/domains.yaml. Cada fonte
# de API nova precisa ser adicionada aqui manualmente — `cli.py` descobre
# "core"/"govuk" por `if name ==` direto, não por varredura de diretório,
# então este painel não tem como listá-las sozinho.
API_SOURCES: dict[str, list[str]] = {
    "ntrs": ["ntrs.nasa.gov"],
    "rosap": ["rosap.ntl.bts.gov"],
    "core": ["api.core.ac.uk"],
    "govuk": ["www.gov.uk", "assets.publishing.service.gov.uk"],
}


def load_config() -> Config:
    return Config.load(CONFIG_DIR / "domains.yaml")


def data_root() -> Path:
    return load_config().data_root


def is_crawl_source(name: str) -> bool:
    """True para fontes de navegação HTML (têm um SourceSpec em
    config/sources/*.yaml) — só essas têm fronteira de URLs (`urlfrontier.sqlite`)."""
    return (SOURCES_DIR / f"{name}.yaml").exists()


def totals(root: Path) -> dict[str, Any]:
    """Snapshot atual da fila de documentos — mesma leitura que
    `cli.py report` usa, só que direto, sem passar pela CLI."""
    with Frontier(root / "frontier.sqlite") as frontier:
        return frontier.totals()


def visited_pages(root: Path, source: str) -> int:
    """Páginas HTML já visitadas por uma fonte de navegação. Não chamar para
    fontes via API (`is_crawl_source` diz quais fazem sentido)."""
    with URLFrontier(root / "urlfrontier.sqlite", source) as uf:
        return uf.visited_pages()


def counts_by_source_and_status(root: Path) -> dict[str, dict[str, int]]:
    """Cruzamento fonte × status: `{"ntrs": {"stored": 10, "discovered": 2}, ...}`.

    `Frontier.counts_by()` (crawler/core/frontier.py) só agrupa por UMA
    coluna — criar um método novo lá só para este cruzamento contrariaria a
    decisão de manter `crawler/core` intocado pelo painel. A consulta é
    somente leitura, direto no mesmo `frontier.sqlite` que `Frontier` já usa
    (WAL + `busy_timeout`, seguro para ler enquanto outro processo grava —
    mesma lógica de `totals()`/`visited_pages()` acima).
    """
    db = root / "frontier.sqlite"
    if not db.exists():
        return {}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        linhas = conn.execute(
            "SELECT source, status, COUNT(*) AS n FROM documents GROUP BY source, status"
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, dict[str, int]] = {}
    for linha in linhas:
        out.setdefault(linha["source"], {})[linha["status"]] = linha["n"]
    return out


def pending_by_source(root: Path, tiers: list[str] | None) -> dict[str, int]:
    """Quantos documentos pendentes por fonte, filtrados pela mesma faixa que
    `harvest --tier` usaria — resposta a "quantos serão baixados agora".

    `tiers` usa os rótulos da CLI/painel (`strong`/`weak`/`negative`), não os
    valores internos de `crawler.core.record`; convertidos aqui via
    `TIER_LABELS`, mesmo dicionário que `crawler/cli.py::cmd_harvest` usa.
    `tiers` vazio ou `None` = sem filtro — mesmo comportamento de
    `crawler.cli harvest` sem `--tier` (baixa tudo, todas as faixas).

    Reaproveita `Frontier.pending()` — o MESMO generator que
    `Pipeline.harvest()` consome (crawler/core/pipeline.py) — então a
    contagem aqui é garantidamente igual ao que o harvest de fato
    processaria, não uma estimativa à parte.
    """
    valores = [TIER_LABELS[t] for t in tiers] if tiers else None
    contagem: dict[str, int] = {}
    with Frontier(root / "frontier.sqlite") as frontier:
        for rec in frontier.pending(tiers=valores):
            contagem[rec.source] = contagem.get(rec.source, 0) + 1
    return contagem


def list_sources() -> list[dict[str, Any]]:
    """Uma linha por fonte configurada: nome, tipo, host(s), taxa/concorrência
    configuradas (via `Config.policy_for`, sem lógica de resolução nova),
    quantos documentos estão pendentes de baixar e quantos já foram."""
    config = load_config()
    specs = SourceSpec.load_all(SOURCES_DIR) if SOURCES_DIR.exists() else {}
    cruzamento = counts_by_source_and_status(config.data_root)

    fontes = [
        _descrever(nome, "api", hosts, config, cruzamento) for nome, hosts in API_SOURCES.items()
    ]
    fontes += [
        _descrever(nome, spec.kind, spec.scope.allow_hosts, config, cruzamento)
        for nome, spec in sorted(specs.items())
    ]
    return fontes


def _descrever(
    nome: str, tipo: str, hosts: list[str], config: Config, cruzamento: dict[str, dict[str, int]]
) -> dict[str, Any]:
    politicas = []
    for host in hosts:
        p = config.policy_for(host)
        politicas.append({"host": host, "rate_s": p.rate, "concurrency": p.concurrency})
    por_status = cruzamento.get(nome, {})
    return {
        "nome": nome,
        "tipo": tipo,
        "navega_urls": tipo != "api",
        "politicas": politicas,
        "pendentes": por_status.get(STATUS_DISCOVERED, 0),
        "armazenados": por_status.get(STATUS_STORED, 0),
    }


# ------------------------------------------------------------ Etapa 3 (filtro)

FAIXAS_ETAPA3 = ["ambos", "so_conops", "so_dominio", "nenhum"]


def filtro_etapa3(root: Path) -> dict[str, Any]:
    """Resultado de `etapa3/filtrar.py` (<corpus>/filtro.jsonl) para o painel:
    contagem faixa x fonte, limiares em uso e a lista de documentos. So le o
    arquivo — quem calcula as faixas e o script da Etapa 3."""
    caminho = root / "filtro.jsonl"
    if not caminho.exists():
        return {"existe": False}
    docs = [json.loads(l) for l in caminho.read_text(encoding="utf-8").splitlines() if l.strip()]
    fontes = sorted({d["source"] for d in docs})
    contagem = {f: {s: 0 for s in fontes} for f in FAIXAS_ETAPA3}
    for d in docs:
        contagem[d["faixa"]][d["source"]] += 1
    lexico = yaml.safe_load((CONFIG_DIR / "lexicon.yaml").read_text(encoding="utf-8"))
    return {
        "existe": True,
        "gerado_em": datetime.fromtimestamp(caminho.stat().st_mtime).isoformat(timespec="seconds"),
        "parametros": lexico.get("etapa3", {}),
        "fontes": fontes,
        "contagem": contagem,
        "documentos": sorted(docs, key=lambda d: (FAIXAS_ETAPA3.index(d["faixa"]), -d["nota_conops"])),
    }


def rodar_filtro_etapa3() -> None:
    """Roda `etapa3/filtrar.py` e espera: sao segundos, sem rede — nao precisa
    do esquema de jobs em segundo plano do discover/harvest."""
    subprocess.run([sys.executable, str(ROOT / "etapa3" / "filtrar.py")], check=True,
                   capture_output=True, cwd=ROOT)


def arquivo_documento(root: Path, sha: str) -> tuple[Path, str] | None:
    """Arquivo armazenado de um documento (PDF, ou o .txt do NTRS) para o botao
    "ver documento" do painel. Aceita so um sha256 que esteja na fila como
    armazenado — o caminho vem do banco, nunca da requisicao."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        return None
    with sqlite3.connect(root / "frontier.sqlite") as con:
        linha = con.execute(
            "SELECT stored_path, content_kind FROM documents WHERE sha256 = ? AND status = ?",
            (sha, STATUS_STORED),
        ).fetchone()
    if not linha or not linha[0]:
        return None
    caminho = (root / linha[0]).resolve()
    if root.resolve() not in caminho.parents or not caminho.exists():
        return None
    return caminho, "application/pdf" if linha[1] == "pdf" else "text/plain"
