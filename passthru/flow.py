"""Minimal single-worker flow: claim -> cob -> procedure -> precheck -> rule match -> decision with query path.
Rules only for now. LLM hook goes in later (D1/D2)."""
import json, time
from .db import client, tenant_graph
from .sched import next_cases, sweep
from .expr import evaluate
from . import cob, procedure, chase

CLAIM = """MATCH (c:Case {id:$id, status:'queued', version:$v})
           SET c.status='claimed', c.claimed_by=$agent, c.claimed_at=timestamp(), c.version=$v+1
           RETURN c.id"""

# the multi-hop: case -> service <- rule -> criterion -> required doc <- submitted evidence
REQS = """MATCH (c:Case {id:$id})-[:FOR_SERVICE]->(s:Service)<-[:APPLIES_TO]-(r:Rule)
                -[:HAS_CRITERION]->(cr:Criterion)-[:NEEDS_DOC]->(d:DocRequirement)
          OPTIONAL MATCH (c)-[:HAS_EVIDENCE]->(e:Evidence)-[:SATISFIES]->(d)
          RETURN r.id, cr.id, cr.expr, d.type, e.data ORDER BY r.id, cr.id, d.type"""

AUTO = """MATCH (c:Case {id:$id})-[:FOR_SERVICE]->(s:Service)-[:AUTO_APPROVE_LIST]->(p:Payer)
          RETURN s.code"""


def claim(g, case_id, version, agent):
    return bool(g.query(CLAIM, {"id": case_id, "v": version, "agent": agent}).result_set)


def decide(g, case_id, db=None, tenant=None):
    path = []
    if db is not None:
        token = g.query("MATCH (c:Case {id:$id}) RETURN c.patient_token", {"id": case_id}).result_set[0][0]
        action, reason, cob_path = cob.route(db, token, intended=tenant)
        path.extend(cob_path)
        if action == "hold":
            return "pend", reason, path
    if g.query(AUTO, {"id": case_id}).result_set:
        return "approve", "auto-approve list", path + [{"step": "auto_approve_list"}]
    act, why, proc_path = procedure.apply(g, db, case_id)
    path.extend(proc_path)
    if act:
        return act, why, path
    rows = g.query(REQS, {"id": case_id}).result_set
    if not rows:
        return "needs_review", "no matching rule", path
    crit, evidence_path = {}, []
    for r, cr, expr, d, data in rows:
        crit.setdefault((r, cr, expr), {})
        if data is not None:
            crit[(r, cr, expr)][d] = json.loads(data)
        evidence_path.append({"rule": r, "criterion": cr, "doc": d, "present": data is not None})
    path.extend(evidence_path)
    missing = sorted({p["doc"] for p in evidence_path if not p["present"]})
    if missing:
        # missing docs = pend with a list, never an auto deny
        # shortest-path chase via FalkorDB algo.SPpaths (falls back to list alone if algo empty)
        path.extend(chase.chase_missing(g, case_id, missing))
        return "pend", "missing: " + ", ".join(missing), path
    results = {}
    for (r, cr, expr), evidence in crit.items():
        results[cr] = evaluate(expr, evidence)
        path.append({"rule": r, "criterion": cr, "expr": expr, "result": results[cr]})
    failed = sorted(cr for cr, ok in results.items() if ok is False)
    if failed:
        # agents don't get to deny on their own, a human signs off
        return "needs_review", "proposed deny: fails " + ", ".join(failed), path
    if any(ok is None for ok in results.values()):
        return "needs_review", "evidence can't answer every criterion", path
    return "approve", "all criteria met", path


def record(g, case_id, version, outcome, reason, path, agent):
    # only lands if we still hold the claim (scheduler may have reclaimed or auto-approved it)
    if outcome == "needs_review":
        # park for a human; no Decision yet, clear agent claim so a reviewer can grab it
        r = g.query("""MATCH (c:Case {id:$id, status:'claimed', claimed_by:$agent, version:$v})
                       SET c.status='needs_review', c.version=$v+1,
                           c.review_reason=$r, c.path_json=$path,
                           c.claimed_by=null, c.claimed_at=null
                       RETURN c.id""",
                    {"id": case_id, "v": version, "agent": agent,
                     "path": json.dumps(path), "r": reason})
        return bool(r.result_set)
    r = g.query("""MATCH (c:Case {id:$id, status:'claimed', claimed_by:$agent, version:$v})
                   SET c.status='decided', c.version=$v+1
                   CREATE (c)-[:DECIDED {by:$agent, at:timestamp(), path_json:$path}]->(:Decision {outcome:$o, reason:$r})
                   RETURN c.id""",
                {"id": case_id, "v": version, "agent": agent, "path": json.dumps(path), "o": outcome, "r": reason})
    return bool(r.result_set)


def run(tenant, agent="worker-1", limit=None):
    db = client()
    g = tenant_graph(db, tenant, bound_tenant=tenant)
    stats = {}
    t0 = time.time()
    stats.update({f"sweep_{k}": n for k, n in sweep(g).items()})
    for case_id, v, _ in next_cases(g, limit):
        if not claim(g, case_id, v, agent):
            stats["conflict"] = stats.get("conflict", 0) + 1
            continue
        outcome, reason, path = decide(g, case_id, db=db, tenant=tenant)
        if not record(g, case_id, v + 1, outcome, reason, path, agent):
            stats["lost"] = stats.get("lost", 0) + 1
            continue
        stats[outcome] = stats.get(outcome, 0) + 1
    stats["secs"] = round(time.time() - t0, 2)
    return stats


if __name__ == "__main__":
    for t in ("tenant_us_acme", "tenant_in_bharat"):
        print(t, run(t))
