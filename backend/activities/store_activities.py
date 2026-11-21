"""Storage activities for review cards."""

from dataclasses import dataclass
from typing import Optional, Dict, Any
from temporalio import activity

from backend.models import ReviewCard, ReviewStatus


_in_memory_store: Dict[str, ReviewCard] = {}


@dataclass
class StoreCardInput:
    """Input for store_card activity."""
    
    card: ReviewCard
    idempotency_key: str


@dataclass
class UpdateCardInput:
    """Input for update_card activity."""
    
    workflow_id: str
    updates: Dict[str, Any]
    idempotency_key: str


@dataclass
class GetCardInput:
    """Input for get_card activity."""
    
    workflow_id: str


@activity.defn(name="store_card")
async def store_card(input: StoreCardInput) -> bool:
    """
    Store a review card.
    
    Idempotent: upsert by workflow_id.
    """
    activity.logger.info(
        f"Storing card {input.card.workflow_id} (key: {input.idempotency_key})"
    )
    
    _in_memory_store[input.card.workflow_id] = input.card
    
    activity.logger.info(f"Stored card {input.card.workflow_id}")
    
    return True


@activity.defn(name="update_card")
async def update_card(input: UpdateCardInput) -> bool:
    """
    Update a review card.
    
    Idempotent: updates are applied by field.
    """
    activity.logger.info(
        f"Updating card {input.workflow_id} (key: {input.idempotency_key})"
    )
    
    if input.workflow_id not in _in_memory_store:
        activity.logger.error(f"Card {input.workflow_id} not found")
        return False
    
    card = _in_memory_store[input.workflow_id]
    
    for key, value in input.updates.items():
        if key == "status":
            card.status = ReviewStatus(value)
        elif key == "approved_by":
            card.approved_by = value
        elif key == "posted_url":
            card.posted_url = value
        elif key == "skip_reason":
            card.skip_reason = value
        elif key == "evidence":
            card.evidence = value
    
    activity.logger.info(f"Updated card {input.workflow_id}")
    
    return True


@activity.defn(name="get_card")
async def get_card(input: GetCardInput) -> Optional[ReviewCard]:
    """Get a review card by workflow ID."""
    activity.logger.info(f"Getting card {input.workflow_id}")
    
    card = _in_memory_store.get(input.workflow_id)
    
    if card:
        activity.logger.info(f"Found card {input.workflow_id}")
    else:
        activity.logger.info(f"Card {input.workflow_id} not found")
    
    return card


def get_all_cards() -> list[ReviewCard]:
    """Get all cards (for API access)."""
    return list(_in_memory_store.values())
