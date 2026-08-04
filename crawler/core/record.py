"""Esquema unico de registro — contrato entre a Etapa 1 e o resto do pipeline.

Todo adaptador de fonte emite `DocumentRecord`. Nenhuma camada abaixo da
descoberta conhece o formato nativo da fonte; `raw_metadata` preserva o JSON/XML
original para auditoria e para reprocessamento sem nova coleta.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# Faixas do pre-filtro lexico (ver prefilter.py e secao 3.3 do plano).
TIER_STRONG = "strong"
TIER_WEAK = "weak"
TIER_NEGATIVE = "negative_sample"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_url(url: str) -> str:
    """Canonicaliza URL para dedupe.

    Nao remove query strings: no Liferay (Cosmos) o `?t=` faz parte do endereco
    e sem ele o servidor recusa. Normaliza apenas esquema, host e barra final.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    elif netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parts.query, ""))


@dataclass
class DocumentRecord:
    """Um documento *candidato*, descoberto mas ainda nao necessariamente baixado."""

    source: str  # "ntrs" | "rosap" | "cordis" | "dtic" | "psas" | "cosmos" | "seed"
    source_id: str  # id nativo: 20200001712, dot:78914, AD1113565, DOI...
    title: str
    landing_url: str  # pagina de citacao — e o que se publica, nunca o PDF
    candidate_urls: list[str] = field(default_factory=list)  # ordem de preferencia

    abstract: str | None = None
    authors: list[str] = field(default_factory=list)
    organization: str | None = None
    pub_date: str | None = None  # ISO-8601 (ou so o ano, quando e o que a fonte da)
    doc_type: str | None = None  # stiType, dc:type, tipo de entregavel
    subject_terms: list[str] = field(default_factory=list)
    rights: str | None = None  # "public domain" | "Distribution A" | ...

    export_control: bool = False  # True -> descartar antes de baixar
    export_control_reason: str | None = None

    lexicon_score: float = 0.0
    tier: str = TIER_NEGATIVE
    matched_terms: list[str] = field(default_factory=list)

    retrieved_at: str = field(default_factory=utcnow_iso)
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.source_id = str(self.source_id)
        self.candidate_urls = [canonical_url(u) for u in self.candidate_urls if u]

    @property
    def key(self) -> str:
        """Chave de identidade *da descoberta*: (fonte, id nativo).

        Nao confundir com a identidade do *conteudo*, que e o SHA-256 do arquivo.
        No Liferay a URL muda a cada reedicao, por isso URL nunca e chave.
        """
        return f"{self.source}:{self.source_id}"

    @property
    def text_for_scoring(self) -> str:
        """Campos sobre os quais o pre-filtro lexico decide SE vale baixar."""
        return "\n".join(
            p for p in (self.title, self.abstract, " ".join(self.subject_terms)) if p
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DocumentRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def sha256_file(path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
