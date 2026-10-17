from passthru import evalmem


def test_batches_disjoint_and_seeded():
    a, b = evalmem.make_cases(42)
    assert len(a) == len(b) == 40
    assert not {c["id"] for c in a} & {c["id"] for c in b}
    assert evalmem.make_cases(42) == (a, b)


def test_v2_beats_v1_on_unseen_batch():
    r = evalmem.run(42)
    v1, v2 = r["v1"], r["v2"]
    assert v1["version"] == 1 and v2["version"] == 2
    assert v2["first_pass_approve"] / v2["n"] > v1["first_pass_approve"] / v1["n"]
    # v2 chases instead of proposing denies: nothing parked for a human, short charts pend
    assert v2["needs_review"] == 0
    assert v2["approve"] == v2["could_pass"] and v2["pend"] == v2["n"] - v2["could_pass"]
    assert r["fixed_from"].startswith("eval-")
