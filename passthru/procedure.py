"""Procedures: how to prep a case for a payer before it goes out, versioned per service.

Agents only read the active version and apply its steps. A new version only comes from
a reviewer's deny (learn_from_review), and swapping active is a version CAS.
"""
import json
from .db import client, tenant_graph, PROVIDER

ACTIVE = """MATCH (s:Service {code:$code})<-[:FOR_SERVICE]-(p:Procedure {active:true})
            OPTIONAL MATCH (p)-[st:STEP]->(a:Action)
            RETURN p.id, p.version, st.order, a.name, st.doc, st.field, st.at_least
            ORDER BY st.order"""

# old must still be the active one at version $v, otherwise 0 rows = someone else got there
SUPERSEDE = """MATCH (s:Service {code:$code})<-[:FOR_SERVICE]-(old:Procedure {active:true, version:$v})
               SET old.active=false
               CREATE (new:Procedure {id:old.id, version:$v + 1, active:true,
                                      created_by:$who, created_at:timestamp()})
               CREATE (new)-[:SUPERSEDES]->(old), (new)-[:FOR_SERVICE]->(s)
               MERGE (dr:DenialReason {code:$fix})
               CREATE (new)-[:FIXES]->(dr)
               WITH new UNWIND $steps AS st
               MERGE (a:Action {name:st.action})
               CREATE (new)-[:STEP {order:st.order, doc:st.doc, field:st.field, at_least:st.at_least}]->(a)
               RETURN DISTINCT new.version"""


def active(g, code):
    rows = g.query(ACTIVE, {"code": code}).result_set
    if not rows:
        return None
    steps = [{"order": o, "action": a, "doc": d, "field": f, "at_least": m}
             for _, _, o, a, d, f, m in rows if a]
    return {"id": rows[0][0], "version": rows[0][1], "steps": steps}


def new_version(g, code, expected_version, steps, fixes, author):
    """Swap in a new active version. Returns the new version, or None if the CAS lost."""
    if not steps:
        raise ValueError("a procedure needs at least one step")
    steps = [{"order": i + 1, "action": s["action"], "doc": s.get("doc", ""),
              "field": s.get("field", ""), "at_least": s.get("at_least", 0)}
             for i, s in enumerate(steps)]
    r = g.query(SUPERSEDE, {"code": code, "v": expected_version, "steps": steps,
                            "fix": fixes, "who": author}).result_set
    return r[0][0] if r else None


def learn_from_review(case_id, reviewer, denial_code, step, tenant):
    """Learner path: a reviewer denied this case, turn their fix into the next procedure version.

    The new step goes first, the old steps follow. Only works off a blind reviewer's deny.
    """
    g = tenant_graph(client(), tenant, bound_tenant=tenant)
    row = g.query("""MATCH (:Reviewer {pseudonym:$who})-[:REVIEWED]->(c:Case {id:$id})
                           -[:DECIDED {by:$who}]->(:Decision {outcome:'deny'})
                     MATCH (c)-[:FOR_SERVICE]->(s:Service)
                     RETURN s.code""", {"id": case_id, "who": reviewer}).result_set
    if not row:
        raise PermissionError(f"{case_id}: no reviewer deny by {reviewer}, nothing to learn from")
    if not g.query("MATCH (d:DenialReason {code:$c}) RETURN d", {"c": denial_code}).result_set:
        raise ValueError(f"unknown denial reason {denial_code}")
    code = row[0][0]
    cur = active(g, code)
    v = new_version(g, code, cur["version"], [step] + cur["steps"], denial_code, reviewer)
    if v is None:
        return None
    g.query("""MATCH (p:Procedure {id:$pid, version:$v}), (c:Case {id:$id})
               CREATE (p)-[:LEARNED_FROM]->(c)""", {"pid": cur["id"], "v": v, "id": case_id})
    return {"id": cur["id"], "version": v, "supersedes": cur["version"]}


def _facts(data):
    return json.loads(data) if data else {}


def chase(g, db, case_id, token, st):
    """Make sure the case carries a doc that clears the bar; if the submitted one doesn't,
    pull the clinic's current copy from the provider graph and attach that instead."""
    doc, field, need = st["doc"], st["field"], st["at_least"]
    row = g.query("MATCH (:Case {id:$id})-[:HAS_EVIDENCE]->(e:Evidence {type:$t}) RETURN e.data",
                  {"id": case_id, "t": doc}).result_set
    had = _facts(row[0][0]).get(field) if row else None
    out = {"step": "chase_doc", "doc": doc, "field": field, "need": need, "had": had}
    if isinstance(had, (int, float)) and had >= need:
        return {**out, "ok": True, "source": "submitted"}
    found = None
    if db is not None and token:
        prow = db.select_graph(PROVIDER).query(
            "MATCH (:Patient {id:$p})-[:HAS_DOC]->(d:Document {type:$t}) RETURN d.data",
            {"p": token, "t": doc}).result_set
        found = _facts(prow[0][0]).get(field) if prow else None
    out["found"] = found
    if not (isinstance(found, (int, float)) and found >= need):
        return {**out, "ok": False}
    g.query("""MATCH (c:Case {id:$id})
               MERGE (e:Evidence {id:$eid})
               SET e.prev_data=e.data, e.data=$data, e.type=$t, e.source='chased:provider'
               MERGE (c)-[:HAS_EVIDENCE]->(e)
               MERGE (d:DocRequirement {type:$t}) MERGE (e)-[:SATISFIES]->(d)""",
            {"id": case_id, "eid": f"{case_id}:{doc}", "t": doc,
             "data": prow[0][0]})
    return {**out, "ok": True, "source": "chased"}


def apply(g, db, case_id):
    """Run the active procedure's steps on a claimed case.

    Returns (outcome, reason, path); outcome is None when the case should go on to the rules.
    """
    row = g.query("MATCH (c:Case {id:$id})-[:FOR_SERVICE]->(s:Service) RETURN s.code, c.patient_token",
                  {"id": case_id}).result_set
    if not row:
        return None, None, []
    code, token = row[0]
    proc = active(g, code)
    if not proc:
        return None, None, []
    g.query("MATCH (c:Case {id:$id}) SET c.procedure_id=$p, c.procedure_version=$v",
            {"id": case_id, "p": proc["id"], "v": proc["version"]})
    path = [{"step": "procedure", "id": proc["id"], "version": proc["version"]}]
    for st in proc["steps"]:
        if st["action"] != "chase_doc":
            path.append({"step": st["action"]})
            continue
        res = chase(g, db, case_id, token, st)
        path.append(res)
        if not res["ok"]:
            # still short after chasing = ask for it, don't send it out to get denied
            got = res.get("found") if res.get("found") is not None else res["had"]
            return "pend", (f"chase: {st['doc']}.{st['field']} {got} < {st['at_least']} "
                            f"(procedure v{proc['version']})"), path
    return None, None, path


def refresh_success_rate(g, code):
    """Learner bookkeeping: approve share of decided cases per version."""
    rows = g.query("""MATCH (:Service {code:$code})<-[:FOR_SERVICE]-(p:Procedure)
                      OPTIONAL MATCH (c:Case)-[:DECIDED]->(d:Decision)
                      WHERE c.procedure_id = p.id AND c.procedure_version = p.version
                      WITH p, count(d) AS n, sum(CASE WHEN d.outcome = 'approve' THEN 1 ELSE 0 END) AS ok
                      SET p.success_rate = CASE WHEN n = 0 THEN null ELSE toFloat(ok) / n END
                      RETURN p.version, p.active, n, ok, p.success_rate ORDER BY p.version""",
                   {"code": code}).result_set
    return [{"version": v, "active": a, "decided": n, "approved": int(ok), "success_rate": rate}
            for v, a, n, ok, rate in rows]
