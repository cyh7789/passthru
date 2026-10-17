"""Read-only numbers for the dashboard. One query set per tenant graph, nothing crosses graphs."""
import json
from .db import client, tenant_graph, TENANTS
from .sched import now_ms


def tenant_snapshot(g, now=None, soonest=5):
    now = now or now_ms()
    status = dict(g.query("MATCH (c:Case) RETURN c.status, count(c)").result_set)
    outcomes = dict(g.query("MATCH (:Case)-[:DECIDED]->(d:Decision) RETURN d.outcome, count(d)").result_set)
    flags = g.query("""MATCH (c:Case) RETURN sum(CASE WHEN coalesce(c.at_risk,false) THEN 1 ELSE 0 END),
                              sum(CASE WHEN coalesce(c.breached,false) THEN 1 ELSE 0 END)""").result_set[0]
    open_ = g.query("""MATCH (c:Case) WHERE c.status IN ['queued','claimed','needs_review']
                       RETURN c.id, c.status, c.deadline, coalesce(c.at_risk,false)
                       ORDER BY c.deadline LIMIT $n""", {"n": soonest}).result_set
    workers = dict(g.query("""MATCH (c:Case {status:'claimed'}) RETURN c.claimed_by, count(c)""").result_set)
    procs = g.query("""MATCH (p:Procedure)-[:FOR_SERVICE]->(s:Service)
                       RETURN p.id, s.code, p.version, p.active, p.success_rate ORDER BY p.id, p.version""").result_set
    payer = g.query("MATCH (p:Payer) RETURN p.id, p.country, p.sla_std_h, p.sla_urgent_h").result_set[0]
    return {
        "payer": dict(zip(["id", "country", "sla_std_h", "sla_urgent_h"], payer)),
        "status": status,
        "outcomes": outcomes,
        "at_risk": flags[0] or 0,
        "breached": flags[1] or 0,
        "next_deadlines": [{"id": i, "status": s, "ms_left": d - now, "at_risk": r} for i, s, d, r in open_],
        "workers_busy": workers,
        "procedures": [dict(zip(["id", "service", "version", "active", "success_rate"], p)) for p in procs],
    }


def snapshot(db=None, now=None):
    db = db or client()
    now = now or now_ms()
    return {"at": now, "tenants": {t: tenant_snapshot(tenant_graph(db, t, bound_tenant=t), now) for t in TENANTS}}


HOPS = """MATCH (c:Case {id:$id})-[:FOR_SERVICE]->(s:Service)
           OPTIONAL MATCH (s)<-[:APPLIES_TO]-(r:Rule)-[:HAS_CRITERION]->(cr:Criterion)
                            -[:NEEDS_DOC]->(d:DocRequirement)
           OPTIONAL MATCH (c)-[:HAS_EVIDENCE]->(e:Evidence)-[:SATISFIES]->(d)
           RETURN s.code, r.id, cr.id, cr.expr, d.type, e.data IS NOT NULL
           ORDER BY r.id, cr.id, d.type"""


def case_path(tenant, case_id, db=None):
    """One case: status, decision, stored path_json, and the live Rule→Criterion→Doc hops."""
    db = db or client()
    g = tenant_graph(db, tenant, bound_tenant=tenant)
    rows = g.query("""MATCH (c:Case {id:$id})
                      OPTIONAL MATCH (c)-[k:DECIDED]->(d:Decision)
                      RETURN c.status, c.review_reason, coalesce(k.path_json, c.path_json),
                             d.outcome, d.reason, k.by""",
                   {"id": case_id}).result_set
    if not rows:
        return None
    status, review_reason, path_raw, outcome, reason, by = rows[0]
    hops = [{"service": s, "rule": r, "criterion": cr, "expr": expr, "doc": doc, "present": bool(present)}
            for s, r, cr, expr, doc, present in g.query(HOPS, {"id": case_id}).result_set
            if r is not None]
    path = None
    if path_raw:
        try:
            path = json.loads(path_raw) if isinstance(path_raw, str) else path_raw
        except (TypeError, ValueError):
            path = path_raw
    chase_steps = [s for s in (path or []) if isinstance(s, dict) and s.get("step") == "chase"]
    if not chase_steps:
        missing = sorted({h["doc"] for h in hops if h.get("doc") and not h.get("present")})
        if missing:
            from . import chase
            chase_steps = chase.chase_missing(g, case_id, missing)
    return {
        "tenant": tenant,
        "id": case_id,
        "status": status,
        "outcome": outcome,
        "reason": reason or review_reason,
        "decided_by": by,
        "path": path,
        "hops": hops,
        "chase": chase_steps,
    }
