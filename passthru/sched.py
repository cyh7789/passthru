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
    q = "MATCH (c:Case {status:'queued'}) RETURN c.id, c.version, c.deadline ORDER BY c.deadline, c.id"
    rows = g.query(q).result_set
    return rows[:n] if n else rows


def reclaim_stale(g, older_than_ms=60_000, now=None):
    # a worker that died mid-case shouldn't sit on it; bump version so its late write loses the CAS
    now = now or now_ms()
    r = g.query("""MATCH (c:Case {status:'claimed'}) WHERE c.claimed_at < $cutoff
                   SET c.status='queued', c.claimed_by=null, c.version=c.version+1
                   RETURN count(c)""", {"cutoff": now - older_than_ms})
    return r.result_set[0][0]
