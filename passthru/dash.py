"""Dashboard: one page, polls nothing, gets a snapshot over SSE every second."""
import asyncio, json, os
from pathlib import Path
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route
from sse_starlette.sse import EventSourceResponse
from .db import client, TENANTS, TenantError
from . import metrics

STATIC = Path(__file__).parent / "static"
_db = None


def db():
    global _db
    _db = _db or client()
    return _db


async def index(_):
    return FileResponse(STATIC / "index.html")


async def snap(_):
    return JSONResponse(metrics.snapshot(db()))


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


async def events(request):
    every = float(os.getenv("PASSTHRU_DASH_EVERY", "1"))

    async def gen():
        while not await request.is_disconnected():
            yield {"event": "snapshot", "data": json.dumps(metrics.snapshot(db()))}
            await asyncio.sleep(every)
    return EventSourceResponse(gen())


app = Starlette(routes=[
    Route("/", index),
    Route("/api/snapshot", snap),
    Route("/api/case", case),
    Route("/events", events),
])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
