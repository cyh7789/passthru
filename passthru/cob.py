"""Coordination of benefits: walk coverage order on the provider graph.

Primary payer first. Secondary only after a primary EOB (paid or denied) is on file.
"""
from .db import PROVIDER

ORDER = """MATCH (p:Patient {id:$id})-[h:HAS_COVERAGE]->(pl:Plan)-[:OF]->(r:PayerRef)
           RETURN h.order, r.tenant, pl.id ORDER BY h.order, r.tenant"""

EOB = """MATCH (p:Patient {id:$id})-[:HAS_DOC]->(d:Document {type:'primary_eob'})
         RETURN d.id, d.outcome, d.paid_amount LIMIT 1"""


def coverage(db, patient_id):
    rows = db.select_graph(PROVIDER).query(ORDER, {"id": patient_id}).result_set
    return [{"order": o, "tenant": t, "plan": pl} for o, t, pl in rows]


def primary_eob(db, patient_id):
    rows = db.select_graph(PROVIDER).query(EOB, {"id": patient_id}).result_set
    if not rows:
        return None
    _, outcome, amount = rows[0]
    return {"outcome": outcome, "paid_amount": amount}


def route(db, patient_id, intended=None):
    """Decide whether a claim can go to `intended` (or the primary, if omitted).

    Returns (action, reason, path) where action is 'submit' or 'hold'.
    """
    cov = coverage(db, patient_id)
    path = [{"step": "coverage_order", "payers": cov}]
    if not cov:
        return "hold", "no coverage on file", path
    primary = cov[0]
    target = intended or primary["tenant"]
    path.append({"step": "intended_payer", "tenant": target})
    if target == primary["tenant"]:
        note = "single coverage" if len(cov) == 1 else "primary first; secondary waiting on EOB"
        if len(cov) > 1 and primary_eob(db, patient_id):
            note = "primary first; secondary already has EOB on file"
        path.append({"step": "cob_ok", "submit_to": target})
        return "submit", note, path
    # secondary (or further) — need primary EOB first
    eob = primary_eob(db, patient_id)
    path.append({"step": "primary_eob", "present": eob is not None, "eob": eob})
    if eob is None:
        path.append({"step": "cob_hold", "blocked": target, "until": "primary_eob"})
        return "hold", f"need primary EOB before {target}", path
    path.append({"step": "cob_ok", "submit_to": target})
    return "submit", f"secondary after primary EOB ({eob['outcome']})", path
