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
from backend.activities import (
    search_recent,
    lookup_post,
    research_post,
    draft_reply,
    store_card,
    update_card,
    get_card,
)


async def main():
    """Start the Temporal worker."""
    settings = get_settings()
    
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    
    logger.info(f"Connecting to Temporal at {settings.temporal_host}")
    
    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
    )
    
    logger.info("Starting worker")
    
    worker = Worker(
        client,
        task_queue="x-conversation-finder",
        workflows=[ReviewWorkflow, TopicWatchWorkflow],
        activities=[
            search_recent,
            lookup_post,
            research_post,
            draft_reply,
            store_card,
            update_card,
            get_card,
        ],
    )
    
    logger.info("Worker running")
    
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
