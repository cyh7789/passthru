"""One-shot demo for the Graph Hacks walkthrough.

Resets the graphs, runs the 20-worker swarm, the MRI hero (v1 deny → v2 pass), then pulls
clusters, decision replay, a COB example, and a cross-tenant refuse count. Prints a short
script-shaped report. Does not start the dash (run `make dash` alongside) and does not run
evalmem (that resets again — use `make evalmem` for the memory numbers).
"""
import argparse, json
from . import load, swarm, hero, clusters, replay, metrics
from .db import client, tenant_graph, TENANTS
from .mcp_server import Tools


def cob_example(db):
    """Prefer a dual-coverage decided case; otherwise any with coverage_order steps."""
    cob_steps = {"coverage_order", "intended_payer", "cob_ok", "cob_hold", "await_primary_eob"}
    best = None
    for t in TENANTS:
        g = tenant_graph(db, t, bound_tenant=t)
        rows = g.query("""MATCH (c:Case)-[k:DECIDED]->(:Decision)
                          WHERE k.path_json CONTAINS 'coverage_order'
                          RETURN c.id, k.path_json LIMIT 80""").result_set
        for cid, raw in rows:
            path = json.loads(raw) if isinstance(raw, str) else raw
            steps = [s for s in (path or []) if isinstance(s, dict) and s.get("step") in cob_steps]
            if not steps:
                continue
            cov = next((s for s in steps if s.get("step") == "coverage_order"), None)
            dual = bool(cov and len(cov.get("payers") or []) > 1)
            hit = {"tenant": t, "case": cid, "steps": steps, "dual": dual}
            if dual:
                return hit
            best = best or hit
    return best


def isolation_probe(n=50):
    """Bound to Acme, ask for Bharat n times. All must refuse and land AccessDenied."""
    tools = Tools("tenant_us_acme", agent="demo-probe")
    refused = 0
    for _ in range(n):
        out = tools.queue("tenant_in_bharat", n=1)
        if isinstance(out, dict) and out.get("error") == "denied":
            refused += 1
    g = tenant_graph(client(), "tenant_us_acme", bound_tenant="tenant_us_acme")
    logged = g.query("MATCH (a:AccessDenied {agent:'demo-probe'}) RETURN count(a)").result_set[0][0]
    other = client().select_graph("tenant_in_bharat").query(
        "MATCH (a:AccessDenied {agent:'demo-probe'}) RETURN count(a)").result_set[0][0]
    return {"attempts": n, "refused": refused, "logged_on_bound": logged, "logged_on_other": other}


def tenant_summary(snap):
    out = {}
    for t, s in snap["tenants"].items():
        st, oc = s["status"], s["outcomes"]
        decided = sum(oc.values())
        out[t] = {
            "status": st,
            "outcomes": oc,
            "at_risk": s["at_risk"],
            "breached": s["breached"],
            "automation_rate": round(oc.get("approve", 0) / decided, 3) if decided else None,
            "procedures": [p for p in s["procedures"] if p.get("active")],
        }
    return out


def run(seed=42, workers=20, probe=50, reset=True):
    if reset:
        load.main(reset=True)
    swarm_stats = swarm.run(workers)
    hero_out = {"part1": hero.run(), "part2": hero.run_v2()}
    db = client()
    snap = metrics.snapshot(db)
    fix = {t: clusters.tenant_clusters(t, db=db, top=3) for t in TENANTS}
    repl = {}
    for t in TENANTS:
        sample = replay.sample(t, n=10, seed=seed)
        entry = {"tenant": t, "reproduced": sample["reproduced"], "of": sample["of"]}
        if sample["cases"]:
            entry["browser_cypher"] = sample["cases"][0]["cypher"]
        repl[t] = entry
    iso = isolation_probe(probe)
    cob = cob_example(db)
    return {
        "seed": seed,
        "swarm": swarm_stats,
        "tenants": tenant_summary(snap),
        "hero": {
            "us": hero_out["part1"].get("tenant_us_acme"),
            "in": hero_out["part1"].get("tenant_in_bharat"),
            "v2": hero_out["part2"],
        },
        "fix_first": {t: (fix[t][0]["fix_first"] if fix[t] else None) for t in TENANTS},
        "replay": repl,
        "isolation": iso,
        "cob": cob,
        "falkordb": "multigraph isolation + CAS claims + algo.SPpaths + labelPropagation; remove it and nothing here runs",
        "next": ["make dash  # http://localhost:8000",
                 "make evalmem  # v1 vs v2 on unseen (resets graphs)",
                 "FalkorDB Browser http://localhost:3000"],
    }


def print_report(r):
    s = r["swarm"]
    print(f"=== passthru demo (seed {r['seed']}) ===")
    print(f"swarm: {s['workers']} workers, {s['secs']}s, {s['cases_per_min']} cases/min")
    for t in TENANTS:
        st = s.get(t, {})
        print(f"  {t}: done={st.get('done', 0)} conflicts={st.get('conflict', 0)} "
              f"lost={st.get('lost', 0)} dupes={st.get('duplicates', 0)}")
    print("\nper-tenant outcomes after swarm (+ hero cases layered on):")
    for t, info in r["tenants"].items():
        print(f"  {t}: {info['outcomes']}  auto≈{info['automation_rate']}  "
              f"at_risk={info['at_risk']} breached={info['breached']}")
    us, ind = r["hero"]["us"], r["hero"]["in"]
    print("\nhero MRI (same package, two graphs):")
    print(f"  Acme needs {us.get('needs')} → agent {us.get('agent')} → final {us.get('final')}")
    print(f"  Bharat needs {ind.get('needs')} → agent {ind.get('agent')} → final {ind.get('final')}")
    v2 = r["hero"]["v2"]
    nxt = v2.get("next") or {}
    print(f"  v2 active={v2.get('active')} → next {nxt.get('case')} → {nxt.get('agent')} "
          f"(rates {v2.get('rates')})")
    if r["cob"]:
        print(f"\nCOB example: {r['cob']['tenant']} / {r['cob']['case']}")
        for step in r["cob"]["steps"]:
            print(f"  {step}")
    else:
        print("\nCOB example: none in this run's decided paths")
    print("\nfix-first (label propagation):")
    for t, tip in r["fix_first"].items():
        print(f"  {t}: {tip}")
    print("\nreplay:")
    cypher_shown = False
    for t, x in r["replay"].items():
        print(f"  {t}: {x['reproduced']}/{x['of']}")
        if not cypher_shown and x.get("browser_cypher"):
            print(f"  browser:\n{x['browser_cypher']}")
            cypher_shown = True
    iso = r["isolation"]
    print(f"\nisolation: {iso['refused']}/{iso['attempts']} refused, "
          f"logged on bound={iso['logged_on_bound']}, other={iso['logged_on_other']}")
    print(f"\nwhy FalkorDB: {r['falkordb']}")
    print("\nalso:")
    for line in r["next"]:
        print(f"  {line}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("-n", type=int, default=20, help="swarm workers")
    ap.add_argument("--probe", type=int, default=50)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-reset", action="store_true")
    a = ap.parse_args()
    out = run(seed=a.seed, workers=a.n, probe=a.probe, reset=not a.no_reset)
    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print_report(out)
