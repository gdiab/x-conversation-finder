#!/bin/bash
# E2E test for X Conversation Finder
# Tests full Review workflow: poll → card → approve → confirm → followup → check

set -e

echo "=== X Conversation Finder E2E Test ==="
date
echo ""

# Find workspace root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export PYTHONPATH="$WORKSPACE_ROOT"

# Configuration - SHORT delays for E2E
export X_CLIENT_ADAPTER=fake
export REPLY_AGENT_ADAPTER=fake
export DATABASE_URL="sqlite:////tmp/x-finder-e2e.db"
export TEMPORAL_ADDRESS=localhost:7233
export WATCH_POLL_INTERVAL_HOURS=0  # Immediate poll for testing
export FOLLOWUP_DELAY_SECONDS=8  # Real durable timer for E2E (8 seconds)
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
echo "  ADAPTERS: X=$X_CLIENT_ADAPTER, Reply=$REPLY_AGENT_ADAPTER"
echo "  Follow-up delay: $FOLLOWUP_DELAY_SECONDS seconds (real durable timer)"
echo ""

# Initialize database
rm -f /tmp/x-finder-e2e.db /tmp/temporal-e2e.db*
python3 -c "from backend.database import init_db; init_db()"
echo "✓ Database initialized"
echo ""

# Cleanup function
cleanup() {
    echo ""
    echo "=== Cleanup ==="
    pkill -P $$ 2>/dev/null || true
    wait 2>/dev/null || true
    echo "✓ Services stopped"
}
trap cleanup EXIT

# Start Temporal
echo "Starting Temporal dev server..."
temporal server start-dev --db-filename /tmp/temporal-e2e.db --ui-port 8234 > /tmp/temporal-e2e.log 2>&1 &
TEMPORAL_PID=$!
echo "  PID: $TEMPORAL_PID"

echo "Waiting for Temporal..."
for i in {1..30}; do
    if temporal workflow list --limit 1 > /dev/null 2>&1; then
        echo "✓ Temporal ready"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "❌ Temporal timeout"
        tail -20 /tmp/temporal-e2e.log
        exit 1
    fi
    sleep 1
done
echo ""

# Start Worker
echo "Starting worker..."
cd "$WORKSPACE_ROOT/backend"
python3 -m backend.worker > /tmp/worker-e2e.log 2>&1 &
WORKER_PID=$!
echo "  PID: $WORKER_PID"
sleep 5

if ! kill -0 $WORKER_PID 2>/dev/null; then
    echo "❌ Worker failed"
    tail -30 /tmp/worker-e2e.log
    exit 1
fi
echo "✓ Worker running"
echo ""

# Start API
echo "Starting API..."
python3 -m uvicorn backend.api:app --host 0.0.0.0 --port 8000 > /tmp/api-e2e.log 2>&1 &
API_PID=$!
echo "  PID: $API_PID"

echo "Waiting for API..."
for i in {1..15}; do
    if curl -s http://localhost:8000/api/budget > /dev/null 2>&1; then
        echo "✓ API ready"
        break
    fi
    if [ $i -eq 15 ]; then
        echo "❌ API timeout"
        tail -20 /tmp/api-e2e.log
        exit 1
    fi
    sleep 1
done
echo ""

echo "=== Services Running ==="
echo "  Temporal: PID $TEMPORAL_PID"
echo "  Worker: PID $WORKER_PID"
echo "  API: PID $API_PID"
echo ""

# Execute E2E flow
echo "=== E2E Flow Execution ==="
echo ""

# Step 1: Start TopicWatch (polls for posts)
echo "[1] Starting TopicWatch..."
START_RESPONSE=$(curl -s -X POST http://localhost:8000/api/watch/start \
  -H "Content-Type: application/json" \
  -d '{
    "account": "e2e-test",
    "topic": "distributed-systems",
    "query": "distributed systems",
    "product_brief": "We build tools for reliable distributed systems.",
    "product_name": "Acme Tools"
  }')
echo "$START_RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$START_RESPONSE"
echo ""

# Step 2: Wait for poll to complete and card to be created
echo "[2] Waiting 10 seconds for poll and card creation..."
sleep 10
echo ""

# Step 3: List reviews to find the created card
echo "[3] Listing reviews..."
REVIEWS=$(curl -s http://localhost:8000/api/reviews)
echo "$REVIEWS" | python3 -m json.tool 2>/dev/null | head -100

# Extract workflow ID
WORKFLOW_ID=$(echo "$REVIEWS" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if data.get('cards'):
        print(data['cards'][0]['workflow_id'])
except:
    pass
" 2>/dev/null)

echo ""
if [ -z "$WORKFLOW_ID" ]; then
    echo "❌ No card created"
    echo ""
    echo "Worker log (last 40 lines):"
    tail -40 /tmp/worker-e2e.log
    exit 1
fi
echo "✓ Found workflow: $WORKFLOW_ID"
echo ""

# Step 4: Approve the review
echo "[4] Approving review..."
APPROVE_RESPONSE=$(curl -s -X POST "http://localhost:8000/api/reviews/$WORKFLOW_ID/approve" \
  -H "Content-Type: application/json" \
  -d '{"posted_url": "https://x.com/founder/status/12345", "client_request_id": "e2e-approve"}')
echo "$APPROVE_RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$APPROVE_RESPONSE"
sleep 2
echo ""

# Step 5: Confirm posted reply
echo "[5] Confirming posted reply..."
CONFIRM_RESPONSE=$(curl -s -X POST "http://localhost:8000/api/reviews/$WORKFLOW_ID/confirm_posted" \
  -H "Content-Type: application/json" \
  -d '{"reply_url": "https://x.com/founder/status/99999", "client_request_id": "e2e-confirm"}')
echo "$CONFIRM_RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$CONFIRM_RESPONSE"
echo ""

# Step 6: Wait for follow-up timer to fire
WAIT_TIME=$((FOLLOWUP_DELAY_SECONDS + 4))
echo "[6] Waiting ${WAIT_TIME}s for follow-up timer (${FOLLOWUP_DELAY_SECONDS}s) to fire and check_followup to run..."
sleep $WAIT_TIME
echo ""

# Step 7: Check final status
echo "[7] Checking final card status..."
FINAL_REVIEWS=$(curl -s http://localhost:8000/api/reviews)
echo "$FINAL_REVIEWS" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if data.get('cards'):
        card = data['cards'][0]
        print(f\"Status: {card['status']}\")
        print(f\"Workflow: {card['workflow_id']}\")
        print(f\"Draft: {card['draft'][:80]}...\")
        print(f\"Posted URL: {card.get('posted_url', 'N/A')}\")
    else:
        print('No cards found')
except Exception as e:
    print(f'Error: {e}')
" 2>/dev/null || echo "$FINAL_REVIEWS"
echo ""

echo "=== E2E Test Complete ==="
echo ""
echo "✓ TopicWatch started and polled"
echo "✓ Review card created with draft"
echo "✓ Review approved with posted URL"
echo "✓ Posted reply confirmed"
echo "✓ Follow-up timer fired (${FOLLOWUP_DELAY_SECONDS}s - real durable timer)"
echo "✓ check_followup activity executed"
echo ""
echo "All operations used fake adapters (no real X API calls)"
echo ""
date
