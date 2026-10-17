"""Explainability check: take decided cases, re-walk every hop in their path_json against the
graph (rule -> criterion -> doc, evidence present or not, criterion result), and hand back the
Cypher you'd paste into FalkorDB Browser to see the same path."""
import json, random
from .db import client, tenant_graph, TENANTS
from .expr import evaluate
from . import cob
from .flow import AUTO

HOP = """MATCH (c:Case {id:$id})-[:FOR_SERVICE]->(s:Service)<-[:APPLIES_TO]-(r:Rule {id:$r})
               -[:HAS_CRITERION]->(cr:Criterion {id:$cr})-[:NEEDS_DOC]->(d:DocRequirement {type:$d})
         OPTIONAL MATCH (c)-[:HAS_EVIDENCE]->(e:Evidence)-[:SATISFIES]->(d)
         RETURN e IS NOT NULL"""

EVIDENCE = """MATCH (c:Case {id:$id})-[:HAS_EVIDENCE]->(e:Evidence)-[:SATISFIES]->(d:DocRequirement)
              RETURN d.type, e.data"""

PROC = """MATCH (c:Case {id:$id})-[:FOR_SERVICE]->(:Service)<-[:FOR_SERVICE]-(p:Procedure {id:$p, version:$v})
          RETURN p.id"""

BROWSER = """MATCH p=(c:Case {{id:'{id}'}})-[:FOR_SERVICE]->(:Service)<-[:APPLIES_TO]-(:Rule)-[:HAS_CRITERION]->(:Criterion)-[:NEEDS_DOC]->(d:DocRequirement)
OPTIONAL MATCH q=(c)-[:HAS_EVIDENCE]->(:Evidence)-[:SATISFIES]->(d)
RETURN p, q"""


def stored(g, case_id):
    rows = g.query("""MATCH (c:Case {id:$id})
                      OPTIONAL MATCH (c)-[k:DECIDED]->(d:Decision)
                      RETURN c.status, coalesce(k.path_json, c.path_json), d.outcome, k.by""",
                   {"id": case_id}).result_set
    if not rows:
        return None
    status, path, outcome, by = rows[0]
    return {"status": status, "path": json.loads(path) if path else None, "outcome": outcome, "by": by}


def verify(g, case_id, db=None):
    """Re-check one case's recorded path. Returns {ok, checked, mismatches, cypher}.
    COB steps get checked against the provider graph when db is given."""
    s = stored(g, case_id)
    if not s or not s["path"]:
        return {"id": case_id, "ok": False, "checked": 0, "mismatches": ["no path recorded"]}
    ev = {d: json.loads(data) for d, data in g.query(EVIDENCE, {"id": case_id}).result_set if data}
    token = g.query("MATCH (c:Case {id:$id}) RETURN c.patient_token", {"id": case_id}).result_set[0][0]
    checked, bad = 0, []
    for st in s["path"]:
        step = st.get("step")
        if step == "auto_approve_list":
            checked += 1
            if not g.query(AUTO, {"id": case_id}).result_set:
                bad.append("service not on auto-approve list")
            continue
        if db is not None and step == "coverage_order":
            checked += 1
            if cob.coverage(db, token) != st["payers"]:
                bad.append("coverage order changed")
            continue
        if db is not None and step == "primary_eob":
            checked += 1
            if (cob.primary_eob(db, token) is not None) != st["present"]:
                bad.append("primary EOB on file changed")
            continue
        if "expr" in st:
            checked += 1
            got = evaluate(st["expr"], ev)
            if got != st["result"]:
                bad.append(f"{st['criterion']}: recorded {st['result']}, now {got}")
        elif "doc" in st and "rule" in st:
            checked += 1
            rows = g.query(HOP, {"id": case_id, "r": st["rule"], "cr": st["criterion"], "d": st["doc"]}).result_set
            if not rows:
                bad.append(f"no hop {st['rule']}->{st['criterion']}->{st['doc']}")
            elif any(r[0] for r in rows) != st["present"]:
                bad.append(f"{st['doc']}: recorded present={st['present']}, graph says {not st['present']}")
        elif st.get("step") == "procedure":
            checked += 1
            if not g.query(PROC, {"id": case_id, "p": st["id"], "v": st["version"]}).result_set:
                bad.append(f"no procedure {st['id']} v{st['version']}")
    return {"id": case_id, "ok": not bad and checked > 0, "checked": checked,
            "mismatches": bad, "cypher": BROWSER.format(id=case_id)}


def sample(tenant, n=10, seed=42):
    """Pick n agent-decided cases at random (fixed seed) and verify each."""
    db = client()
    g = tenant_graph(db, tenant, bound_tenant=tenant)
    ids = [r[0] for r in g.query("""MATCH (c:Case)-[k:DECIDED]->(:Decision)
                                    WHERE k.path_json IS NOT NULL AND k.path_json <> '[]'
                                    RETURN c.id ORDER BY c.id""").result_set]
    picked = random.Random(seed).sample(ids, min(n, len(ids)))
    res = [verify(g, i, db) for i in picked]
    return {"tenant": tenant, "reproduced": sum(r["ok"] for r in res), "of": len(res), "cases": res}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    for t in TENANTS:
        out = sample(t, a.n, a.seed)
        print(f"{t}: {out['reproduced']}/{out['of']} reproducible")
        for r in out["cases"]:
            if not r["ok"]:
                print("  ", r["id"], r["mismatches"])
    if out["cases"]:
        print("\nBrowser, e.g.:\n" + out["cases"][0]["cypher"])
