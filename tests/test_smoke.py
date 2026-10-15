import json, pytest
from passthru import load, flow
from passthru.db import client, tenant_graph, TenantError, TENANTS


@pytest.fixture(scope="module")
def db():
    load.main(reset=True)
    return client()


def count(g, q):
    return g.query(q).result_set[0][0]


def test_graphs_loaded(db):
    names = db.list_graphs()
    for n in [*TENANTS, "provider_demo"]:
        assert n in names
    assert count(db.select_graph("tenant_us_acme"), "MATCH (c:Case) RETURN count(c)") == 120
    assert count(db.select_graph("tenant_in_bharat"), "MATCH (c:Case) RETURN count(c)") == 80
    assert count(db.select_graph("provider_demo"), "MATCH (p:Patient) RETURN count(p)") == 300


def test_tenants_isolated(db):
    us, ind = db.select_graph("tenant_us_acme"), db.select_graph("tenant_in_bharat")
    assert count(us, "MATCH (p:Payer) RETURN count(p)") == 1
    assert count(us, "MATCH (r:Rule) WHERE r.id STARTS WITH 'IN-' RETURN count(r)") == 0
    assert count(ind, "MATCH (r:Rule) WHERE r.id STARTS WITH 'US-' RETURN count(r)") == 0
    # no names in payer graphs
    assert count(us, "MATCH (n) WHERE n.name IS NOT NULL AND n:Case RETURN count(n)") == 0
    for _ in range(50):
        with pytest.raises(TenantError):
            tenant_graph(db, "tenant_in_bharat", bound_tenant="tenant_us_acme")


def test_same_mri_different_docs(db):
    q = """MATCH (:Service {code:'MRI-LSPINE'})<-[:APPLIES_TO]-(:Rule)-[:HAS_CRITERION]->(:Criterion)
                 -[:NEEDS_DOC]->(d:DocRequirement) RETURN collect(DISTINCT d.type)"""
    us = set(db.select_graph("tenant_us_acme").query(q).result_set[0][0])
    ind = set(db.select_graph("tenant_in_bharat").query(q).result_set[0][0])
    assert us == {"pt_record", "clinic_note", "neuro_exam"}
    assert ind == {"clinic_note", "estimate", "id_proof"}


def test_cob_order(db):
    rows = db.select_graph("provider_demo").query(
        """MATCH (p:Patient)-[h:HAS_COVERAGE]->(:Plan)-[:OF]->(r:PayerRef)
           WITH p, collect([h.order, r.tenant]) AS cov WHERE size(cov) > 1 RETURN p.id, cov""").result_set
    assert rows
    for _, cov in rows:
        assert sorted(cov)[0] == [1, "tenant_us_acme"]


def test_claim_is_cas(db):
    g = db.select_graph("tenant_us_acme")
    cid, v = g.query("MATCH (c:Case {status:'queued'}) RETURN c.id, c.version LIMIT 1").result_set[0]
    assert flow.claim(g, cid, v, "a1")
    assert not flow.claim(g, cid, v, "a2")  # second agent loses


def test_flow_decides_everything_once(db):
    load.main(reset=True)
    for t in TENANTS:
        flow.run(t)
        g = db.select_graph(t)
        assert count(g, "MATCH (c:Case) WHERE c.status <> 'decided' RETURN count(c)") == 0
        assert count(g, "MATCH (c:Case)-[:DECIDED]->() WITH c, count(*) AS n WHERE n > 1 RETURN count(c)") == 0
        paths = g.query("MATCH (:Case)-[d:DECIDED]->(x:Decision) WHERE x.outcome='pend' RETURN d.path_json LIMIT 5").result_set
        for (p,) in paths:
            steps = json.loads(p)
            assert any(step.get("present") is False for step in steps)
