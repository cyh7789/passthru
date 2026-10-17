import json
from passthru import load, swarm, replay
from passthru.db import client, TENANTS


def test_every_decision_path_replays():
    load.main(reset=True)
    swarm.run(20)
    for t in TENANTS:
        out = replay.sample(t, n=10, seed=42)
        assert out["of"] == 10 and out["reproduced"] == 10, out
        assert all(c["checked"] > 0 for c in out["cases"])


def test_tampered_evidence_gets_caught():
    load.main(reset=True)
    swarm.run(20)
    g = client().select_graph("tenant_us_acme")
    row = g.query("""MATCH (c:Case)-[k:DECIDED]->(:Decision {outcome:'approve'})
                     WHERE k.path_json CONTAINS '"expr"'
                     MATCH (c)-[:FOR_SERVICE]->(:Service)<-[:APPLIES_TO]-(:Rule)-[:HAS_CRITERION]->(:Criterion)
                           -[:NEEDS_DOC]->(d:DocRequirement)<-[:SATISFIES]-(e:Evidence)<-[:HAS_EVIDENCE]-(c)
                     RETURN c.id, e.id LIMIT 1""").result_set
    cid, eid = row[0]
    assert replay.verify(g, cid)["ok"]
    g.query("MATCH (c:Case {id:$c})-[h:HAS_EVIDENCE]->(:Evidence {id:$e}) DELETE h", {"c": cid, "e": eid})
    r = replay.verify(g, cid)
    assert not r["ok"] and r["mismatches"]
