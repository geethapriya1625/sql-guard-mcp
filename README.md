# sql-guard-mcp

[![ci](https://github.com/geethapriya1625/sql-guard-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/geethapriya1625/sql-guard-mcp/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![mcp](https://img.shields.io/badge/MCP-python%20SDK%202.x-purple)
![license](https://img.shields.io/badge/license-MIT-green)

**A read-only SQL [Model Context Protocol](https://modelcontextprotocol.io) server that you can safely point an AI agent at.**

Giving an agent database access is useful and risky. Agents write `SELECT *` on tables with personal data, run unbounded queries, and sometimes produce a `DELETE`. This server lets an agent explore and query SQLite or PostgreSQL, but every query passes three independent layers first, and each one would stop the bad case on its own.

```mermaid
flowchart LR
    A[AI agent] -- run_query --> G
    subgraph L1 [Layer 1 · AST guard]
      G[sqlglot parse] --> C1{one statement?<br/>SELECT only?<br/>no INTO / FOR UPDATE?}
      C1 --> C2{tables allowed?<br/>columns visible?<br/>functions allowed?}
      C2 --> RW[rewrite: clamp LIMIT<br/>to max_rows + 1]
    end
    subgraph L2 [Layer 2 · read-only connection]
      RW --> RO[SQLite mode=ro + query_only<br/>Postgres READ ONLY txn, always rolled back]
    end
    subgraph L3 [Layer 3 · time box]
      RO --> TO[SQLite progress-handler deadline<br/>Postgres statement_timeout]
    end
    TO --> R[rows + truncated flag]
    C1 -. reason .-> A
    C2 -. reason .-> A
    G & TO --> AU[(audit.jsonl)]
```

## Tools

| Tool | What the agent gets |
|---|---|
| `list_tables` | Tables it may query, plus the server's `max_rows` and `timeout_ms` so it can plan |
| `describe_table` | Columns, types, nullability, primary keys. Restricted columns are left out and counted |
| `run_query` | Columns, rows, `truncated`, elapsed time, plus notes such as "LIMIT 100000 lowered to 50" |
| `explain_query` | The database's query plan, without running the query |

Rejections are written for the agent to act on. For example, `SELECT * on 'customers' would expose restricted columns (email); list the columns you need` gives the model a way forward instead of a dead end.

## What gets blocked

| Attempt | Layer | Response |
|---|---|---|
| `SELECT 1; DROP TABLE customers` | guard | send exactly one statement per call |
| `DELETE`, `UPDATE`, `INSERT`, `DROP`, `ATTACH`, `PRAGMA`, `COPY`, `SET` | guard | only SELECT queries are allowed |
| `WITH gone AS (DELETE ... RETURNING *) SELECT ...` | guard | data-modifying CTE caught by walking the AST |
| `SELECT * INTO backup FROM orders` / `FOR UPDATE` | guard | creates a table / takes locks |
| `SELECT email FROM customers`, `c.email`, `(SELECT email AS e ...) s` | guard | column is restricted, including through aliases and subqueries |
| `SELECT * FROM customers` | guard | star expansion would expose a restricted column |
| `SELECT * FROM api_keys` | guard | table is not available to this agent |
| `pg_sleep`, `pg_read_file`, `dblink`, `load_extension`, ... | guard | function is not allowed |
| `pg_catalog`, `information_schema`, `sqlite_master` | guard | use `list_tables` / `describe_table` instead |
| a write that somehow got past the guard | connection | read-only transaction rejects it (tested directly) |
| `WITH RECURSIVE n ...` that never ends | time box | query exceeded the 2000 ms limit |
| `SELECT ... LIMIT 1000000` | guard rewrite | clamped to `max_rows`, with `truncated: true` |

The test suite bypasses the guard on purpose and hands raw `DELETE` statements and `pg_sleep` straight to the executor. That proves layers two and three hold on their own and aren't just backing up the parser.

## Quick start

```bash
git clone https://github.com/geethapriya1625/sql-guard-mcp && cd sql-guard-mcp
pip install -e ".[dev]"
python examples/make_demo_db.py          # small shop database
sql-guard-mcp --config examples/policy.yaml
```

### Claude Desktop / any MCP client

```json
{
  "mcpServers": {
    "shop-db": {
      "command": "sql-guard-mcp",
      "args": ["--config", "/absolute/path/to/policy.yaml"]
    }
  }
}
```

For remote clients, run `--transport streamable-http`.

### Policy

```yaml
database_url: postgresql://readonly_user:${PGPASSWORD}@db.internal/shop   # env vars expand
max_rows: 50
timeout_ms: 2000
allowed_tables: []            # empty = all, minus denied_tables
denied_tables: [api_keys]
denied_columns: [customers.email]
audit_log: audit.jsonl
```

In production, also connect as a database role that only has `SELECT` grants. This server is defense in depth on top of least privilege, not a replacement for it.

## A real session

Captured from the stdio transport against the demo database:

```text
tools: ['list_tables', 'describe_table', 'run_query', 'explain_query']

run_query: revenue by customer, excluding cancelled orders (4-table join)
→ {"columns": ["name", "revenue"], "rows": [["Globex Corporation", 2881.5], ["Initech LLC", 2400.0],
   ["Contoso GmbH", 1242.0], ["Northwind Traders", 1130.0], ["Umbrella Logistics", 649.95]],
   "row_count": 5, "truncated": false, "elapsed_ms": 1.05}

run_query: SELECT * FROM customers
→ {"error": "SELECT * on 'customers' would expose restricted columns (email); list the columns you need"}

run_query: SELECT secret FROM api_keys
→ {"error": "table 'api_keys' is not available to this agent"}

run_query: DELETE FROM orders
→ {"error": "only SELECT queries are allowed, got DELETE"}
```

Each call writes an audit line with the original SQL, the SQL that actually ran (with the injected `LIMIT 51`), the decision, row count, timing and the tables touched.

## Design decisions

- **Parse, don't pattern-match.** Regex filters miss `DeLeTe`, comments, and data-modifying CTEs. sqlglot gives a typed AST in the right dialect, so the checks walk real nodes.
- **Fail closed on the unknown.** Anything sqlglot can only parse as a generic `Command` is rejected, so statements the parser doesn't model are blocked by default.
- **Fetch `max_rows + 1`.** That's the cheapest way to tell the agent its result was cut off without running a `COUNT(*)`.
- **Hide instead of erroring in `describe_table`.** The agent never learns restricted column names, but it does learn that some exist, so it isn't confused when `SELECT *` is refused.
- **Conservative column checks.** An unqualified column that shares a name with a restricted one is refused in any query that touches the restricted table. Occasionally that's a false positive, but it never leaks data.

## Tests

```bash
pytest                                                             # SQLite: 51 tests
TEST_POSTGRES_URL=postgresql://user:pass@localhost/db pytest       # + 5 Postgres tests
```

56 tests cover 27 rejection cases (injection, writes, hidden columns, system catalogs, dangerous functions), the row cap, truncation, the read-only connection with the guard bypassed, runaway-query timeouts, introspection, the audit log, and full MCP round trips through the SDK client. CI runs everything on Python 3.11 to 3.13 against a Postgres 16 service container.

## License

MIT
