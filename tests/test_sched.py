from passthru import load, flow, sched
from passthru.db import client
from passthru.sched import H

US = {"sla_std_h": 168, "sla_urgent_h": 72}
IN = {"sla_std_h": 1, "sla_urgent_h": 1, "sla_discharge_h": 3}


def test_sla_per_tenant():
    assert sched.sla_hours(US, "pa", False) == 168
    assert sched.sla_hours(US, "pa", True) == 72
    assert sched.sla_hours(IN, "cashless", False) == 1
    assert sched.sla_hours(IN, "discharge", False) == 3
    assert sched.deadline_ms(IN, "discharge", False, 1000) == 1000 + 3 * H


def test_cases_get_deadlines_and_queue_is_ordered():
    load.main(reset=True)
    db = client()
    for t in ("tenant_us_acme", "tenant_in_bharat"):
        g = db.select_graph(t)
        assert g.query("MATCH (c:Case) WHERE c.deadline IS NULL RETURN count(c)").result_set[0][0] == 0
        dl = [r[2] for r in sched.next_cases(g)]
        assert dl == sorted(dl)
    ind = db.select_graph("tenant_in_bharat")
    rows = ind.query("MATCH (c:Case) RETURN c.kind, (c.deadline - c.received_at) / 3600000 AS h").result_set
    assert {k: h for k, h in rows} == {"cashless": 1, "discharge": 3}
    us_first = sched.next_cases(db.select_graph("tenant_us_acme"), 1)[0][0]
    assert db.select_graph("tenant_us_acme").query(
        "MATCH (c:Case {id:$id}) RETURN c.urgent", {"id": us_first}).result_set[0][0]


def test_stale_claim_gets_reclaimed():
    load.main(reset=True)
    g = client().select_graph("tenant_us_acme")
    cid, v, _ = sched.next_cases(g, 1)[0]
    assert flow.claim(g, cid, v, "dead-worker")
    assert sched.reclaim_stale(g, older_than_ms=60_000) == 0
    assert sched.reclaim_stale(g, older_than_ms=60_000, now=sched.now_ms() + 120_000) == 1
    st, v2 = g.query("MATCH (c:Case {id:$id}) RETURN c.status, c.version", {"id": cid}).result_set[0]
    assert st == "queued" and v2 == v + 2
    assert not flow.claim(g, cid, v + 1, "dead-worker")  # stale version loses
    assert flow.claim(g, cid, v2, "w2")


def test_sweep_flags_at_risk_then_bharat_auto_approves_overdue():
    load.main(reset=True)
    ind = client().select_graph("tenant_in_bharat")
    recv = ind.query("MATCH (c:Case) RETURN min(c.received_at)").result_set[0][0]
    # 50 min into a 1h cashless window: cashless at risk, discharge (3h) not yet
    r = sched.sweep(ind, now=recv + 50 * 60_000)
    assert r["overdue"] == 0
    kinds = ind.query("MATCH (c:Case {at_risk:true}) RETURN collect(DISTINCT c.kind)").result_set[0][0]
    assert kinds == ["cashless"]
    first = sched.next_cases(ind, 1)[0][0]
    assert ind.query("MATCH (c:Case {id:$id}) RETURN c.at_risk", {"id": first}).result_set[0][0]
    # a worker holds one case when the clock runs out
    cid, v, _ = sched.next_cases(ind, 1)[0]
    assert flow.claim(ind, cid, v, "slow-worker")
    r = sched.sweep(ind, now=recv + 61 * 60_000)
    n_cashless = ind.query("MATCH (c:Case {kind:'cashless'}) RETURN count(c)").result_set[0][0]
    assert r["overdue"] == n_cashless
    assert ind.query("""MATCH (c:Case {kind:'cashless'})-[:DECIDED {by:'scheduler'}]->(d:Decision {outcome:'approve'})
                        RETURN count(c)""").result_set[0][0] == n_cashless
    assert not flow.record(ind, cid, v + 1, "pend", "late", [], "slow-worker")  # too late
    assert ind.query("MATCH (c:Case {id:$id})-[:DECIDED]->(d) RETURN count(d)", {"id": cid}).result_set[0][0] == 1


def test_sweep_acme_only_marks_breach():
    load.main(reset=True)
    us = client().select_graph("tenant_us_acme")
    recv = us.query("MATCH (c:Case) RETURN min(c.received_at)").result_set[0][0]
    r = sched.sweep(us, now=recv + 73 * H)
    assert r["overdue"] == us.query("MATCH (c:Case {urgent:true}) RETURN count(c)").result_set[0][0]
    assert us.query("MATCH (c:Case)-[:DECIDED]->() RETURN count(c)").result_set[0][0] == 0
    assert us.query("MATCH (c:Case {breached:true}) RETURN count(c)").result_set[0][0] == r["overdue"]
    assert sched.sweep(us, now=recv + 73 * H)["overdue"] == 0  # counted once
