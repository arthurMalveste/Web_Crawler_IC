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
import threading
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
#: PAGINA reivindicada por um worker, ainda em processamento — existe so'
#: entre `claim_next()` e o `mark()` que a resolve. Se sobreviver ate o
#: proximo `crawl()` desta fonte (processo anterior caiu no meio), e' orfa:
#: `reabrir_reivindicadas()` devolve para PENDING.
STATE_CLAIMED = "claimed"
#: DOCUMENTO (kind=document) ja convertido em DocumentRecord e entregue ao
#: chamador. Documentos nascem PENDING e so' viram EMITTED quando de fato
#: emitidos — nunca ha um `state=visited` para kind=document (ver
#: `Crawler._montar_registro`), entao contagens de pagina por `state=visited`
#: continuam sem mistura.
STATE_EMITTED = "emitted"

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
    """Fila de PAGINAS de uma fonte — SQLite, usada por varias threads durante
    o rastreamento paralelo (ver `Crawler.crawl`).

    Mesmo desenho de thread-safety de `core/frontier.py::Frontier`: uma unica
    conexao, `check_same_thread=False`, e TODO acesso a `self.conn` serializado
    atras de `self._lock` (`RLock` porque nao ha necessidade real de reentrancia
    aqui hoje, mas mantem o mesmo padrao caso um metodo futuro chame outro).
    O custo e' desprezivel — cada operacao e' um SELECT/UPDATE indexado de uma
    linha, e o tempo real do rastreamento esta' na rede, nao no banco.
    """

    def __init__(self, db_path: str | Path, source: str):
        self.source = source
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA journal_mode=WAL")
        # Defesa contra contencao externa (outro processo lendo o mesmo banco);
        # entre threads deste processo o `self._lock` ja evita a contencao
        # antes de chegar ao SQLite.
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.commit()
        self._lock = threading.RLock()

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
        with self._lock, closing(self.conn.cursor()) as cur:
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
        # Sem lock proprio: cada `add()` ja trava individualmente. Um lock
        # aqui em volta do laco so serializaria threads diferentes chamando
        # `add_many` sem necessidade nenhuma.
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
        with self._lock:
            self.conn.execute(sql, [state, utcnow_iso(), *fields.values(), self.source, url])
            self.conn.commit()

    def reabrir_reivindicadas(self) -> int:
        """Devolve para PENDING qualquer URL travada em CLAIMED — orfa de um
        processo anterior que caiu (kill, crash, excecao nao tratada) entre
        reivindicar a URL e resolve-la. Chamado no inicio de todo `crawl()`
        (ver `Crawler.crawl`): sem isso, uma URL reivindicada e nunca resolvida
        fica invisivel pra sempre (nem pending, nem visited), reduzindo
        silenciosamente a cobertura da fonte a cada interrupcao."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE urls SET state = ? WHERE source = ? AND state = ?",
                (STATE_PENDING, self.source, STATE_CLAIMED),
            )
            self.conn.commit()
            return cur.rowcount

    def requeue_failed(self, kind: str | None = None) -> int:
        """Devolve paginas `failed` desta fonte para `pending`.

        Sem isso, uma falha transitoria durante o rastreamento (timeout
        pontual, servidor em manutencao) marcava a URL como falha PARA SEMPRE
        — `add()` so reabre URL em `STATE_PENDING` (ver docstring de `add`), e
        o unico jeito de recuperar era `reset()`, que descarta TODO o
        progresso, inclusive paginas ja visitadas com sucesso. Espelha
        `core/frontier.py::Frontier.requeue_failed`.
        """
        sql = "UPDATE urls SET state = ?, error = NULL WHERE source = ? AND state = ?"
        params: list[Any] = [STATE_PENDING, self.source, STATE_FAILED]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------ leitura

    def next_batch(self, n: int = 1, kind: str | None = None) -> list[CrawlURL]:
        """Proximas URLs a visitar.

        A ordenacao — `priority DESC, depth ASC` — e o que implementa as duas
        estrategias com o mesmo codigo: no modo focado o LinkScorer atribui
        prioridades diferentes; no BFS todas valem 0 e a ordem cai para
        profundidade, que e exatamente busca em largura.

        Chamado com `n` = tamanho do pool de threads durante o rastreamento
        paralelo: as `n` linhas devolvidas sao distintas (`LIMIT` do SQL), e o
        chamador so busca o proximo lote depois que TODAS as desta rodada
        terminarem de ser visitadas (ver `Crawler.crawl`) — por isso nao ha
        risco de duas threads receberem a mesma URL, mesmo sem "reservar" a
        linha explicitamente.
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
        with self._lock:
            linhas = self.conn.execute(sql, params).fetchall()
        return [
            CrawlURL(
                url=r["url"],
                depth=r["depth"],
                priority=r["priority"],
                kind=r["kind"],
                anchor=r["anchor"],
                parent=r["parent"],
            )
            for r in linhas
        ]

    def claim_next(self, kind: str | None = None) -> CrawlURL | None:
        """Reivindica atomicamente a PROXIMA URL pendente de maior prioridade:
        SELECT e UPDATE->CLAIMED na mesma secao travada, sem liberar o lock
        entre os dois. E' o que permite workers em fila continua (cada um
        reivindica-processa-reivindica de novo, sem esperar os colegas de uma
        rodada) sem duas threads pegarem a mesma URL — substitui o desenho por
        RODADAS de `next_batch()` (mantido, ainda usado por quem quiser um
        lote fechado; ver testes)."""
        sql = "SELECT url, depth, priority, kind, anchor, parent FROM urls WHERE source = ? AND state = ?"
        params: list[Any] = [self.source, STATE_PENDING]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY priority DESC, depth ASC, rowid ASC LIMIT 1"
        with self._lock, closing(self.conn.cursor()) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                "UPDATE urls SET state = ? WHERE source = ? AND url = ?",
                (STATE_CLAIMED, self.source, row["url"]),
            )
            self.conn.commit()
            return CrawlURL(
                url=row["url"], depth=row["depth"], priority=row["priority"],
                kind=row["kind"], anchor=row["anchor"], parent=row["parent"],
            )

    def known(self, url: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "SELECT 1 FROM urls WHERE source = ? AND url = ?", (self.source, canonical_url(url))
            )
            return cur.fetchone() is not None

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT state, COUNT(*) n FROM urls WHERE source = ? GROUP BY state", (self.source,)
            ).fetchall()
        return {r["state"]: r["n"] for r in rows}

    def visited_pages(self) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) n FROM urls WHERE source = ? AND state = ? AND kind = ?",
                (self.source, STATE_VISITED, KIND_PAGE),
            ).fetchone()
        return row["n"]

    def documentos_pendentes(self) -> Iterator[CrawlURL]:
        """Documentos (kind=document) ainda NAO emitidos como DocumentRecord.

        A maioria dos documentos e' emitida na hora, assim que descoberta (ver
        `Crawler._emitir_agora`) — o que sobra aqui e' so' o residual: URLs de
        documento que sobreviveram de uma execucao ANTERIOR interrompida antes
        de serem emitidas (a pagina-mae ja foi visitada num processo que nao
        existe mais, entao o metadado dela precisa ser rebuscado — ver
        `Crawler._emitir_pendentes`). Reexecutar sobre uma fonte ja completa
        nao reprocessa nada: documentos ja emitidos ficam em STATE_EMITTED,
        fora desta consulta.
        """
        with self._lock:
            linhas = self.conn.execute(
                "SELECT url, depth, priority, kind, anchor, parent FROM urls "
                "WHERE source = ? AND kind = ? AND state = ? ORDER BY priority DESC",
                (self.source, KIND_DOCUMENT, STATE_PENDING),
            ).fetchall()
        for r in linhas:
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
        with self._lock:
            self.conn.execute("DELETE FROM urls WHERE source = ?", (self.source,))
            self.conn.commit()
