"""Hero case.

Part 1: the same lumbar MRI package sent to Acme (US) and Bharat (IN). Two graphs, two
different doc lists. Bharat approves, Acme proposes a deny on the 6-week conservative
therapy rule and a (blind) reviewer signs it off.

Part 2: the reviewer's fix becomes Procedure v2 (chase the PT record first). The next
Acme MRI was put together after 3 weeks of PT but the clinic's chart has 7 by now, so v2
pulls the current record and it goes through.
"""
import copy, json
from . import load, flow, review, procedure
from .db import client, tenant_graph

# one synthetic package, sent as-is to both payers
DOCS = [
    {"type": "clinic_note", "date": "2026-09-02", "facts": {"indication": True}},
    {"type": "neuro_exam", "date": "2026-09-02", "facts": {"deficit": True}},
    {"type": "pt_record", "date": "2026-09-20", "facts": {"weeks": 3, "improved": False}},
    {"type": "estimate", "date": "2026-09-25", "facts": {"amount": 12000}},
    {"type": "id_proof", "date": "2026-09-25", "facts": {"verified": True}},
]
TENANTS = {"tenant_us_acme": "US", "tenant_in_bharat": "IN"}
REVIEWER = "rev-hero"
US = "tenant_us_acme"
NEXT = "hero-us-mri-2"
FIX = {"action": "chase_doc", "doc": "pt_record", "field": "weeks", "at_least": 6}

NEEDS = """MATCH (s:Service {code:'MRI-LSPINE'})<-[:APPLIES_TO]-(r:Rule)-[:HAS_CRITERION]->(cr:Criterion)
                 -[:NEEDS_DOC]->(d:DocRequirement)
           RETURN DISTINCT d.type ORDER BY d.type"""


def case_id(tenant):
    return f"hero-{TENANTS[tenant].lower()}-mri-1"


def seed(db):
    patients, requests = [], []
    for tenant, country in TENANTS.items():
        pid = f"hero-{country.lower()}"
        patients.append({"id": pid, "name": "Hero Demo", "birth_year": 1980, "country": country,
                         "coverage": [{"order": 1, "plan": f"{tenant}:basic", "tenant": tenant}],
                         "documents": DOCS})
        requests.append({
            "id": case_id(tenant), "tenant": tenant,
            "kind": "pa" if country == "US" else "cashless",
            "patient": {"reference": f"Patient/{pid}"},
            "priority": {"coding": [{"code": "normal"}]},
            "item": [{"productOrService": {"coding": [{"code": "MRI-LSPINE"}]}}],
            "supportingInfo": [{"category": d["type"], "value": d["facts"]} for d in DOCS],
        })
    load.load_provider(db, patients)
    load.load_cases(db, requests)


def run(agent="hero-agent"):
    db = client()
    seed(db)
    out = {}
    for tenant in TENANTS:
        g = tenant_graph(db, tenant, bound_tenant=tenant)
        cid = case_id(tenant)
        needs = [r[0] for r in g.query(NEEDS).result_set]
        v = g.query("MATCH (c:Case {id:$id}) RETURN c.version", {"id": cid}).result_set[0][0]
        if not flow.claim(g, cid, v, agent):
            raise RuntimeError(f"{cid} already taken, reload first (make load)")
        outcome, reason, path = flow.decide(g, cid, db=db, tenant=tenant)
        flow.record(g, cid, v + 1, outcome, reason, path, agent)
        out[tenant] = {"case": cid, "needs": needs, "agent": outcome, "reason": reason}
    # Acme's proposed deny goes through a human
    us = "tenant_us_acme"
    if out[us]["agent"] == "needs_review":
        review.claim_for_review(case_id(us), REVIEWER, us)
        review.submit_review(case_id(us), REVIEWER, "deny",
                             "MISSING_PT_RECORD: needs 6 weeks conservative therapy, 3 on file", us)
    for tenant in TENANTS:
        g = db.select_graph(tenant)
        row = g.query("""MATCH (c:Case {id:$id}) OPTIONAL MATCH (c)-[k:DECIDED]->(d:Decision)
                         RETURN c.status, d.outcome, d.reason, k.by""", {"id": case_id(tenant)}).result_set[0]
        out[tenant].update(dict(zip(("status", "final", "final_reason", "decided_by"), row)))
    return out


def seed_next(db, cid=NEXT, chart_weeks=7):
    """Another Acme MRI, same stale package (3 wks PT); the clinic chart has `chart_weeks`."""
    pid = cid.replace("-mri", "")
    chart = copy.deepcopy(DOCS)
    for d in chart:
        if d["type"] == "pt_record":
            d.update(date="2026-10-10", facts={"weeks": chart_weeks, "improved": False})
    load.load_provider(db, [{"id": pid, "name": "Hero Demo Two", "birth_year": 1975, "country": "US",
                             "coverage": [{"order": 1, "plan": f"{US}:basic", "tenant": US}],
                             "documents": chart}])
    load.load_cases(db, [{
        "id": cid, "tenant": US, "kind": "pa",
        "patient": {"reference": f"Patient/{pid}"},
        "priority": {"coding": [{"code": "normal"}]},
        "item": [{"productOrService": {"coding": [{"code": "MRI-LSPINE"}]}}],
        "supportingInfo": [{"category": d["type"], "value": d["facts"]} for d in DOCS],
    }])


def process(db, cid, agent="hero-agent"):
    g = tenant_graph(db, US, bound_tenant=US)
    v = g.query("MATCH (c:Case {id:$id}) RETURN c.version", {"id": cid}).result_set[0][0]
    if not flow.claim(g, cid, v, agent):
        raise RuntimeError(f"{cid} already taken, reload first (make load)")
    outcome, reason, path = flow.decide(g, cid, db=db, tenant=US)
    flow.record(g, cid, v + 1, outcome, reason, path, agent)
    return {"case": cid, "agent": outcome, "reason": reason,
            "procedure": next((p for p in path if p.get("step") == "procedure"), None),
            "chase": next((p for p in path if p.get("step") == "chase_doc"), None)}


def run_v2():
    """Part 2. Run after run(): the reviewer's deny turns into v2, then the next case."""
    db = client()
    g = tenant_graph(db, US, bound_tenant=US)
    learned = procedure.learn_from_review(case_id(US), REVIEWER, "MISSING_PT_RECORD", FIX, US)
    seed_next(db)
    nxt = process(db, NEXT)
    return {"learned": learned, "active": procedure.active(g, "MRI-LSPINE"), "next": nxt,
            "rates": procedure.refresh_success_rate(g, "MRI-LSPINE")}


if __name__ == "__main__":
    print(json.dumps({"part1": run(), "part2": run_v2()}, indent=1))
