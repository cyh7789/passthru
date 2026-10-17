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
    met, late = g.query("""MATCH (c:Case)-[k:DECIDED]->(:Decision)
                           RETURN sum(CASE WHEN k.at <= c.deadline THEN 1 ELSE 0 END),
                                  sum(CASE WHEN k.at > c.deadline THEN 1 ELSE 0 END)""").result_set[0]
    met, late = int(met or 0), int(late or 0)
    twice = g.query("""MATCH (c:Case)-[d:DECIDED]->() WITH c, count(d) AS n WHERE n > 1
                       RETURN count(c)""").result_set[0][0]
    refused = g.query("""MATCH (a:AccessDenied) RETURN a.asked, count(a), max(a.at)""").result_set
    return {
        "payer": dict(zip(["id", "country", "sla_std_h", "sla_urgent_h"], payer)),
        "status": status,
        "outcomes": outcomes,
        "at_risk": flags[0] or 0,
        "breached": flags[1] or 0,
        "next_deadlines": [{"id": i, "status": s, "ms_left": d - now, "at_risk": r} for i, s, d, r in open_],
        "workers_busy": workers,
        "decided_twice": twice,
        "refused": {"count": sum(n for _, n, _ in refused), "asked": {a: n for a, n, _ in refused},
                    "last_at": max((at for *_, at in refused), default=None)},
        "sla": {"open": sum(v for k, v in status.items() if k in ("queued", "claimed", "needs_review")),
                "met": met, "late": late,
                "met_pct": round(100 * met / (met + late), 1) if met + late else None,
                "soonest_ms": (open_[0][2] - now) if open_ else None},
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


def procedure_timeline(tenant, service, db=None):
    """Every version of a service's procedure, oldest first, with what each one added."""
    db = db or client()
    g = tenant_graph(db, tenant, bound_tenant=tenant)
    rows = g.query("""MATCH (p:Procedure)-[:FOR_SERVICE]->(:Service {code:$code})
                      OPTIONAL MATCH (p)-[st:STEP]->(a:Action)
                      OPTIONAL MATCH (p)-[:FIXES]->(dr:DenialReason)
                      OPTIONAL MATCH (p)-[:LEARNED_FROM]->(c:Case)
                      RETURN p.id, p.version, p.active, p.created_by, p.created_at, p.success_rate,
                             dr.code, c.id, st.order, a.name, st.doc, st.field, st.at_least
                      ORDER BY p.version, st.order""", {"code": service}).result_set
    versions = {}
    for pid, v, act, by, at, rate, fix, case, order, action, doc, field, need in rows:
        cur = versions.setdefault(v, {"id": pid, "version": v, "active": bool(act), "created_by": by,
                                      "created_at": at, "success_rate": rate, "fixes": fix,
                                      "learned_from": case, "steps": []})
        if action and not any(s["order"] == order for s in cur["steps"]):
            cur["steps"].append({"order": order, "action": action, "doc": doc or "",
                                 "field": field or "", "at_least": need or 0})
    out, prev = [], []
    for v in sorted(versions):
        cur = versions[v]
        key = lambda s: (s["action"], s["doc"], s["field"], s["at_least"])
        had = {key(s) for s in prev}
        now = {key(s) for s in cur["steps"]}
        cur["added"] = [s for s in cur["steps"] if key(s) not in had]
        cur["removed"] = [s for s in prev if key(s) not in now]
        out.append(cur)
        prev = cur["steps"]
    return out
