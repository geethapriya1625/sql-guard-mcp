import sqlite3

import pytest

from sqlguard.config import Policy
from sqlguard.executor import QueryFailed, SQLiteBackend, build_backend
from sqlguard.guard import CheckedQuery, check_query


def test_row_cap_and_truncation(policy):
    backend = build_backend(policy)
    res = backend.run(check_query("SELECT id FROM orders ORDER BY id", policy))
    assert res.rows == [[101], [102], [103]]
    assert res.truncated is True


def test_not_truncated_when_under_cap(policy):
    res = build_backend(policy).run(check_query("SELECT id FROM orders WHERE id < 103", policy))
    assert len(res.rows) == 2 and res.truncated is False


def test_connection_is_read_only_even_if_guard_is_bypassed(policy):
    """Defence in depth: hand the executor a write the guard would have rejected."""
    backend = SQLiteBackend(policy)
    sneaky = CheckedQuery(original="", sql="DELETE FROM orders", tables=[], row_cap=10)
    with pytest.raises(QueryFailed, match="readonly|read-only|query_only"):
        backend.run(sneaky)
    path = policy.database_url.removeprefix("sqlite:///")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 6


def test_timeout_interrupts_runaway_query(sqlite_url):
    pol = Policy(database_url=sqlite_url, timeout_ms=200, max_rows=1)
    runaway = (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n"
    )
    with pytest.raises(QueryFailed, match="exceeded the 200 ms"):
        build_backend(pol).run(check_query(runaway, pol))


def test_schema_introspection(policy):
    backend = build_backend(policy)
    assert "orders" in backend.tables()
    cols = {c.name: c for c in backend.columns("orders")}
    assert cols["id"].primary_key and not cols["status"].nullable


def test_explain(policy):
    plan = build_backend(policy).explain(check_query("SELECT * FROM orders WHERE id = 101", policy))
    assert "orders" in plan.lower()


def test_missing_file():
    with pytest.raises(QueryFailed, match="not found"):
        SQLiteBackend(Policy(database_url="sqlite:///nope/missing.db"))
