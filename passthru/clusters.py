"""Denial/pend reason clusters per tenant, via FalkorDB label propagation.

Each non-approved case gets GAP edges to what held it up (missing DocRequirement or failed
Criterion, read from the path the agent stored). algo.labelPropagation over Case+gap nodes
groups cases stuck on the same things; the biggest / most at-risk group says which
procedure to fix first.
"""
import json
from collections import Counter
from .db import client, tenant_graph

PATHS = """MATCH (c:Case)
           OPTIONAL MATCH (c)-[k:DECIDED]->(d:Decision)
           WITH c, k, d WHERE c.status = 'needs_review' OR d.outcome IN ['pend', 'deny']
           RETURN c.id, coalesce(k.path_json, c.path_json)"""

LINK_DOCS = """UNWIND $rows AS row
               MATCH (c:Case {id:row.case})
               UNWIND row.docs AS dt
               MATCH (d:DocRequirement {type:dt})
               CREATE (c)-[:GAP {kind:'missing'}]->(d)"""

LINK_CRITS = """UNWIND $rows AS row
                MATCH (c:Case {id:row.case})
                UNWIND row.crits AS cid
                MATCH (cr:Criterion {id:cid})
                CREATE (c)-[:GAP {kind:'failed'}]->(cr)"""

LP = """CALL algo.labelPropagation({nodeLabels:['Case','DocRequirement','Criterion'],
                                 relationshipTypes:['GAP'], maxIterations:20})
        YIELD node, communityId
        WITH node, communityId WHERE node:Case AND (node)-[:GAP]->()
        MATCH (node)-[:FOR_SERVICE]->(s:Service)
        OPTIONAL MATCH (node)-[:GAP]->(x)
        RETURN communityId, node.id, s.code, coalesce(node.at_risk, false),
               collect(coalesce(x.type, x.id))"""


def gaps(path):
    docs, crits = set(), set()
    for step in path or []:
        if not isinstance(step, dict):
            continue
        if step.get("doc") and step.get("present") is False:
            docs.add(step["doc"])
        if step.get("criterion") and step.get("result") is False:
            crits.add(step["criterion"])
    return sorted(docs), sorted(crits)


def build(g):
    """Rebuild GAP edges from stored paths. Returns how many cases got linked."""
    g.query("MATCH (:Case)-[e:GAP]->() DELETE e")
    rows = []
    for cid, raw in g.query(PATHS).result_set:
        try:
            path = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        docs, crits = gaps(path)
        if docs or crits:
            rows.append({"case": cid, "docs": docs, "crits": crits})
    for i in range(0, len(rows), 200):
        g.query(LINK_DOCS, {"rows": rows[i:i + 200]})
        g.query(LINK_CRITS, {"rows": rows[i:i + 200]})
    return len(rows)


def clusters(g, rebuild=True, top=6):
    if rebuild:
        build(g)
    groups = {}
    for comm, cid, svc, risk, gap in g.query(LP).result_set:
        c = groups.setdefault(comm, {"cases": 0, "at_risk": 0, "services": Counter(), "gaps": Counter(), "pairs": Counter()})
        c["cases"] += 1
        c["at_risk"] += int(bool(risk))
        c["services"][svc] += 1
        for x in gap:
            c["gaps"][x] += 1
            c["pairs"][(svc, x)] += 1
    out = []
    for comm, c in groups.items():
        (svc, gap), hits = c["pairs"].most_common(1)[0]
        out.append({
            "community": comm,
            "cases": c["cases"],
            "at_risk": c["at_risk"],
            "gaps": dict(c["gaps"].most_common()),
            "services": dict(c["services"].most_common()),
            "fix_first": {"procedure": f"proc-{svc}", "service": svc, "gap": gap, "cases": hits},
        })
    out.sort(key=lambda c: (c["at_risk"], c["cases"]), reverse=True)
    return out[:top]


def tenant_clusters(tenant, db=None, top=6):
    db = db or client()
    return clusters(tenant_graph(db, tenant, bound_tenant=tenant), top=top)


if __name__ == "__main__":
    from .db import TENANTS
    for t in TENANTS:
        cs = tenant_clusters(t)
        if not cs:
            print(f"{t}: nothing stuck")
            continue
        for c in cs:
            ff = c.get("fix_first") or {}
            print(f"{t}: {c['cases']} cases · at_risk {c.get('at_risk', 0)} · "
                  f"chase {ff.get('gap')} on {ff.get('service')} ({ff.get('cases')} cases)")
