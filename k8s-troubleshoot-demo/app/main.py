"""FastAPI app: isolated K8s Troubleshooter UI + replay API."""

from __future__ import annotations

import asyncio
import json
import os
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.orch_status import build_history_list, build_status, execute_ack
from app.replay import catalog_summaries, match_session, timeline_events
from app.store import Run, RunConflict, RunStore

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
CATALOG_PATH = STATIC_DIR / "k8s-demo-sessions.json"

store = RunStore()


def _load_catalog() -> dict[str, Any]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _catalog() -> dict[str, Any]:
    global CATALOG
    CATALOG = _load_catalog()
    return CATALOG


CATALOG: dict[str, Any] = _load_catalog()


def _cors_origins() -> list[str]:
    raw = os.getenv("K8S_DEMO_CORS_ORIGINS", "*").strip()
    if raw == "*":
        return ["*"]
    return [part.strip() for part in raw.split(",") if part.strip()]


# Starlette 0.37 StreamingResponse SSE (FastAPI 0.111). sse-starlette 3.4.11
# requires starlette>=0.49.1 and cannot be installed with this stack.
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-store",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _sse_pack(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def _sse_response(content: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        content,
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global CATALOG
    CATALOG = _load_catalog()
    yield


app = FastAPI(title="K8s Troubleshooter demo", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class StartRunBody(BaseModel):
    query: str = Field(..., min_length=1)
    pace: Literal["realtime", "fast"] = "realtime"


class ExecuteBody(BaseModel):
    run_id: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)
    pace: Literal["realtime", "fast"] = "realtime"


def _error_snapshot(run: Run, message: str) -> dict[str, Any]:
    return {
        "type": "run.error",
        "run_id": run.run_id,
        "session_id": run.session_id,
        "title": run.title,
        "query": run.query,
        "delay_ms": 0,
        "workflow": {"iter": 0, "caption": message, "nodes": []},
        "topology": {"visible": False, "namespace": None, "focus": None, "nodes": [], "edges": []},
        "iteration": None,
        "final": None,
        "error": message,
    }


async def emit_run(run: Run, session: dict[str, Any]) -> None:
    rng = random.Random()
    try:
        events = timeline_events(session, run.query, run.run_id, rng=rng)
        for event in events:
            store.append(run, event)
            if run.pace != "fast":
                await asyncio.sleep((event.get("delay_ms") or 0) / 1000.0)
    except Exception as exc:  # noqa: BLE001 — demo emitter should close the stream
        store.append(run, _error_snapshot(run, str(exc)))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def ui() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "k8s-troubleshoot-replay.html",
        media_type="text/html",
    )


@app.get("/orch")
def orch_ui() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "k8s-orchestrator.html",
        media_type="text/html",
    )


@app.get("/k8s-troubleshoot.css")
def component_css() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "k8s-troubleshoot.css",
        media_type="text/css",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/k8s-demo-sessions.json")
def catalog_file() -> FileResponse:
    return FileResponse(
        CATALOG_PATH,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/v1/catalog")
def catalog() -> dict[str, Any]:
    return {"sessions": catalog_summaries(_catalog())}


@app.post("/execute")
async def execute(body: ExecuteBody) -> JSONResponse:
    try:
        existing = store.get_idempotent(body.run_id, body.prompt)
    except RunConflict:
        raise HTTPException(
            status_code=409,
            detail="run_id already used with a different prompt",
        ) from None
    if existing:
        return JSONResponse(status_code=202, content=execute_ack(existing))

    session = match_session(_catalog(), body.prompt)
    run = store.create(
        session,
        body.prompt,
        body.pace,
        run_id=body.run_id,
        details=body.details,
        orchestrated=True,
    )
    if not session:
        store.append(run, _error_snapshot(run, "No catalog session matched that query"))
        return JSONResponse(status_code=202, content=execute_ack(run, queued=True))

    if body.pace == "fast":
        await emit_run(run, session)
    else:
        asyncio.create_task(emit_run(run, session))
    return JSONResponse(status_code=202, content=execute_ack(run, queued=True))


@app.get("/status")
def orch_status(run_id: str = Query(..., min_length=1)) -> dict[str, Any]:
    run = store.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return build_status(run, ttl_sec=store.ttl_sec)


@app.get("/history")
def list_history() -> dict[str, Any]:
    return build_history_list(store)


@app.get("/history/{run_id}")
def get_history_run(run_id: str) -> dict[str, Any]:
    run = store.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return build_status(run, ttl_sec=store.ttl_sec)


@app.post("/v1/runs")
async def start_run(body: StartRunBody) -> dict[str, str]:
    session = match_session(_catalog(), body.query)
    if not session:
        raise HTTPException(status_code=422, detail="No catalog session matched that query")
    run = store.create(session, body.query, body.pace)
    if body.pace == "fast":
        await emit_run(run, session)
    else:
        asyncio.create_task(emit_run(run, session))
    return {
        "run_id": run.run_id,
        "session_id": run.session_id,
        "title": run.title,
        "status": run.status,
        "stream_url": f"/v1/runs/{run.run_id}/stream",
        "events_url": f"/v1/runs/{run.run_id}/events",
    }


@app.get("/v1/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = store.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return {
        "run_id": run.run_id,
        "session_id": run.session_id,
        "title": run.title,
        "status": run.status,
        "seq": run.seq,
        "snapshot": run.snapshot,
    }


@app.get("/v1/runs/{run_id}/events")
def get_events(run_id: str, after_seq: int = Query(default=0, ge=0)) -> dict[str, Any]:
    run = store.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return {
        "run_id": run.run_id,
        "status": run.status,
        "events": [e for e in run.events if e.get("seq", 0) > after_seq],
    }


@app.get("/v1/runs/{run_id}/stream")
async def stream_run(run_id: str, after_seq: int = Query(default=0, ge=0)) -> StreamingResponse:
    run = store.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")

    async def gen():
        last = after_seq
        for event in list(run.events):
            seq = int(event.get("seq") or 0)
            if seq <= last:
                continue
            last = seq
            yield _sse_pack("message", json.dumps(event))
            if event.get("type") in ("run.finished", "run.error"):
                return
        if run.status in ("finished", "error"):
            return
        queue: asyncio.Queue = asyncio.Queue()
        run.subscribers.add(queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _sse_pack("message", json.dumps(event))
                if event.get("type") in ("run.finished", "run.error"):
                    return
        finally:
            run.subscribers.discard(queue)

    return _sse_response(gen())


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("K8S_DEMO_PORT", "8115"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)
