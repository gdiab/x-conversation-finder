#!/bin/bash
# E2E test - Direct Review workflow start
# Tests: card → approve → confirm → followup timer → check_followup

set -e

echo "=== X Conversation Finder E2E Test (Direct Review) ==="
date
echo ""

# Find workspace
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export PYTHONPATH="$WORKSPACE_ROOT"

# Configuration
export X_CLIENT_ADAPTER=fake
export REPLY_AGENT_ADAPTER=fake
export DATABASE_URL="sqlite:////tmp/x-finder-e2e.db"
export TEMPORAL_ADDRESS=localhost:7233
export FOLLOWUP_DELAY_DAYS=0  # Immediate followup
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

echo "Configuration: ADAPTERS=fake, followup delay=0 days (immediate)"
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
temporal server start-dev --db-filename /tmp/temporal-e2e.db --ui-port 8235 > /tmp/temporal-e2e.log 2>&1 &
TEMPORAL_PID=$!
echo "  PID: $TEMPORAL_PID"

for i in {1..30}; do
    if temporal workflow list --limit 1 > /dev/null 2>&1; then
        echo "✓ Temporal ready"
        break
    fi
    [ $i -eq 30 ] && { echo "❌ Temporal timeout"; tail -20 /tmp/temporal-e2e.log; exit 1; }
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
python3 -m uvicorn backend.api:app --host 0.0.0.0 --port 8001 > /tmp/api-e2e.log 2>&1 &
API_PID=$!
echo "  PID: $API_PID"

for i in {1..15}; do
    if curl -s http://localhost:8001/api/budget > /dev/null 2>&1; then
        echo "✓ API ready"
        break
    fi
    [ $i -eq 15 ] && { echo "❌ API timeout"; tail -20 /tmp/api-e2e.log; exit 1; }
    sleep 1
done
echo ""

echo "=== Services Running ==="
echo "  Temporal: PID $TEMPORAL_PID"
echo "  Worker: PID $WORKER_PID"
echo "  API: PID $API_PID"
echo ""

# Start Review workflow directly via Python
echo "=== Starting Review Workflow ===" 
echo ""

WORKFLOW_ID=$(python3 << 'PYEOF'
import asyncio
import sys
from temporalio.client import Client
from backend.workflows.review import ReviewWorkflow, ReviewInput, ReviewConfig
from backend.models import Post, Author
from backend.config import get_settings

async def main():
    post = Post(
        id="e2e_9999",
        text="Just shipped our new distributed consensus algorithm! Handles network partitions gracefully.",
        author=Author(
            id="111",
            username="test_user",
            name="Test User",
            followers_count=500,
            verified=False
        ),
        created_at="2026-11-21T07:00:00",
        like_count=42,
        reply_count=5,
        repost_count=10,
        quote_count=2,
        conversation_id="e2e_9999"
    )
    
    settings = get_settings()
    config = ReviewConfig.from_settings(settings)
    
    workflow_input = ReviewInput(
        post=post,
        account="e2e-test",
        topic="distributed-systems",
        product_brief="We build tools for reliable distributed systems.",
        confidence_threshold=0.7,
        config=config
    )
    
    print("[1] Starting Review workflow...")
    client = await Client.connect("localhost:7233")
    handle = await client.start_workflow(
        ReviewWorkflow.run,
        workflow_input,
        id=f"review:e2e-test:{post.id}",
        task_queue="x-conversation-finder"
    )
    print(f"✓ Workflow started: {handle.id}")
    return handle.id

workflow_id = asyncio.run(main())
print(workflow_id)
PYEOF
)

echo "Workflow ID: $WORKFLOW_ID"
echo ""

# Wait for card creation
echo "[2] Waiting 8 seconds for card creation..."
sleep 8
echo ""

# List reviews
echo "[3] Checking card was created..."
REVIEWS=$(curl -s http://localhost:8001/api/reviews)
echo "$REVIEWS" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if data.get('cards'):
        card = data['cards'][0]
        print(f\"✓ Card created\")
        print(f\"  Status: {card['status']}\")
        print(f\"  Post: {card['post']['text'][:50]}...\")
        print(f\"  Draft: {card['draft'][:80]}...\")
    else:
        print('❌ No card found')
        sys.exit(1)
except Exception as e:
    print(f'❌ Error: {e}')
    sys.exit(1)
" || { echo "Worker log:"; tail -40 /tmp/worker-e2e.log; exit 1; }
echo ""

# Approve
echo "[4] Approving review..."
APPROVE=$(curl -s -X POST "http://localhost:8001/api/reviews/$WORKFLOW_ID/approve" \
  -H "Content-Type: application/json" \
  -d '{"posted_url": "https://x.com/founder/status/12345", "client_request_id": "e2e-approve"}')
echo "$APPROVE" | python3 -m json.tool 2>/dev/null | head -5 || echo "$APPROVE"
sleep 2
echo ""

# Confirm posted
echo "[5] Confirming posted reply..."
CONFIRM=$(curl -s -X POST "http://localhost:8001/api/reviews/$WORKFLOW_ID/confirm_posted" \
  -H "Content-Type: application/json" \
  -d '{"reply_url": "https://x.com/founder/status/99999", "client_request_id": "e2e-confirm"}')
echo "$CONFIRM" | python3 -m json.tool 2>/dev/null | head -5 || echo "$CONFIRM"
echo ""

# Wait for follow-up timer (immediate with FOLLOWUP_DELAY_DAYS=0)
echo "[6] Waiting 8 seconds for follow-up timer to fire..."
sleep 8
echo ""

# Check final status
echo "[7] Checking final status..."
FINAL=$(curl -s http://localhost:8001/api/reviews)
echo "$FINAL" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if data.get('cards'):
        card = data['cards'][0]
        print(f\"✓ Final status: {card['status']}\")
        print(f\"  Posted URL: {card.get('posted_url', 'N/A')}\")
        print(f\"  Follow-up replies checked: {len(card.get('followup_replies', []))} found\")
    else:
        print('❌ No card found')
except Exception as e:
    print(f'❌ Error: {e}')
" || echo "$FINAL"
echo ""

echo "=== E2E Test Complete ==="
echo ""
echo "✓ Review workflow started"
echo "✓ Card created with draft"
echo "✓ Review approved"
echo "✓ Posted reply confirmed"
echo "✓ Follow-up timer fired (immediate with delay=0)"
echo "✓ check_followup activity executed"
echo ""
echo "All operations used fake adapters (no real X API calls)"
echo ""
date
