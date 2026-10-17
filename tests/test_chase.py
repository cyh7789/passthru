"""Shortest-path missing-doc chase via FalkorDB algo.SPpaths."""
from passthru import load, flow, chase
from passthru.db import client, tenant_graph


def test_chase_one_uses_sppaths():
    load.main(reset=True)
    g = tenant_graph(client(), "tenant_us_acme", bound_tenant="tenant_us_acme")
    cid = g.query("""MATCH (c:Case)-[:FOR_SERVICE]->(:Service {code:'MRI-LSPINE'})
                     RETURN c.id LIMIT 1""").result_set[0][0]
    step = chase.chase_one(g, cid, "pt_record")
    assert step is not None
    assert step["algo"] == "algo.SPpaths"
    assert step["doc"] == "pt_record"
    assert step["length"] == 4
    assert step["hops"] == ["Case", "Service", "Rule", "Criterion", "DocRequirement"]
    assert step["rels"] == ["FOR_SERVICE", "APPLIES_TO", "HAS_CRITERION", "NEEDS_DOC"]
    assert step["nodes"][-1] == "pt_record"
    assert step["nodes"][0] == cid


def test_decide_pend_includes_chase():
    load.main(reset=True)
    db = client()
    g = tenant_graph(db, "tenant_us_acme", bound_tenant="tenant_us_acme")
    # pick a queued MRI that already has at least one missing DocRequirement on the graph
    cid, v = g.query("""MATCH (c:Case {status:'queued'})-[:FOR_SERVICE]->(:Service {code:'MRI-LSPINE'})
                               <-[:APPLIES_TO]-(:Rule)-[:HAS_CRITERION]->(:Criterion)
                               -[:NEEDS_DOC]->(d:DocRequirement)
                        WHERE NOT (c)-[:HAS_EVIDENCE]->(:Evidence)-[:SATISFIES]->(d)
                        RETURN c.id, c.version LIMIT 1""").result_set[0]
    assert flow.claim(g, cid, v, "chase-t")
    outcome, reason, path = flow.decide(g, cid, db=db, tenant="tenant_us_acme")
    assert outcome == "pend"
    assert reason.startswith("missing:")
    missing = [p.strip() for p in reason[len("missing:"):].split(",")]
    chase_steps = [s for s in path if isinstance(s, dict) and s.get("step") == "chase"]
    assert chase_steps, "pend path should carry SPpaths chase steps"
    assert {s["doc"] for s in chase_steps} == set(missing)
    for s in chase_steps:
        assert s["algo"] == "algo.SPpaths"
        assert s["length"] == 4
        assert s["hops"][-1] == "DocRequirement"
        assert s["nodes"][-1] == s["doc"]


def test_chase_missing_skips_unreachable():
    load.main(reset=True)
    g = tenant_graph(client(), "tenant_us_acme", bound_tenant="tenant_us_acme")
    cid = g.query("MATCH (c:Case) RETURN c.id LIMIT 1").result_set[0][0]
    assert chase.chase_missing(g, cid, ["no_such_doc_type_xyz"]) == []


def test_case_path_exposes_chase():
    from passthru import metrics
    load.main(reset=True)
    g = tenant_graph(client(), "tenant_us_acme", bound_tenant="tenant_us_acme")
    cid = g.query("""MATCH (c:Case)-[:FOR_SERVICE]->(:Service {code:'MRI-LSPINE'})
                            <-[:APPLIES_TO]-(:Rule)-[:HAS_CRITERION]->(:Criterion)
                            -[:NEEDS_DOC]->(d:DocRequirement)
                     WHERE NOT (c)-[:HAS_EVIDENCE]->(:Evidence)-[:SATISFIES]->(d)
                     RETURN c.id LIMIT 1""").result_set[0][0]
    detail = metrics.case_path("tenant_us_acme", cid)
    assert detail and detail["chase"]
    assert all(s["algo"] == "algo.SPpaths" for s in detail["chase"])
