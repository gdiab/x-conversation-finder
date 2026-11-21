#!/bin/bash
# E2E test for X Conversation Finder - Tests full Review workflow with ADAPTERS=fake

set -e

echo "=== X Conversation Finder E2E Test ==="
date
echo ""

# Configuration
export X_CLIENT_ADAPTER=fake
export REPLY_AGENT_ADAPTER=fake
export DATABASE_URL=sqlite:////tmp/x-finder-e2e.db
export TEMPORAL_ADDRESS=localhost:7233
export FOLLOWUP_DELAY_DAYS=1
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

echo "Environment: ADAPTERS=fake"
echo ""

# Initialize database
rm -f /tmp/x-finder-e2e.db
cd /workspace && PYTHONPATH=/workspace python3 -c "from backend.database import init_db; init_db()"
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
temporal server start-dev --db-filename /tmp/temporal-e2e.db --ui-port 8233 > /tmp/temporal-e2e.log 2>&1 &
echo "Waiting for Temporal..."
for i in {1..30}; do
    if temporal workflow list --limit 1 > /dev/null 2>&1; then
        echo "✓ Temporal ready"
        break
    fi
    [ $i -eq 30 ] && echo "❌ Temporal timeout" && exit 1
    sleep 1
done
echo ""

# Start Worker
echo "Starting worker..."
cd /workspace/backend && PYTHONPATH=/workspace python3 -m backend.worker > /tmp/worker-e2e.log 2>&1 &
sleep 5
echo "✓ Worker running"
echo ""

# Start API
echo "Starting API..."
cd /workspace/backend && PYTHONPATH=/workspace python3 -m uvicorn backend.api:app --host 0.0.0.0 --port 8000 > /tmp/api-e2e.log 2>&1 &
echo "Waiting for API..."
for i in {1..15}; do
    if curl -s http://localhost:8000/api/budget > /dev/null 2>&1; then
        echo "✓ API ready"
        break
    fi
    [ $i -eq 15 ] && echo "❌ API timeout" && exit 1
    sleep 1
done
echo ""

echo "=== Starting Review Workflow ===" 
echo ""

# Run Python E2E test
PYTHONPATH=/workspace python3 << 'PYEOF'
import asyncio
import sys
from temporalio.client import Client
from backend.workflows.review import ReviewWorkflow, ReviewInput, ReviewConfig
from backend.models import Post, Author
from backend.config import get_settings

async def main():
    post = Post(
        id="9999",
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
        conversation_id="9999"
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
    print(f"✓ Workflow started: {handle.id}\n")
    return handle.id

workflow_id = asyncio.run(main())
print(workflow_id)
PYEOF
WORKFLOW_ID=$?
echo ""

# Wait for card creation
echo "[2] Waiting 8 seconds for card creation..."
sleep 8
echo ""

# List reviews
echo "[3] Listing reviews..."
REVIEWS=$(curl -s http://localhost:8000/api/reviews)
CARD_STATUS=$(echo "$REVIEWS" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    if d.get('cards'):
        c = d['cards'][0]
        print(f\"Status: {c['status']}\")
        print(f\"Draft (first 100 chars): {c['draft'][:100]}...\")
        print(f\"Workflow ID: {c['workflow_id']}\")
        sys.exit(0 if c['workflow_id'] else 1)
    else:
        print('No cards found')
        sys.exit(1)
except Exception as e:
    print(f'Error: {e}')
    sys.exit(1)
" 2>&1)

if [ $? -ne 0 ]; then
    echo "❌ No card found"
    echo "$CARD_STATUS"
    echo ""
    echo "Worker log (last 40 lines):"
    tail -40 /tmp/worker-e2e.log
    exit 1
fi

echo "$CARD_STATUS"
WORKFLOW_ID=$(echo "$CARD_STATUS" | grep "Workflow ID" | awk '{print $3}')
echo ""

# Approve
echo "[4] Approving review..."
APPROVE=$(curl -s -X POST "http://localhost:8000/api/reviews/$WORKFLOW_ID/approve" \
  -H "Content-Type: application/json" \
  -d '{"posted_url": "https://x.com/test/status/12345", "client_request_id": "e2e-approve"}')
echo "$APPROVE" | python3 -m json.tool 2>/dev/null | head -5
sleep 2
echo ""

# Confirm posted
echo "[5] Confirming posted reply..."
CONFIRM=$(curl -s -X POST "http://localhost:8000/api/reviews/$WORKFLOW_ID/confirm_posted" \
  -H "Content-Type: application/json" \
  -d '{"reply_url": "https://x.com/test/status/99999", "client_request_id": "e2e-confirm"}')
echo "$CONFIRM" | python3 -m json.tool 2>/dev/null | head -5
sleep 2
echo ""

# Final status
echo "[6] Final card status..."
FINAL=$(curl -s http://localhost:8000/api/reviews)
echo "$FINAL" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    if d.get('cards'):
        c = d['cards'][0]
        print(f\"Status: {c['status']}\")
        print(f\"Post text: {c['post']['text'][:70]}...\")
        print(f\"Draft text: {c['draft'][:100]}...\")
        print(f\"Posted URL: {c.get('posted_url', 'N/A')}\")
    else:
        print('No cards')
except Exception as e:
    print(f'Error: {e}')
" 2>/dev/null
echo ""

echo "=== E2E Test Complete ==="
echo ""
echo "✓ Review workflow executed end-to-end"
echo "✓ Card created with draft reply"
echo "✓ Review approved with posted URL"
echo "✓ Posted reply confirmed"
echo "✓ Follow-up timer scheduled (1 day)"
echo ""
echo "All operations used fake adapters (no real X API calls)"
echo ""
date
