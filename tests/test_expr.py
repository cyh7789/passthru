import json, pytest
from passthru import load, flow
from passthru.db import client
from passthru.expr import evaluate


def test_expr_basics():
    ev = {"pt_record": {"weeks": 7, "improved": False}, "estimate": {"amount": 12000}, "id_proof": {"verified": True}}
    assert evaluate("pt_record.weeks >= 6", ev) is True
    assert evaluate("pt_record.weeks >= 4 and not pt_record.improved", ev) is True
    assert evaluate("estimate.amount <= 10000 and id_proof.verified", ev) is False
    assert evaluate("neuro_exam.deficit", ev) is None  # no evidence -> unknown, not false


def test_expr_rejects_code():
    for bad in ["__import__('os').system('true')", "pt_record.weeks.__class__()", "[1][0]"]:
        with pytest.raises(ValueError):
            evaluate(bad, {"pt_record": {"weeks": 1}})


def test_failed_criterion_goes_to_human_not_deny():
    load.main(reset=True)
    g = client().select_graph("tenant_us_acme")
    flow.run("tenant_us_acme")
    assert g.query("MATCH (x:Decision {outcome:'deny'}) RETURN count(x)").result_set[0][0] == 0
    # proposed denies sit in needs_review with no Decision yet
    rows = g.query("""MATCH (c:Case {status:'needs_review'})
                      WHERE c.review_reason STARTS WITH 'proposed deny'
                      RETURN c.review_reason, c.path_json""").result_set
    assert rows
    for reason, p in rows:
        assert reason.startswith("proposed deny")
        assert any(s.get("result") is False for s in json.loads(p))
    # approvals really passed every criterion
    for (p,) in g.query("""MATCH (c:Case)-[d:DECIDED]->(x:Decision {outcome:'approve'})
                           WHERE x.reason = 'all criteria met' RETURN d.path_json""").result_set:
        assert all(s["result"] is True for s in json.loads(p) if "result" in s)
