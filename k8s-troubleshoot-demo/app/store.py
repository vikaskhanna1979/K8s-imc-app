"""In-memory run store with SSE fan-out."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


class RunConflict(Exception):
    """Same run_id already exists with a different prompt."""


@dataclass
class Run:
    run_id: str
    session_id: str
    title: str
    query: str
    pace: str
    status: str = "running"
    orch_status: str = "RUNNING"
    seq: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    snapshot: dict[str, Any] | None = None
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    created: float = field(default_factory=time.time)
    mission: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class RunStore:
    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}

    def create(
        self,
        session: dict[str, Any] | None,
        query: str,
        pace: str,
        *,
        run_id: str | None = None,
        details: dict[str, Any] | None = None,
        orchestrated: bool = False,
    ) -> Run:
        if not run_id:
            run_id = "d-" + uuid.uuid4().hex[:12]
        session = session or {}
        run = Run(
            run_id=run_id,
            session_id=str(session.get("id") or ""),
            title=str(session.get("title") or ""),
            query=query,
            pace=pace,
            status="accepted" if orchestrated else "running",
            orch_status="ACCEPTED" if orchestrated else "RUNNING",
            mission=query,
            details=dict(details or {}),
        )
        self._runs[run_id] = run
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def get_idempotent(self, run_id: str, prompt: str) -> Run | None:
        existing = self._runs.get(run_id)
        if existing is None:
            return None
        if existing.query.strip() != prompt.strip():
            raise RunConflict(run_id)
        return existing

    def append(self, run: Run, event: dict[str, Any]) -> dict[str, Any]:
        run.seq += 1
        payload = {**event, "seq": run.seq, "run_id": run.run_id}
        run.events.append(payload)
        run.snapshot = payload
        kind = payload.get("type")
        if kind == "run.finished":
            run.status = "finished"
            run.orch_status = "COMPLETED"
        elif kind == "run.error":
            run.status = "error"
            run.orch_status = "FAILED"
        else:
            if run.status == "accepted":
                run.status = "running"
            if run.orch_status == "ACCEPTED":
                run.orch_status = "RUNNING"
        for queue in list(run.subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass
        return payload
