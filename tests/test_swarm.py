from passthru import load, flow, swarm
from passthru.db import client, TENANTS

OUTCOMES = """MATCH (c:Case) OPTIONAL MATCH (c)-[:DECIDED]->(d:Decision)
              RETURN c.id, c.status, d.outcome ORDER BY c.id"""


def snapshot():
    db = client()
    return {t: db.select_graph(t).query(OUTCOMES).result_set for t in TENANTS}


def test_20_workers_each_case_once_and_same_answers_as_one_worker():
    load.main(reset=True)
    for t in TENANTS:
        flow.run(t)
    serial = snapshot()

    load.main(reset=True)
    r = swarm.run(20)
    for t in TENANTS:
        assert r[t]["duplicates"] == 0
        assert r[t]["left_queued"] == 0
        assert r[t].get("lost", 0) == 0
    assert r["tenant_us_acme"]["done"] == 120 and r["tenant_in_bharat"]["done"] == 80
    # racing shouldn't change any decision
    assert snapshot() == serial


def test_workers_stay_in_their_tenant():
    assert sum(swarm.split(20).values()) == 20
    assert set(swarm.split(20)) == set(TENANTS)
