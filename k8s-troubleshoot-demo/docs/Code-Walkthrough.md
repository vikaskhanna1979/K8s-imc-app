# K8s Troubleshooter — Code Walkthrough

| Field | Value |
| --- | --- |
| Document | Newcomer walkthrough of `k8s-troubleshoot-demo/` |
| Audience | Engineers new to this module (including Python beginners) integrating it with a parent orchestrator |
| Code root | `k8s-troubleshoot-demo/` |
| Runtime | FastAPI + uvicorn on port **8115** |

Read this before the [LLD](LLD.md). For the HTTP contract only, use the [Orchestrator API guide](Orchestrator-API.md). For how `GET /orch` triggers and polls runs, use the [in-house orchestrator](In-House-Orchestrator.md). For hands-on testing, use the [User guide](User-Guide.md).

---

## 1. What this module is

This repo is a **standalone K8s Troubleshooter demo**. It looks like an AI agent walking a cluster (intent → kubectl → RCA), but it never talks to a cluster, an LLM, kubeconfig, or the TCAP portal. It **replays canned sessions** from a JSON catalog, with realistic delays, so a UI and a multi-domain orchestrator can be wired and demoed.

Think of it as a **scripted RCA player**, not a live operator.

1. You type (or POST) a problem: *“Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?”*
2. The app **matches** that text to one of five pre-authored sessions.
3. It **plays** that session as a timeline: Analyze Intent → Gather Context → hypothesis/kubectl/analysis loop → Report.
4. The UI shows a workflow graph, cluster topology, iteration cards, and a kubectl pane.
5. Every “AI” sentence and every `kubectl` stdout line is already written in `static/k8s-demo-sessions.json`.

That is the whole product today. The LLD keeps the snapshot/event shape stable so a real LLM + kubectl executor can replace the stubs later.

**Mental model in one sentence:** catalog JSON is the “agent”; FastAPI is a timed snapshot player; the HTML is a renderer; `/execute` + `/status` is how an orchestrator pretends this is a real domain agent.

---

## 2. Python patterns used in this repo

| You see | Meaning |
| --- | --- |
| `@app.post("/execute")` | When HTTP POST hits this path, run this function. |
| `class ExecuteBody(BaseModel)` | FastAPI parses JSON into this shape. Missing `run_id`/`prompt` → HTTP 422. |
| `async def execute(...)` | Can start background work without blocking the HTTP response. |
| `asyncio.create_task(...)` | Fire-and-forget: start the timeline, return 202 immediately. |
| `dict[str, Any]` | A JSON object: keys are strings, values can be anything. |
| `@dataclass class Run` | A structured in-memory record (named fields, like a typed dict). |

The process starts in `app/main.py`. `app = FastAPI(...)` is the whole web app. `uvicorn app.main:app` means: load module `app.main`, serve the object named `app`.

---

## 3. Folder map

```
k8s-troubleshoot-demo/
├── app/
│   ├── main.py          # HTTP routes, CORS, SSE, emit_run
│   ├── replay.py        # match query → session, build timeline snapshots
│   ├── store.py         # in-memory runs, seq numbers, SSE fan-out, 30-min TTL
│   └── orch_status.py   # maps a Run into orchestrator /status and /history JSON
├── static/
│   ├── k8s-demo-sessions.json        # the five stubbed RCAs (source of truth)
│   ├── k8s-troubleshoot-replay.html  # Troubleshooter UI
│   ├── k8s-orchestrator.html         # lightweight trigger/poll console
│   └── k8s-troubleshoot.css          # tokenized stylesheet
├── tests/test_replay_api.py
├── run-prompt.sh                     # CLI client of POST /v1/runs
├── docs/
│   ├── Orchestrator-API.md           # HTTP contract for a parent orchestrator
│   ├── In-House-Orchestrator.md      # GET /orch functions and APIs
│   ├── Code-Walkthrough.md           # this file
│   ├── User-Guide.md                 # tester checklist
│   └── LLD.md                        # as-built design
└── api.md                            # shared execute/status/history envelope
```

| File | Job |
| --- | --- |
| `app/main.py` | Boundary: serve UI, start runs, stream events |
| `app/replay.py` | Pure logic: scoring, steps, delays, full snapshots |
| `app/store.py` | Process-local run state; lost on restart |
| `app/orch_status.py` | Progress bar, `sub_agents`, `ui_url` for the orchestrator |
| Catalog JSON | All content: hypotheses, kubectl, stdout, topology, final RCA |

---

## 4. Two ways in (do not mix them)

There are **two HTTP contracts** and **two UIs**.

### 4.1 Orchestrator path (the integration)

A parent orchestrator **owns** `run_id`. It never waits for RCA on the POST.

```
POST /execute  { run_id, prompt }  →  202 ACCEPTED
GET  /status?run_id=               →  ACCEPTED → RUNNING → COMPLETED | FAILED
GET  /history                      →  last 30 minutes of runs
Open /?run_id=...                  →  Troubleshooter joined at the current snapshot
```

Console for this path: `http://127.0.0.1:8115/orch`

### 4.2 Demo replay API (CLI / custom UI)

```
POST /v1/runs  { query, pace }  →  server-generated run_id like d-c5220332f937
GET  /v1/runs/{id}/stream       →  SSE of full snapshots
GET  /v1/runs/{id}/events       →  same events as JSON
```

`run-prompt.sh` uses this path. Unmatched query → **422** (different from `/execute`).

Standalone Troubleshooter: `http://127.0.0.1:8115/` — the browser can replay locally **without** calling `/v1/runs` at all.

---

## 5. How a run actually works

```
query / prompt
  → match_session()          # token score against catalog prompts/keywords
  → build_steps()            # linear script from that session
  → timeline_events()        # one full snapshot per step
  → emit_run()               # append to RunStore, sleep between events
  → UI / SSE / /status       # consumers read snapshots
```

### Matching (`replay.py`)

Query is tokenized (stopwords like `why`, `pod`, `namespace` dropped). Exact prompt match scores 1000. Keyword hits +20. If the best score is under 15, there is no match.

- `POST /v1/runs` unmatched → **422**
- `POST /execute` unmatched → still **202**, then `/status` is **FAILED**

### Timeline (`build_steps`)

For N catalog turns the script is:

1. Analyze Intent
2. Gather Context
3. Topology appears (`topology.ready`)
4. For each turn, four phases: generate hypothesis → run kubectl → “analyzing…” → validate
5. Report Generation + final RCA

The UI fakes streaming with **bands**: first hypothesis only, then command+stdout, then analyzing spinner, then analysis + key takeaways.

### Timing

`pace=realtime` (orchestrator default): gather ~1.6s, kubectl ~2s, analyzing 5–10s per iteration. AMF has 4 iterations (~40–60s). `pace=fast` dumps every event immediately (tests / `run-prompt.sh --instant`).

Every SSE message is a **full snapshot**, not a delta: `workflow.nodes`, `topology`, `iteration`, `final`.

---

## 6. Walk the Python files in orchestrator order

Skip the HTML player until the HTTP contract is clear.

### 6.1 `app/main.py` — the door

- Loads `static/k8s-demo-sessions.json` at startup.
- Serves `/` (Troubleshooter) and `/orch` (toy orchestrator).
- `store = RunStore()` is a **module-level singleton**. All runs live in that one process. Two uvicorn workers would split memory; SSE would not follow. For a demo orchestrator, run **one** worker.

`emit_run` is the background player:

```python
async def emit_run(run: Run, session: dict[str, Any]) -> None:
    events = timeline_events(session, run.query, run.run_id, rng=rng)
    for event in events:
        store.append(run, event)
        if run.pace != "fast":
            await asyncio.sleep((event.get("delay_ms") or 0) / 1000.0)
```

`POST /execute` in short:

1. Look up `run_id`. If it exists with a **different** prompt → 409.
2. If it exists with the **same** prompt → 202, do not start a second timeline.
3. Else match the prompt to a catalog session, create a `Run` with `orchestrated=True` (starts as `ACCEPTED`).
4. If no session: append a `run.error` snapshot, still return 202 (FAILED shows up on poll).
5. If `pace` is realtime: `create_task(emit_run)` so the HTTP response does not wait ~40 seconds.

### 6.2 `app/store.py` — what a run is

`Run` holds:

- `run_id`, `query`/`mission`, `pace`
- `orch_status`: `ACCEPTED` → `RUNNING` → `COMPLETED` | `FAILED` (what `/status` shows)
- `status`: `accepted` / `running` / `finished` / `error` (demo `/v1/runs` language)
- `events`: list of full snapshots, each with `seq`
- `subscribers`: asyncio queues for SSE
- `details`: orchestrator passthrough JSON plus later `session_id`, `namespace`, `ui_url`

`append` is the heartbeat: new event → bump `seq` → update snapshot → flip status → notify SSE listeners.

Idempotency: `get_idempotent(run_id, prompt)` returns the existing run, or raises `RunConflict` if the prompt differs.

### 6.3 `app/replay.py` — how a prompt becomes a movie

You do **not** call this from the large orchestrator. It is internal.

1. `match_session(catalog, prompt)` — token score; need ≥ 15.
2. `build_steps(session)` — linear script (analyze, context, topology, 4 phases × N turns, report, final).
3. `apply_index` — “if we have played steps 0..i, what should the graph and cards show?”
4. `timeline_events` — one **full** snapshot per step.

Matching is keyword-ish, not an LLM. `"Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?"` → session `amf-crashloop`. Garbage text → no match → FAILED on the orchestrator path.

### 6.4 `app/orch_status.py` — poll JSON

Maps workflow node colors to orchestrator `sub_agents` and invents `progress` (10 / 25 / 25+16×iter / 95 / 100). That number is a demo bar, not real work remaining.

`sub_agents` keys are the five graph nodes:

- Analyze Intent
- Gather Context
- Generate Hypothesis
- Validate Hypothesis
- Report Generation

That is how a parent orchestrator can show a tree without understanding kubectl or topology.

### 6.5 Catalog JSON — the five demos

`static/k8s-demo-sessions.json` is the content. Each session has `prompts[]` (what matching looks for), `turns[]` (fake kubectl + analysis), `topology`, `final` (RCA).

| Session id | Symptom | Stub RCA |
| --- | --- | --- |
| `amf-crashloop` | CrashLoop AMF in `5g-core` | Missing `AMF_BIND_ADDR` |
| `imagepull-nginx` | ImagePullBackOff in `ts` | Typo `nginx:lastest` |
| `busybox-pending` | Pending `busybox-pod` | CPU request 4 vs allocatable 2 |
| `payments-crashloop` | CrashLoop `api-0` in `payments` | Liveness probe too aggressive |
| `ue-latch-calico` | UE cannot latch | Calico node restarts |

Diagnose-only: stubs never apply/create; they stop at RCA and point at “Creation mode + HITL”.

---

## 7. The two UIs

**Troubleshooter** (`/`): intent bar, workflow SVG, topology (only after Gather Context), iteration cards (latest on top), kubectl terminal, session summary.

- No `run_id` in the URL: JS matches and plays with `setTimeout` (same algorithm as Python).
- `/?run_id=...`: join the **server** run at the current snapshot, then SSE for the rest. Intent bar is disabled.

Join sequence in `k8s-troubleshoot-replay.html`:

1. `GET /v1/runs/{id}/events` — apply all buffered snapshots at once.
2. `EventSource(/v1/runs/{id}/stream?after_seq=...)` — live remainder.

**Orchestrator console** (`/orch`): five scenario cards. Each Run does `POST /execute` and polls `/status` every 1s. Recent runs come from `/history` every 5s. This page is a miniature of the parent orchestrator UI.

---

## 8. Status machines (do not mix them)

Orchestrator (`/execute` + `/status`) — **use this**:

```
create  → ACCEPTED   (HTTP 202, progress 0)
first event → RUNNING
run.finished → COMPLETED  (progress 100)
run.error / no catalog match → FAILED  (HTTP 200 on poll)
```

Demo API (`/v1/runs`) — **do not use from the orchestrator**:

```
create → status=running, run_id like d-abc123
run.finished → finished
run.error → error
unmatched query → HTTP 422
```

If you accidentally call `/v1/runs` from the parent orchestrator you will get a server-generated id, no `ACCEPTED` handshake, and 422 on unknown prompts.

---

## 9. Constraints that will bite integration

1. **Memory only.** Restart this FastAPI process and every `run_id` 404s.
2. **One process.** Horizontal scale of this demo will split runs. Pin it to one replica.
3. **TTL 30 min** from `created` for terminal runs (`K8S_RUN_HISTORY_TTL_SEC`). In-flight runs are never pruned.
4. **No auth.** CORS defaults to `*`.
5. **JS and Python both implement matching/timeline.** Standalone `/` without `run_id` replays in the browser. Orchestrator join (`/?run_id=`) uses the **server** timeline. Always send operators through `/?run_id=` so the parent and the agent UI stay aligned.

There is **no cancel/pause API**. Control today means: start, poll, show UI, list history.

---

## 10. Suggested reading order

1. Start the app, `curl /health`, open `/orch`, click one Run, watch `/status` in another terminal.
2. Read `ExecuteBody` and `execute()` in `app/main.py` (the 202 path).
3. Read `Run` + `append` in `app/store.py` (why poll status changes).
4. Read `build_status` in `app/orch_status.py` (the JSON the parent UI will bind).
5. Skim `match_session` and `build_steps` in `app/replay.py` (why a prompt becomes AMF vs busybox).
6. Skim `SCENARIOS` + `startRun` / `pollOnce` in `static/k8s-orchestrator.html`.
7. Read `test_execute_*` in `tests/test_replay_api.py` — executable examples of 202, concurrent runs, 409, FAILED, history TTL.
8. Then the [LLD](LLD.md) if you need snapshot schema, delays, or the stub→live LLM map.

---

## 11. How to run it

```bash
cd k8s-troubleshoot-demo
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8115
```

Then open `http://127.0.0.1:8115/` or `http://127.0.0.1:8115/orch`. Tests: `python3 -m pytest -q`.

If you already know TCAP / `demo-k8s-agent` on port 3001: ignore it. This isolated app does not share session state with that copy.
