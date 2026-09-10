# K8s Troubleshooter — testing user guide

This guide is for testers and for anyone wiring the **multi-domain orchestrator** to this agent. The agent is a stubbed catalog player (no live LLM or cluster). The orchestrator still talks to it the same way it would a real domain agent: **trigger → poll → open the agent UI**.

**Base URL (local):** `http://127.0.0.1:8115`  
**Health:** [http://127.0.0.1:8115/health](http://127.0.0.1:8115/health)  
**Orchestrator console:** [http://127.0.0.1:8115/orch](http://127.0.0.1:8115/orch)

If you are on another machine, replace `127.0.0.1` with the host IP (port **8115**).

---

## 1. Start the app

```bash
cd /root/vikas/k8s-imc-app/k8s-troubleshoot-demo
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8115
```

Confirm:

```bash
curl -sS http://127.0.0.1:8115/health
# {"status":"ok"}
```

Automated regression (optional):

```bash
python3 -m pytest -q
```

---

## 2. How the orchestrator uses this agent

The orchestrator **owns** `run_id`. It never waits for RCA to finish on the HTTP request.

```
Orchestrator                         K8s agent (:8115)                    Operator
     |  POST /execute {run_id, prompt}      |                                |
     |<--------- 202 ACCEPTED --------------|                                |
     |                                      |  (timeline runs in background) |
     |  GET /status?run_id=   (every 1–2s)  |                                |
     |<--------- ACCEPTED / RUNNING --------|                                |
     |  GET /status?run_id=                 |                                |
     |<--------- COMPLETED or FAILED -------|                                |
     |  operator clicks agent link -------->|  GET /?run_id=<run_id>        |
     |                                      |-------- live UI at current ---|
```

### 2.1 Trigger — `POST /execute`

Always **HTTP 202**. Body the orchestrator sends:

```json
{
  "run_id": "RUN-K8S-SCN-1042-1",
  "prompt": "Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?",
  "details": { "region": "north" }
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `run_id` | yes | Orchestrator-generated. Unique per mission/agent instance. |
| `prompt` | yes | User / mission text. Matched to a catalog session. |
| `details` | no | Opaque passthrough; echoed on `/status` (plus `session_id`, `namespace`, `ui_url`). |
| `pace` | no | `"realtime"` (default, demo delays) or `"fast"` (pytest / skip waits). Orchestrator should omit this. |

Example:

```bash
curl -sS -X POST http://127.0.0.1:8115/execute \
  -H 'Content-Type: application/json' \
  -d '{
    "run_id": "RUN-K8S-SCN-1042-1",
    "prompt": "Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?",
    "details": { "region": "north" }
  }'
```

202 body (contract in [api.md](../../api.md); this agent sets `"agent": "K8s"`):

```json
{
  "run_id": "RUN-K8S-SCN-1042-1",
  "current_status": "ACCEPTED",
  "agent": "K8s",
  "message": "K8s activity queued"
}
```

Rules:

- Same `run_id` + same prompt again → **202** idempotent (returns **current** status).
- Same `run_id` + different prompt → **409**.
- Unmatched prompt → still **202**, then poll shows **FAILED**.
- Several `run_id`s can run at once.

### 2.2 Poll — `GET /status?run_id=`

Orchestrator polls until a terminal status. **FAILED is still HTTP 200.** Unknown `run_id` is **404**.

```bash
curl -sS "http://127.0.0.1:8115/status?run_id=RUN-K8S-SCN-1042-1"
```

Poll loop the orchestrator can copy:

```bash
RUN_ID=RUN-K8S-SCN-1042-1
while true; do
  BODY=$(curl -sS "http://127.0.0.1:8115/status?run_id=${RUN_ID}")
  STATUS=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["current_status"])' "$BODY")
  echo "$BODY" | python3 -m json.tool
  case "$STATUS" in
    COMPLETED|FAILED) break ;;
  esac
  sleep 2
done
```

| `current_status` | Meaning | Orchestrator action |
| --- | --- | --- |
| `ACCEPTED` | Queued, timeline not started | Keep polling |
| `RUNNING` | Workflow in progress | Keep polling; agent link is already valid |
| `COMPLETED` | RCA finished (`progress` 100) | Stop; show success |
| `FAILED` | No catalog match or emitter error | Stop; show failure |

Useful poll fields:

| Field | Use in orchestrator UI |
| --- | --- |
| `mission` | The prompt |
| `current_response` | One-line caption (e.g. `Iteration 1 · Analyzing output`) |
| `progress` | 0–100 progress bar |
| `sub_agents` | Five workflow nodes: Analyze Intent, Gather Context, Generate Hypothesis, Validate Hypothesis, Report Generation (`QUEUED` / `RUNNING` / `COMPLETED` / `FAILED`) |
| `details.ui_url` | Relative agent link: `/?run_id=RUN-K8S-SCN-1042-1` |
| `details.session_id` | Catalog session (`amf-crashloop`, …) |
| `details.namespace` | Cluster namespace once topology is known |

Realtime demo timing (so you know how long to poll): gather ~1.6s, kubectl ~2s, analyzing **5–10s per iteration**. AMF has **4** iterations (~40–60s). The other three scenarios have **3** (~30–45s).

### 2.3 Agent link in the orchestrator UI

When the operator clicks this agent in the orchestrator, open the Troubleshooter **at the current snapshot** (not a replay from t=0).

**Absolute URL to put on the orchestrator “Watch” / agent button:**

```
http://127.0.0.1:8115/?run_id=<run_id>
```

Or resolve `details.ui_url` against the agent base:

```
agent_ui = AGENT_BASE + status.details.ui_url
# e.g. http://127.0.0.1:8115/?run_id=RUN-K8S-SCN-1042-1
```

Suggested orchestrator behaviour:

1. After **202**, enable the agent link immediately (join shows Idle/Running as events arrive).
2. While **RUNNING**, the same URL continues live over SSE.
3. After **COMPLETED** / **FAILED**, the same URL still works (final report or error caption).
4. Open in a new tab/iframe; do not reuse one iframe for two `run_id`s.

The joined page disables the intent bar and Run button so the operator cannot start a second local replay on top of the orchestrator run.

---

## 3. Scenario catalog (all links)

Standalone UI (no orchestrator): [http://127.0.0.1:8115/](http://127.0.0.1:8115/)  
Lightweight orchestrator console: [http://127.0.0.1:8115/orch](http://127.0.0.1:8115/orch)  
Catalog JSON: [http://127.0.0.1:8115/k8s-demo-sessions.json](http://127.0.0.1:8115/k8s-demo-sessions.json)  
Catalog summaries: [http://127.0.0.1:8115/v1/catalog](http://127.0.0.1:8115/v1/catalog)

Use a **fresh `run_id`** each time you re-test a scenario (or reuse the same id + same prompt for idempotency).

### 3.1 AMF CrashLoop · `5g-core`

| | |
| --- | --- |
| Session | `amf-crashloop` |
| RCA | Deployment missing `AMF_BIND_ADDR` |
| Iterations | 4 |
| Topology | Open5GS 5GC (flavor picker: Open5GS / OAI) |
| Suggested `run_id` | `RUN-K8S-AMF-1` |

**Prompt (copy this):** `Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?`

Other matching prompts: `Check CrashLoopBackOff in 5g-core` · `Find the exact cause of AMF CrashLoopBackOff`

**Trigger**

```bash
curl -sS -X POST http://127.0.0.1:8115/execute \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"RUN-K8S-AMF-1","prompt":"Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?"}'
```

**Poll:** [http://127.0.0.1:8115/status?run_id=RUN-K8S-AMF-1](http://127.0.0.1:8115/status?run_id=RUN-K8S-AMF-1)

**Orchestrator UI link:** [http://127.0.0.1:8115/?run_id=RUN-K8S-AMF-1](http://127.0.0.1:8115/?run_id=RUN-K8S-AMF-1)

**Events (debug):** [http://127.0.0.1:8115/v1/runs/RUN-K8S-AMF-1/events](http://127.0.0.1:8115/v1/runs/RUN-K8S-AMF-1/events)

What to see: topology `ns 5g-core`, AMF node CrashLoop, four kubectl steps (`get all` → `describe pod` → `logs --previous` → `get deploy -o yaml`), session summary RESOLVED.

### 3.2 ImagePullBackOff · `ts` / `imagepull-demo`

| | |
| --- | --- |
| Session | `imagepull-nginx` |
| RCA | Image tag `nginx:lastest` (typo) |
| Iterations | 3 |
| Suggested `run_id` | `RUN-K8S-IMAGE-1` |

**Prompt:** `why is imagepull-demo failing`

Other matching prompts: `check pods in ts ns` · `Check pods in the current ns`

**Trigger**

```bash
curl -sS -X POST http://127.0.0.1:8115/execute \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"RUN-K8S-IMAGE-1","prompt":"why is imagepull-demo failing"}'
```

**Poll:** [http://127.0.0.1:8115/status?run_id=RUN-K8S-IMAGE-1](http://127.0.0.1:8115/status?run_id=RUN-K8S-IMAGE-1)

**Orchestrator UI link:** [http://127.0.0.1:8115/?run_id=RUN-K8S-IMAGE-1](http://127.0.0.1:8115/?run_id=RUN-K8S-IMAGE-1)

**Events:** [http://127.0.0.1:8115/v1/runs/RUN-K8S-IMAGE-1/events](http://127.0.0.1:8115/v1/runs/RUN-K8S-IMAGE-1/events)

What to see: topology `ns ts`, ImagePull node highlighted, kubectl `get pods -o wide` → `describe` → `get pod -o yaml`.

### 3.3 Pending pod · `ts` / `busybox-pod`

| | |
| --- | --- |
| Session | `busybox-pending` |
| RCA | CPU request 4 vs node allocatable 2 |
| Iterations | 3 |
| Suggested `run_id` | `RUN-K8S-BUSYBOX-1` |

**Prompt:** `why is busybox-pod in pending state`

Other matching prompts: `check busybox-pod in ns ts` · `Pod busybox-pod in namespace ts is Pending — find why`

**Trigger**

```bash
curl -sS -X POST http://127.0.0.1:8115/execute \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"RUN-K8S-BUSYBOX-1","prompt":"why is busybox-pod in pending state"}'
```

**Poll:** [http://127.0.0.1:8115/status?run_id=RUN-K8S-BUSYBOX-1](http://127.0.0.1:8115/status?run_id=RUN-K8S-BUSYBOX-1)

**Orchestrator UI link:** [http://127.0.0.1:8115/?run_id=RUN-K8S-BUSYBOX-1](http://127.0.0.1:8115/?run_id=RUN-K8S-BUSYBOX-1)

**Events:** [http://127.0.0.1:8115/v1/runs/RUN-K8S-BUSYBOX-1/events](http://127.0.0.1:8115/v1/runs/RUN-K8S-BUSYBOX-1/events)

What to see: topology Pending busybox, kubectl `get pod -o wide` → `describe` → `get nodes` allocatable CPU.

### 3.4 Payments CrashLoop · `api-0`

| | |
| --- | --- |
| Session | `payments-crashloop` |
| RCA | Liveness `initialDelaySeconds=3` vs ~14s startup |
| Iterations | 3 |
| Suggested `run_id` | `RUN-K8S-PAY-1` |

**Prompt:** `Why is pod api-0 CrashLoopBackOff in payments?`

Other matching prompts: `Check CrashLoopBackOff in payments` · `Pod api-0 in payments is CrashLoopBackOff`

**Trigger**

```bash
curl -sS -X POST http://127.0.0.1:8115/execute \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"RUN-K8S-PAY-1","prompt":"Why is pod api-0 CrashLoopBackOff in payments?"}'
```

**Poll:** [http://127.0.0.1:8115/status?run_id=RUN-K8S-PAY-1](http://127.0.0.1:8115/status?run_id=RUN-K8S-PAY-1)

**Orchestrator UI link:** [http://127.0.0.1:8115/?run_id=RUN-K8S-PAY-1](http://127.0.0.1:8115/?run_id=RUN-K8S-PAY-1)

**Events:** [http://127.0.0.1:8115/v1/runs/RUN-K8S-PAY-1/events](http://127.0.0.1:8115/v1/runs/RUN-K8S-PAY-1/events)

What to see: topology `ns payments` (ingress → svc → api-0 CrashLoop → redis), kubectl `get pod -o wide` → `describe` → `logs --previous`.

### 3.5 Customer unable to latch · Calico CNI

| | |
| --- | --- |
| Session | `ue-latch-calico` |
| RCA | Calico `calico-node` pods Ready 1/1 with 61–63 restarts |
| Iterations | 6 |
| Topology | `kube-system` CNI + OAI 5GC |
| Suggested `run_id` | `RUN-K8S-LATCH-1` |

**Prompt (copy this):** `Customer unable to latch on the network`

Other matching prompts: `UE unable to latch on the network` · `customer unable to latch onto the network`

**Trigger**

```bash
curl -sS -X POST http://127.0.0.1:8115/execute \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"RUN-K8S-LATCH-1","prompt":"Customer unable to latch on the network"}'
```

**Poll:** [http://127.0.0.1:8115/status?run_id=RUN-K8S-LATCH-1](http://127.0.0.1:8115/status?run_id=RUN-K8S-LATCH-1)

**Orchestrator UI link:** [http://127.0.0.1:8115/?run_id=RUN-K8S-LATCH-1](http://127.0.0.1:8115/?run_id=RUN-K8S-LATCH-1)

**Events:** [http://127.0.0.1:8115/v1/runs/RUN-K8S-LATCH-1/events](http://127.0.0.1:8115/v1/runs/RUN-K8S-LATCH-1/events)

What to see: topology `ns kube-system`, Calico nodes highlighted, kubectl `get pods -A -o wide` → kube-system → empty `app=cni` → DaemonSets → YAML → `k8s-app=calico-node`. History AMF-selector RCA is fed but overridden. Session summary RESOLVED.

### 3.6 Quick-copy table

| Scenario | Trigger prompt | Poll | Agent UI |
| --- | --- | --- | --- |
| AMF | `Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?` | [/status?run_id=RUN-K8S-AMF-1](http://127.0.0.1:8115/status?run_id=RUN-K8S-AMF-1) | [/?run_id=RUN-K8S-AMF-1](http://127.0.0.1:8115/?run_id=RUN-K8S-AMF-1) |
| ImagePull | `why is imagepull-demo failing` | [/status?run_id=RUN-K8S-IMAGE-1](http://127.0.0.1:8115/status?run_id=RUN-K8S-IMAGE-1) | [/?run_id=RUN-K8S-IMAGE-1](http://127.0.0.1:8115/?run_id=RUN-K8S-IMAGE-1) |
| Busybox Pending | `why is busybox-pod in pending state` | [/status?run_id=RUN-K8S-BUSYBOX-1](http://127.0.0.1:8115/status?run_id=RUN-K8S-BUSYBOX-1) | [/?run_id=RUN-K8S-BUSYBOX-1](http://127.0.0.1:8115/?run_id=RUN-K8S-BUSYBOX-1) |
| Payments probe | `Why is pod api-0 CrashLoopBackOff in payments?` | [/status?run_id=RUN-K8S-PAY-1](http://127.0.0.1:8115/status?run_id=RUN-K8S-PAY-1) | [/?run_id=RUN-K8S-PAY-1](http://127.0.0.1:8115/?run_id=RUN-K8S-PAY-1) |
| UE latch / Calico | `Customer unable to latch on the network` | [/status?run_id=RUN-K8S-LATCH-1](http://127.0.0.1:8115/status?run_id=RUN-K8S-LATCH-1) | [/?run_id=RUN-K8S-LATCH-1](http://127.0.0.1:8115/?run_id=RUN-K8S-LATCH-1) |

Trigger all five (concurrent instances):

```bash
curl -sS -X POST http://127.0.0.1:8115/execute -H 'Content-Type: application/json' \
  -d '{"run_id":"RUN-K8S-AMF-1","prompt":"Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?"}'
curl -sS -X POST http://127.0.0.1:8115/execute -H 'Content-Type: application/json' \
  -d '{"run_id":"RUN-K8S-IMAGE-1","prompt":"why is imagepull-demo failing"}'
curl -sS -X POST http://127.0.0.1:8115/execute -H 'Content-Type: application/json' \
  -d '{"run_id":"RUN-K8S-BUSYBOX-1","prompt":"why is busybox-pod in pending state"}'
curl -sS -X POST http://127.0.0.1:8115/execute -H 'Content-Type: application/json' \
  -d '{"run_id":"RUN-K8S-PAY-1","prompt":"Why is pod api-0 CrashLoopBackOff in payments?"}'
curl -sS -X POST http://127.0.0.1:8115/execute -H 'Content-Type: application/json' \
  -d '{"run_id":"RUN-K8S-LATCH-1","prompt":"Customer unable to latch on the network"}'
```

Then open the UI links above in **separate browser tabs**. Each tab must stay on its own session (AMF vs ImagePull vs Pending vs Payments vs Calico latch).

---

## 4. Thorough test checklist

Do these in order. Unless noted, use **realtime** (default `pace`) so you can join mid-flight.

### A. Standalone UI (no orchestrator)

1. Open [http://127.0.0.1:8115/](http://127.0.0.1:8115/).
2. For each Known issues dropdown item, Run and wait until **Resolved**.
3. Confirm topology appears only after Gather Context.
4. Confirm iteration cards stack latest-on-top and the kubectl pane matches the command.
5. On the AMF run, switch **Open5GS 5GC** vs **OAI 5GC** in the topology flavor picker.

### B. Orchestrator happy path (one scenario)

1. `POST /execute` for AMF with `RUN-K8S-AMF-1` (section 3.1).
2. Immediately `GET /status` — expect `ACCEPTED` or `RUNNING`, `progress` ≥ 0, `agent` = `K8s`.
3. Poll every 2s. Watch `current_response` and `sub_agents` move Analyze → Context → Generate/Validate → Report.
4. **Before it finishes**, open [/?run_id=RUN-K8S-AMF-1](http://127.0.0.1:8115/?run_id=RUN-K8S-AMF-1). The page must **not** restart from Analyze Intent if later steps already ran; it should show the current iteration and keep advancing.
5. Intent input and Run stay **disabled**.
6. Poll until `COMPLETED`, `progress` 100. UI shows Session summary **RESOLVED**.
7. Refresh the same UI URL after complete — summary still present.

### C. All five scenarios

Run section 3.5. For each completed status payload check:

- `details.session_id` matches the table.
- `details.ui_url` is `/?run_id=<that id>`.
- Opening the UI link shows the **correct** namespace and RCA (not a mix of another run).

### D. Concurrent instances (orchestrator view)

1. Start AMF + Payments at the same time with different `run_id`s.
2. Open both UI links in two tabs within a few seconds.
3. Confirm tab A is `5g-core` / `open5gs-amf-0` and tab B is `payments` / `api-0`.
4. Confirm both `/status` payloads stay independent (`session_id` does not swap).

### E. Mid-flight join vs finished join

1. Start busybox (`RUN-K8S-BUSYBOX-1`), wait ~5s, open the UI (should be Gather Context or Iteration 1).
2. Leave the tab open until Resolved.
3. Start imagepull with a **new** id, wait until `/status` is `COMPLETED`, *then* open the UI — should land on the summary with no long replay from the start.

### F. Failure and edge cases

| Test | How | Expected |
| --- | --- | --- |
| Unknown run | [http://127.0.0.1:8115/status?run_id=RUN-DOES-NOT-EXIST](http://127.0.0.1:8115/status?run_id=RUN-DOES-NOT-EXIST) | HTTP **404** |
| Unknown UI join | [http://127.0.0.1:8115/?run_id=RUN-DOES-NOT-EXIST](http://127.0.0.1:8115/?run_id=RUN-DOES-NOT-EXIST) | Match line: **Run not found** |
| Unmatched prompt | `POST /execute` with `"prompt":"unrelated xyzzy widget"` and `run_id` `RUN-K8S-FAIL-1` | 202, then `/status` **FAILED** (HTTP 200), `current_response` mentions no catalog match |
| Idempotent retry | `POST /execute` twice with same id + same AMF prompt | Both 202; second body’s `current_status` is current (often `RUNNING` or `COMPLETED`) |
| Conflicting prompt | Second `POST /execute` same id, busybox prompt | HTTP **409** |
| Missing fields | `POST /execute` `{}` | HTTP **422** |

Unmatched example:

```bash
curl -sS -X POST http://127.0.0.1:8115/execute \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"RUN-K8S-FAIL-1","prompt":"unrelated xyzzy widget"}'
curl -sS "http://127.0.0.1:8115/status?run_id=RUN-K8S-FAIL-1"
# current_status FAILED, HTTP 200
```

UI: [http://127.0.0.1:8115/?run_id=RUN-K8S-FAIL-1](http://127.0.0.1:8115/?run_id=RUN-K8S-FAIL-1)

### G. Fast path (optional)

`pace` is **not** for the orchestrator. For a quick COMPLETED poll without waiting ~40s:

```bash
curl -sS -X POST http://127.0.0.1:8115/execute \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"RUN-K8S-AMF-FAST","prompt":"Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?","pace":"fast"}'
curl -sS "http://127.0.0.1:8115/status?run_id=RUN-K8S-AMF-FAST"
# current_status COMPLETED immediately
```

UI: [http://127.0.0.1:8115/?run_id=RUN-K8S-AMF-FAST](http://127.0.0.1:8115/?run_id=RUN-K8S-AMF-FAST)

---

## 5. How to wire the orchestrator UI

Minimal integration (no code in this repo):

1. **Agent registry** — base URL `http://<host>:8115`, agent name `K8s`.
2. **On mission start** — generate `run_id` (example `RUN-K8S-<mission>-<n>`), `POST /execute` with `{ run_id, prompt, details }`. Do not block on RCA.
3. **On 202** — store `run_id`; start a 1–2s poll of `GET /status?run_id=`.
4. **Render poll** — status chip from `current_status`, bar from `progress`, subtitle from `current_response`, optional tree from `sub_agents`.
5. **Agent button** — `href = agentBase + details.ui_url` (or `agentBase + "/?run_id=" + run_id`). Open in new tab or iframe. Label e.g. “Open K8s Troubleshooter”.
6. **Stop polling** on `COMPLETED` or `FAILED`. Keep the same link for the historical view.
7. **Multi-agent missions** — one `run_id` per agent instance. Never share a `run_id` across K8s and RAN.

Pseudo-code:

```python
ack = http.post(f"{K8S_AGENT}/execute", json={
    "run_id": run_id,
    "prompt": mission_text,
    "details": {"mission_id": mission_id},
})
assert ack.status_code == 202

while True:
    st = http.get(f"{K8S_AGENT}/status", params={"run_id": run_id}).json()
    update_orchestrator_row(st)  # progress, current_response, sub_agents
    link = K8S_AGENT.rstrip("/") + st["details"]["ui_url"]
    set_agent_href(link)         # clickable as soon as 202 succeeded
    if st["current_status"] in ("COMPLETED", "FAILED"):
        break
    sleep(2)
```

---

## 6. What this agent does **not** do

- No live kubectl / kubeconfig / LLM.
- No Redis: runs live in **one process** memory. Restarting uvicorn drops all `run_id`s (UI join then shows run not found).
- Demo `POST /v1/runs` still exists for the CLI (`run-prompt.sh`). The orchestrator should use **`/execute` + `/status` only**.

Related: [LLD](LLD.md) · orchestrator contract [api.md](../../api.md) · [README](../README.md)
