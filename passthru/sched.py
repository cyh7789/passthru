"""Deadline-aware queue. Deadlines come from the payer node, so each tenant keeps its own clock."""
import time

H = 3600 * 1000


def sla_hours(payer, kind, urgent):
    if kind == "discharge":
        return payer.get("sla_discharge_h", payer["sla_std_h"])
    return payer["sla_urgent_h"] if urgent else payer["sla_std_h"]


def deadline_ms(payer, kind, urgent, received_ms):
    return received_ms + int(sla_hours(payer, kind, urgent) * H)


def now_ms():
    return int(time.time() * 1000)


def next_cases(g, n=None):
    q = """MATCH (c:Case {status:'queued'}) RETURN c.id, c.version, c.deadline
           ORDER BY coalesce(c.at_risk, false) DESC, c.deadline, c.id"""
    rows = g.query(q).result_set
    return rows[:n] if n else rows


def reclaim_stale(g, older_than_ms=60_000, now=None):
    # a worker that died mid-case shouldn't sit on it; bump version so its late write loses the CAS
    now = now or now_ms()
    r = g.query("""MATCH (c:Case {status:'claimed'}) WHERE c.claimed_at < $cutoff
                   SET c.status='queued', c.claimed_by=null, c.version=c.version+1
                   RETURN count(c)""", {"cutoff": now - older_than_ms})
    return r.result_set[0][0]


def sweep(g, now=None):
    """Flag cases close to their deadline, then deal with the ones past it.
    The payer decides what overdue means: Acme just records a breach, Bharat auto-approves."""
    now = now or now_ms()
    p = g.query("MATCH (p:Payer) RETURN properties(p)").result_set[0][0]
    pct = p.get("at_risk_pct", 0.25)
    flagged = g.query("""MATCH (c:Case) WHERE c.status IN ['queued','claimed'] AND NOT coalesce(c.at_risk, false)
                           AND c.deadline - $now <= (c.deadline - c.received_at) * $pct
                         SET c.at_risk=true RETURN count(c)""", {"now": now, "pct": pct}).result_set[0][0]
    if p.get("on_overdue") == "auto_approve":
        # version bump so a worker still holding the claim loses its write
        overdue = g.query("""MATCH (c:Case) WHERE c.status IN ['queued','claimed'] AND c.deadline < $now
                             SET c.status='decided', c.breached=true, c.version=c.version+1
                             CREATE (c)-[:DECIDED {by:'scheduler', at:$now, path_json:$path}]->
                                    (:Decision {outcome:'approve', reason:'deadline passed, payer policy auto-approves'})
                             RETURN count(c)""",
                          {"now": now, "path": '[{"step": "overdue_auto_approve"}]'}).result_set[0][0]
    else:
        overdue = g.query("""MATCH (c:Case) WHERE c.status IN ['queued','claimed'] AND c.deadline < $now
                               AND NOT coalesce(c.breached, false)
                             SET c.breached=true RETURN count(c)""", {"now": now}).result_set[0][0]
    return {"at_risk": flagged, "overdue": overdue}
