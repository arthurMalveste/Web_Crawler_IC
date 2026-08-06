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

from pathlib import Path
from typing import Any

from crawler.core.fetcher import Config
from crawler.core.frontier import Frontier
from crawler.engine.spec import SourceSpec
from crawler.engine.urlfrontier import URLFrontier

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SOURCES_DIR = CONFIG_DIR / "sources"

# Fontes por API dedicada não têm SourceSpec (config/sources/*.yaml) — o host
# vem dos próprios adaptadores (crawler/adapters/ntrs.py, rosap.py) e das
# entradas correspondentes em config/domains.yaml.
API_SOURCES: dict[str, list[str]] = {
    "ntrs": ["ntrs.nasa.gov"],
    "rosap": ["rosap.ntl.bts.gov"],
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


def list_sources() -> list[dict[str, Any]]:
    """Uma linha por fonte configurada: nome, tipo, host(s), taxa/concorrência
    configuradas (via `Config.policy_for`, sem lógica de resolução nova) e
    quantos documentos já existem na fila hoje."""
    config = load_config()
    specs = SourceSpec.load_all(SOURCES_DIR) if SOURCES_DIR.exists() else {}
    por_fonte = totals(config.data_root)["por_fonte"]

    fontes = [
        _descrever(nome, "api", hosts, config, por_fonte) for nome, hosts in API_SOURCES.items()
    ]
    fontes += [
        _descrever(nome, spec.kind, spec.scope.allow_hosts, config, por_fonte)
        for nome, spec in sorted(specs.items())
    ]
    return fontes


def _descrever(
    nome: str, tipo: str, hosts: list[str], config: Config, por_fonte: dict[str, int]
) -> dict[str, Any]:
    politicas = []
    for host in hosts:
        p = config.policy_for(host)
        politicas.append({"host": host, "rate_s": p.rate, "concurrency": p.concurrency})
    return {
        "nome": nome,
        "tipo": tipo,
        "navega_urls": tipo != "api",
        "politicas": politicas,
        "documentos_na_fila": por_fonte.get(nome, 0),
    }
