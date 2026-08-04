"""Fronteira de URLs — fila com prioridade, profundidade e retomada.

Distinta da fila de documentos (`core/frontier.py`), que guarda candidatos ja
identificados. Esta guarda *enderecos ainda nao visitados* e decide a ordem em
que o crawler os visita. E dessa ordem que sai a diferenca entre o rastreamento
focado e o BFS cego.

Persistida em SQLite pelo mesmo motivo da outra: um rastreamento de milhares de
paginas nao pode perder o progresso se a maquina cair, e reexecutar nao pode
revisitar o que ja foi visto.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..core.record import canonical_url, utcnow_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS urls (
    url         TEXT NOT NULL,
    source      TEXT NOT NULL,
    depth       INTEGER NOT NULL DEFAULT 0,
    priority    REAL NOT NULL DEFAULT 0.0,
    state       TEXT NOT NULL DEFAULT 'pending',
    kind        TEXT,              -- 'page' | 'document'
    anchor      TEXT,              -- texto da ancora que levou ate aqui
    parent      TEXT,
    http_status INTEGER,
    error       TEXT,
    found_at    TEXT,
    visited_at  TEXT,
    PRIMARY KEY (source, url)
);
CREATE INDEX IF NOT EXISTS idx_urls_fila
    ON urls(source, state, priority DESC, depth ASC);
CREATE INDEX IF NOT EXISTS idx_urls_state ON urls(state);
"""

STATE_PENDING = "pending"
STATE_VISITED = "visited"
STATE_SKIPPED = "skipped"  # fora de escopo / armadilha / orcamento
STATE_FAILED = "failed"

KIND_PAGE = "page"
KIND_DOCUMENT = "document"


@dataclass
class CrawlURL:
    url: str
    depth: int
    priority: float
    kind: str
    anchor: str | None = None
    parent: str | None = None


class URLFrontier:
    def __init__(self, db_path: str | Path, source: str):
        self.source = source
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "URLFrontier":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.conn.commit()
        self.close()

    # ------------------------------------------------------------------ escrita

    def add(
        self,
        url: str,
        *,
        depth: int,
        priority: float = 0.0,
        kind: str = KIND_PAGE,
        anchor: str | None = None,
        parent: str | None = None,
    ) -> bool:
        """Enfileira uma URL. Retorna False se ja era conhecida.

        Quando a mesma URL e reencontrada por um caminho melhor (prioridade
        maior ou profundidade menor), a entrada existente e *promovida* em vez
        de ignorada: no rastreamento focado, descobrir que um endereco tambem e
        alcancavel por um link muito relevante e informacao util.
        """
        url = canonical_url(url)
        with closing(self.conn.cursor()) as cur:
            cur.execute(
                "SELECT state, priority, depth FROM urls WHERE source = ? AND url = ?",
                (self.source, url),
            )
            row = cur.fetchone()
            if row is not None:
                if row["state"] == STATE_PENDING and (
                    priority > row["priority"] or depth < row["depth"]
                ):
                    cur.execute(
                        """UPDATE urls SET priority = ?, depth = ?, anchor = COALESCE(?, anchor)
                           WHERE source = ? AND url = ?""",
                        (max(priority, row["priority"]), min(depth, row["depth"]), anchor, self.source, url),
                    )
                    self.conn.commit()
                return False
            cur.execute(
                """INSERT INTO urls (url, source, depth, priority, state, kind, anchor, parent, found_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (url, self.source, depth, priority, STATE_PENDING, kind, anchor, parent, utcnow_iso()),
            )
        self.conn.commit()
        return True

    def add_many(self, urls: list[CrawlURL]) -> int:
        novos = 0
        for u in urls:
            if self.add(
                u.url,
                depth=u.depth,
                priority=u.priority,
                kind=u.kind,
                anchor=u.anchor,
                parent=u.parent,
            ):
                novos += 1
        return novos

    def mark(self, url: str, state: str, **fields: Any) -> None:
        url = canonical_url(url)
        cols = ", ".join(f"{k} = ?" for k in fields)
        sql = (
            f"UPDATE urls SET state = ?, visited_at = ?{', ' + cols if cols else ''} "
            "WHERE source = ? AND url = ?"
        )
        self.conn.execute(sql, [state, utcnow_iso(), *fields.values(), self.source, url])
        self.conn.commit()

    # ------------------------------------------------------------------ leitura

    def next_batch(self, n: int = 1, kind: str | None = None) -> list[CrawlURL]:
        """Proximas URLs a visitar.

        A ordenacao — `priority DESC, depth ASC` — e o que implementa as duas
        estrategias com o mesmo codigo: no modo focado o LinkScorer atribui
        prioridades diferentes; no BFS todas valem 0 e a ordem cai para
        profundidade, que e exatamente busca em largura.
        """
        sql = (
            "SELECT url, depth, priority, kind, anchor, parent FROM urls "
            "WHERE source = ? AND state = ?"
        )
        params: list[Any] = [self.source, STATE_PENDING]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY priority DESC, depth ASC, rowid ASC LIMIT ?"
        params.append(n)
        return [
            CrawlURL(
                url=r["url"],
                depth=r["depth"],
                priority=r["priority"],
                kind=r["kind"],
                anchor=r["anchor"],
                parent=r["parent"],
            )
            for r in self.conn.execute(sql, params)
        ]

    def known(self, url: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM urls WHERE source = ? AND url = ?", (self.source, canonical_url(url))
        )
        return cur.fetchone() is not None

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT state, COUNT(*) n FROM urls WHERE source = ? GROUP BY state", (self.source,)
        )
        return {r["state"]: r["n"] for r in rows}

    def visited_pages(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM urls WHERE source = ? AND state = ? AND kind = ?",
            (self.source, STATE_VISITED, KIND_PAGE),
        ).fetchone()
        return row["n"]

    def documents_found(self) -> Iterator[CrawlURL]:
        for r in self.conn.execute(
            "SELECT url, depth, priority, kind, anchor, parent FROM urls "
            "WHERE source = ? AND kind = ? ORDER BY priority DESC",
            (self.source, KIND_DOCUMENT),
        ):
            yield CrawlURL(
                url=r["url"],
                depth=r["depth"],
                priority=r["priority"],
                kind=r["kind"],
                anchor=r["anchor"],
                parent=r["parent"],
            )

    def reset(self) -> None:
        """Zera a fronteira desta fonte — usado para reexecutar um experimento
        de rastreamento do zero (focado vs BFS na mesma fonte)."""
        self.conn.execute("DELETE FROM urls WHERE source = ?", (self.source,))
        self.conn.commit()
