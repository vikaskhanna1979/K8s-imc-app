# Low-Level Design: K8s Troubleshooter Demo Agent

| Field | Value |
| --- | --- |
| Document | LLD — K8s Troubleshooter demo |
| Version | 1.1 |
| Status | As-implemented (stubbed LLM) |
| Code root | `k8s-troubleshoot-demo/` |
| Runtime | FastAPI + uvicorn on port **8115** |

### Document map

| Document | Audience | Use it for |
| --- | --- | --- |
| **This LLD** | Implementers changing the agent | As-built modules, catalog schema, snapshot/event contract, stub→live replacement |
| [Code-Walkthrough.md](Code-Walkthrough.md) | Engineers new to this module | Folder map, Python patterns, how a run moves through the code |
| [Orchestrator-API.md](Orchestrator-API.md) | External orchestrator teams | HTTP contract: `/execute`, `/status`, `/history`, UI join. **Do not import Python from this repo.** |
| [In-House-Orchestrator.md](In-House-Orchestrator.md) | Engineers tracing `GET /orch` | JS functions, APIs called, multi-run isolation, status polling |
| [User-Guide.md](User-Guide.md) | Testers | Scenario prompts, poll timing, checklists |
| [api.md](../api.md) | Multi-domain envelope | Shared 202 / status / history JSON shape (examples use `"agent": "RAN"`; this app sets `"K8s"`) |

---

## 1. Purpose

This document describes the **as-built** low-level design of the standalone K8s Troubleshooter demo.

The application **looks** like an agentic Kubernetes RCA loop (intent → context → hypothesis → kubectl → analysis → report). In the current code there is **no live LLM, no kubeconfig, no cluster access, and no TCAP / agent-runtime-service**. Every “AI” string and every `kubectl` result is a **pre-authored stub** in `static/k8s-demo-sessions.json`.

The demo exists to:

1. Replay known troubleshooting sessions with realistic timing.
2. Drive the Troubleshooter UI (workflow graph, topology, iteration cards, session summary).
3. Expose a snapshot/SSE API so a custom UI can consume the same timeline.
4. Expose an orchestrator contract (`POST /execute`, `GET /status`, `GET /history`) so a parent mission controller can start, poll, and open this agent like any other domain worker.

This LLD is the contract for replacing those stubs with a real LLM + kubectl executor later, without changing the UI event model **or** the orchestrator HTTP envelope. Request/response field lists for an external orchestrator live in [Orchestrator-API.md](Orchestrator-API.md); this LLD describes how those routes are implemented.

---

## 2. Scope

### In scope

- Isolated FastAPI process and static UI.
- Catalog matching (user query → canned session).
- Timed replay of workflow / topology / iteration snapshots.
- In-memory run store and SSE fan-out.
- Catalog session schema (including LLM stub fields).
- Orchestrator HTTP envelope: `POST /execute` (202), `GET /status`, `GET /history` — implemented here, specified in [Orchestrator-API.md](Orchestrator-API.md).

### Out of scope (explicitly not implemented)

| Capability | Current state |
| --- | --- |
| LLM client (OpenAI / Azure / local) | Stub strings in catalog |
| Live `kubectl` / kubeconfig | Stub `command` + `stdout` |
| Knowledge / SOP retrieval | Stub `knowledge` + `final.knowledgeEngine` |
| Session-history RAG | Stub `history` (score / fed) |
| HITL / Creation mode apply | Mentioned only in `final.creationHint` |
| Persistence | In-memory `RunStore`; lost on process restart |
| AuthN / AuthZ | None; CORS default `*` |
| Cancel / pause / resume | No API; orchestrator can only start, poll, and open the UI |

---

## 3. System context

```
 External orchestrator          k8s-troubleshoot-demo (:8115)
  POST /execute ──────────────►│  match catalog, create Run (ACCEPTED)
  GET  /status  ◄──────────────│  poll ACCEPTED → RUNNING → COMPLETED|FAILED
  GET  /history ◄──────────────│  30-minute in-memory list
  open /?run_id= ─────────────►│  Troubleshooter joins current snapshot + SSE

 Browser (standalone) ────────►│  GET /  → HTML (client-side replay)
 curl / custom UI ────────────►│  POST /v1/runs → server replay + SSE (not for orchestrators)

 Catalog: static/k8s-demo-sessions.json  (stubbed LLM + kubectl + topology)
         No TCAP portal, no agent-runtime-service, no cluster
```

Four consumers share the same catalog. The isolated UI can still replay locally. When opened with `/?run_id=`, it joins the **server** timeline at the current snapshot. An external orchestrator must use `/execute` + `/status` only — see [Orchestrator-API.md](Orchestrator-API.md).

| Path | Entry | Matcher | Timeline | Timing |
| --- | --- | --- | --- | --- |
| **UI (standalone)** | `GET /` → `k8s-troubleshoot-replay.html` | JS `matchSession` | JS `buildSteps` + `applyStep` | `setTimeout` |
| **UI (orchestrator join)** | `GET /?run_id=` | Server already matched | `GET .../events` then SSE | Live; no replay from t=0 |
| **Demo API** | `POST /v1/runs` | Python `match_session` | `timeline_events` | `asyncio.sleep` (`pace=realtime`) or immediate (`pace=fast`) |
| **Orchestrator** | `POST /execute` | Python `match_session` | Same emitter, orchestrator `run_id` | Always async 202; poll `GET /status` |

---

## 4. Module map

```
k8s-troubleshoot-demo/
├── app/
│   ├── main.py          # FastAPI routes, CORS, SSE, emit_run, /execute, /status, /history
│   ├── replay.py        # catalog match + step timeline + snapshots
│   ├── orch_status.py   # orchestrator GET /status and /history DTOs
│   └── store.py         # Run dataclass + in-memory store + 30-min history TTL
├── static/
│   ├── k8s-demo-sessions.json       # stub catalog (source of truth)
│   ├── k8s-troubleshoot-replay.html # isolated UI
│   ├── k8s-orchestrator.html        # toy orchestrator (execute + poll + history)
│   └── k8s-troubleshoot.css         # tokenized component CSS
├── docs/
│   ├── LLD.md                       # this file
│   ├── Code-Walkthrough.md          # newcomer path through the code
│   ├── Orchestrator-API.md          # external orchestrator HTTP contract
│   ├── In-House-Orchestrator.md     # GET /orch trigger, poll, history
│   └── User-Guide.md                # tester checklist
├── api.md                           # shared execute/status/history envelope
├── tests/test_replay_api.py
└── run-prompt.sh                    # CLI client of /v1/runs
```

### 4.1 `app/main.py` — HTTP boundary

Responsibilities:

- Load catalog at import and again on lifespan startup (`_load_catalog`).
- Serve UI, CSS, and raw catalog JSON.
- Create runs, emit events, stream SSE.
- Orchestrator `POST /execute` (202), `GET /status`, `GET /history`, `GET /history/{run_id}`.

Process-global state:

- `CATALOG: dict` — parsed JSON.
- `store: RunStore` — all live runs.

Environment:

| Variable | Default | Effect |
| --- | --- | --- |
| `K8S_DEMO_CORS_ORIGINS` | `*` | CORS allow list (comma-separated) |
| `K8S_DEMO_PORT` | `8115` | Used only by `python -m app.main` |
| `K8S_RUN_HISTORY_TTL_SEC` | `1800` | How long terminal runs stay in `GET /history` / `GET /status` (seconds). In-flight runs are never pruned. |

### 4.2 `app/replay.py` — matching and timeline

Pure functions. No I/O. Given a session + query + `run_id`, produces an ordered list of **full snapshots** (not deltas).

Key exports:

| Function | Role |
| --- | --- |
| `tokens` / `score_session` / `match_session` | Intent → session |
| `catalog_summaries` | Public catalog without turns/stdout |
| `build_steps` | Session → ordered step list |
| `delay_ms_for` | Per-step demo delay |
| `apply_index` | Steps[0..idx] → workflow/iteration UI state |
| `build_snapshot` | State → wire snapshot |
| `timeline_events` | Full event list for one run |

### 4.3 `app/store.py` — run lifecycle

`Run` is the unit of work. Orchestrator runs use the caller-supplied `run_id` and start at `orch_status=ACCEPTED`. `RunStore.append` assigns `seq`, updates `snapshot` / `status` / `orch_status` (`RUNNING` → `COMPLETED` / `FAILED`) and `finished_at`, and `put_nowait`s to SSE subscriber queues. Duplicate execute with a different prompt raises `RunConflict` (HTTP 409).

`RunStore` keeps runs for `ttl_sec` (default 1800) measured from `created`. Lazy `prune()` on create/get/list drops terminal `COMPLETED`/`FAILED` runs past that window. `ACCEPTED`/`RUNNING` are never dropped. `list_history()` returns remaining runs newest-first. `expires_at` on API payloads is `created + ttl_sec`.

### 4.4 `app/orch_status.py` — orchestrator DTOs

Pure mapping from `Run` + latest snapshot → the JSON an external orchestrator binds in its UI. Does not emit events or match catalog.

| Function | Role |
| --- | --- |
| `build_status` | `GET /status` and `GET /history/{run_id}` payload (`mission`, `progress`, `sub_agents`, `details.ui_url`, `expires_at`) |
| `build_history_list` / `build_history_item` | `GET /history` rows (newest first) |
| `execute_ack` | 202 body: `{ run_id, current_status, agent: "K8s", message }` |
| `progress_for` | Demo bar 0–100 from workflow nodes / iteration (not wall-clock remaining) |

`sub_agents` keys are display names: Analyze Intent, Gather Context, Generate Hypothesis, Validate Hypothesis, Report Generation. Node `pending`/`running`/`done` maps to `QUEUED`/`RUNNING`/`COMPLETED`. Field-level contract: [Orchestrator-API.md](Orchestrator-API.md).

### 4.5 Static UI

Single-file HTML. Standalone mode: same scoring, step list, delays, and band-reveal logic as Python. Orchestrator join mode (`/?run_id=`): fetch buffered snapshots, `applySnapshot` each (no timed replay from the start), then `EventSource` for the remainder. Intent controls stay disabled while attached to an orchestrator run.

`GET /orch` serves `k8s-orchestrator.html`: five scenario cards plus a **Recent runs** panel that polls `GET /history` every 5s.

---

## 5. Intended agent loop vs stubbed loop

The product workflow the UI encodes:

```
Analyze Intent
      │
Gather Context  (KB + history + topology)
      │
      ▼
┌─ Generate Hypothesis  ◄──────────────┐
│         │                            │
│   Validate Hypothesis                │  per catalog turn
│         │                            │
│   (kubectl + analyze stdout)         │
└─────────┴────────────────────────────┘
      │  solution found or max retries
Report Generation
```

### 5.1 What a live agent would do at each node

| Workflow node | Live behavior (not implemented) | Stub source |
| --- | --- | --- |
| **Analyze Intent** | Classify Diagnose vs Create; extract ns / object / symptom | `session.route.mode`, `session.route.reason` |
| **Gather Context** | Retrieve SOP/KB, similar past sessions, cluster topology | `session.knowledge`, `session.history`, `session.topology` |
| **Generate Hypothesis** | LLM: given intent + context + prior takeaways, emit hypothesis + next kubectl | `turn.hypothesis`, `turn.command`, `turn.llm` |
| **Validate Hypothesis** | Execute kubectl, feed stdout back to LLM for analysis / KTAs / next command or stop | `turn.stdout`, `turn.analysis`, `turn.takeaways` |
| **Report Generation** | LLM: RCA, evidence, remedial action, HITL hint | `session.final` |

### 5.2 Reveal bands (progressive disclosure)

Each turn is not shown all at once. `apply_index` / JS `applyStep` maintain a **band** of booleans per turn:

| Phase (`turn.phase`) | Event type | `hyp` | `cmd` | `out` | `analyzing` | `analysis` |
| --- | --- | --- | --- | --- | --- | --- |
| `generate` | `iteration.generate` | ✓ | | | | |
| `command` | `iteration.command` | ✓ | ✓ | ✓ | | |
| `output` | `iteration.analyzing` | ✓ | ✓ | ✓ | ✓ | |
| `validate` | `iteration.complete` | ✓ | ✓ | ✓ | ✓ | ✓ |

This is how the UI fakes streaming: hypothesis first, then command+stdout, then “Analyzing output…”, then analysis + takeaways.

`turn.llm` is **not** copied onto the wire snapshot. The API exposes the expanded fields (`hypothesis`, `command`, `stdout`, `takeaways`, `analysis`). The nested `llm` object is catalog metadata (compact stand-in for a model JSON response).

---

## 6. Catalog data model

File: `static/k8s-demo-sessions.json`

```
{
  "version": 1,
  "source": "... No live LLM or cluster.",
  "sessions": [ Session, ... ],
  "howToAdd": "..."
}
```

### 6.1 Session

| Field | Type | Used by |
| --- | --- | --- |
| `id` | string | Match result, `run.session_id` |
| `title` | string | UI, API, graph caption on `run.started` |
| `description` | string | Matcher (token score +3) |
| `prompts[]` | string | Matcher (exact=1000, substring=80, token=8) |
| `keywords[]` | string | Matcher (+20) |
| `intent` | string | UI problem line if user query empty |
| `route.mode` / `route.reason` | string | Context cards after Gather Context |
| `history.title` / `history.meta` | string | Context cards (prior RCA feed) |
| `knowledge.title` / `knowledge.meta` | string | Context cards (SOP hit) |
| `turns[]` | Turn | Iteration loop |
| `maxRetries` | int | UI report header (default 8); not enforced by Python emitter |
| `final` | Final | Report card on `run.finished` |
| `topology` | Topology | Shown only after Gather Context |

### 6.2 Turn (one diagnose iteration)

| Field | Meaning |
| --- | --- |
| `id` | Integer index (0-based). Must match `build_steps` turn id. |
| `title` | Card header, e.g. `Iteration 1` |
| `time` | Display timestamp |
| `validation` | Badge: `New` / `Confirmed` |
| `hypothesis` | Full narrative shown in Hypothesis block |
| `fromPrior` | Optional “fed from previous turn” line |
| `command` | Stub kubectl (shown in AI pane + terminal) |
| `stdout` | Stub command output |
| `analysis` | Stub LLM interpretation of stdout |
| `takeaways[]` | Key Findings (KTA) bullets |
| `llm` | Compact stub of a structured model reply (see 6.4) |

### 6.3 Final report

| Field | Meaning |
| --- | --- |
| `outcome` | `resolved` or anything else → unresolved / max retries caption |
| `retriesUsed` | Shown as `retries N of maxRetries` |
| `solution` | RCA paragraph |
| `remedialAction` | Suggested fix (diagnose-only; not applied) |
| `knowledgeEngine` | Long KB/history synthesis for the report |
| `creationHint` | Copy directing user to Creation mode + HITL |
| `lastHypothesis` | Last turn hypothesis echoed in Final status |
| `exhaustedMessage` | Unresolved path copy |

### 6.4 Nested `turn.llm` stub (LLM response contract)

This is the **intended structured LLM output** per iteration. The demo never parses it at runtime; UI/API read the sibling expanded fields instead.

```json
{
  "hypothesis": "short working theory",
  "command": "kubectl ... | null if this turn closes the loop",
  "knowledge_insights": "optional SOP citation",
  "key_takeaways": "optional compact KTA string",
  "solution": "present on the last turn when RCA is ready"
}
```

**Replacement rule for a live agent:** one LLM call per generate/analyze step should return this object (or a superset). The orchestrator would then:

1. Show `hypothesis`.
2. If `command` is non-null, execute it and attach real stdout.
3. Call the model again with stdout to fill `analysis` / `takeaways` (or use a single tool-calling loop).
4. If `command` is null and `solution` is set, exit the loop and build `final`.

### 6.5 Topology

Two shapes:

1. **Flat** — `nodes[]` + `edges[]` on the session (imagepull, busybox, payments).
2. **Flavored** — `topology.flavors[id] = { label, nodes, edges }` (AMF: `open5gs` default, `oai` alternate). UI select `#topoFlavor` switches flavor locally; **the API snapshot always uses the default flavor** (`active_topology(session)` without UI override).

Node fields: `id`, `role`, `name`, `kind`, `status` (`running` | `crashloop` | `pending` | `imagepull` | `error`), `x`, `y`.

Edge fields: `from`, `to`, `label`.

Hot-highlight: a node is “hot” if the current kubectl command mentions its `name`/`role`, or if it is `topology.focus` and unhealthy.

### 6.6 Shipped sessions

| `id` | Symptom | Turns | RCA (stub) |
| --- | --- | --- | --- |
| `amf-crashloop` | CrashLoopBackOff `open5gs-amf-0` in `5g-core` | 4 | Missing `AMF_BIND_ADDR` |
| `imagepull-nginx` | ImagePullBackOff `imagepull-demo` in `ts` | 3 | Image tag `nginx:lastest` |
| `busybox-pending` | Pending `busybox-pod` in `ts` | 3 | CPU request 4 > allocatable 2 |
| `payments-crashloop` | CrashLoopBackOff `api-0` in `payments` | 3 | Liveness `initialDelaySeconds=3` vs 14s startup |
| `ue-latch-calico` | Customer unable to latch on the network | 6 | Calico `calico-node` 61–63 restarts |

Diagnose policy encoded in every stub: **never apply/create**; stop at exact RCA and point Creation mode + HITL.

---

## 7. Intent matching

Identical algorithm in `replay.py` and the HTML.

1. Normalize: lowercase, keep `[a-z0-9_-]`, drop tokens of length ≤ 1 and a stopword set (`the`, `pod`, `why`, `namespace`, …).
2. Score each session:
   - Exact prompt match → **1000** (short-circuit).
   - Query length ≥ 12 and query ⊆ prompt or prompt ⊆ query → **+80**.
   - Shared prompt tokens → **+8** each.
   - Keyword in query or in token set → **+20** each.
   - Shared tokens from `title` + `description` → **+3** each.
3. Pick highest score. Reject if score **< 15**.

API: unmatched query → HTTP **422** `{ "detail": "No catalog session matched that query" }`.  
UI: show “No matching issue. Try: …” and do not play.

---

## 8. Step timeline

`build_steps(session)` produces a linear script. For `N` turns:

| Index pattern | `kind` | `id` / `phase` | Caption |
| --- | --- | --- | --- |
| 0 | `node` | `analyze` | Analyze Intent · diagnose |
| 1 | `node` | `context` | Gather Context · knowledge, history, topology |
| 2 | `context` | — | Cluster topology gathered |
| 3 + 4i | `turn` | `generate` | Iteration i+1 · Generate Hypothesis |
| 4 + 4i | `turn` | `command` | Iteration i+1 · Running kubectl |
| 5 + 4i | `turn` | `output` | Iteration i+1 · Analyzing output |
| 6 + 4i | `turn` | `validate` | … Validate Hypothesis [· solution found on last] |
| after turns | `node` | `report` | Report Generation |
| last | `final` | — | Resolved · remedial action suggested **or** Max retries reached |

`generate` / `validate` nodes are **not** in the initial node list as separate step kinds; they are driven by turn phases. Workflow node ids on the graph are always:

`analyze` → `context` → (`generate` ⇄ `validate`) → `report`

### 8.1 Delays (`delay_ms`)

| Step | Delay |
| --- | --- |
| node `context` | 1600 ms |
| kind `context` (topology reveal) | 1200 ms |
| turn `generate` | 1100 ms |
| turn `command` | 2000 ms |
| turn `output` | 5000–10000 ms (RNG) |
| turn `validate` | 1500 ms |
| other | 800 ms |

Semantics:

- **`pace=realtime`**: server `await asyncio.sleep(delay_ms/1000)` *before the next event is appended* (the delay is stored on the event that was just emitted).
- **`pace=fast`**: all events appended immediately; `delay_ms` still present so a client can animate (`run-prompt.sh --instant` vs timed view).
- UI: `nextDelay()` uses the *current* step’s delay as the wait *until the next tick*.

### 8.2 Event types

| `type` | When | UI meaning |
| --- | --- | --- |
| `run.started` | Before any step (`apply_index(..., -1)`) | All nodes pending |
| `workflow.node` | Analyze / Gather Context / Report node | Node `running` |
| `topology.ready` | After Gather Context delay | `topology.visible = true` |
| `iteration.generate` | Hypothesis band | Generate node running |
| `iteration.command` | kubectl + stdout | Validate node running |
| `iteration.analyzing` | Analyzing placeholder | Validate still running |
| `iteration.complete` | Full analysis + KTAs | Treat as iteration done |
| `run.finished` | Final report | All nodes `done` |
| `run.error` | Emitter exception | Stream closes |

`topology.visible` is **false** on every snapshot until `topology.ready` inclusive.

---

## 9. Snapshot contract (wire format)

Every SSE `message` and every buffered event is a **full snapshot**, not a patch.

```json
{
  "type": "iteration.complete",
  "seq": 12,
  "run_id": "d-c5220332f937",
  "session_id": "amf-crashloop",
  "title": "AMF CrashLoop · 5g-core",
  "query": "Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?",
  "delay_ms": 1500,
  "workflow": {
    "iter": 4,
    "caption": "Iteration 4 · Validate Hypothesis · solution found",
    "nodes": [
      { "id": "analyze", "status": "done" },
      { "id": "context", "status": "done" },
      { "id": "generate", "status": "done" },
      { "id": "validate", "status": "running" },
      { "id": "report", "status": "pending" }
    ]
  },
  "topology": {
    "visible": true,
    "namespace": "5g-core",
    "focus": "amf",
    "flavor": "open5gs",
    "nodes": [ /* ... */ ],
    "edges": [ /* ... */ ]
  },
  "iteration": {
    "id": 3,
    "phase": "validate",
    "hypothesis": "...",
    "command": "kubectl get deploy open5gs-amf -n 5g-core -o yaml",
    "stdout": "...",
    "takeaways": ["..."],
    "analysis": "..."
  },
  "final": null
}
```

Node `status`: `pending` | `running` | `done`.

`iteration` is `null` outside turn steps. Fields inside `iteration` are `null` when their band is off.

`final` is `null` until the `final` step; then a deep copy of `session.final`.

`run.error` additionally sets `error` (string) and empty workflow/topology.

---

## 10. HTTP API

Base URL: `http://<host>:8115`

**Split of concerns**

| Consumer | Routes | Spec |
| --- | --- | --- |
| External orchestrator | `POST /execute`, `GET /status`, `GET /history`, `GET /history/{run_id}`, `GET /?run_id=`, `GET /health` | **[Orchestrator-API.md](Orchestrator-API.md)** — request/response fields, errors, client sketch |
| Demo / CLI / custom Troubleshooter | `POST /v1/runs`, `GET /v1/runs/{id}`, `/events`, `/stream`, `GET /v1/catalog` | This section (10.1–10.2) |
| Operators / testers | `GET /`, `GET /orch` | [User-Guide.md](User-Guide.md) |

Orchestrators must **not** call `POST /v1/runs` (server-generated `run_id`, unmatched prompt → 422 instead of 202 then `FAILED`).

| Method | Path | Behavior |
| --- | --- | --- |
| `GET` | `/health` | `{ "status": "ok" }` |
| `GET` | `/` | Isolated UI HTML |
| `GET` | `/orch` | Lightweight orchestrator console (scenario cards + recent runs) |
| `GET` | `/k8s-troubleshoot.css` | Component stylesheet |
| `GET` | `/k8s-demo-sessions.json` | Full catalog (includes stdout / llm stubs) |
| `GET` | `/v1/catalog` | Summaries only: `id`, `title`, `description`, `prompts`, `keywords` — **no turns / stdout** |
| `POST` | `/execute` | Orchestrator: `{ run_id, prompt, details? }` → **202** `{ run_id, current_status: ACCEPTED, agent, message }` |
| `GET` | `/status?run_id=` | Orchestrator poll (`ACCEPTED` / `RUNNING` / `COMPLETED` / `FAILED`). Unknown or expired run → 404. FAILED is still HTTP 200. Includes `expires_at`. |
| `GET` | `/history` | `{ ttl_sec, count, runs[] }` — in-memory runs still inside the 30-minute window, newest first |
| `GET` | `/history/{run_id}` | Same payload as `GET /status` (plus `expires_at`). Unknown or expired → 404. |
| `POST` | `/v1/runs` | Body `{ "query": str, "pace": "realtime"\|"fast" }` |
| `GET` | `/v1/runs/{id}` | `{ run_id, session_id, title, status, seq, snapshot }` |
| `GET` | `/v1/runs/{id}/events?after_seq=0` | Buffered events with `seq > after_seq` |
| `GET` | `/v1/runs/{id}/stream?after_seq=0` | SSE |

### 10.1 `POST /v1/runs`

Success 200:

```json
{
  "run_id": "d-<12 hex>",
  "session_id": "amf-crashloop",
  "title": "AMF CrashLoop · 5g-core",
  "status": "running",
  "stream_url": "/v1/runs/{run_id}/stream",
  "events_url": "/v1/runs/{run_id}/events"
}
```

- `pace=fast`: `await emit_run(...)` so the response returns after the run is finished (status may already be `finished` in store; the POST body still reports the status at create time, which is `running` — callers should read events/snapshot).
- `pace=realtime`: `asyncio.create_task(emit_run)` — fire and forget; client must SSE or poll.

### 10.2 SSE protocol

- Event name `message`, data = JSON snapshot.
- Event name `ping`, data empty, every 15s while waiting.
- Generator exits on `run.finished` or `run.error`.
- Late subscribers: replay buffered events with `seq > after_seq`, then attach a queue.

Concurrency: each `Run.subscribers` is a `set[asyncio.Queue]`. `append` uses `put_nowait`; `QueueFull` is swallowed (queues are unbounded by default, so this is defensive).

### 10.3 Status machine

Demo API (`POST /v1/runs`):

```
create → status=running
append(run.finished) → status=finished, orch_status=COMPLETED
append(run.error)    → status=error, orch_status=FAILED
```

Orchestrator (`POST /execute`):

```
create → orch_status=ACCEPTED, HTTP 202
first timeline event → RUNNING
run.finished → COMPLETED
run.error / catalog miss → FAILED (poll HTTP 200)
```

No cancel / pause API. Do not mix the two `status` vocabularies: orchestrators poll `orch_status` (`ACCEPTED` / `RUNNING` / `COMPLETED` / `FAILED`) via `/status`. The demo API reports `run.status` (`accepted` / `running` / `finished` / `error`) on `/v1/runs/{id}`.

Wire-level field lists, idempotency, and a Python client sketch: [Orchestrator-API.md](Orchestrator-API.md).

---

## 11. Sequence diagrams

### 11.1 Isolated UI (browser-only)

```
Browser                FastAPI                 Catalog JSON
   │  GET /               │
   │◄──── HTML ───────────┤
   │  GET /k8s-demo-sessions.json
   │◄──── sessions[] ─────┤
   │
   │  user submits query
   │  matchSession() in JS
   │  loadScenario / play()
   │  tick() every delay_ms
   │  render graph, topo, cards
   │
   (never POST /v1/runs)
```

### 11.2 Replay API (realtime)

```
Client                 main.start_run           RunStore           emit_run
  │  POST /v1/runs           │                     │                  │
  │  {query, pace:realtime}  │                     │                  │
  │                          │ match_session       │                  │
  │                          │ store.create        │                  │
  │                          │ create_task(emit)   │                  │
  │◄─ {run_id, stream_url} ──┤                     │                  │
  │                          │                     │                  │
  │  GET .../stream          │                     │                  │
  │                          │                     │                  ├─ timeline_events
  │                          │                     │◄─ append(event) ─┤  sleep(delay)
  │◄─ SSE snapshot ──────────┼─────────────────────┤                  │
  │         …                │                     │                  │
  │◄─ run.finished ──────────┤                     │                  │
```

### 11.3 Orchestrator execute / poll / UI join

The parent orchestrator owns `run_id`. Integration details (registry, poll interval, UI URL, HTTP errors): [Orchestrator-API.md](Orchestrator-API.md). Toy implementation in-repo: `GET /orch` → `static/k8s-orchestrator.html` — function and API trace: [In-House-Orchestrator.md](In-House-Orchestrator.md).

```
Orchestrator              POST /execute              RunStore
  │  {run_id, prompt}          │                        │
  │                            │ create ACCEPTED        │
  │◄─ 202 ACCEPTED ────────────┤ create_task(emit)      │
  │  GET /status?run_id=       │                        │
  │◄─ RUNNING / COMPLETED ─────┤                        │
  │  open /?run_id=            │                        │
Browser                       │                        │
  │  GET /v1/runs/id/events    │                        │
  │  applySnapshot (current)   │                        │
  │  EventSource after_seq     │                        │
```

### 11.4 Future live agent (target, not built)

```
POST /v1/runs
  → Analyze Intent (classifier / LLM)
  → Gather Context (KB retriever + history + live topology)
  loop until solution or maxRetries:
      → LLM(generate) → hypothesis + command
      → kubectl executor → stdout
      → LLM(analyze) → analysis + takeaways + next command | stop
  → LLM(report) → final
  SSE snapshots keep the same `type` / `workflow` / `iteration` / `final` shape
```

---

## 12. UI internals

Layout (`k8s-troubleshoot-replay.html`):

1. **Intent bar** — text input, Run, known-issues `<select>`, match line.
2. **Live workflow SVG** — five nodes; generate/validate wrapped in a loop track with iteration badge.
3. **Cluster topology SVG** — hidden until Gather Context; optional flavor select.
4. **Context cards** — Analyze Intent + Gather Context (KB / history).
5. **AI analysis cards** — one article per revealed turn, latest on top; left = hypothesis / KTA / command, right = kubectl terminal.
6. **Session summary** — injected into `#summaryHost` on final step.

CSS tokens (`:root`): `--kt-bg`, `--kt-elev`, `--kt-text`, `--kt-muted`, `--kt-line`, `--kt-accent`, `--kt-ok`, `--kt-warn`, `--kt-danger`, `--kt-term-*`. Destination apps can restyle without changing JS.

TCAP iframe leftovers: `postMessage` `tcap-theme-request` / `tcap-theme` (always reports `light`). Harmless in isolation.

---

## 13. Error handling

| Case | Behavior |
| --- | --- |
| Unknown query (`POST /v1/runs`) | 422 |
| Unknown query (`POST /execute`) | 202 then poll `FAILED` |
| Duplicate `run_id` + different prompt | 409 |
| Unknown `run_id` | 404 |
| Expired terminal run (`created` older than TTL) | 404 on `/status` and `/history/{run_id}`; omitted from `GET /history` |
| Emitter exception | `run.error` snapshot appended; SSE closes; poll `FAILED` |
| Catalog file missing at import | Process fails to start |
| UI catalog fetch fail | Match line error; no chips |
| SSE wait idle | `ping` every 15s |

No structured logging, tracing, or metrics. No request IDs beyond `run_id`.

---

## 14. Testing

`tests/test_replay_api.py` (TestClient):

- AMF query matches `amf-crashloop`.
- Unrelated query → 422.
- Fast run: event order, topology visibility gate, four `iteration.complete` with full iteration payload, `run.finished` with all nodes `done`.
- SSE stream closes on `run.finished`.
- `/`, CSS, catalog JSON, `/v1/catalog` omits `turns`/`stdout`, `/health`.

Run: `python3 -m pytest -q` from the demo root (`pytest.ini` sets `pythonpath = .`).

---

## 15. Operational notes

- **Statelessness:** violated. `RunStore` is process-local. Horizontal scale would split runs across workers; SSE would not follow.
- **Catalog reload:** lifespan re-reads JSON once at startup. Edits require process restart (UI also caches catalog in JS until refresh).
- **CLI:** `./run-prompt.sh "…"` POSTs realtime and prints timed snapshots; `--instant` uses `pace=fast`.
- **Security:** open CORS, no auth, catalog JSON includes full stub stdout (fine for demo, not for production RCA data).

---

## 16. Dual-implementation risks

JS and Python independently implement:

- Stopword tokenization and scoring.
- `buildSteps` / `build_steps`.
- Band reveal in `applyStep` / `apply_index`.
- Delay table.

They must stay in lockstep. Drift would mean the isolated UI and `/v1/runs` disagree. Prefer extracting a single source of truth (JSON step definitions, or having the UI consume SSE) before adding a live LLM.

---

## 17. Stub → live LLM replacement map

When wiring a real model, keep the **snapshot schema and workflow node ids** stable. Swap only the producers:

| Stub field | Live producer | Notes |
| --- | --- | --- |
| `match_session` | Keep as demo fallback **or** skip; live path always starts a run | Matcher is not an LLM |
| `route` | Intent classifier | Still Diagnose-only in this demo |
| `knowledge` / `history` | Retriever | Feed threshold 0.60 is documented in stubs (`fed` vs not) |
| `topology` | Cluster graph builder from kube API | Flavor picker is UI-only today |
| `turn.hypothesis` + `turn.command` | LLM generate step | Validate command allow-list (read-only kubectl) |
| `turn.stdout` | kubectl executor | Timeout, truncation, deny apply/delete |
| `turn.analysis` + `takeaways` | LLM analyze step | Same band timing can stay |
| `turn.llm` | Parsed model JSON | Promote to the runtime object, not just catalog |
| `final.*` | LLM report step | `outcome` drives resolved vs exhausted |

Suggested module split (not present yet):

```
app/
  matcher.py      # existing score_session
  orchestrator.py # loop: generate → exec → analyze
  llm.py          # client + JSON schema
  kubectl.py      # allow-listed runner
  snapshots.py    # existing build_snapshot / apply_index
```

Until those exist, **the agent application is a deterministic catalog player** with LLM-shaped strings.

---

## 18. File-to-responsibility index

| File | Responsibility |
| --- | --- |
| `app/main.py` | Routes, CORS, `emit_run`, SSE generator |
| `app/replay.py` | Match, steps, delays, snapshots |
| `app/store.py` | `Run`, seq, status, 30-minute history TTL, subscriber fan-out |
| `app/orch_status.py` | Orchestrator DTOs: `build_status`, `execute_ack`, history rows, `progress_for` |
| `static/k8s-orchestrator.html` | Scenario cards + recent-runs panel (`GET /history`) |
| `static/k8s-demo-sessions.json` | All stubbed LLM / kubectl / topology content |
| `static/k8s-troubleshoot-replay.html` | Client-side matcher, player, renderer |
| `static/k8s-troubleshoot.css` | Layout + tokens |
| `run-prompt.sh` | Timed CLI consumer of the replay API |
| `tests/test_replay_api.py` | API + asset contract tests |
| `docs/LLD.md` | As-built design (this file) |
| `docs/Code-Walkthrough.md` | Newcomer walkthrough of the code |
| `docs/Orchestrator-API.md` | External orchestrator HTTP contract |
| `docs/In-House-Orchestrator.md` | `/orch` JS functions, APIs, multi-run polling |
| `docs/User-Guide.md` | Tester / wiring checklist |
| `api.md` | Shared execute/status/history envelope |

Related: [Code walkthrough](Code-Walkthrough.md) · [Orchestrator API](Orchestrator-API.md) · [In-house orchestrator](In-House-Orchestrator.md) · [User guide](User-Guide.md) · [api.md](../api.md) · [README](../README.md)
