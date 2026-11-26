#!/bin/bash
# Full E2E test for X Conversation Finder
# TopicWatch → Review card → edit the draft (re-gated) → stale approve refused
# (409 draft_changed) → approve the edited draft → confirm → follow-up timer →
# check_followup
#
# Starts its own stack with fake adapters (scripts/e2e-lib.sh); settings there.

set -euo pipefail

echo "=== X Conversation Finder Full E2E Test ==="
date
echo ""

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/e2e-lib.sh"

e2e_configure e2e-full 8012
export E2E_X_FIXTURES_PATH="$WORKSPACE_ROOT/backend/tests/fixtures/e2e_x.json"
export WATCH_POLL_INTERVAL_HOURS=0  # TEST-ONLY: immediate poll (allowed only with the fake X adapter)

e2e_start_stack

echo "=== E2E Flow Execution ==="
echo ""

ACCOUNT=e2e-full-test
# As in e2e-test.sh: post 1001, founder reply 9999 (conversation 1001), and a
# brief that answers the post's question, so the should-we-reply rules keep it
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
CARD=$(e2e_request GET "/reviews/$WORKFLOW_ID")
ORIGINAL_DRAFT=$(printf '%s' "$CARD" | e2e_json "d['evidence']['draft']['text']")
ORIGINAL_GATE_STATE=$(printf '%s' "$CARD" | e2e_json "d.get('edited_text_gate_state')")
echo "✓ Card is pending"
echo "  Draft: $ORIGINAL_DRAFT"
echo "  Edited-text gate state: $ORIGINAL_GATE_STATE"
[ "$ORIGINAL_GATE_STATE" = "None" ] || e2e_fail "A never-edited draft has gate state '$ORIGINAL_GATE_STATE', expected null"
echo ""

echo "[3] Editing the draft: dropping its closing sentence (the edited text is checked again)..."
EDITED_DRAFT=$(TEXT="$ORIGINAL_DRAFT" "$PYTHON" -c '
import os, re
sentences = re.split(r"(?<=[.!?])\s+", os.environ["TEXT"].strip())
if len(sentences) < 2:
    raise SystemExit("draft has one sentence; nothing to drop")
print(" ".join(sentences[:-1]))')
EDIT_BODY=$(TEXT="$EDITED_DRAFT" "$PYTHON" -c '
import json, os
print(json.dumps({"new_text": os.environ["TEXT"], "client_request_id": "e2e-full-edit"}))')
e2e_request POST "/reviews/$WORKFLOW_ID/edit" "$EDIT_BODY" | "$PYTHON" -m json.tool
CARD=$(e2e_request GET "/reviews/$WORKFLOW_ID")
SHOWN_DRAFT=$(printf '%s' "$CARD" | e2e_json "d['evidence']['draft']['text']")
EDITED_GATE_STATE=$(printf '%s' "$CARD" | e2e_json "d.get('edited_text_gate_state')")
[ "$SHOWN_DRAFT" = "$EDITED_DRAFT" ] || e2e_fail "Card draft is '$SHOWN_DRAFT' after the edit, expected '$EDITED_DRAFT'"
[ "$EDITED_GATE_STATE" = "passed" ] || e2e_fail "Edited-text gate state is '$EDITED_GATE_STATE', expected 'passed'"
echo "✓ Card shows the edited draft, gate state: $EDITED_GATE_STATE"
echo ""

echo "[4] Approving the draft shown before the edit (must be refused)..."
STALE_BODY=$(TEXT="$ORIGINAL_DRAFT" "$PYTHON" -c '
import json, os
print(json.dumps({
    "posted_url": "https://x.com/founder/status/12345",
    "client_request_id": "e2e-full-approve-stale",
    "draft_text": os.environ["TEXT"],
    "draft_gate_state": None,
}))')
e2e_expect_error POST "/reviews/$WORKFLOW_ID/approve" "$STALE_BODY" 409 draft_changed | "$PYTHON" -m json.tool
e2e_wait_for_status "$WORKFLOW_ID" pending 5
echo "✓ Stale approve refused with draft_changed; card still pending"
echo ""

echo "[5] Approving the edited draft on the card (its text and gate state)..."
e2e_approve "$WORKFLOW_ID" "https://x.com/founder/status/12345" e2e-full-approve | "$PYTHON" -m json.tool
e2e_wait_for_status "$WORKFLOW_ID" approved 15
echo "✓ Review approved"
echo ""

echo "[6] Confirming posted reply..."
e2e_confirm "$WORKFLOW_ID" "https://x.com/founder/status/9999" e2e-full-confirm | "$PYTHON" -m json.tool
echo "✓ Reply confirmed"
echo ""

echo "[7] Waiting for the follow-up timer (${FOLLOWUP_DELAY_SECONDS}s) and check_followup..."
e2e_wait_for_status "$WORKFLOW_ID" followup_due $((FOLLOWUP_DELAY_SECONDS + 20))
echo ""

echo "[8] Checking final card..."
e2e_check_followup "$WORKFLOW_ID" 9999 9999001,9999002
FINAL_DRAFT=$(e2e_request GET "/reviews/$WORKFLOW_ID" | e2e_json "d['evidence']['draft']['text']")
[ "$FINAL_DRAFT" = "$EDITED_DRAFT" ] || e2e_fail "Final draft is '$FINAL_DRAFT', expected the edited draft"
echo "✓ The approved draft is the edited one"
echo ""

echo "=== E2E Test Complete ==="
echo ""
echo "All assertions passed:"
echo "  - TopicWatch started and polled; Review card created with status 'pending'"
echo "  - Edited draft re-checked (gate state 'passed')"
echo "  - Approve of the draft shown before the edit refused (409 draft_changed)"
echo "  - Edited draft approved with the text and gate state on the card"
echo "  - Posted reply confirmed"
echo "  - Follow-up timer fired (${FOLLOWUP_DELAY_SECONDS}s, real durable timer); check_followup stored the replies"
echo ""
echo "All operations used fake adapters (no X or OpenAI calls)"
echo ""
date
