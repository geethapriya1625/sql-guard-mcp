"""MCP server exposing a database to an AI agent through four read-only tools."""

from __future__ import annotations

import argparse
from typing import Any

from mcp.server.mcpserver import MCPServer

from .config import Policy, load_policy
from .executor import AuditLog, Backend, QueryFailed, build_backend
from .guard import QueryRejected, check_query

INSTRUCTIONS = """\
Read-only SQL access. Start with list_tables, then describe_table for the tables you need,
then run_query. Write one SELECT per call and name columns explicitly. Results are capped
(see max_rows in list_tables); if `truncated` is true, add filters or aggregate instead of
paging through everything. Rejections explain what to change."""


def visible_tables(policy: Policy, backend: Backend) -> list[str]:
    allowed, denied = set(policy.allowed_tables), set(policy.denied_tables)
    return [
        t
        for t in backend.tables()
        if t.lower() not in denied and (not allowed or t.lower() in allowed)
    ]


def create_server(policy: Policy, backend: Backend | None = None) -> MCPServer:
    backend = backend or build_backend(policy)
    audit = AuditLog(policy.audit_log)
    hidden_cols = {c for c in policy.denied_columns}
    server = MCPServer(name="sql-guard", instructions=INSTRUCTIONS, version="0.1.0")

    @server.tool(description="List the tables this agent may query, with server limits.")
    def list_tables() -> dict[str, Any]:
        return {
            "dialect": policy.dialect,
            "tables": visible_tables(policy, backend),
            "max_rows": policy.max_rows,
            "timeout_ms": policy.timeout_ms,
        }

    @server.tool(description="Column names, types, nullability and primary keys for one table.")
    def describe_table(table: str) -> dict[str, Any]:
        if table.lower() not in {t.lower() for t in visible_tables(policy, backend)}:
            return {"error": f"table '{table}' is not available to this agent"}
        cols = backend.columns(table)
        shown = [c for c in cols if f"{table.lower()}.{c.name.lower()}" not in hidden_cols]
        return {
            "table": table,
            "columns": [c.__dict__ for c in shown],
            "restricted_columns": len(cols) - len(shown),
        }

    @server.tool(
        description="Run one read-only SELECT. Returns columns, rows and a truncated flag."
    )
    def run_query(sql: str) -> dict[str, Any]:
        try:
            checked = check_query(sql, policy)
        except QueryRejected as exc:
            audit.write(tool="run_query", sql=sql, decision="rejected", reason=str(exc))
            return {"error": str(exc), "rejected": True}
        try:
            result = backend.run(checked)
        except QueryFailed as exc:
            audit.write(
                tool="run_query", sql=sql, executed=checked.sql, decision="failed", reason=str(exc)
            )
            return {"error": str(exc), "rejected": False}
        audit.write(
            tool="run_query",
            sql=sql,
            executed=checked.sql,
            decision="ok",
            rows=len(result.rows),
            truncated=result.truncated,
            elapsed_ms=result.elapsed_ms,
            tables=checked.tables,
        )
        return {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": len(result.rows),
            "truncated": result.truncated,
            "elapsed_ms": result.elapsed_ms,
            "notes": checked.notes,
        }

    @server.tool(description="Show the database's query plan for a SELECT without running it.")
    def explain_query(sql: str) -> dict[str, Any]:
        try:
            checked = check_query(sql, policy)
            return {"plan": backend.explain(checked)}
        except (QueryRejected, QueryFailed) as exc:
            return {"error": str(exc)}

    return server


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="sql-guard-mcp")
    p.add_argument("--config", help="policy YAML (or set SQL_GUARD_CONFIG)")
    p.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http"])
    args = p.parse_args(argv)
    create_server(load_policy(args.config)).run(transport=args.transport)


if __name__ == "__main__":  # pragma: no cover
    main()
