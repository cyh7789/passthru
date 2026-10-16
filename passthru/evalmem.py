"""Does procedural memory actually help? Spec claim #1, measured on synthetic cases.

80 Acme lumbar MRI requests from one seed. Each package was put together at some point in
PT (2-8 weeks); the clinic chart may have moved on since (+0-5 weeks). Batch A (40) runs
on v1, a blind reviewer denies one of A's proposed denies, that becomes v2, then batch B
(the other 40, never touched) runs on v2. We report first-pass approvals with denominators,
plus how many in each batch could have passed at all (chart >= 6 weeks) so the ceiling is visible.
"""
import argparse, json, random
from . import load, flow, review, procedure
from .db import client, tenant_graph

US = "tenant_us_acme"
SERVICE = "MRI-LSPINE"
REVIEWER = "rev-eval"
FIX = {"action": "chase_doc", "doc": "pt_record", "field": "weeks", "at_least": 6}


def make_cases(seed, n=80):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        sub = rng.randint(2, 8)
        out.append({"id": f"eval-{i:02d}", "submitted_weeks": sub, "chart_weeks": sub + rng.randint(0, 5)})
    rng.shuffle(out)
    return out[: n // 2], out[n // 2:]


def _docs(weeks):
    return [{"type": "clinic_note", "date": "2026-09-02", "facts": {"indication": True}},
            {"type": "neuro_exam", "date": "2026-09-02", "facts": {"deficit": True}},
            {"type": "pt_record", "date": "2026-09-20", "facts": {"weeks": weeks, "improved": False}}]


def seed_cases(db, cases):
    load.load_provider(db, [{"id": f"p-{c['id']}", "name": "Eval Synthetic", "birth_year": 1970,
                             "country": "US", "documents": _docs(c["chart_weeks"]),
                             "coverage": [{"order": 1, "plan": f"{US}:basic", "tenant": US}]}
                            for c in cases])
    load.load_cases(db, [{"id": c["id"], "tenant": US, "kind": "pa",
                          "patient": {"reference": f"Patient/p-{c['id']}"},
                          "priority": {"coding": [{"code": "normal"}]},
                          "item": [{"productOrService": {"coding": [{"code": SERVICE}]}}],
                          "supportingInfo": [{"category": d["type"], "value": d["facts"]}
                                             for d in _docs(c["submitted_weeks"])]}
                         for c in cases])


def run_batch(db, g, cases, agent):
    res = {}
    for c in cases:
        v = g.query("MATCH (c:Case {id:$id}) RETURN c.version", {"id": c["id"]}).result_set[0][0]
        if not flow.claim(g, c["id"], v, agent):
            raise RuntimeError(f"{c['id']} already claimed")
        outcome, reason, path = flow.decide(g, c["id"], db=db, tenant=US)
        flow.record(g, c["id"], v + 1, outcome, reason, path, agent)
        res[c["id"]] = outcome
    counts = {k: sum(1 for o in res.values() if o == k) for k in ("approve", "pend", "needs_review")}
    return {"n": len(cases), "first_pass_approve": counts["approve"],
            "could_pass": sum(1 for c in cases if c["chart_weeks"] >= 6), **counts}, res


def run(seed=42, reset=True):
    if reset:
        load.main(reset=True)
    db = client()
    g = tenant_graph(db, US, bound_tenant=US)
    a, b = make_cases(seed)
    seed_cases(db, a + b)
    v1 = procedure.active(g, SERVICE)["version"]
    sa, out_a = run_batch(db, g, a, "eval-v1")
    # one human fix, taken from batch A
    target = next((cid for cid, o in out_a.items() if o == "needs_review"), None)
    if target is None:
        raise RuntimeError("batch A had nothing for a reviewer to fix, try another seed")
    review.claim_for_review(target, REVIEWER, US)
    review.submit_review(target, REVIEWER, "deny", "MISSING_PT_RECORD: needs 6 weeks conservative therapy", US)
    learned = procedure.learn_from_review(target, REVIEWER, "MISSING_PT_RECORD", FIX, US)
    sb, _ = run_batch(db, g, b, "eval-v2")
    return {"seed": seed, "service": SERVICE, "tenant": US,
            "v1": {"version": v1, **sa}, "fixed_from": target, "learned": learned,
            "v2": {"version": learned["version"], **sb},
            "note": "synthetic cases, dev-time check, not a clinical or payer result"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    print(json.dumps(run(ap.parse_args().seed), indent=1))
