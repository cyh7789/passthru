from starlette.testclient import TestClient
from passthru import load, flow, clusters
from passthru.dash import app
from passthru.db import client, tenant_graph


def test_gaps_from_path():
    path = [{"step": "cob_ok"}, {"rule": "R", "criterion": "R-a", "doc": "pt_record", "present": False},
            {"rule": "R", "criterion": "R-b", "doc": "clinic_note", "present": True},
            {"rule": "R", "criterion": "R-b", "expr": "x", "result": False}]
    assert clusters.gaps(path) == (["pt_record"], ["R-b"])
    assert clusters.gaps(None) == ([], [])


def test_clusters_cover_every_stuck_case():
    load.main(reset=True)
    flow.run("tenant_us_acme")
    g = tenant_graph(client(), "tenant_us_acme", bound_tenant="tenant_us_acme")
    stuck = g.query("""MATCH (c:Case) OPTIONAL MATCH (c)-[:DECIDED]->(d:Decision)
                       WITH c, d WHERE c.status='needs_review' OR d.outcome IN ['pend','deny']
                       RETURN count(c)""").result_set[0][0]
    out = clusters.clusters(g, top=100)
    assert stuck and sum(c["cases"] for c in out) == stuck
    assert len(out) > 1
    for c in out:
        assert c["fix_first"]["procedure"].startswith("proc-")
        assert c["fix_first"]["gap"] in c["gaps"]
        assert len(c["members"]) == min(c["cases"], 60)
        assert all(set(m["gaps"]) <= set(c["gaps"]) for m in c["members"])
    # rebuild is idempotent, no edge pileup
    n1 = g.query("MATCH ()-[e:GAP]->() RETURN count(e)").result_set[0][0]
    clusters.build(g)
    assert g.query("MATCH ()-[e:GAP]->() RETURN count(e)").result_set[0][0] == n1


def test_clusters_endpoint():
    c = TestClient(app)
    assert c.get("/api/clusters?tenant=nope").status_code == 400
    j = c.get("/api/clusters?tenant=tenant_us_acme").json()
    assert j["tenant"] == "tenant_us_acme" and isinstance(j["clusters"], list)
