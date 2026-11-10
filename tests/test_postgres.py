import pytest

from sqlguard.config import Policy
from sqlguard.executor import QueryFailed, build_backend
from sqlguard.guard import CheckedQuery, check_query


@pytest.fixture
def pg_policy(pg_url):
    return Policy(database_url=pg_url, max_rows=3, timeout_ms=300)


def test_query_and_cap(pg_policy):
    res = build_backend(pg_policy).run(
        check_query("SELECT id, placed_at FROM orders ORDER BY id", pg_policy)
    )
    assert res.rows[0] == [101, "2026-01-10"] and res.truncated


def test_numeric_is_json_safe(pg_policy):
    res = build_backend(pg_policy).run(
        check_query("SELECT unit_price FROM products WHERE id = 3", pg_policy)
    )
    assert res.rows == [[310.5]]


def test_read_only_transaction_blocks_writes(pg_policy):
    sneaky = CheckedQuery(original="", sql="DELETE FROM orders", tables=[], row_cap=1)
    with pytest.raises(QueryFailed, match="read-only"):
        build_backend(pg_policy).run(sneaky)


def test_statement_timeout(pg_policy):
    # pg_sleep is denied by the guard, so call the executor directly to prove layer 3 holds.
    slow = CheckedQuery(original="", sql="SELECT pg_sleep(2)", tables=[], row_cap=1)
    with pytest.raises(QueryFailed, match="exceeded the 300 ms"):
        build_backend(pg_policy).run(slow)


def test_introspection(pg_policy):
    backend = build_backend(pg_policy)
    assert {"orders", "customers"} <= set(backend.tables())
    cols = {c.name: c for c in backend.columns("orders")}
    assert cols["id"].primary_key
    assert "Scan" in backend.explain(check_query("SELECT * FROM orders", pg_policy))
