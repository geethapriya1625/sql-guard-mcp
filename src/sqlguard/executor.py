"""Runs checked queries on a connection that is read-only and time-boxed by the database itself.

Layers two and three of the defence:
  * read-only at the connection level: SQLite opens with mode=ro plus PRAGMA query_only;
    Postgres runs every query inside a READ ONLY transaction that is always rolled back.
  * time-boxed: SQLite uses a progress handler that aborts past the deadline; Postgres uses
    a per-transaction statement_timeout.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import Policy
from .guard import CheckedQuery


class QueryFailed(RuntimeError):
    pass


@dataclass
class Result:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    elapsed_ms: float


@dataclass
class ColumnInfo:
    name: str
    type: str
    nullable: bool
    primary_key: bool


class Backend(Protocol):
    def run(self, q: CheckedQuery) -> Result: ...
    def tables(self) -> list[str]: ...
    def columns(self, table: str) -> list[ColumnInfo]: ...
    def explain(self, q: CheckedQuery) -> str: ...


def to_jsonable(v: Any) -> Any:
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    return v


# ----- SQLite ----------------------------------------------------------------


class SQLiteBackend:
    def __init__(self, policy: Policy) -> None:
        path = policy.database_url.removeprefix("sqlite:///")
        if not Path(path).exists():
            raise QueryFailed(f"database file not found: {path}")
        self.path = path
        self.policy = policy

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        conn.execute("PRAGMA query_only = ON")
        return conn

    def run(self, q: CheckedQuery) -> Result:
        conn = self._connect()
        deadline = time.monotonic() + self.policy.timeout_ms / 1000
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        started = time.perf_counter()
        try:
            cur = conn.execute(q.sql)
            rows = cur.fetchmany(q.row_cap + 1)
            cols = [d[0] for d in cur.description or []]
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc):
                raise QueryFailed(f"query exceeded the {self.policy.timeout_ms} ms limit") from exc
            raise QueryFailed(str(exc)) from exc
        finally:
            conn.close()
        return _result(cols, rows, q.row_cap, started)

    def tables(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        return [r[0] for r in rows]

    def columns(self, table: str) -> list[ColumnInfo]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM pragma_table_info(?)", (table,)).fetchall()
        return [ColumnInfo(r[1], r[2] or "", not r[3], bool(r[5])) for r in rows]

    def explain(self, q: CheckedQuery) -> str:
        with self._connect() as conn:
            rows = conn.execute(f"EXPLAIN QUERY PLAN {q.sql}").fetchall()
        return "\n".join(r[3] for r in rows)


# ----- Postgres --------------------------------------------------------------


class PostgresBackend:
    def __init__(self, policy: Policy) -> None:
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise QueryFailed(
                "install the postgres extra: pip install 'sql-guard-mcp[postgres]'"
            ) from exc
        self.policy = policy

    def _txn(self):
        import psycopg

        conn = psycopg.connect(self.policy.database_url, autocommit=False)
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(f"SET LOCAL statement_timeout = {int(self.policy.timeout_ms)}")
        return conn

    def run(self, q: CheckedQuery) -> Result:
        import psycopg

        started = time.perf_counter()
        conn = self._txn()
        try:
            cur = conn.execute(q.sql)
            rows = cur.fetchmany(q.row_cap + 1)
            cols = [d.name for d in cur.description or []]
        except psycopg.errors.QueryCanceled as exc:
            raise QueryFailed(f"query exceeded the {self.policy.timeout_ms} ms limit") from exc
        except psycopg.Error as exc:
            raise QueryFailed(str(exc).strip().splitlines()[0]) from exc
        finally:
            conn.rollback()
            conn.close()
        return _result(cols, rows, q.row_cap, started)

    def tables(self) -> list[str]:
        conn = self._txn()
        try:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() ORDER BY table_name"
            ).fetchall()
        finally:
            conn.rollback()
            conn.close()
        return [r[0] for r in rows]

    def columns(self, table: str) -> list[ColumnInfo]:
        conn = self._txn()
        try:
            rows = conn.execute(
                """
                SELECT c.column_name, c.data_type, c.is_nullable = 'YES',
                       EXISTS (
                         SELECT 1 FROM information_schema.table_constraints tc
                         JOIN information_schema.key_column_usage k
                           ON tc.constraint_name = k.constraint_name
                          AND tc.table_schema = k.table_schema
                         WHERE tc.constraint_type = 'PRIMARY KEY'
                           AND k.table_name = c.table_name AND k.column_name = c.column_name
                       )
                FROM information_schema.columns c
                WHERE c.table_schema = current_schema() AND c.table_name = %s
                ORDER BY c.ordinal_position
                """,
                (table,),
            ).fetchall()
        finally:
            conn.rollback()
            conn.close()
        return [ColumnInfo(r[0], r[1], bool(r[2]), bool(r[3])) for r in rows]

    def explain(self, q: CheckedQuery) -> str:
        conn = self._txn()
        try:
            rows = conn.execute(f"EXPLAIN {q.sql}").fetchall()
        finally:
            conn.rollback()
            conn.close()
        return "\n".join(r[0] for r in rows)


def build_backend(policy: Policy) -> Backend:
    return PostgresBackend(policy) if policy.dialect == "postgres" else SQLiteBackend(policy)


def _result(cols: list[str], rows: list[Any], cap: int, started: float) -> Result:
    truncated = len(rows) > cap
    rows = rows[:cap]
    return Result(
        columns=cols,
        rows=[[to_jsonable(v) for v in r] for r in rows],
        truncated=truncated,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
    )


# ----- audit -----------------------------------------------------------------


class AuditLog:
    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None

    def write(self, **event: Any) -> None:
        if self.path is None:
            return
        event = {"ts": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"), **event}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")
