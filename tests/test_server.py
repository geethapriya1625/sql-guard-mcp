import asyncio
import json

from mcp import Client

from sqlguard.server import create_server


def call(server, tool, args=None):
    async def go():
        async with Client(server) as client:
            result = await client.call_tool(tool, args or {})
            return result

    result = asyncio.run(go())
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


def list_tool_names(server):
    async def go():
        async with Client(server) as client:
            return [t.name for t in (await client.list_tools()).tools]

    return asyncio.run(go())


def test_exposes_four_tools(policy):
    assert sorted(list_tool_names(create_server(policy))) == [
        "describe_table",
        "explain_query",
        "list_tables",
        "run_query",
    ]


def test_list_tables_hides_denied(policy):
    out = call(create_server(policy), "list_tables")
    assert "api_keys" not in out["tables"] and "orders" in out["tables"]
    assert out["max_rows"] == 3


def test_describe_hides_restricted_columns(policy):
    out = call(create_server(policy), "describe_table", {"table": "customers"})
    names = [c["name"] for c in out["columns"]]
    assert "email" not in names and out["restricted_columns"] == 1
    assert "error" in call(create_server(policy), "describe_table", {"table": "api_keys"})


def test_run_query_round_trip_and_audit(policy, tmp_path):
    server = create_server(policy)
    ok = call(server, "run_query", {"sql": "SELECT name FROM customers ORDER BY id LIMIT 2"})
    assert ok["rows"] == [["Northwind Traders"], ["Contoso GmbH"]] and not ok["truncated"]

    bad = call(server, "run_query", {"sql": "SELECT email FROM customers"})
    assert bad["rejected"] and "restricted" in bad["error"]

    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [e["decision"] for e in events] == ["ok", "rejected"]
    assert events[0]["tables"] == ["customers"]


def test_runtime_error_is_reported_not_raised(policy):
    out = call(create_server(policy), "run_query", {"sql": "SELECT nope FROM orders"})
    assert out["rejected"] is False and "nope" in out["error"]
