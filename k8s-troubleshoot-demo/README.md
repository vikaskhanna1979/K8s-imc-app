# K8s Troubleshooter demo

Standalone FastAPI process that serves the current K8s Troubleshooter UI and a replay API.
It does **not** use the TCAP portal, agent-runtime-service, kubeconfig, or an LLM.

**How to test (orchestrator trigger, poll, UI links for every scenario):** [docs/User-Guide.md](docs/User-Guide.md)

**Integrate with an external orchestrator:** [docs/Orchestrator-API.md](docs/Orchestrator-API.md)

**In-house `/orch` flow (functions + APIs):** [docs/In-House-Orchestrator.md](docs/In-House-Orchestrator.md)

**New to this module:** [docs/Code-Walkthrough.md](docs/Code-Walkthrough.md)

**As-built design:** [docs/LLD.md](docs/LLD.md)

## Run in isolation

```bash
cd /root/vikas/.tcap/k8s-troubleshoot-demo
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8115
```

Port is **8115** (or pass `--port` / set `K8S_DEMO_PORT` in your own wrapper). You should see `Uvicorn running on http://0.0.0.0:8115`.

CORS is open by default. Restrict with `K8S_DEMO_CORS_ORIGINS` (comma-separated origins).

## Isolated UI (no TCAP chrome)

Open:

`http://127.0.0.1:8115/`

That is the current Troubleshooter page (intent bar, workflow, topology after Gather Context, AI analysis, kubectl pane). Catalog is loaded from `http://127.0.0.1:8115/k8s-demo-sessions.json`. There is no portal header, sidebar, or iframe.

Lightweight orchestrator console (trigger each catalog scenario, poll `/status` every 1s, list recent runs for 30 minutes):

`http://127.0.0.1:8115/orch`

Do **not** open `http://127.0.0.1:3001` / `demo-k8s-agent` for this isolated run — that is the TCAP-hosted copy.

You can ignore:

- TCAP `npm start` / port 3001
- agent-runtime-service / port 8103
- kubeconfig, LLM settings, HITL

If TCAP is already running, this app can still listen on **8115**. The two UIs do not share session state.

## Component CSS (destination UIs)

`GET /k8s-troubleshoot.css` is the component stylesheet (also under `static/k8s-troubleshoot.css`).

1. Copy the file into your app, or `<link href="http://127.0.0.1:8115/k8s-troubleshoot.css">`.
2. Override tokens without changing JS:

```css
:root {
  --kt-accent: #2563eb;
  --kt-bg: #eef3fb;
}
```

3. Replace layout (grid vs stack) by overriding `.graph-row` / `.split` in a second file loaded after this one.

Tokens: `--kt-bg`, `--kt-elev`, `--kt-text`, `--kt-muted`, `--kt-line`, `--kt-accent`, `--kt-ok`, `--kt-warn`, `--kt-danger`, `--kt-term-bg`, `--kt-term-fg`, `--kt-term-prompt`.

## Orchestrator API

The multi-domain orchestrator owns `run_id` and starts this agent asynchronously, then polls status. Contract matches `api.md`.

```bash
# fire-and-forget (HTTP 202); optional pace=fast is for tests only
curl -sS -X POST http://127.0.0.1:8115/execute \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"RUN-K8S-SCN-1042-1","prompt":"Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?"}'

# poll until COMPLETED or FAILED (always HTTP 200 once the run exists)
curl -sS "http://127.0.0.1:8115/status?run_id=RUN-K8S-SCN-1042-1"

# list runs still in the 30-minute window
curl -sS http://127.0.0.1:8115/history
```

Open the live UI at the current snapshot (not a replay from the start):

`http://127.0.0.1:8115/?run_id=RUN-K8S-SCN-1042-1`

Multiple `run_id`s can run at the same time in one process. Duplicate `POST /execute` with the same `run_id` and prompt is idempotent (202). A different prompt for an existing `run_id` returns 409.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/execute` | `{ "run_id", "prompt", "details"? }` → **202** `{ run_id, current_status: ACCEPTED, agent: "K8s", message }` |
| `GET` | `/status?run_id=` | Poll `{ mission, current_status, current_response, progress, details, created, expires_at, sub_agents }` (`ACCEPTED` / `RUNNING` / `COMPLETED` / `FAILED`) |
| `GET` | `/history` | `{ ttl_sec, count, runs[] }` — previous runs kept 30 minutes (`K8S_RUN_HISTORY_TTL_SEC`) |
| `GET` | `/history/{run_id}` | Same payload as `/status`. Unknown or expired → 404 |

`details.ui_url` is `/?run_id=...`. `GET /status` for an unknown or expired run is 404. Catalog miss still 202s, then polls as `FAILED`.

## Replay API (graph updates)

`POST /v1/runs` returns a real `run_id` (for example `d-c5220332f937`) plus `stream_url`. Use that value — do not leave the placeholder `RUN_ID` in the path.

```bash
# start a run and capture run_id
RESP=$(curl -sS -X POST http://127.0.0.1:8115/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"query":"Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?","pace":"fast"}')
echo "$RESP"
RUN_ID=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['run_id'])" "$RESP")

# JSON event log (easiest in a terminal)
curl -sS "http://127.0.0.1:8115/v1/runs/${RUN_ID}/events"

# or SSE stream
curl -sS -N "http://127.0.0.1:8115/v1/runs/${RUN_ID}/stream"
```

A custom UI can `EventSource("/v1/runs/{id}/stream")` and set graph node colors from `workflow.nodes` after each `iteration.complete`.

Shell helper (starts a run from a prompt and **streams it on the demo timeline** — gather ~1.6s, kubectl ~2s, analyzing 5–10s):

```bash
chmod +x run-prompt.sh
./run-prompt.sh "Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?"
./run-prompt.sh --instant "Check CrashLoopBackOff in 5g-core"
```

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/orch` | Lightweight orchestrator console (five scenario cards + recent runs) |
| `POST` | `/execute` | Orchestrator start (202, orchestrator-owned `run_id`) |
| `GET` | `/status?run_id=` | Orchestrator poll |
| `GET` | `/history` | Recent runs (30 min TTL) |
| `GET` | `/history/{run_id}` | Same as `/status` for a historical run |
| `POST` | `/v1/runs` | `{ "query", "pace": "realtime"\|"fast" }` → `{ run_id, session_id, title, status, stream_url, events_url }` (422 if no match) |
| `GET` | `/v1/runs/{id}` | Current snapshot |
| `GET` | `/v1/runs/{id}/stream?after_seq=0` | SSE snapshots; ping; closes on `run.finished` / `run.error` |
| `GET` | `/v1/runs/{id}/events` | Buffered events (no SSE) |
| `GET` | `/v1/catalog` | Session ids / titles / prompts (no stdout) |
| `GET` | `/health` | Liveness |

`pace=realtime` uses the demo delays (~1.6s gather, 2s command, 5–10s analyzing). `pace=fast` emits immediately and still includes `delay_ms` on each event so a UI can animate.

Every SSE `message` is a full snapshot plus `type`: `run.started`, `workflow.node`, `topology.ready` (only after Gather Context), `iteration.generate`, `iteration.command`, `iteration.analyzing`, `iteration.complete` (treat as iteration done), `run.finished`.

## Tests

```bash
cd /root/vikas/.tcap/k8s-troubleshoot-demo
python3 -m pytest -q
```
