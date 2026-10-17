"""One demo run at a time, kicked off from the dashboard. Runs in a thread so SSE keeps ticking."""
import threading, time
from . import load, swarm, hero

_lock = threading.Lock()
state = {"running": False, "kind": None, "started": None, "result": None, "error": None}


def _go(kind, n):
    try:
        load.main(reset=True)
        if kind == "hero":
            hero.run()
            res = {"kind": "hero"}
        else:
            res = swarm.run(n)
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
        state.update(running=True, kind=kind, started=int(time.time() * 1000), result=None, error=None)
    th = threading.Thread(target=_go, args=(kind, n), daemon=True)
    th.start()
    if wait:
        th.join()
    return True
