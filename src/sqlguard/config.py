"""Server policy. Loaded from YAML; every field has a safe default."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_url: str = Field(
        description="sqlite:///path/to.db or postgresql://user:pass@host/db",
    )
    max_rows: int = Field(default=200, ge=1, le=10_000)
    timeout_ms: int = Field(default=3_000, ge=100, le=60_000)
    # Empty means every table the connection can see (minus denied_tables).
    allowed_tables: list[str] = Field(default_factory=list)
    denied_tables: list[str] = Field(default_factory=list)
    # "table.column" pairs the agent may never read, e.g. customers.email
    denied_columns: list[str] = Field(default_factory=list)
    denied_functions: list[str] = Field(
        default_factory=lambda: [
            # Postgres: sleeping, file and network access, admin, large objects
            "pg_sleep",
            "pg_sleep_for",
            "pg_sleep_until",
            "pg_read_file",
            "pg_read_binary_file",
            "pg_ls_dir",
            "pg_stat_file",
            "lo_import",
            "lo_export",
            "dblink",
            "dblink_exec",
            "pg_terminate_backend",
            "pg_cancel_backend",
            "set_config",
            "current_setting",
            "pg_reload_conf",
            "copy",
            # SQLite: extension loading and expensive generators
            "load_extension",
            "randomblob",
            "zeroblob",
            "readfile",
            "writefile",
        ]
    )
    audit_log: str | None = Field(default=None, description="JSONL path; None disables")

    @field_validator("allowed_tables", "denied_tables", "denied_functions")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return [x.lower() for x in v]

    @field_validator("denied_columns")
    @classmethod
    def _qualified(cls, v: list[str]) -> list[str]:
        for item in v:
            if item.count(".") != 1:
                raise ValueError(f"denied column '{item}' must be written as table.column")
        return [x.lower() for x in v]

    @property
    def dialect(self) -> str:
        return (
            "postgres"
            if self.database_url.startswith(("postgres://", "postgresql://"))
            else "sqlite"
        )


def load_policy(path: str | Path | None = None) -> Policy:
    """Load from a YAML file, or from SQL_GUARD_CONFIG, or just SQL_GUARD_DATABASE_URL."""
    path = path or os.environ.get("SQL_GUARD_CONFIG")
    if path:
        data = yaml.safe_load(Path(path).read_text()) or {}
        url = data.get("database_url", "")
        data["database_url"] = os.path.expandvars(url)
        return Policy.model_validate(data)
    url = os.environ.get("SQL_GUARD_DATABASE_URL")
    if not url:
        raise SystemExit("set SQL_GUARD_CONFIG (YAML) or SQL_GUARD_DATABASE_URL")
    return Policy(database_url=url)
