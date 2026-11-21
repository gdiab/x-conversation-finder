"""Storage activities for review cards using SQLite."""

from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Union
from temporalio import activity
import json

from backend.models import ReviewCard, ReviewStatus, Evidence
from backend.database import get_db, ReviewCardDB


# Type alias for update patches
CardUpdate = Dict[str, Union[str, Evidence, List[Dict[str, Any]]]]


@dataclass
class StoreCardInput:
    """Input for store_card activity."""
    
    card: ReviewCard
    idempotency_key: str


@dataclass
class UpdateCardInput:
    """
    Input for update_card activity.
    
    Valid update keys:
    - status: str (ReviewStatus value)
    - approved_by: str
    - posted_url: str
    - reply_post_id: str
    - skip_reason: str
    - followup_replies: List[Dict[str, Any]]
    - evidence: Evidence (full object with draft)
    """
    
    workflow_id: str
    updates: CardUpdate
    idempotency_key: str


@dataclass
class GetCardInput:
    """Input for get_card activity."""
    
    workflow_id: str


def _card_to_db(card: ReviewCard) -> ReviewCardDB:
    """Convert ReviewCard to database model."""
    return ReviewCardDB(
        workflow_id=card.workflow_id,
        post_data=json.dumps({
            "id": card.post.id,
            "text": card.post.text,
            "author": {
                "id": card.post.author.id,
                "username": card.post.author.username,
                "name": card.post.author.name,
                "followers_count": card.post.author.followers_count,
                "verified": card.post.author.verified,
            },
            "created_at": card.post.created_at,
            "like_count": card.post.like_count,
            "reply_count": card.post.reply_count,
            "repost_count": card.post.repost_count,
            "quote_count": card.post.quote_count,
            "conversation_id": card.post.conversation_id,
            "in_reply_to_user_id": card.post.in_reply_to_user_id,
            "reply_settings": card.post.reply_settings,
            "url": card.post.url,
        }),
        evidence_data=json.dumps({
            "post_id": card.evidence.post_id,
            "matched_topic": card.evidence.matched_topic,
            "why_it_fits": card.evidence.why_it_fits,
            "confidence_score": card.evidence.confidence_score,
            "confidence_inputs": card.evidence.confidence_inputs,
            "founder_materials": [
                {
                    "source": m.source,
                    "passage": m.passage,
                    "relevance": m.relevance,
                }
                for m in card.evidence.founder_materials
            ],
            "draft": {
                "text": card.evidence.draft.text,
                "claim_gate": {
                    "passed": card.evidence.draft.claim_gate.passed,
                    "claims_checked": card.evidence.draft.claim_gate.claims_checked,
                    "unsupported_claims": card.evidence.draft.claim_gate.unsupported_claims,
                    "supported_claims": card.evidence.draft.claim_gate.supported_claims,
                    "reasoning": card.evidence.draft.claim_gate.reasoning,
                },
                "steers_applied": card.evidence.draft.steers_applied,
                "token_count": card.evidence.draft.token_count,
                "model_used": card.evidence.draft.model_used,
            } if card.evidence.draft else None,
            "skip_reason": card.evidence.skip_reason,
        }),
        status=card.status.value,
        created_at=card.created_at,
        updated_at=card.updated_at,
        approved_by=card.approved_by,
        posted_url=card.posted_url,
        reply_post_id=card.reply_post_id,
        skip_reason=card.skip_reason,
        followup_replies=json.dumps(card.followup_replies) if card.followup_replies else None,
    )


@activity.defn(name="store_card")
async def store_card(input: StoreCardInput) -> bool:
    """
    Store a review card in SQLite.
    
    Idempotent: upsert by workflow_id.
    """
    activity.logger.info(
        f"Storing card {input.card.workflow_id} (key: {input.idempotency_key})"
    )
    
    db = get_db()
    try:
        # Check if exists
        existing = db.query(ReviewCardDB).filter(
            ReviewCardDB.workflow_id == input.card.workflow_id
        ).first()
        
        if existing:
            # Update
            card_db = _card_to_db(input.card)
            for key, value in card_db.__dict__.items():
                if not key.startswith('_'):
                    setattr(existing, key, value)
        else:
            # Insert
            card_db = _card_to_db(input.card)
            db.add(card_db)
        
        db.commit()
        activity.logger.info(f"Stored card {input.card.workflow_id}")
        return True
    finally:
        db.close()


@activity.defn(name="update_card")
async def update_card(input: UpdateCardInput) -> bool:
    """
    Update a review card.
    
    Idempotent: updates are applied by field.
    """
    activity.logger.info(
        f"Updating card {input.workflow_id} (key: {input.idempotency_key})"
    )
    
    db = get_db()
    try:
        card_db = db.query(ReviewCardDB).filter(
            ReviewCardDB.workflow_id == input.workflow_id
        ).first()
        
        if not card_db:
            activity.logger.error(f"Card {input.workflow_id} not found")
            return False
        
        # Apply updates
        for key, value in input.updates.items():
            if key == "status":
                card_db.status = value
            elif key == "approved_by":
                card_db.approved_by = value
            elif key == "posted_url":
                card_db.posted_url = value
            elif key == "reply_post_id":
                card_db.reply_post_id = value
            elif key == "skip_reason":
                card_db.skip_reason = value
            elif key == "followup_replies":
                card_db.followup_replies = json.dumps(value) if value else None
            elif key == "evidence":
                # Update evidence_data JSON
                evidence_data = json.loads(card_db.evidence_data)
                if hasattr(value, 'draft') and value.draft:
                    evidence_data["draft"] = {
                        "text": value.draft.text,
                        "claim_gate": {
                            "passed": value.draft.claim_gate.passed,
                            "claims_checked": value.draft.claim_gate.claims_checked,
                            "unsupported_claims": value.draft.claim_gate.unsupported_claims,
                            "supported_claims": value.draft.claim_gate.supported_claims,
                            "reasoning": value.draft.claim_gate.reasoning,
                        },
                        "steers_applied": value.draft.steers_applied,
                        "token_count": value.draft.token_count,
                        "model_used": value.draft.model_used,
                    }
                card_db.evidence_data = json.dumps(evidence_data)
        
        db.commit()
        activity.logger.info(f"Updated card {input.workflow_id}")
        return True
    finally:
        db.close()


@activity.defn(name="get_card")
async def get_card(input: GetCardInput) -> Optional[Dict]:
    """Get a review card by workflow ID."""
    activity.logger.info(f"Getting card {input.workflow_id}")
    
    db = get_db()
    try:
        card_db = db.query(ReviewCardDB).filter(
            ReviewCardDB.workflow_id == input.workflow_id
        ).first()
        
        if not card_db:
            activity.logger.info(f"Card {input.workflow_id} not found")
            return None
        
        activity.logger.info(f"Found card {input.workflow_id}")
        
        # Return as dict
        return {
            "workflow_id": card_db.workflow_id,
            "post": json.loads(card_db.post_data),
            "evidence": json.loads(card_db.evidence_data),
            "status": card_db.status,
            "created_at": card_db.created_at,
            "updated_at": card_db.updated_at,
            "approved_by": card_db.approved_by,
            "posted_url": card_db.posted_url,
            "reply_post_id": card_db.reply_post_id,
            "skip_reason": card_db.skip_reason,
            "followup_replies": json.loads(card_db.followup_replies) if card_db.followup_replies else None,
        }
    finally:
        db.close()


def get_all_cards() -> List[Dict]:
    """Get all cards (for API access)."""
    db = get_db()
    try:
        cards = db.query(ReviewCardDB).all()
        
        return [
            {
                "workflow_id": card.workflow_id,
                "post": json.loads(card.post_data),
                "evidence": json.loads(card.evidence_data),
                "status": card.status,
                "created_at": card.created_at,
                "updated_at": card.updated_at,
                "approved_by": card.approved_by,
                "posted_url": card.posted_url,
                "reply_post_id": card.reply_post_id,
                "skip_reason": card.skip_reason,
                "followup_replies": json.loads(card.followup_replies) if card.followup_replies else None,
            }
            for card in cards
        ]
    finally:
        db.close()
