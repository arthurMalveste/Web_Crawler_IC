"""GOV.UK (Ministry of Defence) — portal de publicacoes do governo britanico.

Achados reais, verificados ao vivo em 2026-08-14 contra `www.gov.uk` (ver
`docs/achados-api.md`):

  1. A URL de busca humana (`/search/all`) e' bloqueada por `robots.txt`
     (`Disallow: /search/all*`), mas o **Search API**
     (`www.gov.uk/api/search.json`) e o **Content API**
     (`www.gov.uk/api/content/<path>`) sao caminhos separados, SEM nenhuma
     restricao em `robots.txt` — mesmo padrao "UI bloqueada, API livre" ja
     visto noutras fontes. Sem chave, sem cadastro.
  2. O Search API NAO devolve os anexos (PDF) do documento — so titulo,
     descricao e o `link` (caminho relativo). O Content API e' quem tem
     `details.attachments[].url`. Por isso este adaptador faz DUAS chamadas
     por documento candidato: busca (barata, ~2s, ate' 1500 resultados por
     chamada) e conteudo (mais barata ainda, <1s).
  3. `start`/`count` paginam corretamente (paginas distintas, confirmado ao
     vivo) — sem o bug do `from` do NTRS nem a inflacao de `totalHits` do
     CORE.
  4. Nem todo resultado tem anexo: tipos como `speech`, `news_story`,
     `oral_statement` nao tem `details.attachments` (confirmado ao vivo,
     valor `None` ou `[]`) — descartado antes de emitir, mesma regra do CORE.
  5. Licenca e' sempre a mesma (Open Government Licence) — nao varia por
     documento, entao `rights` e' uma string fixa em vez de um campo lido da
     API.
"""

from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import urlencode

import structlog

from ..core.record import DocumentRecord
from .base import BaseAdapter

log = structlog.get_logger(__name__)

SEARCH = "https://www.gov.uk/api/search.json"
CONTENT = "https://www.gov.uk/api/content"

_LICENCA = "Crown copyright — Open Government Licence v3.0"


class GovUKAdapter(BaseAdapter):
    name = "govuk"

    def __init__(
        self,
        fetcher,
        *,
        terms: list[str],
        organisations: list[str] | None = None,
        page_size: int = 100,
        max_resultados_por_termo: int = 300,
        **options: Any,
    ):
        super().__init__(fetcher, **options)
        self.terms = terms
        self.organisations = organisations or ["ministry-of-defence"]
        self.page_size = page_size
        self.max_resultados_por_termo = max_resultados_por_termo
        # Dedup ENTRE termos, pelo `link` (disponivel na propria busca, antes
        # da chamada de conteudo): o mesmo documento pode bater buscas por
        # frases diferentes do lexico (ex. "operational concept" e
        # "operating concept" acertando o mesmo "Air Operating Concept").
        # Sem isso, cada acerto repetido pagaria de novo a chamada de
        # conteudo — a busca e' barata, mas e' a unica forma de saber se ha
        # PDF, entao vale nao repeti-la.
        self._vistos: set[str] = set()

    def discover(self) -> Iterator[DocumentRecord]:
        for term in self.terms:
            yield from self._walk(term)

    def _search(self, term: str, start: int) -> dict[str, Any]:
        params = [("q", f'"{term}"'), ("start", start), ("count", self.page_size)]
        params += [("filter_organisations", org) for org in self.organisations]
        return self.fetcher.get_json(f"{SEARCH}?{urlencode(params)}")

    def _walk(self, term: str) -> Iterator[DocumentRecord]:
        start = 0
        colhidos = 0
        emitidos = 0
        while start < self.max_resultados_por_termo:
            data = self._search(term, start)
            hits = data.get("results") or []
            if not hits:
                break
            for hit in hits:
                link = hit.get("link")
                if not link or link in self._vistos:
                    continue
                self._vistos.add(link)
                rec = self._to_record(hit, self.fetcher)
                if rec is not None:
                    emitidos += 1
                    yield rec
            colhidos += len(hits)
            start += len(hits)
            if len(hits) < self.page_size:
                # Pagina incompleta: nao ha mais resultados para este termo.
                break
        log.info("govuk.termo_concluido", termo=term, vistos=colhidos, emitidos=emitidos)

    # ------------------------------------------------------------- conversao

    @staticmethod
    def _to_record(hit: dict[str, Any], fetcher) -> DocumentRecord | None:
        link = hit["link"]
        content = fetcher.get_json(f"{CONTENT}{link}")
        details = content.get("details") or {}
        urls = [
            a["url"]
            for a in (details.get("attachments") or [])
            if a.get("content_type") == "application/pdf" and a.get("url")
        ]
        if not urls:
            # Formatos como speech/news_story/oral_statement nao tem anexo —
            # nada para baixar, nao emite.
            return None

        orgs = [
            o.get("title", "")
            for o in (content.get("links", {}).get("organisations") or [])
            if o.get("title")
        ]

        return DocumentRecord(
            source="govuk",
            source_id=str(content.get("content_id") or link),
            title=content.get("title") or hit.get("title") or "",
            abstract=content.get("description") or hit.get("description"),
            authors=[],
            organization=", ".join(orgs) or None,
            pub_date=content.get("first_published_at"),
            doc_type=content.get("document_type"),
            subject_terms=[],
            candidate_urls=urls,
            landing_url=f"https://www.gov.uk{link}",
            rights=_LICENCA,
            export_control=False,
            raw_metadata={
                "govuk_content_id": content.get("content_id"),
                "base_path": link,
                "search_hit": hit,
            },
        )
