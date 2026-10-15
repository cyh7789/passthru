from concurrent.futures import ThreadPoolExecutor
import pytest
from passthru import load, hero, procedure
from passthru.db import client

US = "tenant_us_acme"
STEP = hero.FIX


def _g():
    return client().select_graph(US)


def test_each_service_starts_on_v1():
    load.main(reset=True)
    p = procedure.active(_g(), "MRI-LSPINE")
    assert p["version"] == 1 and [s["action"] for s in p["steps"]] == ["precheck_required_docs"]


def test_new_version_cas_one_winner():
    load.main(reset=True)
    g = _g()
    with ThreadPoolExecutor(8) as ex:
        got = list(ex.map(lambda i: procedure.new_version(
            client().select_graph(US), "MRI-LSPINE", 1, [STEP], "MISSING_PT_RECORD", f"rev-{i}"), range(8)))
    assert sorted(got, key=str) == [2] + [None] * 7
    # stale version loses too
    assert procedure.new_version(g, "MRI-LSPINE", 1, [STEP], "MISSING_PT_RECORD", "late") is None
    row = g.query("""MATCH (s:Service {code:'MRI-LSPINE'})<-[:FOR_SERVICE]-(p:Procedure)
                     RETURN p.version, p.active ORDER BY p.version""").result_set
    assert row == [[1, False], [2, True]]
    assert g.query("""MATCH (:Procedure {version:2})-[:SUPERSEDES]->(:Procedure {version:1})
                      MATCH (:Procedure {version:2})-[:FIXES]->(:DenialReason {code:'MISSING_PT_RECORD'})
                      RETURN count(*)""").result_set[0][0] == 1


def test_learner_only_learns_from_reviewer_deny():
    load.main(reset=True)
    hero.seed(client())
    # case exists but nobody denied it
    with pytest.raises(PermissionError):
        procedure.learn_from_review(hero.case_id(US), hero.REVIEWER, "MISSING_PT_RECORD", STEP, US)
    assert procedure.active(_g(), "MRI-LSPINE")["version"] == 1


def test_without_v2_next_case_would_go_to_deny():
    load.main(reset=True)
    db = client()
    hero.seed_next(db)
    out = hero.process(db, hero.NEXT)
    assert out["agent"] == "needs_review" and "US-MRI-1a" in out["reason"]
    assert out["procedure"]["version"] == 1 and out["chase"] is None


def test_hero_v1_deny_then_v2_pass():
    load.main(reset=True)
    p1 = hero.run()
    assert p1[US]["final"] == "deny"
    out = hero.run_v2()
    assert out["learned"] == {"id": "proc-MRI-LSPINE", "version": 2, "supersedes": 1}
    nxt = out["next"]
    assert nxt["agent"] == "approve" and nxt["procedure"]["version"] == 2
    assert nxt["chase"]["had"] == 3 and nxt["chase"]["found"] == 7 and nxt["chase"]["source"] == "chased"
    rates = {r["version"]: r["success_rate"] for r in out["rates"]}
    assert rates == {1: 0.0, 2: 1.0}
    g = _g()
    # chased evidence keeps what was originally sent
    prev = g.query("MATCH (e:Evidence {id:$id}) RETURN e.prev_data, e.source",
                   {"id": f"{hero.NEXT}:pt_record"}).result_set[0]
    assert '"weeks": 3' in prev[0] and prev[1] == "chased:provider"
    assert g.query("MATCH (:Procedure {version:2})-[:LEARNED_FROM]->(c:Case) RETURN c.id").result_set[0][0] \
        == hero.case_id(US)


def test_v2_still_short_after_chase_pends():
    load.main(reset=True)
    hero.run()
    hero.run_v2()
    db = client()
    hero.seed_next(db, cid="hero-us-mri-3", chart_weeks=4)
    out = hero.process(db, "hero-us-mri-3")
    # asks for more PT instead of sending it out to get denied
    assert out["agent"] == "pend" and out["reason"].startswith("chase: pt_record.weeks 4 < 6")
