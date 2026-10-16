"""make demo one-shot: swarm + hero + isolation + replay must land clean."""
from passthru import demo


def test_demo_one_shot():
    out = demo.run(seed=42, workers=20, probe=50, reset=True)
    s = out["swarm"]
    assert s["workers"] == 20
    assert s.get("tenant_us_acme", {}).get("duplicates", 1) == 0
    assert s.get("tenant_in_bharat", {}).get("duplicates", 1) == 0
    assert (s.get("tenant_us_acme", {}).get("lost", 0)
            + s.get("tenant_in_bharat", {}).get("lost", 0)) == 0
    us, ind = out["hero"]["us"], out["hero"]["in"]
    assert us["final"] == "deny"
    assert ind["final"] == "approve"
    assert out["hero"]["v2"]["next"]["agent"] == "approve"
    assert out["hero"]["v2"]["active"]["version"] == 2
    iso = out["isolation"]
    assert iso["refused"] == iso["attempts"] == 50
    assert iso["logged_on_bound"] == 50
    assert iso["logged_on_other"] == 0
    for t, r in out["replay"].items():
        assert r["reproduced"] == r["of"] and r["of"] > 0
    assert out["fix_first"]["tenant_us_acme"] is not None
