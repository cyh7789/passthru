from passthru import load, hero


def test_same_mri_two_payers_two_outcomes():
    load.main(reset=True)
    out = hero.run()
    us, inn = out["tenant_us_acme"], out["tenant_in_bharat"]
    assert us["needs"] != inn["needs"]
    assert "pt_record" in us["needs"] and "pt_record" not in inn["needs"]
    assert "estimate" in inn["needs"] and "estimate" not in us["needs"]
    assert inn["final"] == "approve" and inn["decided_by"] == "hero-agent"
    # agent only proposed it; the deny is the reviewer's
    assert us["agent"] == "needs_review" and "US-MRI-1a" in us["reason"]
    assert us["final"] == "deny" and us["decided_by"] == hero.REVIEWER
    assert "6 weeks" in us["final_reason"]
