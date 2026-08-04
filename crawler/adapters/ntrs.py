"""NTRS / NASA STI — fonte ancora.

API REST publica, sem autenticacao, com termos de uso que explicitamente
incentivam a coleta ("we promote the ability for partners and peers to harvest
data").

O comportamento real da API foi verificado contra o servico em 2026-08-04 e
diverge do que a documentacao informal sugere. As tres divergencias que
importam, todas tratadas aqui:

  1. O parametro de paginacao e `page.from`, NAO `from`. `from` e silenciosamente
     ignorado — o servidor devolve a primeira pagina indefinidamente, o que
     produziria um laco infinito colhendo os mesmos registros.
  2. `sort.field` / `sort.order` sao ignorados. A ordem e sempre por relevancia.
     Nao ha ordenacao estavel para paginar; a robustez vem do dedupe por
     (source, source_id) no frontier.
  3. Parametros desconhecidos sao ignorados sem erro. Um HTTP 200 nao prova que
     o filtro foi aplicado — cada filtro usado aqui foi confirmado alterando o
     `stats.total`.

Limites medidos: `page.from + page.size <= 10000` (400 acima disso) e
500 requisicoes / 15 min, publicadas nos headers `X-RateLimit-*` (o fetcher le
esses headers e se auto-ajusta).
"""

from __future__ import annotations

from typing import Any, Iterator

import structlog

from ..core.record import DocumentRecord
from .base import BaseAdapter

log = structlog.get_logger(__name__)

BASE = "https://ntrs.nasa.gov"
API = f"{BASE}/api"

#: Teto rigido do indice: page.from + page.size nao pode passar disso.
HARD_CAP = 10_000

#: 100 e o maximo documentado; 2000 funciona na pratica. 500 e um meio-termo
#: que reduz requisicoes sem depender de comportamento nao documentado extremo.
PAGE_SIZE = 500


class NTRSAdapter(BaseAdapter):
    name = "ntrs"

    def __init__(
        self,
        fetcher,
        *,
        terms: list[str],
        year_start: int = 1960,
        year_end: int = 2026,
        page_size: int = PAGE_SIZE,
        public_only: bool = True,
        require_document: bool = True,
        **options: Any,
    ):
        super().__init__(fetcher, **options)
        self.terms = terms
        self.year_start = year_start
        self.year_end = year_end
        self.page_size = min(page_size, HARD_CAP)
        self.public_only = public_only
        self.require_document = require_document

    # ---------------------------------------------------------------- consulta

    def _params(
        self, term: str, y0: int, y1: int, page_from: int, page_size: int
    ) -> dict[str, Any]:
        p: dict[str, Any] = {
            "q": f'"{term}"',  # aspas fazem busca por frase: sem elas o total
            #                    de "concept of operations" salta de 696 p/ 3240
            "published.gte": f"{y0}-01-01",
            "published.lte": f"{y1}-12-31",
            "page.size": page_size,
            "page.from": page_from,
        }
        if self.public_only:
            p["distribution"] = "PUBLIC"
        if self.require_document:
            # Sem isto entram registros que sao apenas metadado, sem arquivo.
            p["disseminated"] = "DOCUMENT_AND_METADATA"
        return p

    def _search(
        self, term: str, y0: int, y1: int, page_from: int, page_size: int | None = None
    ) -> dict[str, Any]:
        from urllib.parse import urlencode

        params = self._params(term, y0, y1, page_from, page_size or self.page_size)
        return self.fetcher.get_json(f"{API}/citations/search?{urlencode(params)}")

    def _total(self, term: str, y0: int, y1: int) -> int:
        # page.size=1: so interessa o stats.total para decidir o particionamento.
        return int(self._search(term, y0, y1, 0, page_size=1).get("stats", {}).get("total", 0))

    # ------------------------------------------------------- particionamento

    def _partitions(self, term: str) -> Iterator[tuple[int, int]]:
        """Quebra o intervalo de anos ate cada particao caber no teto de 10k.

        Sem isto, qualquer consulta com mais de 10.000 resultados perde
        silenciosamente a cauda — e perda de recall que nao aparece em nenhum
        log de erro.
        """
        pending = [(self.year_start, self.year_end)]
        while pending:
            y0, y1 = pending.pop(0)
            total = self._total(term, y0, y1)
            if total < HARD_CAP or y0 >= y1:
                if total >= HARD_CAP:
                    # Um unico ano estourando o teto: colhe o que da e avisa.
                    # A saida e particionar tambem por `center`.
                    log.warning(
                        "ntrs.particao_saturada",
                        termo=term,
                        ano=y0,
                        total=total,
                        acao="recall incompleto nesta particao",
                    )
                if total:
                    yield (y0, y1)
                continue
            mid = (y0 + y1) // 2
            pending.append((y0, mid))
            pending.append((mid + 1, y1))

    # -------------------------------------------------------------- descoberta

    def discover(self) -> Iterator[DocumentRecord]:
        for term in self.terms:
            for y0, y1 in self._partitions(term):
                yield from self._walk(term, y0, y1)

    def _walk(self, term: str, y0: int, y1: int) -> Iterator[DocumentRecord]:
        page_from = 0
        colhidos = 0
        while page_from < HARD_CAP:
            # page.from + page.size <= 10000, senao o servidor devolve 400.
            size = min(self.page_size, HARD_CAP - page_from)
            if size <= 0:
                break
            data = self._search(term, y0, y1, page_from, page_size=size)

            hits = data.get("results") or []
            if not hits:
                break
            for hit in hits:
                yield self._to_record(hit)
            colhidos += len(hits)
            page_from += len(hits)

            total = int(data.get("stats", {}).get("total", 0))
            if page_from >= total:
                break

        log.info("ntrs.particao", termo=term, span=f"{y0}-{y1}", colhidos=colhidos)

    # ------------------------------------------------------------- conversao

    @staticmethod
    def _to_record(hit: dict[str, Any]) -> DocumentRecord:
        # Os links vem como caminhos relativos (/api/citations/...), nao URLs.
        urls: list[str] = []
        for dl in hit.get("downloads") or []:
            links = dl.get("links") or {}
            # .txt primeiro, deliberadamente: quando existe, a NASA ja entregou
            # o texto extraido e a Etapa 2 nao precisa rodar para este documento.
            for chave in ("fulltext", "pdf", "original"):
                href = links.get(chave)
                if href:
                    urls.append(href if href.startswith("http") else f"{BASE}{href}")

        ec = hit.get("exportControl") or {}
        itar = str(ec.get("itar", "")).upper() == "YES"
        ear = str(ec.get("ear", "")).upper() == "YES"
        flagged = str(ec.get("isExportControl", "")).upper() == "YES"

        authors = [
            (a.get("meta", {}).get("author", {}) or {}).get("name", "")
            for a in hit.get("authorAffiliations") or []
        ]
        center = hit.get("center") or {}

        # subjectCategories vem como lista de strings; keywords, tambem.
        subjects = [s for s in (hit.get("subjectCategories") or []) if isinstance(s, str)]
        subjects += [k for k in (hit.get("keywords") or []) if isinstance(k, str)]

        pub_date = None
        for pub in hit.get("publications") or []:
            pub_date = pub.get("publicationDate") or pub.get("issuePublicationDate")
            if pub_date:
                break
        pub_date = pub_date or hit.get("distributionDate") or hit.get("submittedDate")

        return DocumentRecord(
            source="ntrs",
            source_id=str(hit.get("id")),
            title=hit.get("title") or "",
            abstract=hit.get("abstract"),
            authors=[a for a in authors if a],
            organization=center.get("name"),
            pub_date=pub_date,
            doc_type=hit.get("stiType"),
            subject_terms=subjects,
            candidate_urls=urls,
            landing_url=f"{BASE}/citations/{hit.get('id')}",
            rights=hit.get("distribution"),
            export_control=itar or ear or flagged,
            export_control_reason=(
                f"itar={ec.get('itar')} ear={ec.get('ear')} flag={ec.get('isExportControl')}"
                if (itar or ear or flagged)
                else None
            ),
            raw_metadata=hit,
        )


class NTRSRedistributions:
    """Job de sincronizacao semanal — exigido pelos termos de uso do NTRS.

    Os termos obrigam a consultar as redistribuicoes periodicamente e a remover
    copias de documentos que foram retirados. Nao e opcional e nao e detalhe: e
    a diferenca entre um corpus em conformidade e um passivo juridico num
    projeto com parceria industrial.
    """

    def __init__(self, fetcher):
        self.fetcher = fetcher

    def since(self, iso_date: str) -> list[dict[str, Any]]:
        url = f"{API}/citations/redistributions?redistributedDate.gt={iso_date}"
        data = self.fetcher.get_json(url)
        if isinstance(data, dict):
            return data.get("results") or data.get("redistributions") or []
        return data or []
