#!/usr/bin/env bash
# Start a catalog replay on the demo API and print snapshots on the demo timeline.
# Usage:
#   ./run-prompt.sh "Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?"
#   ./run-prompt.sh --instant "Check CrashLoopBackOff in 5g-core"
# Env: K8S_DEMO_URL (default http://127.0.0.1:8115)
set -euo pipefail

BASE="${K8S_DEMO_URL:-http://127.0.0.1:8115}"
PACE="realtime"
TIMED=1

usage() {
  cat <<'EOF'
Usage: run-prompt.sh [options] [prompt...]

Start a K8s Troubleshooter demo run and stream snapshots with the same delays
as the UI (gather ~1.6s, kubectl ~2s, analyzing 5–10s per iteration).

Options:
  --pace realtime|fast   Server timing (default: realtime, runs in the background)
  --instant              Print everything immediately (no demo delays)
  --url URL              API base (default: http://127.0.0.1:8115)
  -h, --help             Show this help

If no prompt is given, the script reads one line from stdin.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pace)
      PACE="${2:-}"
      shift 2
      ;;
    --instant)
      TIMED=0
      PACE="fast"
      shift
      ;;
    --url)
      BASE="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      break
      ;;
  esac
done

if [[ "$PACE" != "fast" && "$PACE" != "realtime" ]]; then
  echo "pace must be fast or realtime" >&2
  exit 1
fi

PROMPT="${*:-}"
if [[ -z "$PROMPT" ]]; then
  if [[ -t 0 ]]; then
    printf "Prompt: "
  fi
  IFS= read -r PROMPT || true
fi
PROMPT="${PROMPT#"${PROMPT%%[![:space:]]*}"}"
PROMPT="${PROMPT%"${PROMPT##*[![:space:]]}"}"
if [[ -z "$PROMPT" ]]; then
  echo "No prompt provided." >&2
  usage >&2
  exit 1
fi

BODY=$(python3 -c 'import json,sys; print(json.dumps({"query": sys.argv[1], "pace": sys.argv[2]}))' "$PROMPT" "$PACE")

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

HTTP=$(curl -sS -o "$TMP" -w "%{http_code}" -X POST "$BASE/v1/runs" \
  -H "Content-Type: application/json" \
  -d "$BODY") || {
  echo "Could not reach $BASE — is uvicorn running on 8115?" >&2
  exit 1
}

if [[ "$HTTP" != "200" ]]; then
  echo "POST /v1/runs failed ($HTTP)" >&2
  cat "$TMP" >&2
  echo >&2
  exit 1
fi

python3 - "$BASE" "$TMP" "$PACE" "$TIMED" <<'PY'
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

base, start_path, pace, timed_flag = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
timed = timed_flag == "1"
start = json.load(open(start_path))
run_id = start["run_id"]
stream_url = base.rstrip("/") + (start.get("stream_url") or ("/v1/runs/%s/stream" % run_id))
events_url = base.rstrip("/") + (start.get("events_url") or ("/v1/runs/%s/events" % run_id))

print("run_id     " + run_id)
print("session    " + str(start.get("session_id")))
print("title      " + str(start.get("title")))
print("status     " + str(start.get("status")))
if timed:
    print("pace       %s  (timed view — same delays as the demo UI)" % pace)
else:
    print("pace       %s  (instant)" % pace)
print()
sys.stdout.flush()


def one_line(s, n=220):
    t = " ".join(str(s or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def show(ev):
    kind = ev.get("type") or ""
    wf = ev.get("workflow") or {}
    it = ev.get("iteration") or {}
    topo = ev.get("topology") or {}
    cap = wf.get("caption") or ""
    seq = ev.get("seq")
    print("[%s] %s  %s" % (seq, kind, cap), flush=True)
    if kind == "topology.ready":
        print(
            "  ns %s  focus %s  nodes %s"
            % (topo.get("namespace"), topo.get("focus"), len(topo.get("nodes") or [])),
            flush=True,
        )
    if kind == "iteration.generate" and it.get("hypothesis"):
        print("  hypothesis  " + one_line(it.get("hypothesis")), flush=True)
    if kind in ("iteration.command", "iteration.complete") and it.get("command"):
        print("  $ " + str(it.get("command")), flush=True)
    if kind == "iteration.command" and it.get("stdout"):
        lines = str(it.get("stdout")).splitlines()
        for ln in lines[:8]:
            print("    " + ln, flush=True)
        extra = len(lines) - 8
        if extra > 0:
            print("    … %s more lines" % extra, flush=True)
    if kind == "iteration.analyzing":
        ms = int(ev.get("delay_ms") or 0)
        print("  analyzing output (%.1fs)…" % (ms / 1000.0), flush=True)
    if kind == "iteration.complete":
        if it.get("analysis"):
            print("  analysis  " + one_line(it.get("analysis")), flush=True)
        for take in it.get("takeaways") or []:
            print("  • " + one_line(take, 200), flush=True)
    if kind == "run.finished":
        final = ev.get("final") or {}
        print("  outcome  " + str(final.get("outcome") or final), flush=True)
    if kind == "run.error":
        print("  error  " + str(ev.get("error") or cap), flush=True)


class DelayClock:
    def __init__(self, enabled):
        self.enabled = enabled
        self.pending = 0.0
        self.marked = time.monotonic()

    def after(self, ev):
        if not self.enabled:
            return
        now = time.monotonic()
        remain = self.pending - (now - self.marked)
        if remain > 0.02:
            time.sleep(remain)
        self.pending = int(ev.get("delay_ms") or 0) / 1000.0
        self.marked = time.monotonic()


def iter_sse(url):
    proc = subprocess.Popen(
        ["curl", "-sS", "-N", url],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            data = line.split("data:", 1)[1].strip()
            if not data:
                continue
            yield json.loads(data)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def iter_events(url):
    with urllib.request.urlopen(url) as resp:
        payload = json.load(resp)
    for ev in payload.get("events") or []:
        yield ev


clock = DelayClock(timed)
try:
    source = iter_sse(stream_url) if pace == "realtime" else iter_events(events_url)
    for ev in source:
        clock.after(ev)
        show(ev)
        if ev.get("type") in ("run.finished", "run.error"):
            break
except urllib.error.HTTPError as exc:
    print("Failed to read run: %s" % exc, file=sys.stderr)
    sys.exit(1)
except KeyboardInterrupt:
    print("\nstopped", flush=True)
    sys.exit(130)
PY
