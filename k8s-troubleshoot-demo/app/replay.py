"""Catalog matcher and timed step timeline (port of the HTML replay)."""

from __future__ import annotations

import copy
import random
import re
from typing import Any

STOP = {
    "the",
    "a",
    "an",
    "in",
    "ns",
    "namespace",
    "is",
    "with",
    "what",
    "why",
    "how",
    "pod",
    "pods",
    "check",
    "find",
    "issue",
    "issues",
    "problems",
    "problem",
    "to",
    "of",
    "and",
    "for",
    "on",
    "this",
    "that",
    "please",
    "current",
    "exact",
    "cause",
    "then",
    "are",
    "was",
    "from",
    "by",
}

WORKFLOW_NODE_IDS = ("analyze", "context", "generate", "validate", "report")
MATCH_THRESHOLD = 15
PHASE_TO_EVENT = {
    "generate": "iteration.generate",
    "command": "iteration.command",
    "output": "iteration.analyzing",
    "validate": "iteration.complete",
}
PHASE_TO_API = {
    "generate": "generate",
    "command": "command",
    "output": "analyzing",
    "validate": "validate",
}


def tokens(s: str) -> list[str]:
    parts = re.sub(r"[^a-z0-9_-]+", " ", str(s or "").lower()).split()
    return [t for t in parts if t and len(t) > 1 and t not in STOP]


def score_session(query: str, session: dict[str, Any]) -> int:
    q = str(query or "").lower().strip()
    if not q:
        return 0
    q_tokens = set(tokens(q))
    score = 0
    for p in session.get("prompts") or []:
        pl = str(p or "").lower().strip()
        if not pl:
            continue
        if q == pl:
            return 1000
        if len(q) >= 12 and (q in pl or pl in q):
            score += 80
        for t in tokens(p):
            if t in q_tokens:
                score += 8
    for k in session.get("keywords") or []:
        kl = str(k or "").lower()
        if not kl:
            continue
        if kl in q or kl in q_tokens:
            score += 20
    blob = f"{session.get('description') or ''} {session.get('title') or ''}"
    for t in tokens(blob):
        if t in q_tokens:
            score += 3
    return score


def match_session(catalog: dict[str, Any], query: str) -> dict[str, Any] | None:
    sessions = catalog.get("sessions") or []
    ranked = sorted(
        ({"session": s, "score": score_session(query, s)} for s in sessions),
        key=lambda x: x["score"],
        reverse=True,
    )
    if not ranked or ranked[0]["score"] < MATCH_THRESHOLD:
        return None
    return ranked[0]["session"]


def catalog_summaries(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for s in catalog.get("sessions") or []:
        out.append(
            {
                "id": s.get("id"),
                "title": s.get("title"),
                "description": s.get("description"),
                "prompts": list(s.get("prompts") or []),
                "keywords": list(s.get("keywords") or []),
            }
        )
    return out


def build_steps(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    turns = scenario.get("turns") or []
    resolved = (scenario.get("final") or {}).get("outcome") == "resolved"
    steps: list[dict[str, Any]] = [
        {"kind": "node", "id": "analyze", "caption": "Analyze Intent · diagnose"},
        {
            "kind": "node",
            "id": "context",
            "caption": "Gather Context · knowledge, history, topology",
        },
        {"kind": "context", "caption": "Cluster topology gathered"},
    ]
    for i, turn in enumerate(turns):
        n = i + 1
        tid = turn["id"] if isinstance(turn.get("id"), int) else i
        last = i == len(turns) - 1
        steps.append(
            {
                "kind": "turn",
                "turn": tid,
                "phase": "generate",
                "iter": n,
                "caption": f"Iteration {n} · Generate Hypothesis",
            }
        )
        steps.append(
            {
                "kind": "turn",
                "turn": tid,
                "phase": "command",
                "iter": n,
                "caption": f"Iteration {n} · Running kubectl",
            }
        )
        steps.append(
            {
                "kind": "turn",
                "turn": tid,
                "phase": "output",
                "iter": n,
                "caption": f"Iteration {n} · Analyzing output",
            }
        )
        cap = f"Iteration {n} · Validate Hypothesis"
        if last:
            cap += " · solution found"
        steps.append(
            {
                "kind": "turn",
                "turn": tid,
                "phase": "validate",
                "iter": n,
                "caption": cap,
            }
        )
    steps.append({"kind": "node", "id": "report", "caption": "Report Generation"})
    steps.append(
        {
            "kind": "final",
            "caption": "Resolved · remedial action suggested"
            if resolved
            else "Max retries reached",
        }
    )
    return steps


def delay_ms_for(step: dict[str, Any], rng: random.Random | None = None) -> int:
    rng = rng or random.Random()
    if step.get("kind") == "node" and step.get("id") == "context":
        return 1600
    if step.get("kind") == "context":
        return 1200
    if step.get("kind") == "turn" and step.get("phase") == "command":
        return 2000
    if step.get("kind") == "turn" and step.get("phase") == "output":
        return 5000 + int(rng.random() * 5000)
    if step.get("kind") == "turn" and step.get("phase") == "validate":
        return 1500
    if step.get("kind") == "turn" and step.get("phase") == "generate":
        return 1100
    return 800


def event_type_for(step: dict[str, Any]) -> str:
    kind = step.get("kind")
    if kind == "node":
        return "workflow.node"
    if kind == "context":
        return "topology.ready"
    if kind == "turn":
        return PHASE_TO_EVENT.get(step.get("phase") or "", "iteration.generate")
    if kind == "final":
        return "run.finished"
    return "workflow.node"


def turn_by_id(scenario: dict[str, Any], tid: int) -> dict[str, Any] | None:
    for t in scenario.get("turns") or []:
        ident = t["id"] if isinstance(t.get("id"), int) else 0
        if ident == tid:
            return t
    return None


def active_topology(session: dict[str, Any], flavor: str | None = None) -> dict[str, Any] | None:
    t = session.get("topology")
    if not t:
        return None
    flavors = t.get("flavors")
    if flavors:
        fid = flavor or t.get("flavor") or next(iter(flavors), None)
        flavor_data = flavors.get(fid) if fid else None
        if not flavor_data:
            return t
        return {
            "namespace": t.get("namespace"),
            "cluster": t.get("cluster"),
            "focus": t.get("focus"),
            "flavor": fid,
            "label": flavor_data.get("label"),
            "nodes": flavor_data.get("nodes") or [],
            "edges": flavor_data.get("edges") or [],
        }
    return t


def topology_payload(session: dict[str, Any], visible: bool) -> dict[str, Any]:
    raw = session.get("topology") or {}
    if not visible:
        return {
            "visible": False,
            "namespace": raw.get("namespace"),
            "focus": raw.get("focus"),
            "nodes": [],
            "edges": [],
        }
    topo = active_topology(session)
    if not topo:
        return {
            "visible": False,
            "namespace": raw.get("namespace"),
            "focus": raw.get("focus"),
            "nodes": [],
            "edges": [],
        }
    return {
        "visible": True,
        "namespace": topo.get("namespace"),
        "focus": topo.get("focus"),
        "flavor": topo.get("flavor"),
        "nodes": list(topo.get("nodes") or []),
        "edges": list(topo.get("edges") or []),
    }


def _iteration_payload(
    turn: dict[str, Any] | None, band: dict[str, bool] | None, phase: str | None
) -> dict[str, Any] | None:
    if not turn or not band:
        return None
    return {
        "id": turn.get("id", 0),
        "phase": PHASE_TO_API.get(phase or "", "generate"),
        "hypothesis": turn.get("hypothesis") if band.get("hyp") else None,
        "command": turn.get("command") if band.get("cmd") else None,
        "stdout": turn.get("stdout") if band.get("out") else None,
        "takeaways": list(turn.get("takeaways") or []) if band.get("analysis") else None,
        "analysis": turn.get("analysis") if band.get("analysis") else None,
    }


def apply_index(steps: list[dict[str, Any]], scenario: dict[str, Any], idx: int) -> dict[str, Any]:
    node_status = {nid: "pending" for nid in WORKFLOW_NODE_IDS}
    show_context = False
    turn_band: dict[int, dict[str, bool]] = {}
    show_final = False
    iter_n = 0
    current_turn: int | None = None
    current_phase: str | None = None
    caption = "Waiting for an issue"

    if idx < 0 or not steps:
        return {
            "nodes": node_status,
            "show_context": False,
            "iter": 0,
            "show_final": False,
            "caption": caption,
            "iteration": None,
        }

    for k in range(idx + 1):
        step = steps[k]
        if step["kind"] == "node":
            node_status[step["id"]] = "done"
        if step["kind"] == "context":
            show_context = True
        if step["kind"] == "turn":
            tid = int(step["turn"])
            if step["phase"] == "validate":
                turn_band[tid] = {
                    "hyp": True,
                    "cmd": True,
                    "out": True,
                    "analyzing": True,
                    "analysis": True,
                }
                node_status["generate"] = "done"
                node_status["validate"] = "done"
            elif step["phase"] == "output":
                existing = turn_band.get(tid)
                if not existing or not existing.get("analysis"):
                    turn_band[tid] = {
                        "hyp": True,
                        "cmd": True,
                        "out": True,
                        "analyzing": True,
                        "analysis": False,
                    }
                node_status["generate"] = "done"
                node_status["validate"] = "done"
            elif step["phase"] == "command":
                existing = turn_band.get(tid)
                if not existing or not existing.get("analyzing"):
                    turn_band[tid] = {
                        "hyp": True,
                        "cmd": True,
                        "out": True,
                        "analyzing": False,
                        "analysis": False,
                    }
                node_status["generate"] = "done"
                node_status["validate"] = "done"
            elif not turn_band.get(tid):
                turn_band[tid] = {
                    "hyp": True,
                    "cmd": False,
                    "out": False,
                    "analyzing": False,
                    "analysis": False,
                }
                node_status["generate"] = "done"
        if step["kind"] == "final":
            show_final = True
            node_status["report"] = "done"

    cur = steps[idx]
    caption = cur.get("caption") or caption
    if cur["kind"] == "node":
        node_status[cur["id"]] = "running"
    if cur["kind"] == "turn":
        iter_n = int(cur.get("iter") or 0)
        current_turn = int(cur["turn"])
        current_phase = str(cur["phase"])
        if cur["phase"] == "generate":
            node_status["generate"] = "running"
            node_status["validate"] = "done" if iter_n > 1 else "pending"
            turn_band[current_turn] = {
                "hyp": True,
                "cmd": False,
                "out": False,
                "analyzing": False,
                "analysis": False,
            }
        elif cur["phase"] == "command":
            node_status["generate"] = "done"
            node_status["validate"] = "running"
            turn_band[current_turn] = {
                "hyp": True,
                "cmd": True,
                "out": True,
                "analyzing": False,
                "analysis": False,
            }
        elif cur["phase"] == "output":
            node_status["generate"] = "done"
            node_status["validate"] = "running"
            turn_band[current_turn] = {
                "hyp": True,
                "cmd": True,
                "out": True,
                "analyzing": True,
                "analysis": False,
            }
        else:
            node_status["generate"] = "done"
            node_status["validate"] = "running"
            turn_band[current_turn] = {
                "hyp": True,
                "cmd": True,
                "out": True,
                "analyzing": True,
                "analysis": True,
            }
    if cur["kind"] == "final":
        node_status["report"] = "done"

    iteration = None
    if current_turn is not None:
        iteration = _iteration_payload(
            turn_by_id(scenario, current_turn),
            turn_band.get(current_turn),
            current_phase,
        )

    return {
        "nodes": node_status,
        "show_context": show_context,
        "iter": iter_n,
        "show_final": show_final,
        "caption": caption,
        "iteration": iteration,
    }


def workflow_payload(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "iter": state["iter"],
        "caption": state["caption"],
        "nodes": [
            {"id": nid, "status": state["nodes"][nid]} for nid in WORKFLOW_NODE_IDS
        ],
    }


def build_snapshot(
    *,
    event_type: str,
    run_id: str,
    session: dict[str, Any],
    query: str,
    state: dict[str, Any],
    delay_ms: int = 0,
) -> dict[str, Any]:
    final = None
    if state["show_final"]:
        final = copy.deepcopy(session.get("final") or {})
    return {
        "type": event_type,
        "run_id": run_id,
        "session_id": session.get("id"),
        "title": session.get("title"),
        "query": query,
        "delay_ms": delay_ms,
        "workflow": workflow_payload(state),
        "topology": topology_payload(session, state["show_context"]),
        "iteration": state.get("iteration"),
        "final": final,
    }


def timeline_events(
    session: dict[str, Any],
    query: str,
    run_id: str,
    rng: random.Random | None = None,
) -> list[dict[str, Any]]:
    rng = rng or random.Random()
    steps = build_steps(session)
    started_state = apply_index(steps, session, -1)
    started_state["caption"] = session.get("title") or "Run started"
    events = [
        build_snapshot(
            event_type="run.started",
            run_id=run_id,
            session=session,
            query=query,
            state=started_state,
            delay_ms=0,
        )
    ]
    for i, step in enumerate(steps):
        events.append(
            build_snapshot(
                event_type=event_type_for(step),
                run_id=run_id,
                session=session,
                query=query,
                state=apply_index(steps, session, i),
                delay_ms=delay_ms_for(step, rng),
            )
        )
    return events
