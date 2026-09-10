# start a run
curl -sS -X POST http://127.0.0.1:8115/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"query":"why is busybox-pod in pending state","pace":"fast"}'

# stream iteration snapshots (replace RUN_ID)
curl -sS -N "http://127.0.0.1:8115/v1/runs/RUN_ID/stream"
