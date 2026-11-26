#!/bin/bash
# E2E test - Review workflow started directly (no TopicWatch)
# Stored post → Review card → approve → confirm → follow-up timer → check_followup
#
# The post is stored first and the Review is given only its id, as a
# TopicWatch starts it since issue #23 (no post text in the Review's history).
#
# Starts its own stack with fake adapters (scripts/e2e-lib.sh); settings there.

set -euo pipefail

echo "=== X Conversation Finder E2E Test (Direct Review) ==="
date
echo ""

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/e2e-lib.sh"

e2e_configure e2e-review-direct 8011
export E2E_X_FIXTURES_PATH="$WORKSPACE_ROOT/backend/tests/fixtures/e2e_x.json"

e2e_start_stack

echo "=== Starting Review Workflow ==="
echo ""

echo "[1] Storing the post and starting the Review with its id..."
WORKFLOW_ID=$("$PYTHON" << 'PYEOF'
import asyncio
import os
import sys
from datetime import datetime, timezone

from temporalio.client import Client

from backend.config import get_settings
from backend.models import Author, Post
from backend.review_docs import store_posts
from backend.workflows.review import ReviewConfig, ReviewInput, ReviewWorkflow


async def main():
    post = Post(
        # The fake X client maps the founder reply 99999 to this conversation
        id="e2e_9999",
        # A fresh, on-topic question, so it passes the should-we-reply rules (issue #4)
        text="How do you keep distributed systems reliable when the network partitions?",
        author=Author(id="111", username="test_user", name="Test User", followers_count=500, verified=False),
        created_at=datetime.now(timezone.utc).isoformat(),
        like_count=42,
        reply_count=5,
        repost_count=10,
        quote_count=2,
        conversation_id="e2e_9999",
    )
    store_posts([post])

    settings = get_settings()
    workflow_input = ReviewInput(
        post=None,
        post_id=post.id,
        account="e2e-test",
        topic="reliability",
        # Its first paragraph is the fake reply agent's founder passage (issue #4)
        product_brief=(
            "Our product helps developers build reliable distributed systems. "
            "Our product provides automatic failover. Our product guarantees strong consistency."
        ),
        confidence_threshold=settings.confidence_threshold,
        config=ReviewConfig.from_settings(settings),
    )

    client = await Client.connect(os.environ["TEMPORAL_HOST"])
    handle = await client.start_workflow(
        ReviewWorkflow.run,
        workflow_input,
        id=f"review:e2e-test:{post.id}",
        task_queue="x-conversation-finder",
    )
    print(f"✓ Workflow started: {handle.id}", file=sys.stderr)
    print(handle.id)  # only the workflow id goes to stdout


asyncio.run(main())
PYEOF
)
echo ""

echo "[2] Waiting for the card..."
e2e_wait_for_status "$WORKFLOW_ID" pending 30
e2e_request GET "/reviews/$WORKFLOW_ID" | "$PYTHON" -c '
import json, sys
card = json.load(sys.stdin)
print("✓ Card created")
print("  Status:", card["status"])
print("  Post:", card["post"]["text"][:60])
print("  Draft:", card["evidence"]["draft"]["text"][:80])
'
echo ""

echo "[3] Approving the draft on the card (never edited, so gate state null)..."
e2e_approve "$WORKFLOW_ID" "https://x.com/founder/status/12345" e2e-approve | "$PYTHON" -m json.tool
e2e_wait_for_status "$WORKFLOW_ID" approved 15
echo "✓ Review approved"
echo ""

echo "[4] Confirming posted reply..."
e2e_confirm "$WORKFLOW_ID" "https://x.com/founder/status/99999" e2e-confirm | "$PYTHON" -m json.tool
echo "✓ Reply confirmed, follow-up timer starting"
echo ""

echo "[5] Waiting for the follow-up timer (${FOLLOWUP_DELAY_SECONDS}s) and check_followup..."
e2e_wait_for_status "$WORKFLOW_ID" followup_due $((FOLLOWUP_DELAY_SECONDS + 20))
echo ""

echo "[6] Checking final card..."
e2e_check_followup "$WORKFLOW_ID" 99999 9999003
echo ""

echo "=== E2E Test Complete ==="
echo ""
echo "✓ Post stored; Review workflow started with its id"
echo "✓ Card created with a draft"
echo "✓ Draft approved with the text and gate state on the card"
echo "✓ Posted reply confirmed"
echo "✓ Follow-up timer fired and check_followup stored the replies"
echo ""
echo "All operations used fake adapters (no X or OpenAI calls)"
echo ""
date
