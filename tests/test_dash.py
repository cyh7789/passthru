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
