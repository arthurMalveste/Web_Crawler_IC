"""CLI da Etapa 1.

    python -m crawler.cli discover ntrs  --limit 500
    python -m crawler.cli discover rosap --from 2026-07-01 --max-pages 5
    python -m crawler.cli harvest --tier strong --limit 100
    python -m crawler.cli report
    python -m crawler.cli sync-ntrs --since 2026-07-01

`discover` e `harvest` sao comandos separados de proposito: a descoberta so le
metadados e pode ser reexecutada a vontade quando o lexico mudar; so a coleta
gasta banda e disco.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from .adapters.core_api import COREAdapter
from .adapters.crawl import CrawlAdapter
from .adapters.govuk import GovUKAdapter
from .adapters.ntrs import NTRSAdapter, NTRSRedistributions
from .adapters.rosap import RosaPAdapter
from .core.fetcher import Config, Fetcher
from .core.frontier import Frontier
from .core.pipeline import Pipeline
from .core.prefilter import Lexicon
from .core.record import TIER_STRONG, TIER_WEAK
from .core.store import Store
from .engine.spec import STRATEGY_BFS, STRATEGY_FOCUSED, SourceSpec

log = structlog.get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SOURCES_DIR = CONFIG_DIR / "sources"

#: Termos de sinal forte do lexico, usados como consulta pelas fontes de API
#: que aceitam busca textual (NTRS, CORE, GOV.UK) — e o mesmo vocabulario que
#: decide a faixa, entao consulta e filtro nao divergem.
TERMOS_LEXICO_FORTE = [
    "concept of operations",
    "conops",
    "operational concept",
    "operations concept",
    "operating concept",
    "concept of employment",
    "mission operations concept",
]


def setup_logging(verbose: bool = False) -> None:
    # Achado real (2026-08-19): no Windows, sem isto, `sys.stdout` abre na
    # codepage do console (cp1252 tipicamente), nao UTF-8. Uma excecao com
    # caractere fora dessa codepage — ex.: o Playwright embute bordas
    # Unicode (═, ║...) na PROPRIA mensagem de erro quando o navegador nao
    # esta instalado — faz o `print()` do log da excecao quebrar com
    # `UnicodeEncodeError`. Essa segunda excecao NAO esta protegida por
    # nenhum `try/except` (acontece dentro do proprio bloco que trata a
    # primeira), sobe ate `cmd_discover_all` e derruba o processo inteiro —
    # levando junto fontes que nao tinham nada a ver com o erro original.
    # `errors="backslashreplace"` garante que isto nunca mais aconteca: na
    # pior hipotese um caractere vira `\uXXXX` legivel no log, em vez de
    # crashar. Isto e' independente do webui abrir o arquivo de log em UTF-8
    # (`webui/jobs.py`) — o que importa aqui e' a codificacao do PROPRIO
    # `sys.stdout` deste processo filho, nao de como o pai redireciona.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=logging.DEBUG if verbose else logging.INFO
    )
    # Ruido de bibliotecas: o httpx loga cada requisicao em INFO e o pypdf
    # despeja um aviso por objeto malformado ao contar paginas (relatorios
    # governamentais antigos tem muitos). Nada disso e sinal para o operador.
    for nome in ("httpx", "httpcore", "pypdf", "pypdf._reader", "PyPDF2"):
        logging.getLogger(nome).setLevel(logging.WARNING if verbose else logging.ERROR)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if verbose else logging.INFO
        ),
    )


def build(args: argparse.Namespace):
    config = Config.load(CONFIG_DIR / "domains.yaml")
    lexicon = Lexicon.load(CONFIG_DIR / "lexicon.yaml")
    data_root = Path(args.data_root) if args.data_root else config.data_root
    store = Store(data_root)
    frontier = Frontier(data_root / "frontier.sqlite")
    fetcher = Fetcher(config)
    pipeline = Pipeline(frontier, store, fetcher, lexicon)
    return config, lexicon, store, frontier, fetcher, pipeline


def make_adapter(name: str, fetcher, lexicon: Lexicon, args: argparse.Namespace):
    if name == "ntrs":
        terms = args.terms or TERMOS_LEXICO_FORTE
        return NTRSAdapter(
            fetcher,
            terms=terms,
            year_start=args.year_start,
            year_end=args.year_end,
        )
    if name == "rosap":
        return RosaPAdapter(
            fetcher,
            from_date=args.from_date,
            until_date=args.until_date,
            max_pages=args.max_pages,
        )
    if name == "core":
        terms = args.terms or TERMOS_LEXICO_FORTE
        # Chave fora do YAML de proposito (nao pode ser versionada) — vem do
        # `.env` via `os.environ`, ja carregado a esta altura porque `build()`
        # sempre constroi `Config` primeiro (ver `core/env.py::carregar_dotenv`).
        api_key = os.environ.get("CORE_API_KEY", "")
        return COREAdapter(fetcher, api_key=api_key, terms=terms)
    if name == "govuk":
        terms = args.terms or TERMOS_LEXICO_FORTE
        return GovUKAdapter(fetcher, terms=terms)

    # Qualquer outra fonte e resolvida por SourceSpec — adicionar um
    # repositorio novo custa um YAML, nao um modulo Python.
    spec_path = SOURCES_DIR / f"{name}.yaml"
    if not spec_path.exists():
        disponiveis = ", ".join(sorted(p.stem for p in SOURCES_DIR.glob("*.yaml")))
        raise SystemExit(f"fonte desconhecida: {name}. Com spec: {disponiveis}")

    spec = SourceSpec.load(spec_path)
    if getattr(args, "strategy", None):
        spec.strategy = args.strategy
    if getattr(args, "max_pages", None):
        spec.scope.max_pages = args.max_pages
    if getattr(args, "max_depth", None):
        spec.scope.max_depth = args.max_depth
    if getattr(args, "no_render", False):
        spec.render = False
    if getattr(args, "no_sitemap", False):
        spec.sitemap = "none"

    data_root = Path(args.data_root) if args.data_root else fetcher.config.data_root
    return CrawlAdapter(
        fetcher,
        spec=spec,
        lexicon=lexicon,
        frontier_db=data_root / "urlfrontier.sqlite",
        reset=getattr(args, "reset", False),
        retry_failed=getattr(args, "retry_failed", False),
        max_workers=getattr(args, "workers", None),
    )


def cmd_discover(args: argparse.Namespace) -> int:
    _, lexicon, store, frontier, fetcher, pipeline = build(args)
    with fetcher, frontier:
        adapter = make_adapter(args.adapter, fetcher, lexicon, args)
        stats = pipeline.discover(adapter, limit=args.limit)
        saida: dict[str, Any] = {"descoberta": stats.as_dict()}
        if isinstance(adapter, CrawlAdapter) and adapter.stats:
            saida["rastreamento"] = adapter.stats.as_dict()
    print(json.dumps(saida, indent=2, ensure_ascii=False))
    return 0


def _args_para_fonte(nome: str, base: argparse.Namespace) -> argparse.Namespace:
    """Namespace de `discover` para UMA fonte, dentro de `discover-all`.

    Cada fonte usa SEUS PROPRIOS defaults (do YAML ou do adapter) — nao
    tentamos impor `max-pages`/`year-start`/etc. igual pra todas; isso e' o
    que `discover <fonte> --max-pages N` continua servindo para ajuste fino.
    """
    return argparse.Namespace(
        adapter=nome,
        limit=getattr(base, "limit", None),
        terms=None,
        year_start=1960,
        year_end=datetime.now().year,
        from_date=None,
        until_date=None,
        max_pages=None,
        max_depth=None,
        strategy=None,
        reset=False,
        retry_failed=getattr(base, "retry_failed", False),
        no_render=False,
        no_sitemap=False,
        workers=getattr(base, "workers", None),
        data_root=base.data_root,
        verbose=base.verbose,
    )


def cmd_discover_all(args: argparse.Namespace) -> int:
    """Roda `discover` em TODAS as fontes configuradas, em PARALELO.

    Existe porque cobrir as 9 fontes hoje (7 YAML + ntrs + rosap) exigia 9
    invocacoes manuais — facil de esquecer uma. Uma fonte que falha (servidor
    fora do ar, por exemplo) nao pode travar a cobertura das outras: captura,
    loga, segue para a proxima — isso vale tanto rodando em serie quanto em
    paralelo, e continua valendo aqui.

    Ate 2026-08-11 isso era um `for` sequencial: uma fonte inteira (`build()`
    + `discover()`, minutos para CORDIS/ESA Cosmos) tinha que terminar antes
    da proxima nem comecar — sem motivo real, ja que fontes diferentes batem
    em HOSTS diferentes, e e' o `Fetcher` (semaforo por host) quem decide o
    ritmo de cada uma, nao esta funcao. E exatamente o desenho que ja
    sustenta `harvest()` paralelo entre fontes (ver `Pipeline.harvest`) —
    aqui aplica-se o mesmo: construir `Frontier`/`Store`/`Fetcher` UMA vez,
    compartilhados (todos ja sao thread-safe: `Frontier`/`URLFrontier` usam
    WAL + lock, `Store.append_reject` tem lock proprio, `Fetcher` e' o
    proprio ponto que aplica o teto por host), e disparar o `discover()` de
    cada fonte num pool. Testado ao vivo: Playwright (unica fonte com
    `render=True`, ESA EOF) funciona normalmente rodando fora da thread
    principal neste ambiente.
    """
    fontes = ["ntrs", "rosap", "core", "govuk"] + (
        sorted(p.stem for p in SOURCES_DIR.glob("*.yaml")) if SOURCES_DIR.exists() else []
    )
    if args.only:
        fontes = [f for f in fontes if f in args.only]
    if args.skip:
        fontes = [f for f in fontes if f not in args.skip]

    def _rodar(nome: str) -> tuple[str, dict[str, Any]]:
        try:
            sub_args = _args_para_fonte(nome, args)
            adapter = make_adapter(nome, fetcher, lexicon, sub_args)
            stats = pipeline.discover(adapter, limit=sub_args.limit)
            saida: dict[str, Any] = {"descoberta": stats.as_dict()}
            if isinstance(adapter, CrawlAdapter) and adapter.stats:
                saida["rastreamento"] = adapter.stats.as_dict()
            return nome, saida
        except Exception as exc:
            log.error("discover_all.falhou", fonte=nome, error=str(exc))
            return nome, {"erro": str(exc)}

    resultados: dict[str, Any] = {}
    _, lexicon, store, frontier, fetcher, pipeline = build(args)
    with fetcher, frontier:
        with ThreadPoolExecutor(max_workers=len(fontes) or 1, thread_name_prefix="discover-all") as pool:
            futuros = {pool.submit(_rodar, nome): nome for nome in fontes}
            for fut in as_completed(futuros):
                nome, saida = fut.result()
                print(f"=== {nome} ===")
                resultados[nome] = saida

    print(json.dumps(resultados, indent=2, ensure_ascii=False))
    return 0


def cmd_harvest(args: argparse.Namespace) -> int:
    _, _, _, frontier, fetcher, pipeline = build(args)
    tiers = None
    if args.tier:
        tiers = [{"strong": TIER_STRONG, "weak": TIER_WEAK}[t] for t in args.tier]
    with fetcher, frontier:
        stats = pipeline.harvest(
            limit=args.limit, tiers=tiers, max_workers=args.workers, so_pdf=args.pdf
        )
    print(json.dumps(stats.as_dict(), indent=2, ensure_ascii=False))
    return 0


def cmd_experiment(args: argparse.Namespace) -> int:
    """Rastreamento focado vs BFS na MESMA fonte, com orcamento identico.

    Da uma leitura diagnostica de se pontuar links antes de segui-los muda
    quantos candidatos aparecem sob o mesmo orcamento — NAO e' prova de
    precisao: "relevante" aqui e' so o tier do lexico, sem verificacao contra
    verdade nenhuma. As duas execucoes compartilham todo o codigo: a unica
    diferenca e o LinkScorer estar ligado ou desligado. A fronteira de URLs e
    zerada entre elas, senao a segunda herdaria o trabalho da primeira.
    """
    # O sitemap e desligado no experimento, sempre. Ele entrega URLs sem que
    # nenhum link seja seguido — as duas estrategias receberiam exatamente a
    # mesma lista e a comparacao nao mediria nada. O que esta sob teste e a
    # ordem de expansao da fronteira, e isso so aparece navegando.
    args.no_sitemap = True

    resultados: dict[str, Any] = {}
    for estrategia in (STRATEGY_BFS, STRATEGY_FOCUSED):
        _, lexicon, store, frontier, fetcher, pipeline = build(args)
        args.strategy = estrategia
        args.reset = True
        with fetcher, frontier:
            adapter = make_adapter(args.adapter, fetcher, lexicon, args)
            pipeline.discover(adapter)
            resultados[estrategia] = {
                **adapter.stats.as_dict(),
                "curva": adapter.stats.curva,
            }

    bfs = resultados[STRATEGY_BFS]
    foc = resultados[STRATEGY_FOCUSED]
    ganho = (foc["harvest_rate"] / bfs["harvest_rate"]) if bfs["harvest_rate"] else None
    resumo = {
        "fonte": args.adapter,
        "orcamento_paginas": args.max_pages,
        "bfs": bfs,
        "focado": foc,
        "ganho_harvest_rate": round(ganho, 2) if ganho else None,
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "reports" / f"experimento-{args.adapter}-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(resumo, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps({k: v for k, v in resumo.items() if k != "curva"}, indent=2, ensure_ascii=False))
    print(f"\n-> {out}")
    return 0


def cmd_browse(args: argparse.Namespace) -> int:
    """Gera um indice HTML do corpus — resolve os nomes em SHA-256."""
    from .browse import gerar

    config = Config.load(CONFIG_DIR / "domains.yaml")
    data_root = Path(args.data_root) if args.data_root else config.data_root
    tiers = None
    if args.tier:
        tiers = [{"strong": TIER_STRONG, "weak": TIER_WEAK}[t] for t in args.tier]
    saida = Path(args.out) if args.out else ROOT / "reports" / "corpus.html"
    n = gerar(data_root / "frontier.sqlite", saida, tiers, args.limit)
    print(f"{n} documentos -> {saida}")
    if args.abrir:
        import webbrowser

        webbrowser.open(saida.resolve().as_uri())
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    """Devolve os falhos a fila — usar depois de corrigir o coletor."""
    _, _, _, frontier, fetcher, _ = build(args)
    with fetcher, frontier:
        n = frontier.requeue_failed(args.source)
    print(json.dumps({"reenfileirados": n}, indent=2))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    _, _, store, frontier, fetcher, _ = build(args)
    with fetcher, frontier:
        totals = frontier.totals()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "reports" / f"metrics-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(totals, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(totals, indent=2, ensure_ascii=False))
    print(f"\n-> {out}")
    return 0


def cmd_sync_ntrs(args: argparse.Namespace) -> int:
    """Sincronizacao exigida pelos termos de uso do NTRS (consulta semanal)."""
    _, _, store, frontier, fetcher, _ = build(args)
    with fetcher, frontier:
        changed = NTRSRedistributions(fetcher).since(args.since)
        retirados = 0
        for item in changed:
            doc_id = str(item.get("id") or item.get("citationId") or "")
            if not doc_id:
                continue
            key = f"ntrs:{doc_id}"
            disponivel = item.get("available", item.get("distribution")) not in (False, "NONE")
            if not disponivel:
                frontier.mark(key, "withdrawn", withdrawn_at=datetime.now(timezone.utc).isoformat())
                store.append_reject(None, "retirado_pela_fonte", key=key)
                retirados += 1
    print(json.dumps({"alterados": len(changed), "marcados_retirados": retirados}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="crawler", description="Etapa 1 — coleta de ConOps")
    p.add_argument("--data-root", help="sobrepoe data_root do domains.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    fontes = sorted(p.stem for p in SOURCES_DIR.glob("*.yaml")) if SOURCES_DIR.exists() else []
    d = sub.add_parser(
        "discover",
        help="descobrir candidatos (so metadados). Fontes: ntrs, rosap, core, govuk, "
        + ", ".join(fontes),
    )
    d.add_argument("adapter")
    d.add_argument("--limit", type=int)
    d.add_argument("--terms", nargs="*")
    d.add_argument("--year-start", type=int, default=1960)
    d.add_argument("--year-end", type=int, default=datetime.now().year)
    d.add_argument("--from", dest="from_date")
    d.add_argument("--until", dest="until_date")
    d.add_argument("--max-pages", type=int, help="orcamento de paginas (crawl) ou paginas OAI")
    d.add_argument("--max-depth", type=int, help="profundidade maxima (crawl)")
    d.add_argument(
        "--strategy",
        choices=[STRATEGY_FOCUSED, STRATEGY_BFS],
        help="sobrepoe a estrategia do SourceSpec",
    )
    d.add_argument("--reset", action="store_true", help="zerar a fronteira de URLs antes")
    d.add_argument(
        "--retry-failed",
        action="store_true",
        help="reabrir paginas 'failed' desta fonte antes de rastrear (ignorado se --reset)",
    )
    d.add_argument("--no-render", action="store_true", help="desligar Playwright nesta execucao")
    d.add_argument("--no-sitemap", action="store_true", help="forcar navegacao em vez de sitemap")
    d.add_argument(
        "--workers",
        type=int,
        help="threads simultaneas no rastreamento HTML (padrao: concorrencia configurada do host)",
    )
    d.set_defaults(func=cmd_discover)

    da = sub.add_parser(
        "discover-all",
        help="rodar 'discover' em todas as fontes configuradas, uma de cada vez",
    )
    da.add_argument("--limit", type=int, help="teto por fonte")
    da.add_argument("--only", nargs="*", help="rodar so estas fontes")
    da.add_argument("--skip", nargs="*", help="pular estas fontes")
    da.add_argument("--retry-failed", action="store_true", help="reabrir paginas 'failed' em cada fonte")
    da.add_argument("--workers", type=int, help="threads simultaneas por fonte (crawl)")
    da.set_defaults(func=cmd_discover_all)

    e = sub.add_parser(
        "experiment",
        help="rastreamento focado vs BFS na mesma fonte — leitura diagnostica, nao comparacao validada",
    )
    e.add_argument("adapter")
    e.add_argument("--max-pages", type=int, default=150)
    e.add_argument("--max-depth", type=int)
    e.add_argument("--no-render", action="store_true")
    e.add_argument("--workers", type=int, help="threads simultaneas (padrao: concorrencia do host)")
    e.set_defaults(func=cmd_experiment)

    h = sub.add_parser("harvest", help="baixar o que a fila aprovou (paralelo por dominio)")
    h.add_argument("--limit", type=int)
    h.add_argument("--tier", nargs="*", choices=["strong", "weak"])
    h.add_argument(
        "--workers",
        type=int,
        help="threads simultaneas (padrao: soma da concorrencia configurada por dominio em domains.yaml)",
    )
    h.add_argument(
        "--pdf", action="store_true", help="baixar so o PDF (ignora o .txt do NTRS e nao-PDFs)"
    )
    h.set_defaults(func=cmd_harvest)

    t = sub.add_parser("retry-failed", help="reenfileirar os falhos (apos corrigir o coletor)")
    t.add_argument("--source", help="limitar a uma fonte")
    t.set_defaults(func=cmd_retry)

    b = sub.add_parser("browse", help="indice HTML navegavel do corpus")
    b.add_argument("--tier", nargs="*", choices=["strong", "weak"])
    b.add_argument("--limit", type=int)
    b.add_argument("--out", help="caminho do HTML (padrao: reports/corpus.html)")
    b.add_argument("--abrir", action="store_true", help="abrir no navegador ao terminar")
    b.set_defaults(func=cmd_browse)

    r = sub.add_parser("report", help="metricas da coleta")
    r.set_defaults(func=cmd_report)

    s = sub.add_parser("sync-ntrs", help="job semanal de redistribuicoes (obrigatorio)")
    s.add_argument("--since", required=True, help="data ISO da ultima sincronizacao")
    s.set_defaults(func=cmd_sync_ntrs)

    args = p.parse_args(argv)
    setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
