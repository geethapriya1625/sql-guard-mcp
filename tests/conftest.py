import os
import sqlite3
from pathlib import Path

import pytest

from sqlguard.config import Policy

SEED = (Path(__file__).resolve().parents[1] / "examples" / "seed.sql").read_text()


@pytest.fixture
def sqlite_url(tmp_path) -> str:
    db = tmp_path / "shop.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(SEED)
    return f"sqlite:///{db}"


@pytest.fixture
def policy(sqlite_url, tmp_path) -> Policy:
    return Policy(
        database_url=sqlite_url,
        max_rows=3,
        timeout_ms=500,
        denied_tables=["api_keys"],
        denied_columns=["customers.email"],
        audit_log=str(tmp_path / "audit.jsonl"),
    )


@pytest.fixture
def pg_url():
    """Postgres tests run when TEST_POSTGRES_URL is set (CI sets it via a service container)."""
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL not set")
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(SEED)
    return url
