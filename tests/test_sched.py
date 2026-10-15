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
