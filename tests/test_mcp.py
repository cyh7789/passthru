import asyncio
from passthru import load, mcp_server

TOOLS = {"queue", "claim_next", "process", "case_path", "review_queue", "active_procedure"}


def test_tools_listed():
    load.main(reset=True)
    mcp, _ = mcp_server.build("tenant_us_acme")
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == TOOLS


def test_claim_and_process_through_tools():
    load.main(reset=True)
    t = mcp_server.Tools("tenant_in_bharat", "mcp-1")
    got = t.claim_next("tenant_in_bharat")
    assert got["id"]
    r = t.process("tenant_in_bharat", got["id"], got["version"])
    assert r["recorded"] and r["outcome"] in ("approve", "pend", "needs_review")
    p = t.case_path("tenant_in_bharat", got["id"])
    assert p["status"] in ("decided", "needs_review") and p["path_json"]


def test_50_cross_tenant_calls_all_refused_and_logged():
    load.main(reset=True)
    t = mcp_server.Tools("tenant_us_acme", "mcp-us")
    calls = [("queue", ()), ("claim_next", ()), ("review_queue", ()),
             ("case_path", ("x",)), ("active_procedure", ("MRI",))]
    refused = 0
    for i in range(50):
        name, extra = calls[i % len(calls)]
        r = getattr(t, name)("tenant_in_bharat", *extra)
        refused += isinstance(r, dict) and r.get("error") == "denied"
    assert refused == 50
    n = t.g.query("MATCH (a:AccessDenied {bound:'tenant_us_acme', asked:'tenant_in_bharat'}) RETURN count(a)").result_set[0][0]
    assert n == 50
    # nothing got claimed on the other side
    other = t.db.select_graph("tenant_in_bharat")
    assert other.query("MATCH (c:Case) WHERE c.status <> 'queued' RETURN count(c)").result_set[0][0] == 0
