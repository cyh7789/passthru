"""Load rules.yaml into one graph per payer, patients into the provider graph, then cases."""
import json
from pathlib import Path
import yaml
from .db import client, TENANTS, PROVIDER

DATA = Path(__file__).resolve().parent.parent / "data"

INDEXES = [
    "CREATE INDEX FOR (s:Service) ON (s.code)",
    "CREATE INDEX FOR (c:Case) ON (c.id)",
    "CREATE INDEX FOR (c:Case) ON (c.status)",
    "CREATE INDEX FOR (r:Rule) ON (r.id)",
    "CREATE INDEX FOR (d:DocRequirement) ON (d.type)",
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
    # v1 procedure: just "check docs, submit". v2 gets made later by a reviewer
    g.query("""MERGE (pr:Procedure {id:'proc-default', version:1})
               ON CREATE SET pr.active=true, pr.success_rate=null
               MERGE (a:Action {name:'precheck_required_docs'}) MERGE (pr)-[:STEP {order:1}]->(a)""")


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
    g.query("""UNWIND $pts AS pt MATCH (p:Patient {id:pt.id})
               UNWIND pt.documents AS d
               MERGE (doc:Document {id: pt.id + ':' + d.type}) SET doc.type=d.type, doc.date=d.date
               MERGE (p)-[:HAS_DOC]->(doc)""", {"pts": patients})


def load_cases(db, requests):
    by_tenant = {}
    for r in requests:
        code = r["item"][0]["productOrService"]["coding"][0]["code"]
        by_tenant.setdefault(r["tenant"], []).append({
            "id": r["id"], "service": code, "kind": r["kind"],
            "urgent": r["priority"]["coding"][0]["code"] == "stat",
            # tenant graphs only get a token, not the patient's name
            "patient_token": r["patient"]["reference"].split("/")[1],
            "docs": [s["category"] for s in r["supportingInfo"]],
        })
    for tenant, cases in by_tenant.items():
        g = db.select_graph(tenant)
        g.query("""UNWIND $cases AS c
                   MATCH (s:Service {code:c.service})
                   MERGE (k:Case {id:c.id})
                   ON CREATE SET k.status='queued', k.version=0, k.kind=c.kind, k.urgent=c.urgent,
                                 k.patient_token=c.patient_token, k.submitted_docs=c.docs
                   MERGE (k)-[:FOR_SERVICE]->(s)""", {"cases": cases})


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
