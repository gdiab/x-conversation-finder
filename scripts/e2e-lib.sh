#!/bin/bash
# Shared setup for the e2e scripts (issue #27). Source it; do not run it.
#
# The worker and the API run in E2E_DIR, which holds no .env, so the app's
# .env settings do not apply; the settings that matter are exported below.
#
# Each script starts its own stack: a Temporal dev server (in memory), a
# worker and an API, on ports nothing else is using, with fake adapters and a
# SQLite file under data/e2e/. It never talks to a stack it did not start: if
# a port is already in use, it stops before starting anything.
#
# Settings (environment):
#   E2E_TEMPORAL_PORT     Temporal frontend port (default 7243)
#   E2E_TEMPORAL_UI_PORT  Temporal web UI port (default 8243)
#   E2E_API_PORT          API port (default: per script)
#   E2E_DIR               SQLite file and logs (default <repo>/data/e2e)
#   PYTHON                Python interpreter (default <repo>/.venv/bin/python, else python3)
#
# After e2e_start_stack, E2E_API is the API base URL (http://127.0.0.1:<port>/api).

E2E_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$E2E_LIB_DIR/.." && pwd)"
export PYTHONPATH="$WORKSPACE_ROOT"

if [ -z "${PYTHON:-}" ]; then
    if [ -x "$WORKSPACE_ROOT/.venv/bin/python" ]; then
        PYTHON="$WORKSPACE_ROOT/.venv/bin/python"
    else
        PYTHON=python3
    fi
fi

E2E_PIDS=()

e2e_fail() {
    echo "❌ $*"
    exit 1
}

# e2e_configure NAME DEFAULT_API_PORT: export the isolated, fake-adapter settings.
e2e_configure() {
    E2E_NAME="$1"
    E2E_TEMPORAL_PORT="${E2E_TEMPORAL_PORT:-7243}"
    E2E_TEMPORAL_UI_PORT="${E2E_TEMPORAL_UI_PORT:-8243}"
    E2E_API_PORT="${E2E_API_PORT:-$2}"
    E2E_DIR="${E2E_DIR:-$WORKSPACE_ROOT/data/e2e}"
    mkdir -p "$E2E_DIR"
    E2E_DIR="$(cd "$E2E_DIR" && pwd)"
    # Every Python process of the script runs here, where no .env is read
    cd "$E2E_DIR"
    E2E_DB="$E2E_DIR/$E2E_NAME.db"
    E2E_API="http://127.0.0.1:$E2E_API_PORT/api"

    # Always this script's own database: an inherited DATABASE_URL (or a
    # .env) must not point the stack at the app's database
    export DATABASE_URL="sqlite:///$E2E_DB"
    # TEMPORAL_HOST is what the worker and the API read; TEMPORAL_ADDRESS is
    # what the temporal CLI reads
    export TEMPORAL_HOST="127.0.0.1:$E2E_TEMPORAL_PORT"
    export TEMPORAL_ADDRESS="$TEMPORAL_HOST"
    export TEMPORAL_NAMESPACE=default

    # Fake adapters and no credentials, so nothing can call X or OpenAI
    export X_CLIENT_ADAPTER=fake
    export REPLY_AGENT_ADAPTER=fake
    export X_BEARER_TOKEN=""
    export X_API_KEY=""
    export X_API_SECRET=""
    export OPENAI_API_KEY=""
    export DRAFT_MODE=assembled
    # The default threshold, whatever the environment says, so the test posts
    # get a card (a higher CONFIDENCE_THRESHOLD would skip them)
    CONFIDENCE_THRESHOLD=$("$PYTHON" -c "from backend.config.confidence import CONFIDENCE_THRESHOLD as t; print(t)") \
        || e2e_fail "Could not read the default confidence threshold"
    export CONFIDENCE_THRESHOLD

    # Short delays and generous budgets for the test
    export FOLLOWUP_DELAY_SECONDS=8  # a real durable timer, 8 seconds
    export REVIEW_TIMEOUT_HOURS=1
    export ACTIVITY_TIMEOUT_SHORT_SEC=10
    export ACTIVITY_TIMEOUT_MEDIUM_SEC=30
    export ACTIVITY_TIMEOUT_LONG_SEC=60
    export ACTIVITY_MAX_ATTEMPTS=2
    export CLAIM_GATE_MAX_ATTEMPTS=2
    export DAILY_SPEND_LIMIT_USD=100.0
    export DAILY_POST_READ_CAP=1000
    export DAILY_INFERENCE_LIMIT_USD=50.0
    export X_POST_READ_COST=0.005
    export BUDGET_KILL_SWITCH=false

    echo "Configuration:"
    echo "  Adapters: X=$X_CLIENT_ADAPTER, Reply=$REPLY_AGENT_ADAPTER (no credentials)"
    echo "  Temporal: $TEMPORAL_HOST (in memory), UI port $E2E_TEMPORAL_UI_PORT"
    echo "  API: $E2E_API"
    echo "  Database: ${E2E_DB#"$WORKSPACE_ROOT"/}"
    echo "  Follow-up delay: ${FOLLOWUP_DELAY_SECONDS}s (real durable timer)"
    echo ""
}

# e2e_assert_listener PORT PID NAME: fail unless PID is the process listening
# on PORT, so a readiness check never accepts a server another process
# started on the port after the free-port check. Needs lsof.
e2e_assert_listener() {
    local listeners
    listeners=$(lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null || true)
    if ! printf '%s\n' "$listeners" | grep -qx "$2"; then
        e2e_fail "$3 on port $1 is not the process this script started (listening: ${listeners:-none})"
    fi
}

e2e_port_in_use() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

e2e_cleanup() {
    echo ""
    echo "=== Cleanup ==="
    local pid
    for pid in "${E2E_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    for pid in "${E2E_PIDS[@]}"; do
        wait "$pid" 2>/dev/null || true
    done
    echo "✓ Services stopped"
}

# e2e_start_stack: start Temporal, the worker and the API; wait until each is ready.
e2e_start_stack() {
    local port
    for port in "$E2E_TEMPORAL_PORT" "$E2E_TEMPORAL_UI_PORT" "$E2E_API_PORT"; do
        if e2e_port_in_use "$port"; then
            e2e_fail "Port $port is already in use. The e2e stack starts its own services and never uses a running one; set E2E_TEMPORAL_PORT, E2E_TEMPORAL_UI_PORT or E2E_API_PORT to free ports."
        fi
    done

    rm -f "$E2E_DB" "$E2E_DB-wal" "$E2E_DB-shm" "$E2E_DB-journal"
    "$PYTHON" -c "from backend.database import init_db; init_db()" || e2e_fail "Database initialization failed"
    echo "✓ Database initialized"
    echo ""

    trap e2e_cleanup EXIT

    local log_prefix="$E2E_DIR/$E2E_NAME"
    echo "Starting Temporal dev server..."
    temporal server start-dev --ip 127.0.0.1 --port "$E2E_TEMPORAL_PORT" \
        --ui-port "$E2E_TEMPORAL_UI_PORT" > "$log_prefix-temporal.log" 2>&1 &
    E2E_TEMPORAL_PID=$!
    E2E_PIDS+=("$E2E_TEMPORAL_PID")
    local i
    for i in {1..30}; do
        if temporal workflow list --address "$TEMPORAL_ADDRESS" --limit 1 > /dev/null 2>&1; then
            e2e_assert_listener "$E2E_TEMPORAL_PORT" "$E2E_TEMPORAL_PID" Temporal
            echo "✓ Temporal ready"
            break
        fi
        kill -0 "$E2E_TEMPORAL_PID" 2>/dev/null || { tail -20 "$log_prefix-temporal.log"; e2e_fail "Temporal exited"; }
        [ "$i" -eq 30 ] && { tail -20 "$log_prefix-temporal.log"; e2e_fail "Temporal timeout"; }
        sleep 1
    done

    echo "Starting worker..."
    (exec "$PYTHON" -m backend.worker) > "$log_prefix-worker.log" 2>&1 &
    E2E_WORKER_PID=$!
    E2E_PIDS+=("$E2E_WORKER_PID")
    for i in {1..30}; do
        if grep -q "Worker running" "$log_prefix-worker.log" 2>/dev/null; then
            echo "✓ Worker running"
            break
        fi
        kill -0 "$E2E_WORKER_PID" 2>/dev/null || { tail -30 "$log_prefix-worker.log"; e2e_fail "Worker exited"; }
        [ "$i" -eq 30 ] && { tail -30 "$log_prefix-worker.log"; e2e_fail "Worker timeout"; }
        sleep 1
    done

    echo "Starting API..."
    (exec "$PYTHON" -m uvicorn backend.api:app \
        --host 127.0.0.1 --port "$E2E_API_PORT") > "$log_prefix-api.log" 2>&1 &
    E2E_API_PID=$!
    E2E_PIDS+=("$E2E_API_PID")
    for i in {1..30}; do
        if curl -sf "$E2E_API/budget" > /dev/null 2>&1; then
            e2e_assert_listener "$E2E_API_PORT" "$E2E_API_PID" API
            echo "✓ API ready"
            break
        fi
        kill -0 "$E2E_API_PID" 2>/dev/null || { tail -20 "$log_prefix-api.log"; e2e_fail "API exited"; }
        [ "$i" -eq 30 ] && { tail -20 "$log_prefix-api.log"; e2e_fail "API timeout"; }
        sleep 1
    done
    echo ""
}

e2e_worker_log_tail() {
    echo "Worker log (last 40 lines):"
    tail -40 "$E2E_DIR/$E2E_NAME-worker.log"
}

# e2e_request METHOD PATH [JSON]: print the response body; fail unless HTTP 200.
e2e_request() {
    local method="$1" path="$2" body="${3:-}" out code
    if [ -n "$body" ]; then
        out=$(curl -sS --max-time 30 -w "\n%{http_code}" -X "$method" "$E2E_API$path" \
            -H "Content-Type: application/json" -d "$body") || e2e_fail "$method $path: curl failed"
    else
        out=$(curl -sS --max-time 30 -w "\n%{http_code}" -X "$method" "$E2E_API$path") \
            || e2e_fail "$method $path: curl failed"
    fi
    code=$(printf '%s\n' "$out" | tail -n 1)
    out=$(printf '%s\n' "$out" | sed '$d')
    if [ "$code" != "200" ]; then
        echo "$out"
        e2e_fail "$method $path returned HTTP $code"
    fi
    printf '%s\n' "$out"
}

# e2e_expect_error METHOD PATH JSON STATUS CODE: send the request and fail
# unless the API refuses it with HTTP STATUS and error code CODE.
e2e_expect_error() {
    local method="$1" path="$2" body="$3" want_status="$4" want_code="$5" out status code
    out=$(curl -sS --max-time 30 -w "\n%{http_code}" -X "$method" "$E2E_API$path" \
        -H "Content-Type: application/json" -d "$body") || e2e_fail "$method $path: curl failed"
    status=$(printf '%s\n' "$out" | tail -n 1)
    out=$(printf '%s\n' "$out" | sed '$d')
    printf '%s\n' "$out"
    code=$(printf '%s' "$out" | e2e_json "d.get('code', '')" 2>/dev/null) || code=""
    if [ "$status" != "$want_status" ] || [ "$code" != "$want_code" ]; then
        e2e_fail "$method $path returned HTTP $status code '$code', expected HTTP $want_status code '$want_code'"
    fi
}

# e2e_json EXPR: evaluate a Python expression over the JSON on stdin (bound
# to `d`) and print the result.
e2e_json() {
    "$PYTHON" -c "import json, sys; d = json.load(sys.stdin); print($1)"
}

# e2e_wait_for_status WORKFLOW_ID STATUS SECONDS: poll the card until it has STATUS.
e2e_wait_for_status() {
    local workflow_id="$1" want="$2" seconds="$3" status="" i
    for ((i = 0; i < seconds; i++)); do
        status=$(curl -s --max-time 10 "$E2E_API/reviews/$workflow_id" | e2e_json "d.get('status', '')" 2>/dev/null) || status=""
        [ "$status" = "$want" ] && return 0
        sleep 1
    done
    e2e_worker_log_tail
    e2e_fail "Card $workflow_id status is '$status' after ${seconds}s, expected '$want'"
}

# e2e_approve WORKFLOW_ID POSTED_URL REQUEST_ID: approve the draft on the card.
# Reads the card first and sends its draft text and edited-text gate state
# (null if never edited), as the review app does; the API refuses an approve
# without them (issue #27).
e2e_approve() {
    local workflow_id="$1" posted_url="$2" request_id="$3" card body
    card=$(e2e_request GET "/reviews/$workflow_id")
    body=$(printf '%s' "$card" | POSTED_URL="$posted_url" REQUEST_ID="$request_id" "$PYTHON" -c '
import json, os, sys
card = json.load(sys.stdin)
draft = (card.get("evidence") or {}).get("draft") or {}
if not draft.get("text"):
    sys.exit("card has no draft text")
print(json.dumps({
    "posted_url": os.environ["POSTED_URL"],
    "client_request_id": os.environ["REQUEST_ID"],
    "draft_text": draft["text"],
    "draft_gate_state": card.get("edited_text_gate_state"),
}))') || e2e_fail "Could not build the approve request for $workflow_id"
    e2e_request POST "/reviews/$workflow_id/approve" "$body"
}

# e2e_confirm WORKFLOW_ID REPLY_URL REQUEST_ID
e2e_confirm() {
    local body
    body=$(REPLY_URL="$2" REQUEST_ID="$3" "$PYTHON" -c '
import json, os
print(json.dumps({"reply_url": os.environ["REPLY_URL"], "client_request_id": os.environ["REQUEST_ID"]}))')
    e2e_request POST "/reviews/$1/confirm_posted" "$body"
}

# e2e_check_followup WORKFLOW_ID REPLY_ID EXPECTED_IDS: print the final card
# and check that the follow-up ran for the confirmed reply: status
# followup_due, reply post id REPLY_ID, and follow-up replies with exactly the
# post ids EXPECTED_IDS (comma-separated; the replies in e2e_x.json).
e2e_check_followup() {
    local card
    card=$(e2e_request GET "/reviews/$1")
    printf '%s' "$card" | REPLY_ID="$2" EXPECTED_IDS="$3" "$PYTHON" -c '
import json, os, sys
card = json.load(sys.stdin)
draft = (card.get("evidence") or {}).get("draft") or {}
replies = card.get("followup_replies") or []
reply_ids = sorted(r.get("id") for r in replies)
expected = sorted(os.environ["EXPECTED_IDS"].split(","))
status = card.get("status")
print("  Status:", status)
print("  Draft:", (draft.get("text") or "")[:80])
print("  Posted URL:", card.get("posted_url"))
print("  Reply post id:", card.get("reply_post_id"))
print("  Follow-up replies:", ", ".join(reply_ids) or "none")
if status != "followup_due":
    sys.exit("status is %r, expected followup_due" % status)
if card.get("reply_post_id") != os.environ["REPLY_ID"]:
    sys.exit("reply post id is %r, expected %r" % (card.get("reply_post_id"), os.environ["REPLY_ID"]))
if reply_ids != expected:
    sys.exit("follow-up replies are %s, expected %s" % (reply_ids, expected))
' || e2e_fail "Follow-up check failed for $1"
}
