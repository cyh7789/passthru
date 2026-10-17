"""N workers hitting the same tenant graphs at once. Each worker has its own connection and is
bound to one tenant. They all race for the head of the queue on purpose, so CAS gets exercised."""
import argparse, threading, time
from collections import Counter
from .db import client, tenant_graph, TENANTS
from .sched import next_cases, sweep
from . import flow


def worker(tenant, name, stats, lock, peek=5):
    db = client()
    g = tenant_graph(db, tenant, bound_tenant=tenant)
    local = Counter()
    while True:
        batch = next_cases(g, peek)
        if not batch:
            break
        if local:
            with lock:
                stats[tenant].update(local)
            local.clear()
        got = None
        for cid, v, _ in batch:
            if flow.claim(g, cid, v, name):
                got = (cid, v)
                break
            local["conflict"] += 1
        if not got:
            continue
        cid, v = got
        outcome, reason, path = flow.decide(g, cid, db=db, tenant=tenant)
        if flow.record(g, cid, v + 1, outcome, reason, path, name):
            local[outcome] += 1
            local["done"] += 1
        else:
            local["lost"] += 1
    with lock:
        stats[tenant].update(local)


def split(n):
    # US has more cases (120 vs 80), give it the bigger share
    us = max(1, round(n * 0.6))
    return {"tenant_us_acme": us, "tenant_in_bharat": max(1, n - us)}


def duplicates(g):
    return g.query("""MATCH (c:Case)-[d:DECIDED]->() WITH c, count(d) AS n WHERE n > 1
                      RETURN count(c)""").result_set[0][0]


def run(n=20, stats=None):
    # pass stats in to watch the counters move while the run is going
    db = client()
    for t in TENANTS:
        sweep(tenant_graph(db, t, bound_tenant=t))
    stats = stats if stats is not None else {t: Counter() for t in TENANTS}
    lock, threads = threading.Lock(), []
    for t, k in split(n).items():
        for i in range(k):
            threads.append(threading.Thread(target=worker, args=(t, f"{t.split('_')[1]}-w{i+1}", stats, lock)))
    t0 = time.time()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    secs = time.time() - t0
    out = {"workers": n, "secs": round(secs, 2)}
    total = 0
    for t in TENANTS:
        g = tenant_graph(db, t, bound_tenant=t)
        s = dict(stats[t])
        s["duplicates"] = duplicates(g)
        s["left_queued"] = g.query("MATCH (c:Case) WHERE c.status IN ['queued','claimed'] RETURN count(c)").result_set[0][0]
        total += s.get("done", 0)
        out[t] = s
    out["cases_per_min"] = round(total / secs * 60, 1) if secs else None
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=20)
    a = ap.parse_args()
    import json
    print(json.dumps(run(a.n), indent=2))
