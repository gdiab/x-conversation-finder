#!/bin/bash
# E2E test for X Conversation Finder
# TopicWatch → Review card → approve the unedited draft → confirm → follow-up timer → check_followup
#
# Starts its own stack with fake adapters (scripts/e2e-lib.sh); settings there.

set -euo pipefail

echo "=== X Conversation Finder E2E Test ==="
date
echo ""

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/e2e-lib.sh"

e2e_configure e2e-test 8010
export E2E_X_FIXTURES_PATH="$WORKSPACE_ROOT/backend/tests/fixtures/e2e_x.json"
export WATCH_POLL_INTERVAL_HOURS=0  # TEST-ONLY: immediate poll (allowed only with the fake X adapter)

e2e_start_stack

echo "=== E2E Flow Execution ==="
echo ""

ACCOUNT=e2e-test
# The fake X client's post 1001 matches the query; its founder reply 9999 is
# mapped to conversation 1001, and the fixtures hold two replies to 9999.
# The brief answers the post's question (eventual consistency), so the
# should-we-reply rules (issue #4) keep it
WORKFLOW_ID="review:$ACCOUNT:1001"

echo "[1] Starting TopicWatch..."
e2e_request POST /watch/start '{
    "account": "'"$ACCOUNT"'",
    "topic": "distributed-systems",
    "query": "distributed systems",
    "product_brief": "Acme Tools helps teams handle eventual consistency in distributed systems.",
    "product_name": "Acme Tools"
  }' | "$PYTHON" -m json.tool
echo ""

echo "[2] Waiting for the poll to create the card for $WORKFLOW_ID..."
e2e_wait_for_status "$WORKFLOW_ID" pending 30
echo "✓ Card is pending"
echo ""

echo "[3] Listing reviews..."
e2e_request GET /reviews | e2e_json "'\n'.join(f\"  {c['workflow_id']}: {c['status']}\" for c in d['cards'])"
echo ""

echo "[4] Approving the draft on the card (never edited, so gate state null)..."
e2e_approve "$WORKFLOW_ID" "https://x.com/founder/status/12345" e2e-approve | "$PYTHON" -m json.tool
e2e_wait_for_status "$WORKFLOW_ID" approved 15
echo "✓ Review approved"
echo ""

echo "[5] Confirming posted reply..."
e2e_confirm "$WORKFLOW_ID" "https://x.com/founder/status/9999" e2e-confirm | "$PYTHON" -m json.tool
echo "✓ Reply confirmed"
echo ""

echo "[6] Waiting for the follow-up timer (${FOLLOWUP_DELAY_SECONDS}s) and check_followup..."
e2e_wait_for_status "$WORKFLOW_ID" followup_due $((FOLLOWUP_DELAY_SECONDS + 20))
echo ""

echo "[7] Checking final card..."
e2e_check_followup "$WORKFLOW_ID"
echo ""

echo "=== E2E Test Complete ==="
echo ""
echo "✓ TopicWatch started and polled"
echo "✓ Review card created with a draft"
echo "✓ Draft approved with the text and gate state on the card"
echo "✓ Posted reply confirmed"
echo "✓ Follow-up timer fired and check_followup stored the replies"
echo ""
echo "All operations used fake adapters (no X or OpenAI calls)"
echo ""
date
