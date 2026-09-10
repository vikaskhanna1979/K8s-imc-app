"""In-memory run store with SSE fan-out and a 30-minute history window."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

IN_FLIGHT = frozenset({"ACCEPTED", "RUNNING"})
DEFAULT_HISTORY_TTL_SEC = 1800


def _ttl_from_env() -> int:
    raw = os.getenv("K8S_RUN_HISTORY_TTL_SEC", str(DEFAULT_HISTORY_TTL_SEC)).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_HISTORY_TTL_SEC
    return max(1, value)


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
    finished_at: float | None = None
    mission: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def expires_at(self, ttl_sec: int) -> float:
        return self.created + ttl_sec


class RunStore:
    def __init__(
        self,
        *,
        ttl_sec: int | None = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._runs: dict[str, Run] = {}
        self.ttl_sec = int(ttl_sec) if ttl_sec is not None else _ttl_from_env()
        self._time = time_fn or time.time

    def now(self) -> float:
        return float(self._time())

    def _is_expired(self, run: Run, now: float | None = None) -> bool:
        ts = self.now() if now is None else now
        if run.orch_status in IN_FLIGHT:
            return False
        return ts - run.created >= self.ttl_sec

    def prune(self) -> None:
        now = self.now()
        stale = [rid for rid, run in self._runs.items() if self._is_expired(run, now)]
        for rid in stale:
            self._runs.pop(rid, None)

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
        self.prune()
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
            created=self.now(),
        )
        self._runs[run_id] = run
        return run

    def get(self, run_id: str) -> Run | None:
        self.prune()
        return self._runs.get(run_id)

    def get_idempotent(self, run_id: str, prompt: str) -> Run | None:
        existing = self.get(run_id)
        if existing is None:
            return None
        if existing.query.strip() != prompt.strip():
            raise RunConflict(run_id)
        return existing

    def list_history(self) -> list[Run]:
        self.prune()
        return sorted(self._runs.values(), key=lambda r: r.created, reverse=True)

    def append(self, run: Run, event: dict[str, Any]) -> dict[str, Any]:
        run.seq += 1
        payload = {**event, "seq": run.seq, "run_id": run.run_id}
        run.events.append(payload)
        run.snapshot = payload
        kind = payload.get("type")
        if kind == "run.finished":
            run.status = "finished"
            run.orch_status = "COMPLETED"
            run.finished_at = self.now()
        elif kind == "run.error":
            run.status = "error"
            run.orch_status = "FAILED"
            run.finished_at = self.now()
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
