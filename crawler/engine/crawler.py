"""Motor de rastreamento — o caso geral de coleta.

Fluxo por fonte:

    sementes + sitemap  ->  fronteira de URLs (ordenada)
              |
              v
    visitar pagina -> extrair links (ancora + contexto)
              |             |
              |             +-> pontuar cada link -> enfileirar
              v
    extrair metadados do <head>  ->  DocumentRecord

Os dois modos de expansao compartilham todo o codigo; a unica diferenca e a
prioridade atribuida pelo LinkScorer. Isso e proposital: comparar rastreamento
focado com BFS so tem valor se as duas execucoes forem identicas em tudo o mais.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urlsplit

import structlog

from ..core.fetcher import BlockedByPolicy, Fetcher, NotADocument
from ..core.prefilter import Lexicon
from ..core.record import TIER_NEGATIVE, DocumentRecord
from . import sitemap as sitemap_mod
from .extract import PageMetadata, extract_links, extract_metadata, parse_html
from .linkscorer import LinkScorer
from .spec import STRATEGY_FOCUSED, SourceSpec
from .traps import HostBudget, canonicalize, is_trap
from .urlfrontier import (
    KIND_DOCUMENT,
    KIND_PAGE,
    STATE_FAILED,
    STATE_SKIPPED,
    STATE_VISITED,
    URLFrontier,
)

log = structlog.get_logger(__name__)


@dataclass
class CrawlStats:
    """Metricas do rastreamento.

    `harvest_rate` — documentos relevantes por pagina HTML baixada — e' um
    numero diagnostico util (se cai muito, orcamento esta indo pra lugar
    errado), mas nao e' julgamento de precisao: "relevante" aqui e' so o
    tier do lexico (indicio), nao verdade confirmada. Nao tratar como prova
    de que uma estrategia de rastreamento e' melhor que outra.
    """

    paginas_baixadas: int = 0
    paginas_renderizadas: int = 0
    documentos_encontrados: int = 0
    documentos_relevantes: int = 0
    links_vistos: int = 0
    fora_de_escopo: int = 0
    armadilhas: int = 0
    erros: int = 0
    urls_do_sitemap: int = 0
    segundos: float = 0.0
    #: Serie temporal (paginas_baixadas, documentos_relevantes) — permite tracar
    #: a curva de descoberta das duas estrategias no relatorio.
    curva: list[tuple[int, int]] = field(default_factory=list)

    @property
    def harvest_rate(self) -> float:
        return self.documentos_relevantes / max(1, self.paginas_baixadas)

    def as_dict(self) -> dict[str, Any]:
        return {
            "paginas_baixadas": self.paginas_baixadas,
            "paginas_renderizadas": self.paginas_renderizadas,
            "documentos_encontrados": self.documentos_encontrados,
            "documentos_relevantes": self.documentos_relevantes,
            "links_vistos": self.links_vistos,
            "fora_de_escopo": self.fora_de_escopo,
            "armadilhas_evitadas": self.armadilhas,
            "erros": self.erros,
            "urls_do_sitemap": self.urls_do_sitemap,
            "segundos": round(self.segundos, 1),
            "harvest_rate": round(self.harvest_rate, 4),
        }


class Crawler:
    """Motor de rastreamento — visita paginas em paralelo por RODADAS.

    A fronteira de paginas cresce durante o proprio rastreamento (ao contrario
    do harvest, que parte de uma lista fechada), entao nao da pra materializar
    tudo de uma vez no inicio. A solucao e' processar em lotes do tamanho do
    pool: `next_batch(n)` devolve `n` linhas DISTINTAS (LIMIT do SQL), a rodada
    inteira e' disparada em paralelo, e so quando TODAS terminam e' que a
    proxima rodada busca mais — isso e' o que evita duas threads pegando a
    mesma URL, sem precisar inventar semantica de "reserva" de linha no
    SQLite. `Fetcher` ja aplica o teto de `rate`/`concurrency` por HOST (ver
    `core/fetcher.py`), entao visitar paginas em paralelo herda o mesmo
    respeito a cada dominio que os documentos do harvest ja tem.
    """

    def __init__(
        self,
        spec: SourceSpec,
        fetcher: Fetcher,
        lexicon: Lexicon,
        frontier: URLFrontier,
        *,
        renderer: Any | None = None,
        max_workers: int | None = None,
    ):
        self.spec = spec
        self.fetcher = fetcher
        self.lexicon = lexicon
        self.frontier = frontier
        self.renderer = renderer
        self.scorer = LinkScorer(lexicon, enabled=spec.strategy == STRATEGY_FOCUSED)
        self.stats = CrawlStats()
        self._budget = HostBudget(spec.scope.max_pages)
        self._max_workers_override = max_workers
        # Protege os CONTADORES de `self.stats` (`+=` nao e' atomico) — nao os
        # dicts abaixo, que nao precisam de lock: cada URL e' visitada por
        # exatamente UMA thread, uma vez so (garantido pelas rodadas: uma
        # chave nunca aparece em dois lotes, porque so vira PENDING de novo se
        # falhar, e falha nao repete a visita na MESMA rodada). Duas threads
        # escrevendo chaves DIFERENTES do mesmo dict e' seguro sem lock (GIL).
        self._stats_lock = threading.Lock()
        # Metadados das paginas ja visitadas. Guardar aqui evita rebaixar a
        # mesma landing page duas vezes: o metadado que identifica o documento
        # (titulo, autores, resumo) esta na pagina que continha o link, e essa
        # pagina ja foi baixada durante a travessia.
        self._page_meta: dict[str, PageMetadata] = {}
        # Quantos documentos cada pagina continha. Distingue LANDING PAGE (um
        # documento, e o metadado da pagina descreve esse documento) de PAGINA
        # DE LISTAGEM (varios documentos, e o metadado descreve a listagem, nao
        # cada arquivo). Sem essa distincao, os 40 PDFs de uma pagina de indice
        # herdariam todos o mesmo titulo.
        self._docs_por_pagina: dict[str, int] = {}

    # ------------------------------------------------------------------ ciclo

    def _worker_count(self) -> int:
        """Threads simultaneas para ESTE rastreamento.

        Usa a concorrencia configurada do host da(s) semente(s) — o mesmo
        teto que o `Fetcher` ja aplica por host de qualquer forma, entao abrir
        mais threads que isso so as deixaria esperando a vez, sem acelerar
        nada. Fontes com `render=True` (Playwright) ficam sempre em 1: a API
        sincrona do Playwright nao foi desenhada para chamadas concorrentes de
        `.render()` sobre o mesmo browser/context, e a unica fonte assim hoje
        (ESA EOF) tem universo de so 34 documentos — nao ha nada a ganhar
        arriscando isso.
        """
        if self.spec.render or not self.spec.seeds:
            return 1
        host = urlsplit(self.spec.seeds[0]).netloc.lower()
        return max(1, min(self.fetcher.policy_for(host).concurrency, 32))

    def crawl(self) -> Iterator[DocumentRecord]:
        inicio = time.monotonic()
        self._semear()

        n_workers = self._max_workers_override or self._worker_count()
        with ThreadPoolExecutor(max_workers=n_workers, thread_name_prefix="discover") as pool:
            while self.stats.paginas_baixadas < self.spec.scope.max_pages:
                restante = self.spec.scope.max_pages - self.stats.paginas_baixadas
                lote = self.frontier.next_batch(min(n_workers, restante), kind=KIND_PAGE)
                if not lote:
                    break
                futuros = [pool.submit(self._visitar, alvo) for alvo in lote]
                for fut in as_completed(futuros):
                    fut.result()  # relanca excecao de alguma thread, se houve

        # Documentos descobertos durante a travessia viram registros no fim: o
        # rastreamento identifica enderecos, quem baixa e a camada de fetch.
        yield from self._emitir_documentos()

        self.stats.segundos = time.monotonic() - inicio
        log.info(
            "crawl.concluido",
            fonte=self.spec.name,
            estrategia=self.spec.strategy,
            workers=n_workers,
            **self.stats.as_dict(),
        )

    def _semear(self) -> None:
        for url in self.spec.seeds:
            e_doc = self.spec.is_document_url(url)
            self.frontier.add(
                url,
                depth=0,
                # Semente sempre no topo: e escolha humana explicita.
                priority=1000.0,
                kind=KIND_DOCUMENT if e_doc else KIND_PAGE,
                # Sem ancora: nao ha link de origem. Marcar "(semente)" aqui
                # sequestraria o titulo do documento — o nome do arquivo e a
                # melhor evidencia disponivel quando nao ha ancora real.
                anchor=None,
            )

        if self.spec.sitemap == "none" or not self.spec.seeds:
            return

        alvos = (
            [self.spec.sitemap]
            if self.spec.sitemap.startswith("http")
            else sitemap_mod.discover_sitemaps(self.fetcher, self.spec.seeds[0])
        )
        for sm in alvos:
            for url in sitemap_mod.iter_sitemap_urls(self.fetcher, sm):
                if not self._em_escopo(url):
                    continue
                e_doc = self.spec.is_document_url(url)
                score = self.scorer.score(url, depth=1, is_document=e_doc)
                if self.frontier.add(
                    url,
                    depth=1,
                    priority=score.priority,
                    kind=KIND_DOCUMENT if e_doc else KIND_PAGE,
                    anchor=None,  # sitemap nao tem ancora; ver nota em _semear
                ):
                    self.stats.urls_do_sitemap += 1
                    if e_doc:
                        self.stats.documentos_encontrados += 1
            if self.stats.urls_do_sitemap:
                # Um sitemap util dispensa procurar os outros.
                break

        if self.stats.urls_do_sitemap:
            log.info("crawl.sitemap", fonte=self.spec.name, urls=self.stats.urls_do_sitemap)

    def _visitar(self, alvo) -> None:
        if not self._budget.allow(alvo.url):
            self.frontier.mark(alvo.url, STATE_SKIPPED, error="orcamento do host esgotado")
            return

        try:
            body, renderizado = self._buscar_pagina(alvo.url)
        except BlockedByPolicy as exc:
            self.frontier.mark(alvo.url, STATE_SKIPPED, error=str(exc))
            return
        except Exception as exc:
            with self._stats_lock:
                self.stats.erros += 1
            self.frontier.mark(alvo.url, STATE_FAILED, error=str(exc))
            log.warning("crawl.erro_pagina", url=alvo.url, error=str(exc))
            return

        if body is None:
            with self._stats_lock:
                self.stats.erros += 1
            self.frontier.mark(alvo.url, STATE_FAILED, error="sem corpo")
            return

        with self._stats_lock:
            self.stats.paginas_baixadas += 1
            if renderizado:
                self.stats.paginas_renderizadas += 1
        self.frontier.mark(alvo.url, STATE_VISITED, http_status=200)

        soup = parse_html(body)

        # A pagina pode ser, ela propria, uma landing page de documento: se o
        # <head> declara citation_pdf_url, o PDF esta identificado sem adivinhar.
        md = extract_metadata(soup, alvo.url)
        self._page_meta[alvo.url] = md  # chave = alvo.url, unica desta thread — sem lock (ver __init__)

        self._expandir(soup, alvo)
        with self._stats_lock:
            self.stats.curva.append((self.stats.paginas_baixadas, self.stats.documentos_encontrados))

        if md.pdf_url:
            score = self.scorer.score(md.pdf_url, anchor=md.title, depth=alvo.depth, is_document=True)
            if self.frontier.add(
                md.pdf_url,
                depth=alvo.depth,
                priority=score.priority + 5.0,  # declaracao explicita da propria pagina
                kind=KIND_DOCUMENT,
                anchor=md.title or "(citation_pdf_url)",
                parent=alvo.url,
            ):
                self._registrar_documento(md.pdf_url, md.title, alvo.url, md)

    def _expandir(self, soup, alvo) -> None:
        links = extract_links(soup, alvo.url, self.spec.document_extensions)
        with self._stats_lock:
            self.stats.links_vistos += len(links)
        profundidade = alvo.depth + 1
        if profundidade > self.spec.scope.max_depth:
            return

        # Chave = alvo.url, unica desta thread nesta rodada — sem lock.
        self._docs_por_pagina[alvo.url] = sum(
            1 for l in links if l.is_document or self.spec.is_document_url(l.url)
        )

        for link in links:
            if not self._em_escopo(link.url):
                with self._stats_lock:
                    self.stats.fora_de_escopo += 1
                continue
            motivo = is_trap(link.url)
            if motivo:
                with self._stats_lock:
                    self.stats.armadilhas += 1
                continue

            # `extract_links` decide por extensao; o spec pode reconhecer
            # tambem por padrao de URL (Liferay e DTIC servem documento sem
            # extensao no endereco).
            e_doc = link.is_document or self.spec.is_document_url(link.url)

            score = self.scorer.score(
                link.url,
                anchor=link.anchor,
                context=link.context,
                depth=profundidade,
                is_document=e_doc,
            )
            novo = self.frontier.add(
                link.url,
                depth=profundidade,
                priority=score.priority,
                kind=KIND_DOCUMENT if e_doc else KIND_PAGE,
                anchor=link.anchor,
                parent=alvo.url,
            )
            if novo and e_doc:
                self._registrar_documento(link.url, link.anchor, alvo.url, None, score.matched)

    def _registrar_documento(
        self,
        url: str,
        titulo: str | None,
        pagina: str,
        md: PageMetadata | None,
        matched: list[str] | None = None,
    ) -> None:
        with self._stats_lock:
            self.stats.documentos_encontrados += 1

    # -------------------------------------------------------------- documentos

    def _emitir_documentos(self) -> Iterator[DocumentRecord]:
        """Converte os documentos descobertos em registros.

        A landing page (`parent`) e visitada apenas se ainda houver orcamento e
        se ela nao tiver sido baixada: e dela que saem autores, data e resumo.
        """
        for doc in self.frontier.documents_found():
            md = self._page_meta.get(doc.parent) if doc.parent else None
            if md is None and doc.parent and self.stats.paginas_baixadas < self.spec.scope.max_pages:
                md = self._metadados_da_landing(doc.parent)
                if md is not None:
                    self._page_meta[doc.parent] = md

            # O metadado da pagina so descreve ESTE documento quando a pagina e
            # de fato a landing page dele: ou porque declarou `citation_pdf_url`
            # apontando para ca, ou porque continha um unico documento. Numa
            # pagina de indice com dezenas de PDFs, o <title> descreve o indice
            # — usa-lo daria a todos os arquivos o mesmo titulo errado.
            e_landing = bool(md) and (
                (md.pdf_url and canonicalize(md.pdf_url) == canonicalize(doc.url))
                or self._docs_por_pagina.get(doc.parent or "", 99) == 1
            )
            md_doc = md if e_landing else None

            titulo = _melhor_titulo(md_doc.title if md_doc else None, doc.anchor, doc.url)

            # A relevancia so pode ser julgada AQUI, com o metadado resolvido.
            # Julga-la na descoberta subestimaria tudo: em muitos repositorios o
            # arquivo se chama "dot_78914_DS1.pdf" e nao carrega sinal nenhum —
            # o titulo esta na landing page. E o mesmo criterio do pre-filtro
            # (strong ou weak), aplicado igual nas duas estrategias.
            if self.lexicon.score_text(titulo, md_doc.abstract if md_doc else None).tier != TIER_NEGATIVE:
                self.stats.documentos_relevantes += 1
            yield DocumentRecord(
                source=self.spec.name,
                source_id=_source_id(doc.url),
                title=titulo,
                abstract=md_doc.abstract if md_doc else None,
                authors=md_doc.authors if md_doc else [],
                organization=md_doc.publisher if md_doc else None,
                pub_date=md_doc.date if md_doc else None,
                doc_type=None,
                subject_terms=md_doc.subjects if md_doc else [],
                candidate_urls=[doc.url],
                landing_url=doc.parent or doc.url,
                rights=None,
                export_control=False,
                raw_metadata={
                    "crawl": {
                        "depth": doc.depth,
                        "priority": round(doc.priority, 3),
                        "anchor": doc.anchor,
                        "parent": doc.parent,
                        "strategy": self.spec.strategy,
                    },
                    "page_meta": (md_doc.raw if md_doc else {}),
                },
            )

    def _metadados_da_landing(self, url: str) -> PageMetadata | None:
        try:
            body, _ = self._buscar_pagina(url)
        except Exception:
            return None
        if body is None:
            return None
        self.stats.paginas_baixadas += 1
        return extract_metadata(parse_html(body), url)

    # ------------------------------------------------------------------ apoio

    def _buscar_pagina(self, url: str) -> tuple[bytes | None, bool]:
        """Baixa uma pagina. Usa Playwright so quando a fonte declara `render`.

        HTTP puro e o padrao: e ordens de grandeza mais barato. A renderizacao
        existe para as fontes cujo HTML servido nao contem os links (a
        documentacao do ESA EOF carrega os documentos por JavaScript).
        """
        if self.spec.render and self.renderer is not None:
            html = self.renderer.render(url)
            if html:
                return html.encode("utf-8"), True

        res = self.fetcher.fetch(url, accept="text/html,application/xhtml+xml")
        if not res.ok:
            return None, False
        if res.kind not in ("html", "xml", "text"):
            return None, False
        return res.body, False

    def _em_escopo(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return False
        if self.fetcher.config.is_blocked(parts.netloc):
            return False
        if not self.spec.scope.host_allowed(parts.netloc):
            return False
        return self.spec.scope.path_allowed(parts.path)


def _nome_do_arquivo(url: str) -> str:
    from urllib.parse import unquote

    nome = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or url
    # "Urban%20Air%20Mobility%20(UAM)%20Concept%20of%20Operations%202.0_0.pdf"
    # vira "Urban Air Mobility (UAM) Concept of Operations 2.0 0" — legivel e,
    # sobretudo, pontuavel pelo lexico.
    for ext in (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"):
        if nome.lower().endswith(ext):
            nome = nome[: -len(ext)]
            break
    return re.sub(r"[_\-]+", " ", nome).strip() or url


#: Ancoras que nao dizem nada sobre o documento. Preferir o nome do arquivo a
#: qualquer uma delas — "Download PDF" nunca identificou documento nenhum.
ANCORAS_GENERICAS = frozenset(
    {
        "pdf", "download", "download pdf", "here", "click here", "link", "view",
        "read more", "more", "document", "file", "open", "acessar", "baixar",
        "full text", "fulltext", "get pdf", "view pdf", "attachment", "anexo",
    }
)


def _melhor_titulo(md_title: str | None, anchor: str | None, url: str) -> str:
    """Escolhe o rotulo mais informativo disponivel.

    Ordem: metadado da landing page > ancora informativa > nome do arquivo.

    A ancora vem antes do nome do arquivo porque costuma ser texto humano, mas
    so quando de fato informa: "Download PDF" perde para
    "UAM Concept of Operations 2.0". Este titulo nao e so rotulo — e o campo
    sobre o qual o pre-filtro decide a faixa do documento, entao escolher mal
    aqui manda um ConOps para a classe negativa.
    """
    if md_title and md_title.strip():
        return md_title.strip()
    nome = _nome_do_arquivo(url)
    if anchor:
        limpa = " ".join(anchor.split())
        # Ancora que e a propria URL nao e titulo — acontece quando a pagina
        # exibe o endereco como texto do link. O nome do arquivo, ao menos, ja
        # vem sem esquema, host e caminho.
        e_url = limpa.lower().startswith(("http://", "https://", "www."))
        if (
            not e_url
            and limpa.lower().strip(" .:-") not in ANCORAS_GENERICAS
            and len(limpa) > 3
        ):
            return limpa
    return nome


def _source_id(url: str) -> str:
    """Identidade estavel do candidato dentro da fonte.

    E a URL canonica sem parametros de ruido. A identidade do CONTEUDO continua
    sendo o SHA-256 do arquivo — no Liferay a URL muda a cada reedicao.
    """
    return canonicalize(url)
