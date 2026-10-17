"""One demo run at a time, kicked off from the dashboard. Runs in a thread so SSE keeps ticking."""
import threading, time
from collections import Counter
from .db import TENANTS
from . import load, swarm, hero

_lock = threading.Lock()
state = {"running": False, "kind": None, "started": None, "result": None, "error": None,
         "live": {}, "totals": {"runs": 0, "conflict": 0, "lost": 0, "done": 0}}


def _go(kind, n):
    try:
        load.main(reset=True)
        if kind == "hero":
            hero.run()
            res = {"kind": "hero"}
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
    if kind not in ("swarm", "hero"):
        raise ValueError("kind must be swarm or hero")
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
