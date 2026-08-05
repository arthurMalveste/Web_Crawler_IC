"""Orquestracao: descoberta -> pre-filtro -> fila -> fetch -> armazenamento.

Duas fases separadas de proposito. `discover` e barata (so metadados) e pode ser
reexecutada a vontade quando o lexico mudar; `harvest` e cara (banda, disco) e
consome o que a fila ja aprovou. Medir recall contra o gold set exige apenas a
primeira fase — e por isso que expandir o lexico e reexecutar custa pouco.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

import structlog

from ..adapters.base import BaseAdapter
from .fetcher import BlockedByPolicy, Fetcher, NotADocument
from .frontier import (
    STATUS_DISCOVERED,
    STATUS_DUPLICATE,
    STATUS_EXPORT_CONTROL,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_SKIPPED,
    STATUS_STORED,
    STATUS_UNCHANGED,
    Frontier,
)
from .prefilter import Lexicon, NegativeSampler
from .record import (
    TIER_HARD_NEGATIVE,
    TIER_NEGATIVE,
    TIER_STRONG,
    TIER_WEAK,
    DocumentRecord,
    utcnow_iso,
)
from .store import Store, count_pdf_pages

log = structlog.get_logger(__name__)


@dataclass
class DiscoveryStats:
    vistos: int = 0
    novos: int = 0
    ja_conhecidos: int = 0
    export_control: int = 0
    por_faixa: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "vistos": self.vistos,
            "novos": self.novos,
            "ja_conhecidos": self.ja_conhecidos,
            "descartados_export_control": self.export_control,
            "por_faixa": self.por_faixa,
        }


@dataclass
class HarvestStats:
    tentados: int = 0
    armazenados: int = 0
    duplicados: int = 0
    inalterados: int = 0
    rejeitados: int = 0
    falhos: int = 0
    bytes_baixados: int = 0
    texto_ja_extraido: int = 0  # NTRS entrega .txt -> Etapa 2 fica mais barata

    def as_dict(self) -> dict[str, Any]:
        return {
            "tentados": self.tentados,
            "armazenados": self.armazenados,
            "duplicados_por_conteudo": self.duplicados,
            "inalterados_304": self.inalterados,
            "rejeitados_por_politica": self.rejeitados,
            "falhos": self.falhos,
            "volume_baixado_gb": round(self.bytes_baixados / 1e9, 4),
            "com_texto_ja_extraido": self.texto_ja_extraido,
        }


class Pipeline:
    def __init__(
        self,
        frontier: Frontier,
        store: Store,
        fetcher: Fetcher,
        lexicon: Lexicon,
        *,
        collect_negatives: bool = True,
    ):
        self.frontier = frontier
        self.store = store
        self.fetcher = fetcher
        self.lexicon = lexicon
        self.sampler = NegativeSampler(lexicon.neg_cap, lexicon.neg_seed)
        self.collect_negatives = collect_negatives

    # ------------------------------------------------------------- descoberta

    def discover(self, adapter: BaseAdapter, limit: int | None = None) -> DiscoveryStats:
        run_id = f"{adapter.name}-discover-{uuid.uuid4().hex[:8]}"
        self.frontier.start_run(run_id, adapter.name, {"limit": limit, "fase": "discover"})
        st = DiscoveryStats()
        # Fontes de negativos dificeis (PSAS) sao coletadas por inteiro: elas
        # existem exatamente para isso, e amostrar 1 em 8 descartaria o material
        # mais informativo que o projeto tem para validar a Etapa 5.
        tudo_negativo = getattr(adapter, "collect_all_negatives", False)

        for rec in adapter.discover():
            st.vistos += 1

            # Export control antes de qualquer outra coisa: o registro nem entra
            # na fila. O NTRS expoe ITAR/EAR diretamente no metadado.
            if rec.export_control:
                st.export_control += 1
                self.frontier.add(rec, status=STATUS_EXPORT_CONTROL)
                self.store.append_reject(
                    rec, "export_control", detalhe=rec.export_control_reason
                )
                continue

            self.lexicon.score_record(rec)

            if rec.tier == TIER_NEGATIVE:
                if tudo_negativo:
                    rec.tier = TIER_HARD_NEGATIVE
                elif not (self.collect_negatives and self.sampler.accept(rec)):
                    # Amostrar a classe negativa e obrigatorio: sem ela nao ha
                    # precisao/recall/F1 para avaliar a Etapa 5.
                    st.por_faixa["descartado"] = st.por_faixa.get("descartado", 0) + 1
                    continue

            st.por_faixa[rec.tier] = st.por_faixa.get(rec.tier, 0) + 1
            if self.frontier.add(rec, status=STATUS_DISCOVERED):
                st.novos += 1
            else:
                st.ja_conhecidos += 1

            if limit and st.novos >= limit:
                log.info("discover.limite_atingido", adapter=adapter.name, limite=limit)
                break

        self.frontier.finish_run(run_id, st.as_dict())
        log.info("discover.concluido", adapter=adapter.name, **st.as_dict())
        return st

    # ---------------------------------------------------------------- coleta

    def harvest(
        self,
        limit: int | None = None,
        tiers: list[str] | None = None,
        *,
        max_workers: int | None = None,
    ) -> HarvestStats:
        """Baixa o que a fila aprovou — em paralelo, um grupo de threads por
        dominio dentro do teto que `config/domains.yaml` ja declarava.

        Nenhum agendamento manual de dominio acontece aqui: basta submeter
        TODOS os pendentes de uma vez a um pool de threads. Quem garante que
        um dominio nunca ultrapassa seu `rate`/`concurrency` e' o
        `Fetcher` (um semaforo e um lock por host — ver `fetcher.py`); duas
        threads de dominios DIFERENTES simplesmente nunca disputam o mesmo
        semaforo e progridem de verdade em paralelo. E' esse desenho —
        limitar por CHAVE em vez de um limite global — que transforma uma
        coleta de varias fontes de horas em minutos sem arriscar sobrecarregar
        nenhum servidor individual.

        `max_workers` por padrao e' a soma da concorrencia configurada dos
        dominios presentes no lote: threads a mais nao aceleram nada (ficariam
        so esperando a vez no semaforo do proprio host), entao nao ha razao
        para abrir mais que isso.
        """
        run_id = f"harvest-{uuid.uuid4().hex[:8]}"
        self.frontier.start_run(run_id, "harvest", {"limit": limit, "tiers": tiers})
        st = HarvestStats()
        st_lock = threading.Lock()

        pendentes = list(self.frontier.pending(limit=limit, tiers=tiers))
        st.tentados = len(pendentes)

        n_workers = max_workers or self._worker_count(pendentes)
        log.info("harvest.iniciado", pendentes=len(pendentes), workers=n_workers)

        concluidos = 0
        with ThreadPoolExecutor(max_workers=n_workers, thread_name_prefix="harvest") as pool:
            futuros = [pool.submit(self._harvest_one, rec, st, st_lock) for rec in pendentes]
            for fut in as_completed(futuros):
                fut.result()  # relanca excecao de alguma thread, se houve
                concluidos += 1
                if concluidos % 25 == 0 or concluidos == len(pendentes):
                    log.info("harvest.progresso", concluidos=concluidos, total=len(pendentes))

        self.frontier.finish_run(run_id, st.as_dict())
        log.info("harvest.concluido", **st.as_dict())
        return st

    def _worker_count(self, records: list[DocumentRecord]) -> int:
        """Soma da concorrencia configurada por dominio presente no lote.

        So a URL preferida de cada registro conta para o dimensionamento —
        e' so uma estimativa de QUANTAS threads vale abrir; a correcao de
        quantas rodam de fato por host e' sempre do semaforo no `Fetcher`,
        nao deste numero. Piso de 1 (lote vazio ainda precisa de um pool
        valido) e teto de 32 (nao ha necessidade real de mais do que isso
        mesmo com dezenas de dominios configurados).
        """
        hosts = {
            urlsplit(rec.candidate_urls[0]).netloc.lower()
            for rec in records
            if rec.candidate_urls
        }
        if not hosts:
            return 1
        total = sum(self.fetcher.policy_for(h).concurrency for h in hosts)
        return max(1, min(total, 32))

    def _harvest_one(
        self, rec: DocumentRecord, st: HarvestStats, st_lock: threading.Lock | None = None
    ) -> None:
        """Processa UM documento — chamado de dentro de uma worker thread.

        `frontier` e `store` ja sao seguros entre threads (locks proprios,
        ver `frontier.py`/`store.py`); o unico estado compartilhado que resta
        aqui e' o objeto `st` (contadores agregados de TODAS as threads), por
        isso as mutacoes nele — e so elas — ficam atras de `st_lock`.
        """
        st_lock = st_lock or threading.Lock()

        if not rec.candidate_urls:
            self.frontier.mark(rec.key, STATUS_SKIPPED, error="sem URL de download")
            self.store.append_reject(rec, "sem_url")
            with st_lock:
                st.rejeitados += 1
            return

        etag, last_mod = self.frontier.conditional_headers(rec.key)
        last_error: str | None = None

        # candidate_urls vem em ordem de preferencia. No NTRS o .txt vem antes
        # do .pdf: quando existe, a Etapa 2 nao precisa rodar extracao nenhuma.
        for url in rec.candidate_urls:
            is_text = url.lower().endswith(".txt")
            try:
                # Num endpoint de fulltext o corpo e texto extraido por
                # definicao, e o NTRS entrega tanto texto puro quanto XHTML do
                # Apache Tika. Exigir "documento" ali rejeitaria conteudo bom;
                # a verificacao de formato so faz sentido para o arquivo em si.
                res = self.fetcher.fetch(
                    url, etag=etag, last_modified=last_mod, expect_document=not is_text
                )
            except BlockedByPolicy as exc:
                last_error = f"politica: {exc}"
                self.store.append_reject(rec, "politica", url=url, detalhe=str(exc))
                continue
            except NotADocument as exc:
                last_error = f"nao e documento: {exc}"
                self.store.append_reject(rec, "nao_documento", url=url, detalhe=str(exc))
                continue

            if res.from_cache:
                self.frontier.mark(rec.key, STATUS_UNCHANGED, fetched_at=utcnow_iso())
                with st_lock:
                    st.inalterados += 1
                return

            if not res.ok:
                last_error = f"HTTP {res.status}"
                continue

            if is_text and res.kind == "unknown":
                # Binario ilegivel num endpoint de texto: nao e fulltext.
                last_error = "fulltext ilegivel"
                self.store.append_reject(rec, "fulltext_ilegivel", url=url)
                continue

            # O `fulltext` do NTRS as vezes vem embrulhado em XHTML do Apache
            # Tika. E texto extraido legitimo, mas a Etapa 2 precisa saber que
            # ha um invólucro a remover antes de usar.
            wrapper = res.kind if (is_text and res.kind in ("html", "xml")) else None

            sha, path, novo_em_disco = self.store.put(res.body, res.kind, text=is_text)

            n_pages = count_pdf_pages(path) if res.kind == "pdf" else None
            # Caminho RELATIVO a raiz do corpus: torna o acervo portatil.
            rel = self.store.relative(path)
            conteudo_novo = self.frontier.register_content(
                sha, rel, res.kind, len(res.body), rec.key, n_pages
            )

            status = STATUS_STORED if conteudo_novo else STATUS_DUPLICATE
            with st_lock:
                st.bytes_baixados += len(res.body)
                if is_text:
                    st.texto_ja_extraido += 1
                if conteudo_novo:
                    st.armazenados += 1
                else:
                    st.duplicados += 1

            self.frontier.mark(
                rec.key,
                status,
                sha256=sha,
                stored_path=rel,
                content_kind=res.kind,
                http_status=res.status,
                etag=res.etag,
                last_modified=res.last_modified,
                fetched_at=utcnow_iso(),
                error=None,
            )
            self.store.append_manifest(
                rec,
                sha256=sha,
                stored_path=rel,
                content_kind=res.kind,
                size_bytes=len(res.body),
                n_pages=n_pages,
                fetched_url=url,
                duplicate_of_existing_content=not conteudo_novo,
                text_already_extracted=is_text,
                text_wrapper=wrapper,  # "html" (Tika XHTML) | "xml" | None
            )
            return

        self.frontier.mark(rec.key, STATUS_FAILED, error=last_error, fetched_at=utcnow_iso())
        self.store.append_reject(rec, "falha_download", detalhe=last_error)
        with st_lock:
            st.falhos += 1
