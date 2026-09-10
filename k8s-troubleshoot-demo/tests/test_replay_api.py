"""Replay API: match, SSE snapshots, isolated UI assets."""

from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient

from app.main import app, _load_catalog
from app.replay import match_session


AMF_QUERY = "Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?"
BUSYBOX_QUERY = "why is busybox-pod in pending state"
LATCH_QUERY = "Customer unable to latch on the network"


def _run_id(prefix: str = "RUN-K8S") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _events(client: TestClient, run_id: str) -> list[dict]:
    res = client.get(f"/v1/runs/{run_id}/events")
    assert res.status_code == 200
    return res.json()["events"]


def test_amf_intent_matches_catalog():
    catalog = _load_catalog()
    session = match_session(catalog, AMF_QUERY)
    assert session is not None
    assert session["id"] == "amf-crashloop"


def test_latch_intent_matches_catalog():
    catalog = _load_catalog()
    session = match_session(catalog, LATCH_QUERY)
    assert session is not None
    assert session["id"] == "ue-latch-calico"
    assert len(session["turns"]) == 6
    assert session["final"]["outcome"] == "resolved"


def test_no_match_returns_422():
    with TestClient(app) as client:
        res = client.post("/v1/runs", json={"query": "unrelated xyzzy widget", "pace": "fast"})
        assert res.status_code == 422


def test_fast_stream_topology_iterations_and_finished():
    catalog = _load_catalog()
    session = match_session(catalog, AMF_QUERY)
    assert session is not None
    n_turns = len(session["turns"])

    with TestClient(app) as client:
        start = client.post("/v1/runs", json={"query": AMF_QUERY, "pace": "fast"})
        assert start.status_code == 200, start.text
        body = start.json()
        assert body["session_id"] == "amf-crashloop"
        run_id = body["run_id"]
        assert run_id.startswith("d-")
        assert body["stream_url"] == f"/v1/runs/{run_id}/stream"
        assert body["events_url"] == f"/v1/runs/{run_id}/events"

        events = _events(client, run_id)
        types = [e["type"] for e in events]
        assert types[0] == "run.started"
        assert types[-1] == "run.finished"
        assert types.count("iteration.complete") == n_turns
        assert "topology.ready" in types

        topo_idx = types.index("topology.ready")
        for ev in events[:topo_idx]:
            assert ev["topology"]["visible"] is False
        ready = events[topo_idx]
        assert ready["type"] == "topology.ready"
        assert ready["topology"]["visible"] is True
        assert ready["topology"]["namespace"] == "5g-core"
        assert ready["topology"]["focus"] == "amf"
        assert ready["topology"]["nodes"]

        context_nodes = [
            e
            for e in events
            if e["type"] == "workflow.node"
            and any(n["id"] == "context" and n["status"] == "running" for n in e["workflow"]["nodes"])
        ]
        assert context_nodes
        assert types.index("workflow.node") < topo_idx
        # Gather Context node event comes before topology is shown
        ctx_running_idx = next(
            i
            for i, e in enumerate(events)
            if e["type"] == "workflow.node"
            and any(n["id"] == "context" and n["status"] == "running" for n in e["workflow"]["nodes"])
        )
        assert ctx_running_idx < topo_idx

        completes = [e for e in events if e["type"] == "iteration.complete"]
        for i, ev in enumerate(completes):
            it = ev["iteration"]
            assert it is not None
            assert it["id"] == i
            assert it["phase"] == "validate"
            assert it["hypothesis"]
            assert it["command"]
            assert it["stdout"]
            assert it["analysis"]
            assert it["takeaways"]

        finished = events[-1]
        assert finished["final"] is not None
        assert all(n["status"] == "done" for n in finished["workflow"]["nodes"])

        snap = client.get(f"/v1/runs/{run_id}")
        assert snap.status_code == 200
        assert snap.json()["status"] == "finished"
        assert snap.json()["snapshot"]["type"] == "run.finished"


def test_sse_stream_closes_on_finished():
    with TestClient(app) as client:
        start = client.post("/v1/runs", json={"query": AMF_QUERY, "pace": "fast"})
        run_id = start.json()["run_id"]
        with client.stream("GET", f"/v1/runs/{run_id}/stream") as resp:
            assert resp.status_code == 200
            types = []
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = json.loads(line.split("data:", 1)[1].strip())
                types.append(payload["type"])
                if payload["type"] in ("run.finished", "run.error"):
                    break
            assert types[0] == "run.started"
            assert types[-1] == "run.finished"
            assert "topology.ready" in types
            assert types.count("iteration.complete") == 4


def test_ui_and_static_assets():
    with TestClient(app) as client:
        html = client.get("/")
        assert html.status_code == 200
        assert "text/html" in html.headers.get("content-type", "")
        assert "K8s Troubleshooter" in html.text
        assert "/k8s-troubleshoot.css" in html.text
        assert "CATALOG_URL = \"/k8s-demo-sessions.json\"" in html.text
        assert "joinOrchestratorRun" in html.text

        css = client.get("/k8s-troubleshoot.css")
        assert css.status_code == 200
        assert "text/css" in css.headers.get("content-type", "")
        assert "--kt-accent" in css.text
        assert ".graph-row" in css.text

        catalog = client.get("/k8s-demo-sessions.json")
        assert catalog.status_code == 200
        data = catalog.json()
        ids = [s["id"] for s in data["sessions"]]
        assert "amf-crashloop" in ids
        assert "ue-latch-calico" in ids

        v1 = client.get("/v1/catalog")
        assert v1.status_code == 200
        sessions = v1.json()["sessions"]
        assert {s["id"] for s in sessions} >= {"amf-crashloop", "ue-latch-calico"}
        for s in sessions:
            assert "turns" not in s
            assert "stdout" not in json.dumps(s)

        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        orch = client.get("/orch")
        assert orch.status_code == 200
        assert "text/html" in orch.headers.get("content-type", "")
        assert "POST /execute" in orch.text
        assert "AMF CrashLoop" in orch.text
        assert "ImagePullBackOff" in orch.text
        assert "Pending busybox" in orch.text
        assert "Payments probe" in orch.text
        assert "UE latch / Calico" in orch.text
        assert ".orch-" in client.get("/k8s-troubleshoot.css").text


def test_execute_202_then_status_completed():
    run_id = _run_id()
    with TestClient(app) as client:
        ack = client.post(
            "/execute",
            json={"run_id": run_id, "prompt": AMF_QUERY, "pace": "fast", "details": {"region": "north"}},
        )
        assert ack.status_code == 202, ack.text
        body = ack.json()
        assert body["run_id"] == run_id
        assert body["current_status"] == "ACCEPTED"
        assert body["agent"] == "K8s"
        assert "queued" in body["message"].lower()

        st = client.get("/status", params={"run_id": run_id})
        assert st.status_code == 200
        payload = st.json()
        assert payload["run_id"] == run_id
        assert payload["mission"] == AMF_QUERY
        assert payload["agent"] == "K8s"
        assert payload["current_status"] == "COMPLETED"
        assert payload["progress"] == 100
        assert payload["details"]["region"] == "north"
        assert payload["details"]["session_id"] == "amf-crashloop"
        assert payload["details"]["namespace"] == "5g-core"
        assert payload["details"]["ui_url"] == f"/?run_id={run_id}"
        assert isinstance(payload["created"], float)
        assert set(payload["sub_agents"]) == {
            "Analyze Intent",
            "Gather Context",
            "Generate Hypothesis",
            "Validate Hypothesis",
            "Report Generation",
        }
        assert all(v["status"] == "COMPLETED" for v in payload["sub_agents"].values())


def test_execute_realtime_does_not_block_and_acks_accepted():
    run_id = _run_id()
    with TestClient(app) as client:
        ack = client.post(
            "/execute",
            json={"run_id": run_id, "prompt": AMF_QUERY, "pace": "realtime"},
        )
        assert ack.status_code == 202
        assert ack.json()["current_status"] == "ACCEPTED"
        st = client.get("/status", params={"run_id": run_id})
        assert st.status_code == 200
        assert st.json()["current_status"] in ("ACCEPTED", "RUNNING")
        assert st.json()["progress"] >= 0


def test_execute_concurrent_runs():
    a = _run_id("RUN-K8S-A")
    b = _run_id("RUN-K8S-B")
    with TestClient(app) as client:
        ack_a = client.post("/execute", json={"run_id": a, "prompt": AMF_QUERY, "pace": "fast"})
        ack_b = client.post("/execute", json={"run_id": b, "prompt": BUSYBOX_QUERY, "pace": "fast"})
        assert ack_a.status_code == 202
        assert ack_b.status_code == 202
        sa = client.get("/status", params={"run_id": a}).json()
        sb = client.get("/status", params={"run_id": b}).json()
        assert sa["current_status"] == "COMPLETED"
        assert sb["current_status"] == "COMPLETED"
        assert sa["details"]["session_id"] == "amf-crashloop"
        assert sb["details"]["session_id"] == "busybox-pending"


def test_execute_idempotent_and_conflict():
    run_id = _run_id()
    with TestClient(app) as client:
        first = client.post("/execute", json={"run_id": run_id, "prompt": AMF_QUERY, "pace": "fast"})
        assert first.status_code == 202
        second = client.post("/execute", json={"run_id": run_id, "prompt": AMF_QUERY, "pace": "fast"})
        assert second.status_code == 202
        assert second.json()["run_id"] == run_id
        assert second.json()["current_status"] == "COMPLETED"
        clash = client.post("/execute", json={"run_id": run_id, "prompt": BUSYBOX_QUERY, "pace": "fast"})
        assert clash.status_code == 409


def test_execute_unmatched_polls_failed():
    run_id = _run_id()
    with TestClient(app) as client:
        ack = client.post(
            "/execute",
            json={"run_id": run_id, "prompt": "unrelated xyzzy widget", "pace": "fast"},
        )
        assert ack.status_code == 202
        st = client.get("/status", params={"run_id": run_id})
        assert st.status_code == 200
        payload = st.json()
        assert payload["current_status"] == "FAILED"
        assert "matched" in payload["current_response"].lower()


def test_status_unknown_run_404():
    with TestClient(app) as client:
        st = client.get("/status", params={"run_id": "RUN-DOES-NOT-EXIST"})
        assert st.status_code == 404
