"""Adaptador que expoe o motor de rastreamento com a interface dos demais.

E o que faz o crawler generico e os clientes de API conviverem: para o pipeline,
rastrear o site da FAA e consultar a API do NTRS sao a mesma operacao — ambas
emitem `DocumentRecord`. Trocar de fonte nao muda o codigo a jusante.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import structlog

from ..core.prefilter import Lexicon
from ..core.record import DocumentRecord
from ..engine.crawler import Crawler, CrawlStats
from ..engine.renderer import make_renderer
from ..engine.spec import ROLE_HARD_NEGATIVES, SourceSpec
from ..engine.urlfrontier import URLFrontier
from .base import BaseAdapter

log = structlog.get_logger(__name__)


class CrawlAdapter(BaseAdapter):
    def __init__(
        self,
        fetcher,
        *,
        spec: SourceSpec,
        lexicon: Lexicon,
        frontier_db: str | Path,
        reset: bool = False,
        retry_failed: bool = False,
        max_workers: int | None = None,
        **options: Any,
    ):
        super().__init__(fetcher, **options)
        self.spec = spec
        self.name = spec.name
        self.collect_all_negatives = spec.role == ROLE_HARD_NEGATIVES
        self.lexicon = lexicon
        self.frontier_db = Path(frontier_db)
        self.reset = reset
        #: Reabre paginas `failed` antes de rastrear de novo — recupera de
        #: falha transitoria (timeout, manutencao do site) sem descartar o
        #: progresso inteiro como `reset` faria. Ignorado se `reset` tambem
        #: estiver ligado (reset ja apaga tudo, tornar isso redundante).
        self.retry_failed = retry_failed
        self.max_workers = max_workers
        self.stats: CrawlStats | None = None

    def discover(self) -> Iterator[DocumentRecord]:
        ua = self.fetcher.config.defaults.get("user_agent", "")
        renderer = make_renderer(self.spec, ua)
        frontier = URLFrontier(self.frontier_db, self.spec.name)
        if self.reset:
            # Reexecutar um experimento (focado vs BFS) exige comecar do zero:
            # uma fronteira com URLs ja visitadas falsearia a comparacao.
            frontier.reset()
        elif self.retry_failed:
            n = frontier.requeue_failed()
            if n:
                log.info("crawl.retry_failed", fonte=self.spec.name, reenfileiradas=n)

        try:
            if renderer is not None:
                renderer.__enter__()
            crawler = Crawler(
                self.spec, self.fetcher, self.lexicon, frontier,
                renderer=renderer, max_workers=self.max_workers,
            )
            yield from crawler.crawl()
            self.stats = crawler.stats
        finally:
            if renderer is not None:
                renderer.close()
            frontier.close()
