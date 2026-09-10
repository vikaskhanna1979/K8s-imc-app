# K8s Troubleshooter — Orchestrator API Guide

| Field | Value |
| --- | --- |
| Document | Orchestrator HTTP API — K8s Troubleshooter demo |
| Audience | Teams wiring an external multi-domain orchestrator to this agent |
| Base URL | `http://<host>:8115` (local: `http://127.0.0.1:8115`) |
| Auth | None |
| Content-Type | `application/json` on all POST bodies |

This process is a **stubbed catalog player** (no live LLM, kubeconfig, or cluster). The HTTP contract matches [`api.md`](../api.md) and is the same shape a live K8s agent should keep.

Related: [In-house orchestrator](In-House-Orchestrator.md) · [Code walkthrough](Code-Walkthrough.md) · [User guide](User-Guide.md) · [LLD](LLD.md)

---

## 1. Integration model

The orchestrator **owns `run_id`**. It never waits for RCA on the trigger request.

```
Orchestrator                         K8s agent (:8115)                 Operator
     |  POST /execute {run_id, prompt}     |                              |
     |<--------- 202 ACCEPTED -------------|                              |
     |                                     |  timeline in background      |
     |  GET /status?run_id=  (1–2s)        |                              |
     |<--------- ACCEPTED / RUNNING -------|                              |
     |  GET /status?run_id=                |                              |
     |<--------- COMPLETED | FAILED -------|                              |
     |  operator clicks agent ------------>|  GET /?run_id=<run_id>      |
     |                                     |-------- live UI (current) ---|
```

**Use only these calls from the orchestrator:**

| Method | Path | Role |
| --- | --- | --- |
| `GET` | `/health` | Liveness |
| `POST` | `/execute` | Start a run (always 202 on the success path) |
| `GET` | `/status?run_id=` | Poll until terminal |
| `GET` | `/history` | Recent runs (30 min window) |
| `GET` | `/history/{run_id}` | Same payload as `/status` |
| Browser | `GET /?run_id=` | Operator UI at the **current** snapshot |

**Do not call** `POST /v1/runs` from the orchestrator. That path generates its own `run_id`, returns 422 on unknown prompts, and is for CLI / custom UIs (`run-prompt.sh`).

**Do not import** Python modules from this repo. Treat the agent as an HTTP worker.

CORS is open (`*`) by default. Restrict with `K8S_DEMO_CORS_ORIGINS` (comma-separated origins).

---

## 2. Agent registry

| Field | Value |
| --- | --- |
| Agent name | `K8s` (every `/execute` and `/status` body sets `"agent": "K8s"`) |
| Base URL | `http://<host>:8115` |
| Trigger | `POST {base}/execute` |
| Poll | `GET {base}/status?run_id=` |
| History | `GET {base}/history` |
| Operator UI | `{base}/?run_id={run_id}` or `{base}` + `details.ui_url` |
| Health | `GET {base}/health` → `{"status":"ok"}` |

`run_id` must be unique **per agent instance**. Never reuse the same id for K8s and RAN (or any other domain agent) on one mission.

Suggested id: `RUN-K8S-<mission>-<n>` e.g. `RUN-K8S-SCN-1042-1`.

---

## 3. Status machine

```
POST /execute  →  ACCEPTED     HTTP 202, progress 0
first timeline event  →  RUNNING
run finished  →  COMPLETED     progress 100
no catalog match / emitter error  →  FAILED   poll is still HTTP 200
```

| `current_status` | Meaning | Orchestrator action |
| --- | --- | --- |
| `ACCEPTED` | Queued; timeline not started | Keep polling; enable agent link |
| `RUNNING` | Workflow in progress | Keep polling; link already valid |
| `COMPLETED` | RCA finished | Stop polling; success |
| `FAILED` | Unmatched prompt or internal error | Stop polling; failure. **HTTP 200** |

There is **no cancel, pause, or resume** API.

---

## 4. `POST /execute`

Start (or idempotently re-ack) a run. The RCA continues in the background.

### Request

```http
POST /execute
Content-Type: application/json
```

```json
{
  "run_id": "RUN-K8S-SCN-1042-1",
  "prompt": "Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?",
  "details": { "mission_id": "SCN-1042", "region": "north" }
}
```

| Field | Required | Type | Notes |
| --- | --- | --- | --- |
| `run_id` | yes | string, min length 1 | Orchestrator-generated. Unique per mission/agent. |
| `prompt` | yes | string, min length 1 | Mission text; matched to a catalog session. |
| `details` | no | object | Opaque passthrough. Echoed on `/status` and merged with agent fields (`session_id`, `namespace`, `ui_url`). |
| `pace` | no | `"realtime"` \| `"fast"` | Default `realtime`. **Omit from the production orchestrator.** `fast` is for tests only (skips demo sleeps). |

### Success — HTTP 202

New run (or first ack while still queued):

```json
{
  "run_id": "RUN-K8S-SCN-1042-1",
  "current_status": "ACCEPTED",
  "agent": "K8s",
  "message": "K8s activity queued"
}
```

Idempotent retry (same `run_id` + same `prompt`) also returns **202**, but `current_status` / `message` reflect **current** state:

| `current_status` | `message` |
| --- | --- |
| `ACCEPTED` | `K8s activity queued` |
| `RUNNING` | `K8s activity running` |
| `COMPLETED` | `K8s activity completed` |
| `FAILED` | `K8s activity failed` |

A **new** execute always acks `ACCEPTED` even if `pace=fast` already finished the timeline before the response is built. Always poll `/status` for truth.

### Errors

| HTTP | When | Body |
| --- | --- | --- |
| **202** then poll `FAILED` | Prompt matches no catalog session | Ack is still 202. `/status` → `FAILED`, `current_response` contains `No catalog session matched that query` |
| **409** | Same `run_id`, **different** prompt | `{"detail":"run_id already used with a different prompt"}` |
| **422** | Missing/empty `run_id` or `prompt`, or invalid `pace` | FastAPI validation error |

Unmatched prompt is a **business failure**, not a transport failure: 202 + later `FAILED`.

### curl

```bash
curl -sS -X POST http://127.0.0.1:8115/execute \
  -H 'Content-Type: application/json' \
  -d '{
    "run_id": "RUN-K8S-SCN-1042-1",
    "prompt": "Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?",
    "details": { "region": "north" }
  }'
```

---

## 5. `GET /status?run_id=`

Poll until `COMPLETED` or `FAILED`. **Interval: 1–2 seconds.**

```http
GET /status?run_id=RUN-K8S-SCN-1042-1
```

### Success — HTTP 200 (including FAILED)

```json
{
  "run_id": "RUN-K8S-SCN-1042-1",
  "mission": "Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?",
  "agent": "K8s",
  "current_status": "RUNNING",
  "current_response": "Iteration 1 · Analyzing output",
  "progress": 41,
  "details": {
    "region": "north",
    "session_id": "amf-crashloop",
    "namespace": "5g-core",
    "ui_url": "/?run_id=RUN-K8S-SCN-1042-1"
  },
  "created": 1788960000.12,
  "expires_at": 1788961800.12,
  "sub_agents": {
    "Analyze Intent":        { "status": "COMPLETED", "response": "completed" },
    "Gather Context":        { "status": "COMPLETED", "response": "completed" },
    "Generate Hypothesis":   { "status": "COMPLETED", "response": "completed" },
    "Validate Hypothesis":   { "status": "RUNNING",   "response": "Iteration 1 · Analyzing output" },
    "Report Generation":     { "status": "QUEUED",    "response": "queued" }
  }
}
```

### Field reference

| Field | Type | Use in orchestrator UI |
| --- | --- | --- |
| `run_id` | string | Correlation key you sent |
| `mission` | string | The prompt |
| `agent` | string | Always `"K8s"` |
| `current_status` | enum | Chip: `ACCEPTED` / `RUNNING` / `COMPLETED` / `FAILED` |
| `current_response` | string | One-line subtitle |
| `progress` | int 0–100 | Progress bar (demo estimate, not wall-clock remaining) |
| `created` | float (unix seconds) | Start time |
| `expires_at` | float | `created + ttl` (default +1800s). Terminal runs drop after this. |
| `details.*` | object | Your POST `details` **plus** agent keys below |
| `details.ui_url` | string | Relative: `/?run_id=...` |
| `details.session_id` | string \| null | Catalog id (`amf-crashloop`, …); empty/null if unmatched |
| `details.namespace` | string \| null | Cluster ns once topology is known |
| `sub_agents` | object | Five workflow nodes (keys are **display names**) |

`sub_agents[*].status`: `QUEUED` | `RUNNING` | `COMPLETED` | `FAILED`.

- While `ACCEPTED`, every node is `QUEUED`.
- On `COMPLETED`, every node is `COMPLETED`.

Typical `current_response` values:

- `ACCEPTED` → `K8s accepted - waiting in queue`
- `RUNNING` → workflow caption, e.g. `Iteration 2 · Running kubectl`
- `COMPLETED` → final caption, e.g. `Resolved · remedial action suggested`
- `FAILED` → error string, e.g. `No catalog session matched that query`

### Errors

| HTTP | When |
| --- | --- |
| **404** | Unknown `run_id`, or terminal run older than TTL |
| **422** | Missing `run_id` query param |

Do **not** treat `FAILED` as an HTTP error. Check `current_status`.

### Progress (informational)

| State | Typical `progress` |
| --- | --- |
| `ACCEPTED` | 0 |
| Analyze running/done | 10 |
| Gather Context | 25 |
| Iteration *n* | `min(90, 25 + n*16)` |
| Report running | 95 |
| `COMPLETED` | 100 |
| `FAILED` | often 0 |

Realtime duration to plan poll timeouts: gather ~1.6s, kubectl ~2s, analyzing **5–10s per iteration**. AMF 4 iterations ≈ 40–60s; most others 3 ≈ 30–45s; Calico latch 6 iterations longer.

---

## 6. History

Runs live in **this process memory**. Default TTL **1800 seconds (30 min)** from `created`, override with `K8S_RUN_HISTORY_TTL_SEC`.

- `ACCEPTED` / `RUNNING` are **never** pruned.
- `COMPLETED` / `FAILED` disappear after TTL; then `/status` and `/history/{run_id}` return **404**.

### `GET /history`

Newest first.

```json
{
  "ttl_sec": 1800,
  "count": 1,
  "runs": [
    {
      "run_id": "RUN-K8S-SCN-1042-1",
      "mission": "Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?",
      "agent": "K8s",
      "current_status": "COMPLETED",
      "progress": 100,
      "session_id": "amf-crashloop",
      "created": 1788960000.12,
      "expires_at": 1788961800.12,
      "ui_url": "/?run_id=RUN-K8S-SCN-1042-1",
      "current_response": "Resolved · remedial action suggested",
      "title": "AMF CrashLoop · 5g-core"
    }
  ]
}
```

Suggested poll: every 5s for a “Recent runs” panel.

### `GET /history/{run_id}`

Same JSON as `GET /status?run_id=` (includes `expires_at`). Unknown/expired → **404**.

---

## 7. Operator UI (join, do not replay)

After 202, enable **Open K8s Troubleshooter** immediately:

```
{AGENT_BASE}/?run_id={run_id}
```

or

```
{AGENT_BASE.rstrip("/")} + status["details"]["ui_url"]
```

Rules:

1. Join shows the **current** snapshot (not Analyze Intent from t=0).
2. Same URL stays live over SSE while `RUNNING`.
3. After `COMPLETED` / `FAILED`, the same URL still works (summary or error).
4. Open in a **new tab or dedicated iframe**. Never reuse one iframe for two `run_id`s.
5. The joined page disables the intent bar so the operator cannot start a second local replay.

Unknown `run_id` in the UI: match line **Run not found**.

---

## 8. Demo catalog (prompts to ship)

The agent matches `prompt` to a session (token score; threshold 15). Use these strings for demos.

| Session id | Prompt (copy this) | Iterations | Typical duration |
| --- | --- | --- | --- |
| `amf-crashloop` | `Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?` | 4 | ~40–60s |
| `imagepull-nginx` | `why is imagepull-demo failing` | 3 | ~30–45s |
| `busybox-pending` | `why is busybox-pod in pending state` | 3 | ~30–45s |
| `payments-crashloop` | `Why is pod api-0 CrashLoopBackOff in payments?` | 3 | ~30–45s |
| `ue-latch-calico` | `Customer unable to latch on the network` | 6 | longer |

On success, `/status` `details.session_id` must match the table. Optional summaries: `GET /v1/catalog` (ids, titles, prompts, keywords; no stdout).

---

## 9. Orchestrator client (reference)

```python
from __future__ import annotations

import time
from typing import Any

import httpx

TERMINAL = frozenset({"COMPLETED", "FAILED"})


class K8sAgentClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def health(self) -> dict[str, str]:
        r = httpx.get(f"{self.base}/health", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def execute(
        self, run_id: str, prompt: str, details: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        r = httpx.post(
            f"{self.base}/execute",
            json={"run_id": run_id, "prompt": prompt, "details": details or {}},
            timeout=self.timeout,
        )
        if r.status_code == 409:
            raise ValueError("run_id already used with a different prompt")
        if r.status_code != 202:
            r.raise_for_status()
        return r.json()

    def status(self, run_id: str) -> dict[str, Any]:
        r = httpx.get(
            f"{self.base}/status", params={"run_id": run_id}, timeout=self.timeout
        )
        if r.status_code == 404:
            raise LookupError(f"run unknown or expired: {run_id}")
        r.raise_for_status()
        return r.json()

    def history(self) -> dict[str, Any]:
        r = httpx.get(f"{self.base}/history", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def ui_url(
        self,
        run_id: str | None = None,
        status_payload: dict[str, Any] | None = None,
    ) -> str:
        if status_payload:
            return self.base + status_payload["details"]["ui_url"]
        if not run_id:
            raise ValueError("run_id or status_payload required")
        return f"{self.base}/?run_id={run_id}"

    def poll_until_done(self, run_id: str, every_sec: float = 1.0, on_tick=None) -> dict[str, Any]:
        while True:
            st = self.status(run_id)
            if on_tick:
                on_tick(st)
            if st["current_status"] in TERMINAL:
                return st
            time.sleep(every_sec)
```

Mission wiring:

```python
ack = client.execute(run_id, prompt, details={"mission_id": mission_id})
assert ack["current_status"] == "ACCEPTED"
set_agent_href(client.ui_url(run_id))  # clickable immediately
final = client.poll_until_done(run_id, on_tick=update_orchestrator_row)
# final["current_status"] in {"COMPLETED", "FAILED"}
```

A working browser client of the same three calls is `GET /orch` (`static/k8s-orchestrator.html`).

---

## 10. HTTP status cheat sheet

| Code | Endpoint | Meaning |
| --- | --- | --- |
| 202 | `POST /execute` | Accepted (including unmatched prompt and idempotent retry) |
| 200 | `GET /status`, `/history`, `/history/{id}` | Payload OK. Inspect `current_status` for FAILED |
| 404 | `/status`, `/history/{id}`, UI join | Unknown or expired `run_id` |
| 409 | `POST /execute` | `run_id` reused with a different prompt |
| 422 | `POST /execute` | Validation (missing fields) |

FastAPI error bodies typically look like `{"detail": "..."}` or a validation list.

---

## 11. Operational constraints

1. **In-memory only.** Restarting uvicorn drops every `run_id`. Persist mission↔run mapping on the orchestrator side; treat 404 after restart as agent restart, not a deleted mission.
2. **One process.** This demo is not safe to scale horizontally; SSE and `/status` would not follow the run.
3. **TTL.** Keep orchestrator history if the parent UI needs more than 30 minutes.
4. **Concurrency.** Multiple `run_id`s in one process are supported and stay independent (`session_id` must not swap).
5. **Idempotency.** Safe to retry `POST /execute` with the same id + prompt (e.g. after a lost 202).
6. **No auth.** Demo network only.
7. **`pace`.** Orchestrator should omit it. Use `fast` only in automated tests.

---

## 12. Out of scope for this API

| Capability | Status |
| --- | --- |
| Cancel / pause / resume | Not implemented |
| AuthN / AuthZ | None |
| Live kubectl / LLM | Stub catalog only |
| `POST /v1/runs` + SSE | Demo/CLI only; orchestrator must not use |

SSE (`GET /v1/runs/{id}/stream`) is how the **agent UI** stays live after join. The orchestrator should poll `/status`, not subscribe to SSE, unless you are building a custom Troubleshooter clone.

---

## 13. Quick test (before wiring UI)

```bash
curl -sS http://127.0.0.1:8115/health

curl -sS -X POST http://127.0.0.1:8115/execute \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"RUN-K8S-AMF-1","prompt":"Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?"}'

curl -sS "http://127.0.0.1:8115/status?run_id=RUN-K8S-AMF-1"
```

Then open `http://127.0.0.1:8115/?run_id=RUN-K8S-AMF-1`.

Toy orchestrator already in this app: `http://127.0.0.1:8115/orch`. How that page calls APIs and functions: [In-House-Orchestrator.md](In-House-Orchestrator.md).
