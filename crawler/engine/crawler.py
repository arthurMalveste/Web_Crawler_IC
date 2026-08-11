"""Motor de rastreamento — o caso geral de coleta.

Fluxo por fonte:

    sementes + sitemap  ->  fronteira de URLs (ordenada)
              |
              v
    visitar pagina -> extrair links (ancora + contexto)
              |             |
              |             +-> pontuar cada link -> enfileirar
              |             +-> documento com metadado disponivel? emite JA
              v
    extrair metadados do <head>  ->  DocumentRecord

Os dois modos de expansao compartilham todo o codigo; a unica diferenca e a
prioridade atribuida pelo LinkScorer. Isso e proposital: comparar rastreamento
focado com BFS so tem valor se as duas execucoes forem identicas em tudo o mais.

EMISSAO INCREMENTAL (decisao de 2026-08-11): documentos sao convertidos em
`DocumentRecord` e entregues ao chamador ASSIM QUE descobertos, nao so no fim
do rastreamento inteiro. Antes disso, `--limit`/`Scope.max_documents` nao
paravam NADA de verdade para fontes de crawl HTML — o gerador so' entregava
alguma coisa depois que TODO o orcamento de paginas fosse consumido, entao
qualquer teto de "quantos documentos eu preciso" so' cortava o que entrava na
fila DEPOIS do rastreamento inteiro ja ter rodado. Medido ao vivo: um
`discover-all --limit 10` na FAA rodou 400 paginas (o `max_pages` da fonte)
sem entregar nada por mais de 10 minutos. Ver `Crawler._emitir_agora`.
"""

from __future__ import annotations

import queue
import re
import threading
import time
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
    STATE_EMITTED,
    STATE_FAILED,
    STATE_SKIPPED,
    STATE_VISITED,
    CrawlURL,
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
    """Motor de rastreamento — visita paginas em FILA CONTINUA, nao rodadas.

    A fronteira de paginas cresce durante o proprio rastreamento (ao contrario
    do harvest, que parte de uma lista fechada), entao nao da pra materializar
    tudo de uma vez no inicio. Ate 2026-08-11 isso virava lotes sincronizados
    (`next_batch(n)`, todo o lote esperava o membro mais lento terminar antes
    do proximo comecar) — um worker preso numa URL lenta (retry de rede,
    servidor devagar) deixava os outros OCIOSOS ate a rodada inteira fechar,
    mesmo com fronteira cheia de trabalho pronto. Substituido por fila
    continua: cada worker reivindica-processa-reivindica de novo em loop
    proprio, sem esperar os colegas. `URLFrontier.claim_next()` faz o
    SELECT+UPDATE atomico que evita duas threads pegando a mesma URL — o
    mesmo problema que o desenho por rodadas evitava, resolvido na fronteira
    em vez de no agendador. `Fetcher` ja aplica o teto de `rate`/`concurrency`
    por HOST (ver `core/fetcher.py`), entao visitar paginas em paralelo herda
    o mesmo respeito a cada dominio que os documentos do harvest ja tem.

    Termino e' por CONTAGEM DE TRABALHO EM VOO (`_em_voo`), nao por "fila
    vazia": um worker so' desiste de verdade quando reivindicar nao rende nada
    E ninguem mais esta processando uma pagina que poderia descobrir mais
    (`_em_voo == 0`) — senao workers ociosos por um instante encerrariam cedo
    demais, perdendo paginas que um colega ainda ia descobrir.
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
        # Protege os CONTADORES abaixo (`+=` nao e' atomico) e as decisoes de
        # agendamento (reivindicar mais trabalho, ou desistir) — sao a MESMA
        # secao critica: decidir "ainda ha orcamento?" e reivindicar tem que
        # ser atomico junto, senao duas threads podem ler o mesmo orcamento
        # restante e as duas reivindicarem, estourando o teto.
        self._stats_lock = threading.Lock()
        self._reservadas = 0  # paginas reivindicadas nesta execucao (teto: max_pages)
        self._emitidos = 0  # documentos emitidos nesta execucao (teto: max_documents)
        self._em_voo = 0  # workers dentro de `_visitar()` agora mesmo
        # Quantos documentos cada pagina continha. Distingue LANDING PAGE (um
        # documento, e o metadado da pagina descreve esse documento) de PAGINA
        # DE LISTAGEM (varios documentos, e o metadado descreve a listagem, nao
        # cada arquivo). Sem essa distincao, os 40 PDFs de uma pagina de indice
        # herdariam todos o mesmo titulo. Escrita e leitura sempre pela MESMA
        # thread (a que visitou `alvo.url`) antes de qualquer outra thread
        # precisar da chave — seguro sem lock (ver `_expandir`).
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

    def _orcamento_esgotado(self) -> bool:
        """Chamado so' sob `self._stats_lock` — ve' `crawl()`/`_worker`."""
        if self._reservadas >= self.spec.scope.max_pages:
            return True
        teto_docs = self.spec.scope.max_documents
        return teto_docs is not None and self._emitidos >= teto_docs

    def crawl(self) -> Iterator[DocumentRecord]:
        inicio = time.monotonic()
        # Orfas de uma execucao anterior que caiu entre reivindicar e
        # resolver uma URL — sem isso, ficariam invisiveis para sempre.
        reabertas = self.frontier.reabrir_reivindicadas()
        if reabertas:
            log.info("crawl.reivindicacoes_orfas_reabertas", fonte=self.spec.name, n=reabertas)

        self._achados: queue.SimpleQueue[DocumentRecord] = queue.SimpleQueue()
        self._concluido = threading.Event()
        parar = threading.Event()

        self._semear()

        n_workers = self._max_workers_override or self._worker_count()

        def _worker() -> None:
            while not parar.is_set():
                with self._stats_lock:
                    alvo = None if self._orcamento_esgotado() else self.frontier.claim_next(kind=KIND_PAGE)
                    if alvo is not None:
                        self._reservadas += 1
                        self._em_voo += 1
                if alvo is not None:
                    try:
                        self._visitar(alvo)
                    finally:
                        with self._stats_lock:
                            self._em_voo -= 1
                    continue
                # Nada pendente AGORA — mas outro worker pode estar dentro de
                # `_visitar()` prestes a descobrir mais paginas. So' desiste
                # de verdade quando ninguem mais estiver em voo; senao espera
                # um instante e tenta reivindicar de novo.
                with self._stats_lock:
                    ninguem_em_voo = self._em_voo == 0
                if ninguem_em_voo:
                    self._concluido.set()
                    return
                if self._concluido.wait(timeout=0.05):
                    return

        threads = [
            threading.Thread(target=_worker, name=f"{self.spec.name}-discover-{i}", daemon=True)
            for i in range(n_workers)
        ]
        for t in threads:
            t.start()

        try:
            while not self._concluido.is_set() or not self._achados.empty():
                try:
                    yield self._achados.get(timeout=0.1)
                except queue.Empty:
                    continue
        finally:
            # `parar` ANTES de esperar as threads: garante que nenhuma
            # reivindique mais nada a partir daqui, entao o join abaixo espera
            # no maximo a URL que cada worker ja tinha em maos, nao o resto do
            # orcamento — e' o que faz `--limit`/`max_documents` de fato
            # cortarem o tempo de rastreamento, nao so' o resultado.
            parar.set()
            for t in threads:
                t.join(timeout=30)

        # Residual: documentos cuja pagina-mae foi visitada numa execucao
        # ANTERIOR (retomada) — a emissao normal ja aconteceu na hora, dentro
        # de `_visitar`/`_expandir`. Ver `_emitir_pendentes`.
        yield from self._emitir_pendentes()

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
            if e_doc:
                self._emitir_agora(CrawlURL(url=url, depth=0, priority=1000.0, kind=KIND_DOCUMENT), md=None)

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
                doc = CrawlURL(url=url, depth=1, priority=score.priority, kind=KIND_DOCUMENT if e_doc else KIND_PAGE)
                if self.frontier.add(
                    url,
                    depth=1,
                    priority=score.priority,
                    kind=doc.kind,
                    anchor=None,  # sitemap nao tem ancora; ver nota em _semear
                ):
                    self.stats.urls_do_sitemap += 1
                    # Sitemap nao tem pagina-mae — nao ha metadado a esperar,
                    # entao emite na hora (sem isso, um "kind=bulk"-like que so
                    # usa sitemap nunca teria --limit/max_documents efetivo).
                    if e_doc:
                        self._emitir_agora(doc, md=None)
            if self.stats.urls_do_sitemap:
                # Um sitemap util dispensa procurar os outros.
                break

        if self.stats.urls_do_sitemap:
            log.info("crawl.sitemap", fonte=self.spec.name, urls=self.stats.urls_do_sitemap)

    def _visitar(self, alvo: CrawlURL) -> None:
        if not self._budget.allow(alvo.url):
            self.frontier.mark(alvo.url, STATE_SKIPPED, error="orcamento do host esgotado")
            return

        # Tudo desde aqui — fetch, parse, expansao — numa unica rede de
        # seguranca: uma pagina malformada (HTML que quebra o parser, por
        # exemplo) nao pode derrubar o rastreamento inteiro. Antes so' o fetch
        # tinha essa protecao; um bug de parsing numa unica pagina, entre
        # centenas, propagava ate o chamador e perdia tudo que ainda nao
        # tinha sido emitido.
        try:
            body, renderizado = self._buscar_pagina(alvo.url)
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

            # A pagina pode ser, ela propria, uma landing page de documento:
            # se o <head> declara citation_pdf_url, o PDF esta identificado
            # sem adivinhar. `md` e' local desta chamada — nenhuma outra
            # thread visita `alvo.url`, entao passar isso direto (em vez de
            # guardar num dict compartilhado) e' seguro e mais simples.
            md = extract_metadata(soup, alvo.url)

            self._expandir(soup, alvo, md)
            with self._stats_lock:
                self.stats.curva.append((self.stats.paginas_baixadas, self.stats.documentos_encontrados))

            if md.pdf_url:
                score = self.scorer.score(md.pdf_url, anchor=md.title, depth=alvo.depth, is_document=True)
                doc = CrawlURL(
                    url=md.pdf_url, depth=alvo.depth, priority=score.priority + 5.0,
                    kind=KIND_DOCUMENT, anchor=md.title or "(citation_pdf_url)", parent=alvo.url,
                )
                if self.frontier.add(
                    doc.url, depth=doc.depth, priority=doc.priority, kind=KIND_DOCUMENT,
                    anchor=doc.anchor, parent=alvo.url,
                ):
                    self._emitir_agora(doc, md)
        except BlockedByPolicy as exc:
            self.frontier.mark(alvo.url, STATE_SKIPPED, error=str(exc))
        except Exception as exc:
            with self._stats_lock:
                self.stats.erros += 1
            self.frontier.mark(alvo.url, STATE_FAILED, error=str(exc))
            log.warning("crawl.erro_pagina", url=alvo.url, error=str(exc))

    def _expandir(self, soup, alvo: CrawlURL, md: PageMetadata) -> None:
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
            doc = CrawlURL(
                url=link.url, depth=profundidade, priority=score.priority,
                kind=KIND_DOCUMENT if e_doc else KIND_PAGE, anchor=link.anchor, parent=alvo.url,
            )
            novo = self.frontier.add(
                doc.url, depth=doc.depth, priority=doc.priority, kind=doc.kind,
                anchor=doc.anchor, parent=alvo.url,
            )
            if novo and e_doc:
                # A pagina-mae (`alvo`) acabou de ser visitada por ESTA
                # thread — o metadado dela (`md`) ja esta em maos, entao o
                # documento e' emitido JA, sem esperar o rastreamento
                # terminar (ver docstring do modulo).
                self._emitir_agora(doc, md)

    # -------------------------------------------------------------- documentos

    def _montar_registro(self, doc: CrawlURL, md: PageMetadata | None) -> DocumentRecord:
        """Resolve titulo/metadado e monta o `DocumentRecord`. Usado tanto
        pela emissao imediata (`_emitir_agora`, o caminho comum) quanto pelo
        residual de retomada (`_emitir_pendentes`) — o MESMO criterio nos
        dois, so' muda de onde `md` vem."""
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
        relevante = self.lexicon.score_text(titulo, md_doc.abstract if md_doc else None).tier != TIER_NEGATIVE
        with self._stats_lock:
            if relevante:
                self.stats.documentos_relevantes += 1
            self._emitidos += 1

        return DocumentRecord(
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

    def _emitir_agora(self, doc: CrawlURL, md: PageMetadata | None) -> None:
        """Converte um documento recem-descoberto em `DocumentRecord` e
        entrega na hora — chamado de dentro de uma thread worker (`_semear`
        roda antes delas existirem; `_visitar`/`_expandir` rodam dentro).
        `self._achados` e' uma `queue.SimpleQueue`, segura para varios
        produtores e um consumidor sem lock extra."""
        with self._stats_lock:
            self.stats.documentos_encontrados += 1
        registro = self._montar_registro(doc, md)
        self.frontier.mark(doc.url, STATE_EMITTED)
        self._achados.put(registro)

    def _emitir_pendentes(self) -> Iterator[DocumentRecord]:
        """Residual: documentos cuja pagina-mae foi visitada numa execucao
        ANTERIOR (processo diferente, sem retorno) — a unica forma de um
        documento ainda estar PENDING aqui, ja que a emissao normal acontece
        na hora (`_emitir_agora`). Roda de forma sequencial, DEPOIS que todas
        as threads worker ja terminaram — sem concorrencia, sem lock
        necessario para o fetch extra da landing page.
        """
        for doc in self.frontier.documentos_pendentes():
            md = None
            if doc.parent and self._reservadas < self.spec.scope.max_pages:
                md = self._metadados_da_landing(doc.parent)
            registro = self._montar_registro(doc, md)
            self.frontier.mark(doc.url, STATE_EMITTED)
            yield registro

    def _metadados_da_landing(self, url: str) -> PageMetadata | None:
        """Busca extra, so' usada por `_emitir_pendentes` (fase sequencial,
        sem outras threads rodando — por isso os incrementos abaixo dispensam
        `self._stats_lock`)."""
        try:
            body, _ = self._buscar_pagina(url)
        except Exception:
            return None
        if body is None:
            return None
        self.stats.paginas_baixadas += 1
        self._reservadas += 1
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
