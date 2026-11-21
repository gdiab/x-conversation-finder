#!/bin/bash
# Local E2E test script
# This script demonstrates how to run a full end-to-end test locally

set -e

echo "=== X Conversation Finder E2E Test ==="
echo ""

# Step 1: Start Temporal dev server
echo "Step 1: Starting Temporal dev server..."
echo "Command: temporal server start-dev"
echo ""
echo "NOTE: This needs to be run in a separate terminal"
echo "      Wait for 'Temporal server is running' message"
echo ""

# Step 2: Set up environment with fake adapters
echo "Step 2: Setting up environment with fake adapters..."
cat > /tmp/e2e-test.env <<'EOF'
# Use fake adapters for E2E testing
USE_FAKE_ADAPTERS=true

# X API (not used with fake adapters)
X_BEARER_TOKEN=fake_token_for_testing

# Database (SQLite for local testing)
DATABASE_URL=sqlite:///tmp/x-finder-e2e.db

# Temporal connection
TEMPORAL_ADDRESS=localhost:7233

# Shortened delays for E2E testing
POLL_INTERVAL_HOURS=0.01  # ~36 seconds
FOLLOWUP_DELAY_DAYS=0.0001  # ~8.6 seconds

# Activity timeouts
ACTIVITY_TIMEOUT_SHORT_SEC=10
ACTIVITY_TIMEOUT_LONG_SEC=30
ACTIVITY_MAX_ATTEMPTS=2

# Claim gate
CLAIM_GATE_MAX_ATTEMPTS=2
EOF

echo "Created /tmp/e2e-test.env with shortened delays:"
cat /tmp/e2e-test.env
echo ""

# Step 3: Start the worker
echo "Step 3: Starting the worker..."
echo "Command: cd backend && python3 -m backend.worker"
echo ""
echo "NOTE: This needs to be run in a separate terminal with the env file"
echo "      Wait for 'Worker started' message"
echo ""

# Step 4: Start the API
echo "Step 4: Starting the API..."
echo "Command: cd backend && uvicorn backend.api:app --reload --port 8000"
echo ""
echo "NOTE: This needs to be run in a separate terminal with the env file"
echo "      Wait for 'Application startup complete' message"
echo ""

# Step 5: Drive the flow
echo "Step 5: Driving a card through the full flow..."
echo ""

# Start a watch
echo "5a. Start a topic watch:"
echo '---'
echo 'curl -X POST http://localhost:8000/api/watch/start \\'
echo '  -H "Content-Type: application/json" \\'
echo '  -d '\''{
    "account": "e2e-test",
    "topic": "distributed systems",
    "query": "distributed systems reliability",
    "product_brief": "We build tools for reliable distributed systems. Our product helps teams handle eventual consistency and failure scenarios."
  }'\'
echo '---'
echo ""

echo "Expected output: {\"workflow_id\": \"watch:e2e-test:...\", \"message\": \"...\"}"
echo ""

# Wait for poll
echo "5b. Wait ~40 seconds for first poll (POLL_INTERVAL_HOURS=0.01)"
echo ""

# Check reviews
echo "5c. Check for created reviews:"
echo '---'
echo 'curl http://localhost:8000/api/reviews'
echo '---'
echo ""
echo "Expected output: {\"cards\": [{...}], \"count\": N}"
echo "Note the workflow_id of a pending review"
echo ""

# Approve
echo "5d. Approve a review:"
echo '---'
echo 'WORKFLOW_ID="<workflow_id_from_above>"'
echo 'curl -X POST http://localhost:8000/api/reviews/$WORKFLOW_ID/approve \\'
echo '  -H "Content-Type: application/json" \\'
echo '  -d '\''{
    "posted_url": "https://x.com/founder/status/12345",
    "client_request_id": "e2e-approve-1"
  }'\'
echo '---'
echo ""

echo "Expected output: 200 OK"
echo ""

# Confirm posted
echo "5e. Confirm the posted reply:"
echo '---'
echo 'curl -X POST http://localhost:8000/api/reviews/$WORKFLOW_ID/confirm_posted \\'
echo '  -H "Content-Type: application/json" \\'
echo '  -d '\''{
    "reply_url": "https://x.com/founder/status/99999",
    "client_request_id": "e2e-confirm-1"
  }'\'
echo '---'
echo ""

echo "Expected output: {\"message\": \"Confirmed...\"}"
echo ""

# Wait for follow-up
echo "5f. Wait ~10 seconds for follow-up timer (FOLLOWUP_DELAY_DAYS=0.0001)"
echo ""

# Check follow-up
echo "5g. Check the card for follow-up status:"
echo '---'
echo 'curl http://localhost:8000/api/reviews/$WORKFLOW_ID'
echo '---'
echo ""

echo "Expected status: 'followup_due' or 'complete'"
echo ""

echo "=== E2E Test Flow Complete ==="
echo ""
echo "Summary:"
echo "1. Started Temporal dev server"
echo "2. Started worker with fake adapters"
echo "3. Started API with fake adapters"
echo "4. Created a TopicWatch workflow"
echo "5. Waited for poll to create Review workflows"
echo "6. Approved a review"
echo "7. Confirmed the posted reply"
echo "8. Waited for follow-up timer to fire"
echo "9. Verified final status"
echo ""
echo "All steps use fake adapters so no real X API calls are made."
echo "Delays are shortened to complete the flow in under 2 minutes."
