"""Blind human review queue. Agents never deny; reviewers do, without seeing PII."""
import json
from .db import client, tenant_graph

# blind shape only — no patient_token join out to provider_demo for names/facilities
LIST = """MATCH (c:Case {status:'needs_review'})-[:FOR_SERVICE]->(s:Service)
          RETURN c.id, s.code, c.review_reason, c.path_json, c.deadline, c.claimed_by, c.version
          ORDER BY c.deadline, c.id"""


def list_queue(tenant):
    """Cases waiting on a human, stripped of anything that could identify a patient."""
    g = tenant_graph(client(), tenant, bound_tenant=tenant)
    out = []
    for cid, code, reason, path, deadline, claimed_by, version in g.query(LIST).result_set:
        item = {
            "id": cid,
            "service": code,
            "reason": reason,
            "proposed_outcome": "deny" if reason and reason.startswith("proposed deny") else "review",
            "deadline": deadline,
            "claimed_by": claimed_by,
            "version": version,
        }
        if path:
            try:
                item["path_summary"] = json.loads(path)
            except (TypeError, json.JSONDecodeError):
                item["path_summary"] = path
        out.append(item)
    return out


def claim_for_review(case_id, reviewer_pseudonym, tenant):
    """CAS grab of a needs_review case. Clears any prior agent claim fields."""
    g = tenant_graph(client(), tenant, bound_tenant=tenant)
    row = g.query("MATCH (c:Case {id:$id, status:'needs_review'}) RETURN c.version, c.claimed_by",
                  {"id": case_id}).result_set
    if not row:
        return False
    v, who = row[0]
    if who and who != reviewer_pseudonym:
        return False
    r = g.query("""MATCH (c:Case {id:$id, status:'needs_review', version:$v})
                   WHERE c.claimed_by IS NULL OR c.claimed_by=$who
                   SET c.claimed_by=$who, c.claimed_at=timestamp(), c.version=$v+1
                   RETURN c.id""",
                {"id": case_id, "v": v, "who": reviewer_pseudonym})
    return bool(r.result_set)


def submit_review(case_id, reviewer_pseudonym, outcome, reason, tenant):
    """Human signs off. Deny is only allowed here. Creates Decision + REVIEWED{blind:true}."""
    if outcome not in ("approve", "deny"):
        raise ValueError(f"review outcome must be approve|deny, got {outcome!r}")
    g = tenant_graph(client(), tenant, bound_tenant=tenant)
    row = g.query("""MATCH (c:Case {id:$id, status:'needs_review', claimed_by:$who})
                     RETURN c.version, c.path_json""",
                  {"id": case_id, "who": reviewer_pseudonym}).result_set
    if not row:
        return False
    v, path = row[0]
    path = path or "[]"
    r = g.query("""MATCH (c:Case {id:$id, status:'needs_review', claimed_by:$who, version:$v})
                   SET c.status='decided', c.version=$v+1, c.review_reason=null
                   CREATE (c)-[:DECIDED {by:$who, at:timestamp(), path_json:$path}]->
                          (:Decision {outcome:$o, reason:$r})
                   MERGE (rev:Reviewer {pseudonym:$who})
                   CREATE (rev)-[:REVIEWED {blind:true}]->(c)
                   RETURN c.id""",
                {"id": case_id, "who": reviewer_pseudonym, "v": v,
                 "path": path, "o": outcome, "r": reason})
    return bool(r.result_set)
