"""ROSA P — Repository & Open Science Access Portal (US DOT).

Promovida a prioridade maxima: OAI-PMH completo, acervo em dominio publico e
coleta incremental nativa. Contem relatorios FAA, NHTSA e FHWA — incluindo a
avaliacao da STPA por reguladores de aviacao (dot/78914).

Nao se usa `oaipmh-scythe` aqui: o endpoint tem duas particularidades que uma
biblioteca generica esconde, e ambas foram verificadas contra o servico real em
2026-08-04.

  1. `granularity` e YYYY-MM-DDThh:mm:ssZ. Passar `from=2025-01-01` devolve
     `badArgument: Error parsing date` — a data curta, que a maioria dos
     repositorios aceita, aqui e erro.
  2. O `datestamp` e a data de MODIFICACAO no repositorio, nao a de publicacao.
     Uma reindexacao em massa reescreve todos os datestamps; por isso `from`
     serve para coleta incremental, jamais para recortar por periodo do
     documento (para isso existe `dc:date`).

`ListSets` vem vazio: nao ha particionamento por colecao a explorar.
"""

from __future__ import annotations

import re
from typing import Any, Iterator
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import structlog

from ..core.record import DocumentRecord
from .base import BaseAdapter

log = structlog.get_logger(__name__)

BASE = "https://rosap.ntl.bts.gov"
OAI = f"{BASE}/fedora/oai"

NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

#: oai:dot.stacks:dot:78914 -> ("dot", "78914")
_ID_RE = re.compile(r"oai:[^:]+:(?P<col>[^:]+):(?P<num>\d+)$")


class RosaPAdapter(BaseAdapter):
    name = "rosap"

    def __init__(
        self,
        fetcher,
        *,
        from_date: str | None = None,
        until_date: str | None = None,
        max_pages: int | None = None,
        **options: Any,
    ):
        super().__init__(fetcher, **options)
        self.from_date = _as_oai_datetime(from_date)
        self.until_date = _as_oai_datetime(until_date)
        self.max_pages = max_pages

    def discover(self) -> Iterator[DocumentRecord]:
        params: dict[str, str] = {"verb": "ListRecords", "metadataPrefix": "oai_dc"}
        if self.from_date:
            params["from"] = self.from_date
        if self.until_date:
            params["until"] = self.until_date

        page = 0
        vistos = 0
        while True:
            xml = self.fetcher.get_text(f"{OAI}?{urlencode(params)}")
            root = ET.fromstring(xml)

            err = root.find("oai:error", NS)
            if err is not None:
                log.error("rosap.erro_oai", code=err.get("code"), msg=(err.text or "").strip())
                return

            records = root.findall(".//oai:record", NS)
            for rec_el in records:
                rec = self._to_record(rec_el)
                if rec is not None:
                    vistos += 1
                    yield rec

            page += 1
            token_el = root.find(".//oai:resumptionToken", NS)
            token = (token_el.text or "").strip() if token_el is not None else ""
            if not token or (self.max_pages and page >= self.max_pages):
                break
            # Com resumptionToken NENHUM outro parametro pode ir junto: o
            # protocolo OAI-PMH proibe, e o servidor responde badArgument.
            params = {"verb": "ListRecords", "resumptionToken": token}

        log.info("rosap.concluido", paginas=page, registros=vistos)

    # ------------------------------------------------------------- conversao

    def _to_record(self, rec_el: ET.Element) -> DocumentRecord | None:
        header = rec_el.find("oai:header", NS)
        if header is None:
            return None
        if (header.get("status") or "").lower() == "deleted":
            return None

        oai_id = (header.findtext("oai:identifier", default="", namespaces=NS) or "").strip()
        m = _ID_RE.search(oai_id)
        if not m:
            log.warning("rosap.id_inesperado", oai_id=oai_id)
            return None
        col, num = m.group("col"), m.group("num")
        source_id = f"{col}:{num}"

        dc = rec_el.find(".//oai_dc:dc", NS)
        if dc is None:
            return None

        def all_of(tag: str) -> list[str]:
            return [
                (e.text or "").strip()
                for e in dc.findall(f"dc:{tag}", NS)
                if (e.text or "").strip()
            ]

        def one_of(tag: str) -> str | None:
            vals = all_of(tag)
            return vals[0] if vals else None

        landing = f"{BASE}/view/{col}/{num}"
        # Padrao confirmado: /view/dot/78914/dot_78914_DS1.pdf devolve
        # application/pdf com magic bytes %PDF-1.7.
        pdf = f"{landing}/{col}_{num}_DS1.pdf"

        authors = all_of("contributor.creator") or all_of("creator")

        return DocumentRecord(
            source="rosap",
            source_id=source_id,
            title=one_of("title") or "",
            abstract=one_of("description.abstract") or one_of("description"),
            authors=authors,
            organization=one_of("publisher"),
            pub_date=one_of("date"),
            doc_type=one_of("type"),
            subject_terms=all_of("subject"),
            candidate_urls=[pdf],
            landing_url=landing,
            # Todo o acervo e dominio publico ou tem permissao explicita do
            # detentor — nao ha friccao juridica nesta fonte.
            rights=one_of("rights.accessRights") or one_of("rights") or "Public Domain",
            export_control=False,
            raw_metadata={
                "oai_identifier": oai_id,
                "datestamp": header.findtext("oai:datestamp", default="", namespaces=NS),
                # Preserva o Dublin Core original para auditoria. Campos
                # multivalorados (dc:subject) viram lista.
                "dc": _dc_as_dict(dc),
            },
        )


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _dc_as_dict(dc: ET.Element) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for el in dc:
        name = _localname(el.tag)
        text = (el.text or "").strip()
        if not text:
            continue
        if name in out:
            if isinstance(out[name], list):
                out[name].append(text)
            else:
                out[name] = [out[name], text]
        else:
            out[name] = text
    return out


def _as_oai_datetime(value: str | None) -> str | None:
    """Normaliza para YYYY-MM-DDThh:mm:ssZ, a unica granularidade aceita."""
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return f"{value}T00:00:00Z"
    if value.endswith("Z"):
        return value
    return f"{value}Z"
