#!/usr/bin/env sh
# End-to-end smoke test for the fake skeleton.
#
# It checks GET /healthz, creates a text session, posts one text turn and reads
# the session events. The server must already be running:
#   make run          # local, port 8080
#   make docker-up    # compose, port 8080
#
# Env:
#   BASE_URL    default http://127.0.0.1:8080
#
# Requires curl and python3 (or python). The fake skeleton does not enforce
# auth; DEV_NO_AUTH=1 is the documented dev default.
set -eu

BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
BASE_URL="${BASE_URL%/}"

if ! command -v curl >/dev/null 2>&1; then
    echo "smoke: curl is required" >&2
    exit 1
fi

if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "smoke: python3 (or python) is required" >&2
    exit 1
fi

# json_field <name>: read one JSON object on stdin and print a top-level field.
json_field() {
    "$PYTHON" -c 'import json, sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$1"
}

echo "==> GET $BASE_URL/healthz"
HEALTH="$(curl -fsS --connect-timeout 5 --max-time 10 "$BASE_URL/healthz")"
printf '%s\n' "$HEALTH"
printf '%s' "$HEALTH" | "$PYTHON" -c 'import json, sys; data = json.load(sys.stdin); assert isinstance(data, dict) and "status" in data, "healthz must return a JSON object with a status field"'

echo "==> POST $BASE_URL/sessions"
SESSION_JSON="$(curl -fsS --connect-timeout 5 --max-time 10 \
    -X POST "$BASE_URL/sessions" \
    -H 'Content-Type: application/json' \
    -d '{"mode":"text"}')"
printf '%s\n' "$SESSION_JSON"

SESSION_ID="$(printf '%s' "$SESSION_JSON" | json_field session_id)"
if [ -z "$SESSION_ID" ]; then
    # Tolerate an early implementation that names the field `id`.
    SESSION_ID="$(printf '%s' "$SESSION_JSON" | json_field id)"
fi
if [ -z "$SESSION_ID" ]; then
    echo "smoke: session response has no session_id" >&2
    exit 1
fi
echo "    session_id=$SESSION_ID"

echo "==> POST $BASE_URL/sessions/$SESSION_ID/text"
TEXT_RESPONSE="$(curl -fsS --connect-timeout 5 --max-time 30 \
    -X POST "$BASE_URL/sessions/$SESSION_ID/text" \
    -H 'Content-Type: application/json' \
    -d '{"text":"你好，我想查询一下我的订单。"}')"
printf '%s\n' "$TEXT_RESPONSE"

echo "==> GET $BASE_URL/sessions/$SESSION_ID/events"
EVENTS="$(curl -fsS --connect-timeout 5 --max-time 30 \
    "$BASE_URL/sessions/$SESSION_ID/events?since_seq=0")"
printf '%s\n' "$EVENTS"

printf '%s' "$EVENTS" | "$PYTHON" -c '
import json
import sys

raw = sys.stdin.read().strip()
if not raw:
    raise SystemExit("smoke: events response was empty")

try:
    value = json.loads(raw)
except json.JSONDecodeError:
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
else:
    if isinstance(value, list):
        events = value
    elif isinstance(value, dict) and isinstance(value.get("events"), list):
        events = value["events"]
    else:
        events = [value]

if not events:
    raise SystemExit("smoke: no events returned")
print(f"    events={len(events)}")
'

echo "OK: healthz, session, text turn and events all responded"
