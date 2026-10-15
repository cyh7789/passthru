"""Minimal single-worker flow: claim -> precheck -> rule match -> decision with query path.
Rules only for now. LLM hook goes in later (D1/D2)."""
import json, time
from .db import client, tenant_graph
from .sched import next_cases

CLAIM = """MATCH (c:Case {id:$id, status:'queued', version:$v})
           SET c.status='claimed', c.claimed_by=$agent, c.claimed_at=timestamp(), c.version=$v+1
           RETURN c.id"""

# the multi-hop: case -> service <- rule -> criterion -> required doc
REQS = """MATCH (c:Case {id:$id})-[:FOR_SERVICE]->(s:Service)<-[:APPLIES_TO]-(r:Rule)
                -[:HAS_CRITERION]->(cr:Criterion)-[:NEEDS_DOC]->(d:DocRequirement)
          RETURN r.id, cr.id, d.type"""

AUTO = """MATCH (c:Case {id:$id})-[:FOR_SERVICE]->(s:Service)-[:AUTO_APPROVE_LIST]->(p:Payer)
          RETURN s.code"""


def claim(g, case_id, version, agent):
    return bool(g.query(CLAIM, {"id": case_id, "v": version, "agent": agent}).result_set)


def decide(g, case_id):
    c = g.query("MATCH (c:Case {id:$id}) RETURN c.submitted_docs", {"id": case_id}).result_set[0]
    have = set(c[0] or [])
    if g.query(AUTO, {"id": case_id}).result_set:
        return "approve", "auto-approve list", [{"step": "auto_approve_list"}]
    rows = g.query(REQS, {"id": case_id}).result_set
    if not rows:
        return "needs_review", "no matching rule", []
    path = [{"rule": r, "criterion": cr, "doc": d, "present": d in have} for r, cr, d in rows]
    missing = sorted({p["doc"] for p in path if not p["present"]})
    if missing:
        # missing docs = pend with a list, never an auto deny
        return "pend", "missing: " + ", ".join(missing), path
    return "approve", "all criteria evidenced", path


def record(g, case_id, outcome, reason, path, agent):
    g.query("""MATCH (c:Case {id:$id}) SET c.status='decided'
               CREATE (c)-[:DECIDED {by:$agent, at:timestamp(), path_json:$path}]->(:Decision {outcome:$o, reason:$r})""",
            {"id": case_id, "agent": agent, "path": json.dumps(path), "o": outcome, "r": reason})


def run(tenant, agent="worker-1", limit=None):
    g = tenant_graph(client(), tenant, bound_tenant=tenant)
    stats = {}
    t0 = time.time()
    for case_id, v, _ in next_cases(g, limit):
        if not claim(g, case_id, v, agent):
            stats["conflict"] = stats.get("conflict", 0) + 1
            continue
        outcome, reason, path = decide(g, case_id)
        record(g, case_id, outcome, reason, path, agent)
        stats[outcome] = stats.get(outcome, 0) + 1
    stats["secs"] = round(time.time() - t0, 2)
    return stats


if __name__ == "__main__":
    for t in ("tenant_us_acme", "tenant_in_bharat"):
        print(t, run(t))
