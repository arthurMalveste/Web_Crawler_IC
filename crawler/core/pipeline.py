"""Orquestracao: descoberta -> pre-filtro -> fila -> fetch -> armazenamento.

Duas fases separadas de proposito. `discover` e barata (so metadados) e pode ser
reexecutada a vontade quando o lexico mudar; `harvest` e cara (banda, disco) e
consome o que a fila ja aprovou. Medir recall contra o gold set exige apenas a
primeira fase — e por isso que expandir o lexico e reexecutar custa pouco.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

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

    def harvest(self, limit: int | None = None, tiers: list[str] | None = None) -> HarvestStats:
        run_id = f"harvest-{uuid.uuid4().hex[:8]}"
        self.frontier.start_run(run_id, "harvest", {"limit": limit, "tiers": tiers})
        st = HarvestStats()

        for rec in list(self.frontier.pending(limit=limit, tiers=tiers)):
            st.tentados += 1
            self._harvest_one(rec, st)

        self.frontier.finish_run(run_id, st.as_dict())
        log.info("harvest.concluido", **st.as_dict())
        return st

    def _harvest_one(self, rec: DocumentRecord, st: HarvestStats) -> None:
        if not rec.candidate_urls:
            self.frontier.mark(rec.key, STATUS_SKIPPED, error="sem URL de download")
            self.store.append_reject(rec, "sem_url")
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
            st.bytes_baixados += len(res.body)

            n_pages = count_pdf_pages(path) if res.kind == "pdf" else None
            conteudo_novo = self.frontier.register_content(
                sha, str(path), res.kind, len(res.body), rec.key, n_pages
            )

            if is_text:
                st.texto_ja_extraido += 1

            status = STATUS_STORED if conteudo_novo else STATUS_DUPLICATE
            if conteudo_novo:
                st.armazenados += 1
            else:
                st.duplicados += 1

            self.frontier.mark(
                rec.key,
                status,
                sha256=sha,
                stored_path=str(path),
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
                stored_path=str(path),
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
        st.falhos += 1
