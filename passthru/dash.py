"""Dashboard: one page, polls nothing, gets a snapshot over SSE every second."""
import asyncio, json, os
from pathlib import Path
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route
from sse_starlette.sse import EventSourceResponse
from .db import client
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


async def events(request):
    every = float(os.getenv("PASSTHRU_DASH_EVERY", "1"))

    async def gen():
        while not await request.is_disconnected():
            yield {"event": "snapshot", "data": json.dumps(metrics.snapshot(db()))}
            await asyncio.sleep(every)
    return EventSourceResponse(gen())


app = Starlette(routes=[Route("/", index), Route("/api/snapshot", snap), Route("/events", events)])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
