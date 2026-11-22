#!/usr/bin/env sh
# Start the app, wait for health, then run the headless WS E2E.
set -eu

PYTHON="${PYTHON:-python}"
PORT="${PORT:-8081}"
BASE="http://127.0.0.1:${PORT}"
LOG="${LOG:-/tmp/aicall-e2e.log}"

"$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >"$LOG" 2>&1 &
PID=$!
cleanup() {
    kill "$PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

i=0
while [ "$i" -lt 30 ]; do
    if curl -fsS "$BASE/healthz" >/dev/null 2>&1; then
        break
    fi
    i=$((i + 1))
    sleep 1
done
if [ "$i" -ge 30 ]; then
    echo "server did not become healthy; log:" >&2
    tail -50 "$LOG" >&2 || true
    exit 1
fi

"$PYTHON" scripts/e2e_ws.py --base "$BASE"
