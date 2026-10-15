"""Load rules.yaml into one graph per payer, patients into the provider graph, then cases."""
import json
from pathlib import Path
import yaml
from .db import client, TENANTS, PROVIDER
from .sched import deadline_ms, now_ms

DATA = Path(__file__).resolve().parent.parent / "data"

INDEXES = [
    "CREATE INDEX FOR (s:Service) ON (s.code)",
    "CREATE INDEX FOR (c:Case) ON (c.id)",
    "CREATE INDEX FOR (c:Case) ON (c.status)",
    "CREATE INDEX FOR (c:Case) ON (c.deadline)",
    "CREATE INDEX FOR (r:Rule) ON (r.id)",
    "CREATE INDEX FOR (d:DocRequirement) ON (d.type)",
    "CREATE INDEX FOR (e:Evidence) ON (e.id)",
]


def load_tenant(db, tenant, cfg):
    g = db.select_graph(tenant)
    for q in INDEXES:
        try:
            g.query(q)
        except Exception:
            pass  # already there
    p = cfg["payer"]
    g.query("MERGE (p:Payer {id:$id}) SET p += $props", {"id": p["id"], "props": p})
    for code, svc in cfg["services"].items():
        g.query("""MATCH (p:Payer {id:$pid}) MERGE (s:Service {code:$code}) SET s.name=$name
                   MERGE (s)-[:REQUIRES_PA]->(p)""", {"pid": p["id"], "code": code, "name": svc["name"]})
        for r in svc["rules"]:
            g.query("""MATCH (s:Service {code:$code}) MERGE (r:Rule {id:$id})
                       SET r.text=$text, r.source='demo' MERGE (r)-[:APPLIES_TO]->(s)""",
                    {"code": code, "id": r["id"], "text": r["text"]})
            for c in r["criteria"]:
                g.query("""MATCH (r:Rule {id:$rid}) MERGE (c:Criterion {id:$id}) SET c.expr=$expr
                           MERGE (r)-[:HAS_CRITERION]->(c)
                           WITH c UNWIND $docs AS d
                           MERGE (dr:DocRequirement {type:d}) MERGE (c)-[:NEEDS_DOC]->(dr)""",
                        {"rid": r["id"], "id": c["id"], "expr": c["expr"], "docs": c["docs"]})
    g.query("""MATCH (p:Payer {id:$pid}) UNWIND $codes AS code
               MATCH (s:Service {code:code}) MERGE (s)-[:AUTO_APPROVE_LIST]->(p)""",
            {"pid": p["id"], "codes": cfg.get("auto_approve", [])})
    g.query("UNWIND $codes AS code MERGE (:DenialReason {code:code})", {"codes": cfg["denial_reasons"]})
    # v1 procedure per service: just "check docs, submit". later versions come from reviewers
    g.query("""UNWIND $codes AS code MATCH (s:Service {code:code})
               MERGE (pr:Procedure {id:'proc-' + code, version:1})
               ON CREATE SET pr.active=true
               MERGE (pr)-[:FOR_SERVICE]->(s)
               MERGE (a:Action {name:'precheck_required_docs'}) MERGE (pr)-[:STEP {order:1}]->(a)""",
            {"codes": list(cfg["services"])})


def load_provider(db, patients):
    g = db.select_graph(PROVIDER)
    for q in ["CREATE INDEX FOR (p:Patient) ON (p.id)"]:
        try:
            g.query(q)
        except Exception:
            pass
    g.query("""UNWIND $pts AS pt
               MERGE (p:Patient {id:pt.id}) SET p.name=pt.name, p.birth_year=pt.birth_year, p.country=pt.country
               WITH p, pt UNWIND pt.coverage AS cov
               MERGE (pl:Plan {id:cov.plan}) MERGE (pr:PayerRef {tenant:cov.tenant})
               MERGE (pl)-[:OF]->(pr) MERGE (p)-[h:HAS_COVERAGE]->(pl) SET h.order=cov.order""",
            {"pts": patients})
    # flatten facts we care about onto Document (primary_eob outcome/amount for COB)
    flat = []
    for pt in patients:
        for d in pt["documents"]:
            row = {"pid": pt["id"], "type": d["type"], "date": d["date"],
                   "outcome": d.get("facts", {}).get("outcome"),
                   "paid_amount": d.get("facts", {}).get("paid_amount"),
                   "data": json.dumps(d.get("facts", {}), sort_keys=True)}
            flat.append(row)
    g.query("""UNWIND $docs AS d MATCH (p:Patient {id:d.pid})
               MERGE (doc:Document {id: d.pid + ':' + d.type})
               SET doc.type=d.type, doc.date=d.date, doc.outcome=d.outcome, doc.paid_amount=d.paid_amount,
                   doc.data=d.data
               MERGE (p)-[:HAS_DOC]->(doc)""", {"docs": flat})


def load_cases(db, requests, received_ms=None):
    received_ms = received_ms or now_ms()
    by_tenant = {}
    for r in requests:
        code = r["item"][0]["productOrService"]["coding"][0]["code"]
        by_tenant.setdefault(r["tenant"], []).append({
            "id": r["id"], "service": code, "kind": r["kind"],
            "urgent": r["priority"]["coding"][0]["code"] == "stat",
            # tenant graphs only get a token, not the patient's name
            "patient_token": r["patient"]["reference"].split("/")[1],
            "docs": [s["category"] for s in r["supportingInfo"]],
            "evidence": [{"id": r["id"] + ":" + s["category"], "type": s["category"],
                          "data": json.dumps(s.get("value", {}), sort_keys=True)} for s in r["supportingInfo"]],
        })
    for tenant, cases in by_tenant.items():
        g = db.select_graph(tenant)
        payer = g.query("MATCH (p:Payer) RETURN properties(p)").result_set[0][0]
        for c in cases:
            c["received_at"] = received_ms
            c["deadline"] = deadline_ms(payer, c["kind"], c["urgent"], received_ms)
        g.query("""UNWIND $cases AS c
                   MATCH (s:Service {code:c.service})
                   MERGE (k:Case {id:c.id})
                   ON CREATE SET k.status='queued', k.version=0, k.kind=c.kind, k.urgent=c.urgent,
                                 k.patient_token=c.patient_token, k.submitted_docs=c.docs,
                                 k.received_at=c.received_at, k.deadline=c.deadline
                   MERGE (k)-[:FOR_SERVICE]->(s)""", {"cases": cases})
        # each submitted doc is its own node, hooked to the requirement it can satisfy
        g.query("""UNWIND $cases AS c MATCH (k:Case {id:c.id})
                   UNWIND c.evidence AS e
                   MERGE (x:Evidence {id:e.id}) SET x.type=e.type, x.data=e.data
                   MERGE (k)-[:HAS_EVIDENCE]->(x)
                   MERGE (d:DocRequirement {type:e.type}) MERGE (x)-[:SATISFIES]->(d)""", {"cases": cases})


def main(reset=False):
    db = client()
    if reset:
        for name in db.list_graphs():
            if name in TENANTS or name == PROVIDER:
                db.select_graph(name).delete()
    rules = yaml.safe_load((DATA / "rules.yaml").read_text())
    for t in TENANTS:
        load_tenant(db, t, rules[t])
    load_provider(db, json.loads((DATA / "patients.json").read_text()))
    load_cases(db, json.loads((DATA / "requests.json").read_text()))
    for name in sorted(db.list_graphs()):
        n = db.select_graph(name).query("MATCH (n) RETURN count(n)").result_set[0][0]
        print(f"{name}: {n} nodes")


if __name__ == "__main__":
    import sys
    main(reset="--reset" in sys.argv)
