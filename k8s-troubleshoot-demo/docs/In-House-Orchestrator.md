# K8s Troubleshooter — In-house orchestrator

| Field | Value |
| --- | --- |
| Document | How `GET /orch` starts, polls, and lists agent runs |
| Audience | Engineers tracing the demo orchestrator (`k8s-orchestrator.html`) |
| UI | `http://127.0.0.1:8115/orch` |
| Source | `static/k8s-orchestrator.html` |

This page is a **thin trigger + poller**. It does not keep a database of runs. The agent process (`RunStore` in `app/store.py`) is the source of truth.

HTTP field lists for a **parent** multi-domain orchestrator: [Orchestrator-API.md](Orchestrator-API.md). That external client should copy the same three calls this page uses (`POST /execute`, `GET /status`, `GET /history`).

Related: [Code walkthrough](Code-Walkthrough.md) · [User guide](User-Guide.md) · [LLD](LLD.md)

---

## 1. What `/orch` is

Five scenario cards plus a **Recent runs** panel. Each card has private JS state `{ runId, timer }`. Clicking Run on AMF does not stop Payments.

The in-house orchestrator **owns `run_id`**. It never waits for RCA on the trigger request.

```
Operator                /orch page                     FastAPI (:8115)              RunStore
   |  click Run on AMF card |                                |                          |
   |----------------------->|                                |                          |
   |                        |  run_id = RUN-K8S-amf-crashloop-<hex>
   |                        |  POST /execute {run_id, prompt} |                          |
   |                        |------------------------------->|  create ACCEPTED         |
   |                        |                                |------------------------->|
   |                        |                                |  create_task(emit_run)   |
   |                        |<---------- 202 ACCEPTED -------|                          |
   |                        |  enable "Open agent"           |                          |
   |                        |  GET /status?run_id=  (1s)     |                          |
   |                        |------------------------------->|  build_status(run)       |
   |                        |<----- RUNNING, progress 25 ----|                          |
   |                        |         … more polls …         |  append snapshots        |
   |                        |<----- COMPLETED, progress 100 -|                          |
   |                        |  stop polling                  |                          |
   |  Open agent -------------------------------------------> GET /?run_id=...         |
```

---

## 2. APIs this page actually calls

| When | Method | Path | JS caller | FastAPI handler |
| --- | --- | --- | --- | --- |
| Open `/orch` | `GET` | `/orch` | browser navigation | `orch_ui()` |
| Page CSS | `GET` | `/k8s-troubleshoot.css` | `<link>` | `component_css()` |
| Page load + every 5s + after Run | `GET` | `/history` | `refreshHistory()` | `list_history()` |
| Click **Run** | `POST` | `/execute` | `startRun()` | `execute()` |
| After 202, every 1s until terminal | `GET` | `/status?run_id=` | `pollOnce()` | `orch_status()` |
| Click **Open agent** | `GET` | `/?run_id=` | `openAgent()` → `window.open` | `ui()` then Troubleshooter JS |

**Not** called by `/orch` JavaScript:

- `POST /v1/runs` (CLI / custom UI only)
- `GET /v1/catalog`
- `GET /history/{run_id}` (exists on the server; unused here)
- `/v1/runs/{id}/events` and `/stream` — used later by the Troubleshooter tab after Open agent

---

## 3. Page load — functions, then APIs

```
browser GET /orch
  → main.orch_ui()                 # FileResponse k8s-orchestrator.html
browser GET /k8s-troubleshoot.css
  → main.component_css()

IIFE in k8s-orchestrator.html
  → renderCard() × 5
  → bindCard() × 5                 # wires Run / Open agent; no HTTP yet
  → refreshHistory()               # GET /history
  → setInterval(refreshHistory, 5000)
```

`bindCard` only stores `{ runId, timer }` and attaches click handlers. No agent run exists until **Run**.

Helpers that never call HTTP: `hexId()`, `pillClass()`, `apply()`, `stopPoll()`, `formatDuration()`, `ttlMinutes()`, `renderHistory()`.

---

## 4. Click Run — JS functions

Order inside `startRun()`:

| # | Function | What it does |
| --- | --- | --- |
| 1 | `stopPoll()` | `clearInterval` if this card was already polling |
| 2 | `hexId()` | 8 hex chars for a unique id |
| 3 | `apply("Idle", 0, "Starting…")` | pill / bar / caption; disables Run |
| 4 | **`fetch POST /execute`** | `{ run_id, prompt }` |
| 5 | on 202: enable Open agent, `apply("ACCEPTED", …)` | |
| 6 | `refreshHistory()` | **`GET /history`** |
| 7 | `pollOnce()` | first **`GET /status`** |
| 8 | `setInterval(pollOnce, 1000)` | unless already COMPLETED/FAILED |

`run_id` looks like `RUN-K8S-amf-crashloop-a1b2c3d4`. `prompt` is the hardcoded string on that card. Default `pace` is omitted → server `realtime`.

The Run button stays disabled while `ACCEPTED` or `RUNNING` so the same card cannot double-start. After terminal, Run is enabled again; the next click mints a **new** `run_id`.

---

## 5. `POST /execute` — Python call chain

Handler: `execute()` in `app/main.py`.

```
execute(body)
  ├─ store.get_idempotent(run_id, prompt)     # none on first click
  ├─ match_session(_catalog(), prompt)        # replay.py
  │     ├─ score_session() × each catalog session
  │     │     └─ tokens()
  │     └─ pick highest score if ≥ 15
  ├─ store.create(..., orchestrated=True)     # orch_status = ACCEPTED
  │     └─ store.prune()                      # drop expired COMPLETED/FAILED
  ├─ asyncio.create_task(emit_run(run, session))   # does not block 202
  └─ execute_ack(run, queued=True)            # 202 { ACCEPTED, agent: K8s }
```

The 202 response does **not** wait for RCA.

Unmatched prompt: still 202, then `store.append` of `run.error` so polls show `FAILED`.

Same `run_id` + same prompt again: 202 idempotent (`get_idempotent`). Same `run_id` + different prompt: HTTP **409**.

### Background (after 202): `emit_run`

Not invoked by the poll HTTP. Same process, independent asyncio task:

```
emit_run(run, session)
  └─ timeline_events(session, query, run_id)     # replay.py — the whole movie
        ├─ build_steps(session)
        ├─ apply_index(..., -1) + build_snapshot  → run.started
        └─ for each step:
              event_type_for(step)
              delay_ms_for(step)
              apply_index(...)
              build_snapshot(...)
  then for each event:
        store.append(run, event)                 # ACCEPTED→RUNNING, later COMPLETED
        asyncio.sleep(delay_ms / 1000)
```

`store.append` is the only writer of `Run.orch_status`:

- first event → `RUNNING`
- `run.finished` → `COMPLETED`
- `run.error` → `FAILED`

It also `put_nowait`s to that run’s SSE queues (for Open agent later, not for `/orch`).

---

## 6. Poll loop — JS then Python

`pollOnce()`:

```
GET /status?run_id=<card.runId>
  → if !ok: apply FAILED, stopPoll, re-enable Run
  → apply(current_status, progress, current_response)
  → if COMPLETED or FAILED: stopPoll, re-enable Run
```

`apply()` only updates the DOM. It does not call more APIs.

Fields the card uses:

| Field | Card UI |
| --- | --- |
| `current_status` | ACCEPTED / RUNNING / COMPLETED / FAILED |
| `progress` | bar 0–100 |
| `current_response` | caption (`Iteration 1 · Analyzing output`) |

The card does **not** render `sub_agents` (those are for a richer parent UI).

Server:

```
orch_status(run_id)
  ├─ store.get(run_id)            # prune() then dict lookup
  └─ build_status(run, ttl_sec)
        ├─ progress_for(run)
        │     └─ _node_map(snapshot)
        ├─ _current_response(run)
        ├─ _sub_agents(run)
        └─ run.expires_at(ttl)
```

`FAILED` on `/status` is still **HTTP 200**. Unknown or expired `run_id` is **404** (the card treats that as FAILED).

---

## 7. History panel

| When | JS | API |
| --- | --- | --- |
| Load | `refreshHistory()` | `GET /history` |
| Every 5s | same | same |
| After successful execute | same | same |

```
refreshHistory()
  → GET /history
  → renderHistory(data)

list_history()
  → build_history_list(store)
        ├─ store.list_history()          # prune + sort by created desc
        └─ build_history_item(run) × N
              └─ build_status(...)
```

In-flight rows show “in progress”. Finished rows show time left until the 30-minute TTL (`K8S_RUN_HISTORY_TTL_SEC`, default 1800). History “Open agent” is a normal `<a href>` to `ui_url` (`/?run_id=...`).

---

## 8. How multiple runs stay isolated

Three layers, all keyed by `run_id`.

### Agent: one dict of runs

```python
# app/store.py — process-global
self._runs: dict[str, Run] = {}
```

AMF and Payments at the same time:

```
_runs = {
  "RUN-K8S-amf-crashloop-aaa": Run(orch_status=RUNNING, session_id=amf-crashloop, ...),
  "RUN-K8S-payments-crashloop-bbb": Run(orch_status=ACCEPTED, session_id=payments-crashloop, ...),
}
```

Each `Run` has its own `events[]` / `snapshot` / `seq`, `orch_status`, `emit_run` task, and SSE subscriber set. `GET /status?run_id=` is a dict lookup. `append` only mutates the `Run` it is given.

### UI: one poller per card

Five cards × five `state.runId` + five `setInterval` timers. If you click Run on AMF after it finished, the card drops the old `runId` (stops polling it) and starts a new execute. The old run remains in `RunStore` until TTL and still appears under Recent runs.

### Open agent: one tab per `run_id`

`window.open("/?run_id=" + runId)` so two tabs do not share UI state.

---

## 9. Open agent — orchestrator vs Troubleshooter

Orchestrator `openAgent()`:

```javascript
window.open("/?run_id=" + encodeURIComponent(state.runId));
```

That is **one** navigation from `/orch`: **`GET /`**. Then `k8s-troubleshoot-replay.html` `boot()` takes over:

```
GET /k8s-demo-sessions.json
joinOrchestratorRun(runId)
  GET /v1/runs/{id}/events              # applySnapshot each (current snapshot, not t=0)
  EventSource /v1/runs/{id}/stream?after_seq=N
```

| API | Handler | Functions |
| --- | --- | --- |
| `GET /` | `ui()` | serve HTML |
| `GET /k8s-demo-sessions.json` | `catalog_file()` | file |
| `GET /v1/runs/{id}/events` | `get_events()` | `store.get` |
| `GET /v1/runs/{id}/stream` | `stream_run()` | `store.get`, SSE from `run.subscribers` filled by `store.append` |

Join JS: `applySnapshot` → `render`. Intent bar stays disabled (`serverMode`).

---

## 10. Call graph (one Run, one card)

```
[orch JS]
startRun
  hexId, stopPoll, apply
  POST /execute ──────────────────────────┐
  refreshHistory → GET /history           │
  pollOnce → GET /status (then every 1s)  │
  openAgent → GET /?run_id=  (optional)   │
                                          ▼
[main.py]
execute
  store.get_idempotent
  match_session → score_session → tokens
  store.create → prune
  create_task(emit_run) ──► timeline_events
                              build_steps, apply_index, delay_ms_for,
                              event_type_for, build_snapshot
                            store.append  (status machine)
  execute_ack

orch_status → store.get → build_status → progress_for, _current_response, _sub_agents
list_history → build_history_list → build_history_item → build_status
```

Status tracking is **pull** (`GET /status`). The orchestrator never subscribes to SSE; SSE is only for the Troubleshooter join.

```
emit_run  --append-->  Run.orch_status + Run.snapshot
                              |
              GET /status     |     GET /history
              (one run)       |     (all runs, newest first)
                              v
                     orch_status.build_status()
```

---

## 11. What the in-house orchestrator does not do

| Missing | Effect |
| --- | --- |
| No persist | Refresh `/orch` and cards go Idle; history still comes from the agent until TTL/restart |
| No cancel / pause | You cannot stop an in-flight `emit_run` |
| Card only remembers **latest** `runId` | Older runs for that scenario are history-only |
| No `sub_agents` tree on the card | Progress bar + caption only |
| Does not call `POST /v1/runs` | That path is for `run-prompt.sh` / custom UIs |

---

## 12. Copy this into an external orchestrator

Same three calls; see [Orchestrator-API.md](Orchestrator-API.md) for request/response fields.

1. Generate a unique `run_id` per agent instance (`RUN-K8S-<mission>-<n>`).
2. `POST /execute` with `{ run_id, prompt, details? }`. Expect **202**.
3. Enable the agent link immediately: `{base}/?run_id={run_id}`.
4. Poll `GET /status?run_id=` every 1–2s until `COMPLETED` or `FAILED`.
5. Optionally poll `GET /history` for a recent-runs list (this page uses 5s).
6. Never reuse one `run_id` across K8s and another domain agent. Never reuse one iframe for two `run_id`s.
