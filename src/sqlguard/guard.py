"""Static checks on agent-written SQL, done on the parsed AST rather than with regexes.

This is the first of three layers. Even if a query slips past it, the connection itself is
read-only and time-boxed (see executor.py), so the guard's job is to give the agent a clear,
early "no" with a reason it can act on, and to enforce data rules the database can't express
per-session, such as hidden columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .config import Policy

_WRITE_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,  # anything sqlglot could not model: COPY, VACUUM, SET, GRANT, ...
    exp.Pragma,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
)


class QueryRejected(ValueError):
    """Raised with a message meant to be shown to the agent."""


@dataclass
class CheckedQuery:
    original: str
    sql: str  # rewritten SQL that will actually run
    tables: list[str]
    row_cap: int
    notes: list[str] = field(default_factory=list)


def check_query(sql: str, policy: Policy) -> CheckedQuery:
    dialect = policy.dialect
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise QueryRejected("empty query")

    try:
        statements = [s for s in sqlglot.parse(text, read=dialect) if s is not None]
    except ParseError as exc:
        raise QueryRejected(f"could not parse SQL: {_first_line(str(exc))}") from exc
    if len(statements) != 1:
        raise QueryRejected("send exactly one statement per call")
    tree = statements[0]

    if not isinstance(tree, exp.Query):
        raise QueryRejected(f"only SELECT queries are allowed, got {tree.key.upper()}")
    for node in tree.walk():
        if isinstance(node, _WRITE_NODES):
            raise QueryRejected(f"write or admin operation not allowed: {node.key.upper()}")
    if tree.args.get("into") or tree.find(exp.Into):
        raise QueryRejected("SELECT ... INTO creates a table and is not allowed")
    if tree.find(exp.Lock):
        raise QueryRejected("row locking clauses (FOR UPDATE / FOR SHARE) are not allowed")

    _check_functions(tree, policy)
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    tables = _check_tables(tree, policy, cte_names)
    _check_columns(tree, policy, cte_names)

    rewritten, cap, notes = _apply_row_cap(tree, policy.max_rows, dialect)
    return CheckedQuery(
        original=sql, sql=rewritten, tables=sorted(tables), row_cap=cap, notes=notes
    )


# ----- individual checks -----------------------------------------------------


def _check_functions(tree: exp.Expression, policy: Policy) -> None:
    denied = set(policy.denied_functions)
    for fn in tree.find_all(exp.Func):
        name = (fn.name if isinstance(fn, exp.Anonymous) else fn.sql_name()).lower()
        if name in denied:
            raise QueryRejected(f"function '{name}' is not allowed")


def _check_tables(tree: exp.Expression, policy: Policy, cte_names: set[str]) -> set[str]:
    allowed = set(policy.allowed_tables)
    denied = set(policy.denied_tables)
    seen: set[str] = set()
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if not name or name in cte_names:
            continue
        schema = (table.db or "").lower()
        if schema in {"pg_catalog", "information_schema"} or name.startswith(("pg_", "sqlite_")):
            raise QueryRejected(
                f"system catalog '{table.sql()}' is not queryable here; "
                "use the list_tables and describe_table tools instead"
            )
        if name in denied or (allowed and name not in allowed):
            raise QueryRejected(f"table '{name}' is not available to this agent")
        seen.add(name)
    return seen


def _check_columns(tree: exp.Expression, policy: Policy, cte_names: set[str]) -> None:
    if not policy.denied_columns:
        return
    hidden: dict[str, set[str]] = {}
    for item in policy.denied_columns:
        t, c = item.split(".")
        hidden.setdefault(t, set()).add(c)

    for select in tree.find_all(exp.Select):
        # alias -> real table name for every table in this SELECT's own FROM/JOINs
        scope: dict[str, str] = {}
        for t in select.find_all(exp.Table):
            if t.name.lower() in cte_names:
                continue
            scope[t.alias_or_name.lower()] = t.name.lower()
        guarded = {alias: hidden[real] for alias, real in scope.items() if real in hidden}
        if not guarded:
            continue

        for col in select.find_all(exp.Column):
            cname = col.name.lower()
            qualifier = col.table.lower()
            if qualifier:
                if cname in guarded.get(qualifier, set()):
                    raise QueryRejected(f"column '{scope[qualifier]}.{cname}' is restricted")
            elif any(cname in cols for cols in guarded.values()):
                raise QueryRejected(f"column '{cname}' is restricted")

        for star in select.expressions:
            if isinstance(star, exp.Star) or (
                isinstance(star, exp.Column) and isinstance(star.this, exp.Star)
            ):
                qualifier = star.table.lower() if isinstance(star, exp.Column) else ""
                targets = [qualifier] if qualifier else list(guarded)
                for alias in targets:
                    if alias in guarded:
                        raise QueryRejected(
                            f"SELECT * on '{scope[alias]}' would expose restricted columns "
                            f"({', '.join(sorted(guarded[alias]))}); list the columns you need"
                        )


def _apply_row_cap(tree: exp.Query, max_rows: int, dialect: str) -> tuple[str, int, list[str]]:
    """Clamp or add a LIMIT so one call can never return more than max_rows rows.

    We fetch max_rows + 1 so the executor can tell the agent the result was truncated.
    """
    notes: list[str] = []
    limit = tree.args.get("limit")
    requested: int | None = None
    if limit is not None:
        value = limit.expression if isinstance(limit, exp.Limit) else None
        if isinstance(value, exp.Literal) and value.is_int:
            requested = int(value.this)
    if requested is not None and requested <= max_rows:
        return tree.sql(dialect=dialect), requested, notes
    if requested is not None:
        notes.append(f"LIMIT {requested} lowered to the server cap of {max_rows}")
    capped = tree.limit(max_rows + 1, copy=True)
    return capped.sql(dialect=dialect), max_rows, notes


def _first_line(s: str) -> str:
    return s.strip().splitlines()[0] if s.strip() else s
