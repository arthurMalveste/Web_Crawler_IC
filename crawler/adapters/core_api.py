"""CORE (core.ac.uk) — agregador de repositorios institucionais de acesso
aberto no mundo inteiro.

Diferente das demais fontes: nao e' um repositorio primario (como NTRS,
ROSA-P) nem um portal de projetos financiados (CORDIS) — e' um AGREGADOR que
reindexa milhoes de repositorios universitarios. Isso acrescenta uma camada
academica (teses, artigos revisados por pares) que nenhuma fonte atual cobre,
e foi confirmado ao vivo trazendo ConOps reais de instituicoes novas (MIT,
Univ. of North Texas) — ver `docs/achados-api.md`.

API REST v3, com chave (`Authorization: Bearer`, exigida — ver `.env.example`
e `core/env.py`). Comportamento real verificado em 2026-08-12 contra o
servico, e diverge da documentacao publicada em pontos que importam:

  1. SEM chave, o campo `fullText` vem como a string literal
     "Not available for public API users." em vez do texto de verdade ou de
     ausente — tratado aqui como ausencia (`_texto_valido`).
  2. `totalHits` NAO e confiavel para decidir particionamento (ao contrario
     do `stats.total` do NTRS, que foi validado observando mudanca real no
     numero): uma consulta MESMO sem escopo de campo devolveu totalHits na
     casa dos milhoes/dezenas de milhoes, nitidamente contando ocorrencias em
     texto completo, nao registros que batem a frase. Os RESULTADOS
     retornados, porem, sao relevantes e bem ordenados. Consequencia
     pratica: pagina-se por `offset`/`limit` ate um teto configuravel
     (`max_resultados_por_termo`), NUNCA ate esgotar `totalHits` — ao
     contrario do NTRS, que particiona ate cobrir o total real.
  3. `offset`/`limit` paginam corretamente (offset=0 e offset=5 devolvem
     paginas distintas, confirmado ao vivo) — CORE nao tem o bug do NTRS com
     o parametro `from` sendo ignorado.
  4. Nem todo resultado tem arquivo: `downloadUrl` e `sourceFulltextUrls`
     podem vir os dois vazios (registro so' de metadado, sem full text
     publico) — descartado antes de emitir (`_to_record` devolve `None`).
  5. Latencia real observada: 15 a 60 SEGUNDOS por chamada, mesmo em
     consultas simples, sem relacao aparente com throttling (o cabecalho
     `x-ratelimit-remaining` nao caiu apos uma unica chamada). O orcamento de
     tempo de um `discover core` precisa contar com isso — nao e' um
     problema de rede local, e' o backend deles.
  6. `fullText`, quando presente, e' o TEXTO JA EXTRAIDO (Apache PDFBox sobre
     o PDF), o mesmo tipo de achado de alto impacto do NTRS (`.txt` antes do
     `.pdf`). Guardado em `raw_metadata` para reuso futuro pela Etapa 2 — a
     Pipeline hoje so' sabe baixar `candidate_urls` (URLs), entao usa-lo
     direto exige uma mudanca alem do escopo desta primeira versao.
"""

from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import urlencode

import structlog

from ..core.record import DocumentRecord
from .base import BaseAdapter

log = structlog.get_logger(__name__)

BASE = "https://api.core.ac.uk/v3"

#: String literal que a API devolve no lugar do texto real quando a chave nao
#: tem acesso a full text — nao e' ausencia representada por `None`/`""`.
_FULLTEXT_INDISPONIVEL = "Not available for public API users."


class COREAdapter(BaseAdapter):
    name = "core"

    def __init__(
        self,
        fetcher,
        *,
        api_key: str,
        terms: list[str],
        page_size: int = 100,
        max_resultados_por_termo: int = 500,
        **options: Any,
    ):
        super().__init__(fetcher, **options)
        if not api_key:
            raise ValueError(
                "CORE exige chave de API — defina CORE_API_KEY no .env "
                "(registro gratuito em https://core.ac.uk/services/api#form)"
            )
        self.api_key = api_key
        self.terms = terms
        self.page_size = page_size
        self.max_resultados_por_termo = max_resultados_por_termo

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    # -------------------------------------------------------------- descoberta

    def discover(self) -> Iterator[DocumentRecord]:
        for term in self.terms:
            yield from self._walk(term)

    def _search(self, term: str, offset: int) -> dict[str, Any]:
        params = {
            # Busca por frase, SEM escopo de campo — mesma estrategia do
            # NTRS (que tambem nao escopa a query, deixando o lexico local
            # decidir titulo vs corpo depois). Escopar para `title:"..."`
            # foi testado ao vivo e devolve resultados igualmente bons, mas
            # com `totalHits` ainda mais inconsistente (ver nota 2 do
            # modulo) — sem ganho claro que justifique a complexidade extra.
            "q": f'"{term}"',
            "offset": offset,
            "limit": self.page_size,
        }
        return self.fetcher.get_json(
            f"{BASE}/search/works/?{urlencode(params)}", extra_headers=self._headers()
        )

    def _walk(self, term: str) -> Iterator[DocumentRecord]:
        offset = 0
        colhidos = 0
        emitidos = 0
        while offset < self.max_resultados_por_termo:
            data = self._search(term, offset)
            hits = data.get("results") or []
            if not hits:
                break
            for hit in hits:
                rec = self._to_record(hit)
                if rec is not None:
                    emitidos += 1
                    yield rec
            colhidos += len(hits)
            offset += len(hits)
            if len(hits) < self.page_size:
                # Pagina incompleta: nao ha mais resultados para este termo.
                break
        log.info("core.termo_concluido", termo=term, vistos=colhidos, emitidos=emitidos)

    # ------------------------------------------------------------- conversao

    @staticmethod
    def _to_record(hit: dict[str, Any]) -> DocumentRecord | None:
        urls = [
            u for u in ([hit.get("downloadUrl")] + list(hit.get("sourceFulltextUrls") or [])) if u
        ]
        if not urls:
            # Registro so' de metadado (artigo fechado, sem full text
            # publico indexado) — nada para baixar, nao emite.
            return None

        authors = [a.get("name", "") for a in (hit.get("authors") or []) if a.get("name")]

        landing = next(
            (l["url"] for l in (hit.get("links") or []) if l.get("type") == "display"),
            f"https://core.ac.uk/works/{hit.get('id')}",
        )

        full_text = hit.get("fullText")
        if full_text in (None, "", _FULLTEXT_INDISPONIVEL):
            full_text = None

        return DocumentRecord(
            source="core",
            source_id=str(hit.get("id")),
            title=hit.get("title") or "",
            abstract=hit.get("abstract"),
            authors=authors,
            organization=(
                ", ".join(p.get("name", "") for p in (hit.get("dataProviders") or []) if p.get("name"))
                or None
            ),
            pub_date=hit.get("publishedDate") or (
                str(hit["yearPublished"]) if hit.get("yearPublished") else None
            ),
            # `documentType` veio vazio em toda amostra verificada; quem
            # carrega essa informacao de fato e' `fieldOfStudy` (ex.:
            # "Article", "Konferenzbeitrag") — nome do campo na API e'
            # enganoso, mas e' o dado real disponivel.
            doc_type=hit.get("fieldOfStudy") or hit.get("documentType") or None,
            subject_terms=[],
            candidate_urls=urls,
            landing_url=landing,
            rights="CORE — agregador de acesso aberto; direitos do repositorio de origem",
            export_control=False,
            raw_metadata={
                "core_id": hit.get("id"),
                "doi": hit.get("doi"),
                "data_providers": [p.get("name") for p in (hit.get("dataProviders") or [])],
                "identifiers": hit.get("identifiers"),
                # Texto ja extraido pela CORE (Apache PDFBox) — nao e' URL,
                # entao nao entra em `candidate_urls`. Guardado para a
                # Etapa 2 reaproveitar sem rodar extracao, quando essa etapa
                # ganhar suporte a texto inline (ver nota 6 do modulo).
                "full_text_ja_extraido": full_text,
            },
        )
