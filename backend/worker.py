"""
Temporal worker that runs workflows and activities.

Run with: python -m backend.worker
"""

import asyncio
import logging
from temporalio.client import Client
from temporalio.worker import Worker

from backend.config import get_settings
from backend.workflows import ReviewWorkflow, TopicWatchWorkflow
from backend.activities.x_activities import XActivities
from backend.activities.reply_activities import ReplyActivities
from backend.activities.store_activities import store_card, update_card, get_card, delete_post_text
from backend.activities.claim_gate_activity import claim_gate, verify_edited_text
from backend.activities.written_check_activities import WrittenCheckActivities
from backend.adapters import (
    FakeClaimChecker,
    FakeReplyAgent,
    FakeXClient,
    HttpXClient,
    PlainClaimChecker,
    PlainReplyAgent,
)


def build_activities(x_client, reply_agent, claim_checker, draft_mode=None) -> list:
    """Every activity the Review and TopicWatch workflows call, with its
    dependencies injected (B5: no module globals). A test compares this list
    with the activity names the workflows use: a missing registration makes
    the workflow's call fail at run time only."""
    x_activities = XActivities(x_client)
    reply_activities = ReplyActivities(reply_agent, draft_mode=draft_mode)
    written_check_activities = WrittenCheckActivities(claim_checker)
    return [
        x_activities.search_recent,
        x_activities.lookup_post,
        x_activities.check_followup,
        x_activities.notify,
        reply_activities.research_post,
        reply_activities.plan_reply,
        reply_activities.draft_reply,
        # issue #12: the product-claim check of written drafts and their edits
        written_check_activities.check_written_draft,
        # Standalone activity functions
        store_card,
        update_card,
        get_card,
        delete_post_text,
        claim_gate,
        # The assembled edit re-gate. It was missing here before issue #12, so
        # on a real worker every edit ended as "not_checked".
        verify_edited_text,
    ]


def prepare_database() -> int:
    """issue #49: store a reach score on cards stored without one (before
    reach existed, or by an older worker). Here and not at API start:
    removals run in the worker, so once this code runs, every later removal
    also clears the score. Returns how many cards were scored."""
    from backend.activities.store_activities import backfill_reach

    scored = backfill_reach()
    if scored:
        logging.getLogger(__name__).info(f"Stored a reach score on {scored} cards")
    return scored


async def main():
    """Start the Temporal worker."""
    settings = get_settings()
    
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    
    # Initialize adapters based on config
    logger.info(f"Initializing adapters: x_client={settings.x_client_adapter}, "
                f"reply_agent={settings.reply_agent_adapter}")
    
    # X Client adapter
    if settings.x_client_adapter == "fake":
        x_client = FakeXClient(
            e2e_fixtures_path=settings.e2e_x_fixtures_path if settings.e2e_x_fixtures_path else None
        )
    elif settings.x_client_adapter == "real":
        if not settings.x_bearer_token:
            logger.error("X_BEARER_TOKEN is required when x_client_adapter=real")
            raise ValueError(
                "Real X client selected but X_BEARER_TOKEN is not configured. "
                "Set X_BEARER_TOKEN in environment or use x_client_adapter=fake"
            )
        logger.info("Initializing real X API client with bearer token")
        x_client = HttpXClient(bearer_token=settings.x_bearer_token)
    else:
        logger.error(f"Unknown x_client_adapter: {settings.x_client_adapter}")
        raise ValueError(
            f"Invalid x_client_adapter '{settings.x_client_adapter}'. "
            "Valid options: 'fake', 'real'"
        )
    
    # Reply agent adapter
    if settings.reply_agent_adapter == "fake":
        reply_agent = FakeReplyAgent()
        claim_checker = FakeClaimChecker()
    elif settings.reply_agent_adapter == "plain":
        if not settings.openai_api_key:
            logger.error("OPENAI_API_KEY is required when reply_agent_adapter=plain")
            raise ValueError(
                "Plain reply agent selected but OPENAI_API_KEY is not configured. "
                "Set OPENAI_API_KEY in environment or use reply_agent_adapter=fake"
            )
        logger.info("Initializing plain reply agent with OpenAI API")
        reply_agent = PlainReplyAgent()
        # issue #12: the claim checker is its own adapter and its own call. It
        # uses the same provider and model settings as the drafter.
        claim_checker = PlainClaimChecker()
    else:
        logger.error(f"Unknown reply_agent_adapter: {settings.reply_agent_adapter}")
        raise ValueError(
            f"Invalid reply_agent_adapter '{settings.reply_agent_adapter}'. "
            "Valid options: 'fake', 'plain'"
        )
    
    logger.info(f"Draft mode: {settings.draft_mode}")
    
    logger.info(f"Connecting to Temporal at {settings.temporal_host}")
    
    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
    )
    
    prepare_database()
    logger.info("Starting worker")
    
    worker = Worker(
        client,
        task_queue="x-conversation-finder",
        workflows=[ReviewWorkflow, TopicWatchWorkflow],
        activities=build_activities(x_client, reply_agent, claim_checker, settings.draft_mode),
    )
    
    logger.info("Worker running with injected adapters")
    
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
