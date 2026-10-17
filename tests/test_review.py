import json
from passthru import load, flow, review
from passthru.db import client, TENANTS


def _run_both():
    load.main(reset=True)
    stats = {t: flow.run(t) for t in TENANTS}
    return client(), stats


def test_flow_leaves_needs_review_without_decision():
    db, stats = _run_both()
    assert any(s.get("needs_review", 0) > 0 for s in stats.values())
    for t in TENANTS:
        g = db.select_graph(t)
        n = g.query("MATCH (c:Case {status:'needs_review'}) RETURN count(c)").result_set[0][0]
        if stats[t].get("needs_review", 0):
            assert n == stats[t]["needs_review"]
        # parked cases have no Decision yet
        assert g.query(
            "MATCH (c:Case {status:'needs_review'})-[:DECIDED]->() RETURN count(c)"
        ).result_set[0][0] == 0
        # agents never wrote a deny Decision
        assert g.query(
            "MATCH (:Decision {outcome:'deny'}) RETURN count(*)"
        ).result_set[0][0] == 0


def test_blind_list_has_no_pii_fields():
    db, _ = _run_both()
    tenant = "tenant_us_acme"
    queue = review.list_queue(tenant)
    assert queue, "expected at least one needs_review case on US"
    forbidden = {"name", "patient_name", "facility", "hospital", "patient_token", "birth_year"}
    for item in queue:
        assert forbidden.isdisjoint(item.keys())
        assert "id" in item and "service" in item and "reason" in item
        # list stays on the tenant graph — never pulled provider names
        assert "name" not in json.dumps(item)


def test_submit_review_creates_decision_and_blind_edge():
    db, _ = _run_both()
    tenant = "tenant_us_acme"
    item = review.list_queue(tenant)[0]
    cid = item["id"]
    assert review.claim_for_review(cid, "rev-alpha", tenant)
    assert not review.claim_for_review(cid, "rev-beta", tenant)  # already held
    assert review.submit_review(cid, "rev-alpha", "deny", "criteria not met after look", tenant)
    g = db.select_graph(tenant)
    row = g.query(
        """MATCH (c:Case {id:$id})-[:DECIDED]->(d:Decision)
           MATCH (r:Reviewer {pseudonym:'rev-alpha'})-[e:REVIEWED]->(c)
           RETURN c.status, d.outcome, d.reason, e.blind""",
        {"id": cid},
    ).result_set
    assert row
    status, outcome, reason, blind = row[0]
    assert status == "decided" and outcome == "deny" and blind is True
    assert "criteria" in reason


def test_deny_only_via_review_path():
    db, _ = _run_both()
    # after agents run, zero denies exist
    for t in TENANTS:
        g = db.select_graph(t)
        assert g.query("MATCH (:Decision {outcome:'deny'}) RETURN count(*)").result_set[0][0] == 0
    # approve via review is fine too
    tenant = "tenant_in_bharat"
    queue = review.list_queue(tenant)
    if not queue:
        # IN may have fewer review cases; fall back to US
        tenant = "tenant_us_acme"
        queue = review.list_queue(tenant)
    cid = queue[0]["id"]
    assert review.claim_for_review(cid, "rev-gamma", tenant)
    assert review.submit_review(cid, "rev-gamma", "approve", "manual override ok", tenant)
    g = db.select_graph(tenant)
    out = g.query(
        "MATCH (:Case {id:$id})-[:DECIDED]->(d:Decision) RETURN d.outcome", {"id": cid}
    ).result_set[0][0]
    assert out == "approve"
