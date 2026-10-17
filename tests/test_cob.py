from passthru import load, cob, flow
from passthru.db import client, PROVIDER


def _dual_patients(db):
    rows = db.select_graph(PROVIDER).query(
        """MATCH (p:Patient)-[h:HAS_COVERAGE]->(:Plan)-[:OF]->(r:PayerRef)
           WITH p, collect([h.order, r.tenant]) AS cov
           WHERE size(cov) > 1
           RETURN p.id, cov""").result_set
    out = []
    for pid, cov in rows:
        eob = cob.primary_eob(db, pid)
        payers = [{"order": o, "tenant": t} for o, t in sorted(cov)]
        out.append({"id": pid, "payers": payers, "eob": eob})
    return out


def test_single_coverage_goes_to_primary():
    load.main(reset=True)
    db = client()
    pid = db.select_graph(PROVIDER).query(
        """MATCH (p:Patient)-[h:HAS_COVERAGE]->() WITH p, count(h) AS n
           WHERE n = 1 RETURN p.id LIMIT 1""").result_set[0][0]
    action, reason, path = cob.route(db, pid)
    assert action == "submit"
    assert path[0]["step"] == "coverage_order"
    assert len(path[0]["payers"]) == 1
    assert path[-1]["submit_to"] == path[0]["payers"][0]["tenant"]


def test_dual_without_eob_blocks_secondary():
    load.main(reset=True)
    db = client()
    dual = [p for p in _dual_patients(db) if p["eob"] is None]
    assert dual, "need at least one dual-coverage patient without primary EOB"
    p = dual[0]
    secondary = p["payers"][1]["tenant"]
    action, reason, path = cob.route(db, p["id"], intended=secondary)
    assert action == "hold"
    assert "primary EOB" in reason
    assert any(s.get("step") == "cob_hold" for s in path)
    assert cob.route(db, p["id"], intended=p["payers"][0]["tenant"])[0] == "submit"


def test_dual_with_eob_allows_secondary():
    load.main(reset=True)
    db = client()
    dual = [p for p in _dual_patients(db) if p["eob"] is not None]
    assert dual, "need at least one dual-coverage patient with primary EOB"
    p = dual[0]
    secondary = p["payers"][1]["tenant"]
    action, reason, path = cob.route(db, p["id"], intended=secondary)
    assert action == "submit"
    assert "secondary" in reason
    eob_step = next(s for s in path if s.get("step") == "primary_eob")
    assert eob_step["present"] and eob_step["eob"]["outcome"] in ("paid", "denied")


def test_flow_path_includes_coverage_order():
    load.main(reset=True)
    db = client()
    g = db.select_graph("tenant_us_acme")
    dual_ids = {p["id"] for p in _dual_patients(db)}
    row = None
    for cid, v, token in g.query(
            "MATCH (c:Case {status:'queued'}) RETURN c.id, c.version, c.patient_token").result_set:
        if token in dual_ids:
            row = (cid, v, token)
            break
    assert row, "need a queued US case for a dual-coverage patient"
    cid, v, token = row
    assert flow.claim(g, cid, v, "cob-w")
    outcome, reason, path = flow.decide(g, cid, db=db, tenant="tenant_us_acme")
    assert path[0]["step"] == "coverage_order"
    assert len(path[0]["payers"]) >= 2
    assert outcome in ("approve", "pend", "needs_review")
