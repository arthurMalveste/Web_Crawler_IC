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
import threading
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
    """Fila, estado e deduplicacao — SQLite com UMA conexao, usada por varias
    threads durante o harvest paralelo.

    `sqlite3.Connection` nao e' thread-safe por si so (dai `check_same_thread`
    normalmente barrar isso). Em vez de uma conexao por thread — que com WAL
    funcionaria, mas exigiria coordenar `busy_timeout`/retries de "database is
    locked" espalhados pelo codigo —, este objeto usa uma conexao so e
    serializa TODO acesso a ela atras de `self._lock`. E' um `RLock` (nao um
    `Lock` simples) porque `totals()` chama `counts_by()` e
    `duplicate_stats()`, que tambem travam: um `Lock` comum causaria deadlock
    da mesma thread contra si mesma.

    O custo disso e' desprezivel aqui: cada operacao e' um SELECT/UPDATE
    indexado de uma linha, que leva microssegundos — o tempo real do harvest
    esta' no download, nao no banco. Serializar so as operacoes de banco (e
    NAO a chamada de rede, que fica fora do lock) e' exatamente o que permite
    varias threads de dominios diferentes progredirem em paralelo.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: a seguranca entre threads vem do
        # `self._lock`, nao da checagem padrao do driver.
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL (nao FULL, o padrao do SQLite fora de WAL) e' a combinacao
        # recomendada pela propria documentacao do SQLite para WAL com
        # escritores frequentes: evita o fsync sincrono a cada `commit()`
        # (feito DENTRO de `self._lock` em `add()`/`mark()` — ver comentario
        # da classe). O jornal WAL ja garante que a estrutura do banco nunca
        # corrompe; o unico risco de NORMAL e' perder a ULTIMA transacao numa
        # queda de energia (nao um crash de processo comum) — inofensivo
        # aqui, porque `add()` e' idempotente por (source, source_id): na
        # pior hipotese, redescobre 1 documento na proxima execucao.
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # Defesa extra para contencao vinda de FORA deste processo (ex.: rodar
        # `cli browse` enquanto um harvest esta em andamento): espera ate 30s
        # por um lock do SQLite em vez de falhar na hora com "database is
        # locked". Entre threads deste processo o `self._lock` ja evita a
        # contencao antes de chegar ao SQLite.
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.commit()
        self._lock = threading.RLock()

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
        with self._lock, closing(self.conn.cursor()) as cur:
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
            self.conn.commit()
        return True

    def pending(self, limit: int | None = None, tiers: list[str] | None = None) -> Iterator[DocumentRecord]:
        """Candidatos ainda nao baixados. E daqui que a retomada funciona.

        Busca todas as linhas ja travado e SO ENTAO libera o lock para
        devolver os registros um a um: um generator que segurasse o lock ao
        longo de toda a iteracao prenderia qualquer outra thread ate quem
        estiver consumindo terminar — inclusive threads de harvest tentando
        gravar resultado no meio da leitura.
        """
        sql = "SELECT record_json FROM documents WHERE status = ?"
        params: list[Any] = [STATUS_DISCOVERED]
        if tiers:
            sql += f" AND tier IN ({','.join('?' * len(tiers))})"
            params.extend(tiers)
        sql += " ORDER BY lexicon_score DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            linhas = self.conn.execute(sql, params).fetchall()
        for row in linhas:
            yield DocumentRecord.from_dict(json.loads(row["record_json"]))

    # ---------------------------------------------------------------- estado

    def mark(self, key: str, status: str, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        sql = f"UPDATE documents SET status = ?{', ' + cols if cols else ''} WHERE key = ?"
        with self._lock:
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
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.rowcount

    def get_content(self, sha256: str) -> sqlite3.Row | None:
        with self._lock:
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
        caso apenas acumula a proveniencia (documento visto em >1 repositorio).

        O metodo inteiro roda sob o lock: "ler se existe" e "inserir/atualizar"
        precisam ser atomicos juntos. Sem isso, duas threads baixando o MESMO
        conteudo de fontes diferentes ao mesmo tempo (cenario normal do
        harvest paralelo — e' o proprio caso que `sobreposicao_por_par` mede)
        poderiam ambas ver "nao existe" e tentar INSERT, colidindo no
        PRIMARY KEY do sha256.
        """
        with self._lock:
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
        with self._lock:
            cur = self.conn.execute(
                "SELECT etag, last_modified FROM documents WHERE key = ?", (key,)
            )
            row = cur.fetchone()
        return (row["etag"], row["last_modified"]) if row else (None, None)

    # --------------------------------------------------------------- metricas

    def counts_by(self, column: str) -> dict[str, int]:
        if column not in {"status", "tier", "source"}:
            raise ValueError(f"coluna nao permitida: {column}")
        with self._lock:
            rows = self.conn.execute(
                f"SELECT {column} AS k, COUNT(*) AS n FROM documents GROUP BY {column}"
            ).fetchall()
        return {r["k"]: r["n"] for r in rows}

    def duplicate_stats(self) -> dict[str, Any]:
        """Sobreposicao entre repositorios — resultado interessante por si so."""
        with self._lock:
            rows = self.conn.execute("SELECT provenance FROM content").fetchall()
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
        # `RLock`, nao `Lock`: este metodo trava e DEPOIS chama `counts_by`/
        # `duplicate_stats`, que travam de novo na mesma thread. Um lock comum
        # travaria a propria thread aqui. O beneficio extra e' um snapshot
        # atomico — as quatro leituras refletem o mesmo instante, nao quatro
        # instantes espalhados enquanto outras threads gravam.
        with self._lock:
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
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, started_at, adapter, params) VALUES (?,?,?,?)",
                (run_id, utcnow_iso(), adapter, json.dumps(params, ensure_ascii=False)),
            )
            self.conn.commit()

    def finish_run(self, run_id: str, stats: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE runs SET finished_at = ?, stats = ? WHERE run_id = ?",
                (utcnow_iso(), json.dumps(stats, ensure_ascii=False), run_id),
            )
            self.conn.commit()
