"""One demo run at a time, kicked off from the dashboard. Runs in a thread so SSE keeps ticking."""
import threading, time
from collections import Counter
from .db import TENANTS
from . import load, swarm, hero, evalmem, replay

_lock = threading.Lock()
state = {"running": False, "kind": None, "started": None, "result": None, "error": None,
         "live": {}, "totals": {"runs": 0, "conflict": 0, "lost": 0, "done": 0}, "proof": None}


def _proof(n):
    # memory eval resets on its own data, so put the swarm + hero graph back afterwards for replay
    ev = evalmem.run(reset=False)
    load.main(reset=True)
    swarm.run(n, stats=state["live"])
    hero.run()
    hero.run_v2()
    pick = lambda b: {k: b[k] for k in ("version", "n", "first_pass_approve", "could_pass", "pend", "needs_review")}
    state["proof"] = {"eval": {"v1": pick(ev["v1"]), "v2": pick(ev["v2"]), "fixed_from": ev["fixed_from"]},
                      "replay": {t: {k: v for k, v in replay.sample(t, n=10).items() if k != "cases"} for t in TENANTS},
                      "at": int(time.time() * 1000)}
    return {"kind": "proof"}


def _go(kind, n):
    try:
        load.main(reset=True)
        if kind == "hero":
            hero.run()
            hero.run_v2()
            res = {"kind": "hero"}
        elif kind == "proof":
            res = _proof(n)
        else:
            res = swarm.run(n, stats=state["live"])
            tot = state["totals"]
            tot["runs"] += 1
            for t in TENANTS:
                for k in ("conflict", "lost", "done"):
                    tot[k] += res[t].get(k, 0)
        state["result"] = res
    except Exception as e:  # surface it on the dash instead of dying silently
        state["error"] = str(e)
    finally:
        state["running"] = False


def start(kind="swarm", n=20, wait=False):
    if kind not in ("swarm", "hero", "proof"):
        raise ValueError("kind must be swarm, hero or proof")
    with _lock:
        if state["running"]:
            return False
        state.update(running=True, kind=kind, started=int(time.time() * 1000), result=None, error=None,
                     live={t: Counter() for t in TENANTS})
    th = threading.Thread(target=_go, args=(kind, n), daemon=True)
    th.start()
    if wait:
        th.join()
    return True
