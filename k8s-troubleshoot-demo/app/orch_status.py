"""Map an in-memory Run to the orchestrator GET /status payload (api.md)."""

from __future__ import annotations

from typing import Any

from app.store import Run

AGENT = "K8s"

NODE_LABELS = {
    "analyze": "Analyze Intent",
    "context": "Gather Context",
    "generate": "Generate Hypothesis",
    "validate": "Validate Hypothesis",
    "report": "Report Generation",
}

NODE_STATUS_MAP = {
    "pending": "QUEUED",
    "running": "RUNNING",
    "done": "COMPLETED",
}


def _node_map(snapshot: dict[str, Any] | None) -> dict[str, str]:
    nodes = ((snapshot or {}).get("workflow") or {}).get("nodes") or []
    return {str(n.get("id")): str(n.get("status") or "pending") for n in nodes}


def progress_for(run: Run) -> int:
    if run.orch_status == "ACCEPTED":
        return 0
    if run.orch_status == "COMPLETED":
        return 100
    snapshot = run.snapshot or {}
    nodes = _node_map(snapshot)
    if nodes.get("report") == "done":
        return 100
    if nodes.get("report") == "running":
        return 95
    iter_n = int(((snapshot.get("workflow") or {}).get("iter") or 0))
    if iter_n > 0:
        return min(90, 25 + iter_n * 16)
    if nodes.get("context") in ("running", "done"):
        return 25
    if nodes.get("analyze") in ("running", "done"):
        return 10
    return 0 if run.orch_status != "FAILED" else 0


def _current_response(run: Run) -> str:
    snapshot = run.snapshot or {}
    caption = str(((snapshot.get("workflow") or {}).get("caption") or "")).strip()
    error = str(snapshot.get("error") or "").strip()
    if run.orch_status == "ACCEPTED":
        return "K8s accepted - waiting in queue"
    if run.orch_status == "FAILED":
        return error or caption or "K8s failed"
    if run.orch_status == "COMPLETED":
        return caption or "K8s completed"
    return caption or "K8s running"


def _sub_agents(run: Run) -> dict[str, dict[str, str]]:
    snapshot = run.snapshot or {}
    nodes = _node_map(snapshot)
    caption = str(((snapshot.get("workflow") or {}).get("caption") or "")).strip()
    out: dict[str, dict[str, str]] = {}
    for nid, label in NODE_LABELS.items():
        raw = nodes.get(nid, "pending")
        mapped = NODE_STATUS_MAP.get(raw, "QUEUED")
        if run.orch_status == "FAILED" and raw == "running":
            mapped = "FAILED"
        if run.orch_status == "FAILED" and not nodes:
            mapped = "FAILED" if nid == "analyze" else "QUEUED"
        response = caption if mapped == "RUNNING" else mapped.lower()
        if run.orch_status == "ACCEPTED":
            mapped = "QUEUED"
            response = "queued"
        if run.orch_status == "COMPLETED":
            mapped = "COMPLETED"
            response = "completed"
        out[label] = {"status": mapped, "response": response}
    return out


def build_status(run: Run) -> dict[str, Any]:
    snapshot = run.snapshot or {}
    topo = snapshot.get("topology") or {}
    details = dict(run.details or {})
    details["session_id"] = run.session_id or None
    details["namespace"] = topo.get("namespace")
    details["ui_url"] = f"/?run_id={run.run_id}"
    return {
        "run_id": run.run_id,
        "mission": run.mission or run.query,
        "agent": AGENT,
        "current_status": run.orch_status,
        "current_response": _current_response(run),
        "progress": progress_for(run),
        "details": details,
        "created": run.created,
        "sub_agents": _sub_agents(run),
    }


def execute_ack(run: Run, *, queued: bool = False) -> dict[str, str]:
    status = "ACCEPTED" if queued else run.orch_status
    messages = {
        "ACCEPTED": "K8s activity queued",
        "RUNNING": "K8s activity running",
        "COMPLETED": "K8s activity completed",
        "FAILED": "K8s activity failed",
    }
    return {
        "run_id": run.run_id,
        "current_status": status,
        "agent": AGENT,
        "message": messages.get(status, "K8s activity queued"),
    }
