"""Fila, estado e deduplicacao — SQLite.

Duas identidades distintas, deliberadamente separadas:

  1. identidade da DESCOBERTA = (source, source_id). Evita reprocessar o mesmo
     registro da mesma fonte.
  2. identidade do CONTEUDO   = SHA-256 do arquivo. E o que deduplica entre
     repositorios: o mesmo ConOps vindo da FAA, do ROSA P e do NTRS vira um
     unico arquivo em disco, com as tres proveniencias registradas.

URL nunca e chave: no Liferay (Cosmos) ela carrega uuid + timestamp e muda a
cada reedicao do documento.

Rodar a coleta duas vezes nao duplica nada — e essa propriedade que torna a
retomada apos interrupcao trivial.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

import structlog

from .record import DocumentRecord, utcnow_iso

log = structlog.get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    key             TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    title           TEXT,
    landing_url     TEXT,
    candidate_urls  TEXT,
    tier            TEXT,
    lexicon_score   REAL,
    matched_terms   TEXT,
    export_control  INTEGER DEFAULT 0,
    rights          TEXT,
    status          TEXT NOT NULL DEFAULT 'discovered',
    sha256          TEXT,
    stored_path     TEXT,
    content_kind    TEXT,
    http_status     INTEGER,
    error           TEXT,
    etag            TEXT,
    last_modified   TEXT,
    withdrawn_at    TEXT,
    discovered_at   TEXT,
    fetched_at      TEXT,
    record_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_docs_status  ON documents(status);
CREATE INDEX IF NOT EXISTS idx_docs_tier    ON documents(tier);
CREATE INDEX IF NOT EXISTS idx_docs_source  ON documents(source);
CREATE INDEX IF NOT EXISTS idx_docs_sha     ON documents(sha256);
-- Indice composto que serve `pending()`: filtra por status E ordena por
-- pontuacao. Sem ele o banco usa o indice de status e ordena o resultado em
-- memoria — medido em 200 mil linhas, 342 ms contra 1,4 ms. A ordem das
-- colunas importa: status primeiro (igualdade), pontuacao depois (ordenacao).
CREATE INDEX IF NOT EXISTS idx_docs_fila    ON documents(status, lexicon_score DESC);

-- Um registro por CONTEUDO distinto. `provenance` acumula as chaves de
-- descoberta que levaram ao mesmo arquivo: a sobreposicao entre repositorios
-- e um resultado do trabalho, nao um efeito colateral a esconder.
CREATE TABLE IF NOT EXISTS content (
    sha256      TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    kind        TEXT,
    size_bytes  INTEGER,
    n_pages     INTEGER,
    provenance  TEXT,
    first_seen  TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT,
    finished_at TEXT,
    adapter     TEXT,
    params      TEXT,
    stats       TEXT
);
"""

STATUS_DISCOVERED = "discovered"
STATUS_STORED = "stored"
STATUS_DUPLICATE = "duplicate"
STATUS_SKIPPED = "skipped"  # pre-filtro decidiu nao baixar
STATUS_REJECTED = "rejected"  # politica recusou (tamanho, robots, blocklist)
STATUS_FAILED = "failed"
STATUS_EXPORT_CONTROL = "export_control"
STATUS_UNCHANGED = "unchanged"  # 304 na reexecucao


class Frontier:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Frontier":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.conn.commit()
        self.close()

    # ------------------------------------------------------------- descoberta

    def add(self, rec: DocumentRecord, status: str = STATUS_DISCOVERED) -> bool:
        """Registra um candidato. Retorna False se ja era conhecido.

        Idempotente por (source, source_id): reexecutar a descoberta nao
        duplica nem sobrescreve o estado de download ja alcancado.
        """
        with closing(self.conn.cursor()) as cur:
            cur.execute("SELECT status FROM documents WHERE key = ?", (rec.key,))
            if cur.fetchone() is not None:
                return False
            cur.execute(
                """INSERT INTO documents
                   (key, source, source_id, title, landing_url, candidate_urls,
                    tier, lexicon_score, matched_terms, export_control, rights,
                    status, discovered_at, record_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec.key,
                    rec.source,
                    rec.source_id,
                    rec.title,
                    rec.landing_url,
                    json.dumps(rec.candidate_urls),
                    rec.tier,
                    rec.lexicon_score,
                    json.dumps(rec.matched_terms, ensure_ascii=False),
                    int(rec.export_control),
                    rec.rights,
                    status,
                    utcnow_iso(),
                    json.dumps(asdict(rec), ensure_ascii=False),
                ),
            )
        return True

    def pending(self, limit: int | None = None, tiers: list[str] | None = None) -> Iterator[DocumentRecord]:
        """Candidatos ainda nao baixados. E daqui que a retomada funciona."""
        sql = "SELECT record_json FROM documents WHERE status = ?"
        params: list[Any] = [STATUS_DISCOVERED]
        if tiers:
            sql += f" AND tier IN ({','.join('?' * len(tiers))})"
            params.extend(tiers)
        sql += " ORDER BY lexicon_score DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        for row in self.conn.execute(sql, params):
            yield DocumentRecord.from_dict(json.loads(row["record_json"]))

    # ---------------------------------------------------------------- estado

    def mark(self, key: str, status: str, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        sql = f"UPDATE documents SET status = ?{', ' + cols if cols else ''} WHERE key = ?"
        self.conn.execute(sql, [status, *fields.values(), key])
        self.conn.commit()

    def requeue_failed(self, source: str | None = None) -> int:
        """Devolve os falhos para a fila.

        Usado depois de corrigir o coletor: uma falha registrada e um candidato
        que o codigo da epoca nao soube tratar, nao um documento inexistente.
        """
        sql = "UPDATE documents SET status = ?, error = NULL WHERE status = ?"
        params: list[Any] = [STATUS_DISCOVERED, STATUS_FAILED]
        if source:
            sql += " AND source = ?"
            params.append(source)
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.rowcount

    def get_content(self, sha256: str) -> sqlite3.Row | None:
        cur = self.conn.execute("SELECT * FROM content WHERE sha256 = ?", (sha256,))
        return cur.fetchone()

    def register_content(
        self,
        sha256: str,
        path: str,
        kind: str | None,
        size_bytes: int,
        provenance_key: str,
        n_pages: int | None = None,
    ) -> bool:
        """Registra conteudo novo. Retorna False se o SHA ja existia — nesse
        caso apenas acumula a proveniencia (documento visto em >1 repositorio)."""
        existing = self.get_content(sha256)
        if existing is not None:
            prov = json.loads(existing["provenance"] or "[]")
            if provenance_key not in prov:
                prov.append(provenance_key)
                self.conn.execute(
                    "UPDATE content SET provenance = ? WHERE sha256 = ?",
                    (json.dumps(prov), sha256),
                )
                self.conn.commit()
            return False
        self.conn.execute(
            """INSERT INTO content (sha256, path, kind, size_bytes, n_pages, provenance, first_seen)
               VALUES (?,?,?,?,?,?,?)""",
            (sha256, path, kind, size_bytes, n_pages, json.dumps([provenance_key]), utcnow_iso()),
        )
        self.conn.commit()
        return True

    def conditional_headers(self, key: str) -> tuple[str | None, str | None]:
        cur = self.conn.execute(
            "SELECT etag, last_modified FROM documents WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        return (row["etag"], row["last_modified"]) if row else (None, None)

    # --------------------------------------------------------------- metricas

    def counts_by(self, column: str) -> dict[str, int]:
        if column not in {"status", "tier", "source"}:
            raise ValueError(f"coluna nao permitida: {column}")
        rows = self.conn.execute(
            f"SELECT {column} AS k, COUNT(*) AS n FROM documents GROUP BY {column}"
        )
        return {r["k"]: r["n"] for r in rows}

    def duplicate_stats(self) -> dict[str, Any]:
        """Sobreposicao entre repositorios — resultado interessante por si so."""
        rows = self.conn.execute("SELECT provenance FROM content")
        provs = [json.loads(r["provenance"] or "[]") for r in rows]
        multi = [p for p in provs if len(p) > 1]
        pairs: dict[str, int] = {}
        for p in multi:
            sources = sorted({k.split(":", 1)[0] for k in p})
            if len(sources) > 1:
                pairs[" x ".join(sources)] = pairs.get(" x ".join(sources), 0) + 1
        return {
            "conteudos_unicos": len(provs),
            "conteudos_em_multiplas_fontes": len(multi),
            "sobreposicao_por_par": pairs,
        }

    def totals(self) -> dict[str, Any]:
        total_bytes = self.conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) AS b FROM content"
        ).fetchone()["b"]
        return {
            "por_status": self.counts_by("status"),
            "por_faixa": self.counts_by("tier"),
            "por_fonte": self.counts_by("source"),
            "volume_gb": round(total_bytes / 1e9, 3),
            **self.duplicate_stats(),
        }

    # ------------------------------------------------------------------ runs

    def start_run(self, run_id: str, adapter: str, params: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at, adapter, params) VALUES (?,?,?,?)",
            (run_id, utcnow_iso(), adapter, json.dumps(params, ensure_ascii=False)),
        )
        self.conn.commit()

    def finish_run(self, run_id: str, stats: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, stats = ? WHERE run_id = ?",
            (utcnow_iso(), json.dumps(stats, ensure_ascii=False), run_id),
        )
        self.conn.commit()
