from starlette.testclient import TestClient
from passthru import load, flow, metrics
from passthru.dash import app
from passthru.db import TENANTS


def test_snapshot_counts_match_the_graph():
    load.main(reset=True)
    before = metrics.snapshot()
    assert before["tenants"]["tenant_us_acme"]["status"] == {"queued": 120}
    assert before["tenants"]["tenant_in_bharat"]["status"] == {"queued": 80}
    flow.run("tenant_in_bharat")
    after = metrics.snapshot()["tenants"]["tenant_in_bharat"]
    assert "queued" not in after["status"]
    assert sum(after["outcomes"].values()) + after["status"].get("needs_review", 0) == 80
    assert all(p["version"] == 1 for p in after["procedures"])


def test_dash_serves_page_and_json():
    c = TestClient(app)
    assert "EventSource" in c.get("/").text
    j = c.get("/api/snapshot").json()
    assert set(j["tenants"]) == set(TENANTS)


def test_case_path_returns_hops_and_path_json():
    load.main(reset=True)
    from passthru import hero
    hero.run()
    us = metrics.case_path("tenant_us_acme", "hero-us-mri-1")
    assert us["status"] == "decided"
    assert us["outcome"] == "deny"
    assert us["path"] and isinstance(us["path"], list)
    assert any(h["doc"] == "pt_record" and h["present"] for h in us["hops"])
    assert any(h.get("result") is False for h in us["path"] if isinstance(h, dict) and "result" in h)
    inn = metrics.case_path("tenant_in_bharat", "hero-in-mri-1")
    assert inn["outcome"] == "approve"
    assert metrics.case_path("tenant_us_acme", "no-such") is None


def test_dash_case_endpoint():
    c = TestClient(app)
    load.main(reset=True)
    from passthru import hero
    hero.run()
    j = c.get("/api/case", params={"tenant": "tenant_us_acme", "id": "hero-us-mri-1"}).json()
    assert j["outcome"] == "deny" and j["hops"]
    assert c.get("/api/case", params={"tenant": "tenant_us_acme", "id": "missing"}).status_code == 404
    assert c.get("/api/case").status_code == 400


def test_run_endpoint_swarm_then_busy_guard():
    from passthru import runner
    c = TestClient(app)
    assert c.post("/api/run", params={"kind": "nope"}).status_code == 400
    assert runner.start("swarm", 20, wait=True)
    r = c.get("/api/snapshot").json()["run"]
    assert not r["running"] and r["error"] is None
    assert r["result"]["workers"] == 20
    for t in TENANTS:
        assert r["result"][t]["duplicates"] == 0 and r["result"][t]["left_queued"] == 0
    runner.state["running"] = True
    try:
        assert c.post("/api/run").status_code == 409
    finally:
        runner.state["running"] = False
