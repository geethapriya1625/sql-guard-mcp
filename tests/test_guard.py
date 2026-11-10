import pytest

from sqlguard.config import Policy
from sqlguard.guard import QueryRejected, check_query


@pytest.fixture
def pol():
    return Policy(
        database_url="sqlite:///x.db",
        max_rows=100,
        denied_tables=["api_keys"],
        denied_columns=["customers.email"],
    )


@pytest.fixture
def pg_pol():
    return Policy(database_url="postgresql://u@h/db", max_rows=100)


ALLOWED = [
    "SELECT id, name FROM customers",
    "select c.name, count(*) from customers c join orders o on o.customer_id = c.id"
    " group by c.name",
    "WITH recent AS (SELECT * FROM orders WHERE placed_at > '2026-02-01') SELECT id FROM recent",
    "SELECT id FROM orders UNION SELECT id FROM products",
    "SELECT name FROM (SELECT name, region FROM customers) sub WHERE region = 'EU'",
    "SELECT o.* FROM orders o JOIN customers c ON c.id = o.customer_id",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql, pol):
    assert check_query(sql, pol).sql


REJECTED = {
    "SELECT 1; DROP TABLE customers": "exactly one statement",
    "DELETE FROM orders": "only SELECT",
    "UPDATE orders SET status = 'x'": "only SELECT",
    "INSERT INTO orders VALUES (1,1,'x','2026-01-01')": "only SELECT",
    "DROP TABLE orders": "only SELECT",
    "PRAGMA writable_schema = 1": "only SELECT",
    "ATTACH DATABASE 'x.db' AS x": "got ATTACH",
    "SELECT * INTO backup FROM orders": "INTO",
    "SELECT * FROM api_keys": "not available",
    "SELECT secret FROM orders JOIN api_keys ON 1=1": "not available",
    "SELECT email FROM customers": "restricted",
    "SELECT c.email FROM customers c": "restricted",
    "SELECT * FROM customers": "SELECT *",
    "SELECT c.* FROM customers c JOIN orders o ON o.customer_id = c.id": "SELECT *",
    "SELECT e FROM (SELECT email AS e FROM customers) s": "restricted",
    "SELECT load_extension('evil.so')": "load_extension",
    "SELECT name FROM sqlite_master": "system catalog",
    "": "empty",
    "SELEC id FROM customers": "could not parse",
}


@pytest.mark.parametrize(("sql", "reason"), REJECTED.items())
def test_rejected(sql, reason, pol):
    with pytest.raises(QueryRejected, match=reason):
        check_query(sql, pol)


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("SELECT pg_sleep(30)", "pg_sleep"),
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT * FROM orders FOR UPDATE", "locking"),
        ("WITH gone AS (DELETE FROM orders RETURNING *) SELECT * FROM gone", "DELETE"),
        ("SELECT * FROM pg_catalog.pg_user", "system catalog"),
        ("SELECT * FROM information_schema.tables", "system catalog"),
        ("COPY orders TO '/tmp/x'", "got COPY"),
        ("SET statement_timeout = 0", "got SET"),
    ],
)
def test_postgres_specific(sql, reason, pg_pol):
    with pytest.raises(QueryRejected, match=reason):
        check_query(sql, pg_pol)


def test_limit_added_when_missing(pol):
    q = check_query("SELECT id FROM orders", pol)
    assert q.sql.endswith("LIMIT 101") and q.row_cap == 100 and not q.notes


def test_small_limit_kept(pol):
    q = check_query("SELECT id FROM orders LIMIT 5", pol)
    assert q.sql.endswith("LIMIT 5") and q.row_cap == 5


def test_large_limit_clamped_with_note(pol):
    q = check_query("SELECT id FROM orders LIMIT 100000", pol)
    assert q.sql.endswith("LIMIT 101") and "lowered" in q.notes[0]


def test_allowlist_mode():
    pol = Policy(database_url="sqlite:///x.db", allowed_tables=["orders"])
    check_query("SELECT id FROM orders", pol)
    with pytest.raises(QueryRejected, match="not available"):
        check_query("SELECT id FROM customers", pol)


def test_cte_names_are_not_mistaken_for_tables():
    pol = Policy(database_url="sqlite:///x.db", allowed_tables=["orders"])
    q = check_query("WITH x AS (SELECT id FROM orders) SELECT id FROM x", pol)
    assert q.tables == ["orders"]


def test_denied_column_requires_table_dot_column():
    with pytest.raises(ValueError):
        Policy(database_url="sqlite:///x.db", denied_columns=["email"])
