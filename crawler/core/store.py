"""Armazenamento enderecado por conteudo + manifesto.

O nome do arquivo em disco e o SHA-256 do proprio conteudo. Consequencias:
  - deduplicacao entre repositorios sai de graca;
  - a coleta e idempotente (regravar o mesmo conteudo e no-op);
  - a proveniencia fica no manifesto, nao no nome do arquivo — um mesmo PDF
    pode ter chegado por tres caminhos diferentes.

O que se publica no relatorio sao os metadados e os identificadores persistentes
(DOI, id NTRS, numero AD) — nunca os PDFs. O corpus e interno ao projeto.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import structlog

from .record import DocumentRecord, sha256_bytes

log = structlog.get_logger(__name__)

EXT_BY_KIND = {"pdf": ".pdf", "zip": ".zip", "ole": ".doc", "text": ".txt", "html": ".html"}


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.raw = self.root / "raw"
        self.text = self.root / "text"
        self.manifest_path = self.root / "manifest.jsonl"
        self.rejects_path = self.root / "rejects.jsonl"
        for d in (self.raw, self.text):
            d.mkdir(parents=True, exist_ok=True)

    def path_for(self, sha: str, kind: str | None, text: bool = False) -> Path:
        base = self.text if text else self.raw
        ext = ".txt" if text else EXT_BY_KIND.get(kind or "", ".bin")
        return base / sha[:2] / f"{sha}{ext}"

    def put(self, body: bytes, kind: str | None, *, text: bool = False) -> tuple[str, Path, bool]:
        """Grava e devolve (sha256, caminho, era_novo)."""
        sha = sha256_bytes(body)
        path = self.path_for(sha, kind, text=text)
        if path.exists() and path.stat().st_size == len(body):
            return sha, path, False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(body)
        tmp.replace(path)
        return sha, path, True

    # ------------------------------------------------------------- manifesto

    def append_manifest(self, rec: DocumentRecord, **extra: Any) -> None:
        entry = asdict(rec)
        entry.update(extra)
        self._append(self.manifest_path, entry)

    def append_reject(self, rec: DocumentRecord | None, reason: str, **extra: Any) -> None:
        entry: dict[str, Any] = {"reason": reason, **extra}
        if rec is not None:
            entry.update(
                {
                    "key": rec.key,
                    "source": rec.source,
                    "source_id": rec.source_id,
                    "title": rec.title,
                    "landing_url": rec.landing_url,
                    "tier": rec.tier,
                }
            )
        self._append(self.rejects_path, entry)

    @staticmethod
    def _append(path: Path, entry: dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def count_pdf_pages(path: Path) -> int | None:
    """Validacao de integridade na ingestao. A extracao e da Etapa 2 — aqui so
    se verifica que o arquivo abre e quantas paginas tem."""
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception as exc:  # pypdf levanta uma familia larga de excecoes
        log.warning("pdf.ilegivel", path=str(path), error=str(exc))
        return None
