"""Dashboard: one page, polls nothing, gets a snapshot over SSE every second."""
import asyncio, json, os
from pathlib import Path
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route
from sse_starlette.sse import EventSourceResponse
from .db import client, TENANTS, TenantError
from . import metrics, runner, clusters, review
from .sched import now_ms

STATIC = Path(__file__).parent / "static"
_db = None


def db():
    global _db
    _db = _db or client()
    return _db


async def index(_):
    return FileResponse(STATIC / "index.html")


def full_snapshot():
    s = metrics.snapshot(db())
    s["run"] = runner.state
    return s


async def snap(_):
    return JSONResponse(full_snapshot())


async def run(request):
    kind = request.query_params.get("kind", "swarm")
    try:
        n = max(1, min(int(request.query_params.get("n", 20)), 50))
        ok = runner.start(kind, n)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not ok:
        return JSONResponse({"error": "already running"}, status_code=409)
    return JSONResponse({"started": kind, "n": n}, status_code=202)


async def case(request):
    tenant = request.query_params.get("tenant")
    case_id = request.query_params.get("id")
    if not tenant or not case_id:
        return JSONResponse({"error": "need tenant and id"}, status_code=400)
    if tenant not in TENANTS:
        return JSONResponse({"error": "unknown tenant"}, status_code=400)
    try:
        detail = metrics.case_path(tenant, case_id, db())
    except TenantError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    if detail is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(detail)


async def gap_clusters(request):
    tenant = request.query_params.get("tenant")
    if tenant not in TENANTS:
        return JSONResponse({"error": "unknown tenant"}, status_code=400)
    if runner.state["running"]:
        return JSONResponse({"error": "run in progress"}, status_code=409)
    return JSONResponse({"tenant": tenant, "clusters": clusters.tenant_clusters(tenant, db())})



async def review_queue(request):
    tenant = request.query_params.get("tenant")
    if tenant not in TENANTS:
        return JSONResponse({"error": "unknown tenant"}, status_code=400)
    now = now_ms()
    items = []
    for row in review.list_queue(tenant):
        items.append({
            "id": row["id"],
            "service": row["service"],
            "reason": row["reason"],
            "proposed_outcome": row["proposed_outcome"],
            "deadline": row["deadline"],
            "ms_left": (row["deadline"] - now) if row.get("deadline") is not None else None,
            "needs_human": True,
            "claimed_by": row.get("claimed_by"),
            "version": row.get("version"),
        })
    return JSONResponse({"tenant": tenant, "items": items})

async def events(request):
    every = float(os.getenv("PASSTHRU_DASH_EVERY", "1"))

    async def gen():
        while not await request.is_disconnected():
            yield {"event": "snapshot", "data": json.dumps(full_snapshot())}
            await asyncio.sleep(every)
    return EventSourceResponse(gen())


app = Starlette(routes=[
    Route("/", index),
    Route("/api/snapshot", snap),
    Route("/api/case", case),
    Route("/api/run", run, methods=["POST"]),
    Route("/api/clusters", gap_clusters),
    Route("/api/review", review_queue),
    Route("/events", events),
])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
