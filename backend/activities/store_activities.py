"""Storage activities for review cards using SQLite."""

from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Union
from temporalio import activity
import asyncio

from backend.models import ReviewCard, Evidence
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


@dataclass
class DeletePostTextInput:
    """Input for delete_post_text activity (B8 - 404 handling)."""
    
    workflow_id: str
    idempotency_key: str


def _card_to_db(card: ReviewCard) -> ReviewCardDB:
    """Convert ReviewCard to database model."""
    # Handle status - it might be an enum or a string due to serialization
    if isinstance(card.status, str):
        status_str = card.status
    elif hasattr(card.status, 'value'):
        status_str = card.status.value
    else:
        status_str = str(card.status)
    
    return ReviewCardDB(
        workflow_id=card.workflow_id,
        post_data={
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
        },
        evidence_data={
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
                    "passage_id": m.passage_id,
                }
                for m in card.evidence.founder_materials
            ],
            "draft": {
                "text": card.evidence.draft.text,
                "segments": [
                    {
                        "text": s.text,
                        "kind": s.kind,
                        "source_passage_id": s.source_passage_id,
                        "quote": s.quote,
                    }
                    for s in card.evidence.draft.segments
                ],
                "claim_gate": {
                    "passed": card.evidence.draft.claim_gate.passed,
                    "segments": [
                        {
                            "text": s.text,
                            "kind": s.kind,
                            "source_passage_id": s.source_passage_id,
                            "quote": s.quote,
                        }
                        for s in card.evidence.draft.claim_gate.segments
                    ],
                    "failed_sentences": [
                        {
                            "sentence": sentence,
                            "reason": reason,
                        }
                        for sentence, reason in card.evidence.draft.claim_gate.failed_sentences
                    ],
                    "reasoning": card.evidence.draft.claim_gate.reasoning,
                    # Legacy fields for backwards compatibility
                    "claims": [
                        {
                            "text": c.text,
                            "cited_passage_ids": c.cited_passage_ids,
                            "quoted_spans": c.quoted_spans,
                        }
                        for c in card.evidence.draft.claim_gate.claims
                    ],
                    "failed_claims": [
                        {
                            "claim": {
                                "text": c.text,
                                "cited_passage_ids": c.cited_passage_ids,
                                "quoted_spans": c.quoted_spans,
                            },
                            "reason": reason,
                        }
                        for c, reason in card.evidence.draft.claim_gate.failed_claims
                    ],
                },
                "steers_applied": card.evidence.draft.steers_applied,
                "token_count": card.evidence.draft.token_count,
                "model_used": card.evidence.draft.model_used,
                "input_tokens": card.evidence.draft.input_tokens,
                "output_tokens": card.evidence.draft.output_tokens,
            } if card.evidence.draft else None,
            "skip_reason": card.evidence.skip_reason,
        },
        status=status_str,
        created_at=card.created_at,
        updated_at=card.updated_at,
        approved_by=card.approved_by,
        posted_url=card.posted_url,
        reply_post_id=card.reply_post_id,
        skip_reason=card.skip_reason,
        followup_replies=card.followup_replies if card.followup_replies else None,
    )


def _store_card_sync(input: StoreCardInput) -> bool:
    """Synchronous DB work for store_card (B3)."""
    db = get_db()
    try:
        card_db = _card_to_db(input.card)
        
        # Use INSERT...ON CONFLICT (B3)
        # SQLAlchemy's merge() provides upsert-like behavior
        db.merge(card_db)
        db.commit()
        return True
    finally:
        db.close()


@activity.defn(name="store_card")
async def store_card(input: StoreCardInput) -> bool:
    """
    Store a review card in SQLite.
    
    Idempotent: upsert by workflow_id using merge (INSERT...ON CONFLICT).
    B3: Runs sync DB work via asyncio.to_thread.
    """
    activity.logger.info(
        f"Storing card {input.card.workflow_id} (key: {input.idempotency_key})"
    )
    
    # B3: Run sync DB work in thread pool
    result = await asyncio.to_thread(_store_card_sync, input)
    
    activity.logger.info(f"Stored card {input.card.workflow_id}")
    return result


def _update_card_sync(input: UpdateCardInput) -> bool:
    """Synchronous DB work for update_card (B3)."""
    db = get_db()
    try:
        card_db = db.query(ReviewCardDB).filter(
            ReviewCardDB.workflow_id == input.workflow_id
        ).first()
        
        if not card_db:
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
                card_db.followup_replies = value if value else None
            elif key == "evidence":
                # Update evidence_data JSON
                evidence_data = card_db.evidence_data
                if hasattr(value, 'draft') and value.draft:
                    evidence_data["draft"] = {
                        "text": value.draft.text,
                        "segments": [
                            {
                                "text": s.text,
                                "kind": s.kind,
                                "source_passage_id": s.source_passage_id,
                                "quote": s.quote,
                            }
                            for s in value.draft.segments
                        ],
                        "claim_gate": {
                            "passed": value.draft.claim_gate.passed,
                            "segments": [
                                {
                                    "text": s.text,
                                    "kind": s.kind,
                                    "source_passage_id": s.source_passage_id,
                                    "quote": s.quote,
                                }
                                for s in value.draft.claim_gate.segments
                            ],
                            "failed_sentences": [
                                {
                                    "sentence": sentence,
                                    "reason": reason,
                                }
                                for sentence, reason in value.draft.claim_gate.failed_sentences
                            ],
                            "reasoning": value.draft.claim_gate.reasoning,
                            # Legacy fields
                            "claims": [
                                {
                                    "text": c.text,
                                    "cited_passage_ids": c.cited_passage_ids,
                                    "quoted_spans": c.quoted_spans,
                                }
                                for c in value.draft.claim_gate.claims
                            ],
                            "failed_claims": [
                                {
                                    "claim": {
                                        "text": c.text,
                                        "cited_passage_ids": c.cited_passage_ids,
                                        "quoted_spans": c.quoted_spans,
                                    },
                                    "reason": reason,
                                }
                                for c, reason in value.draft.claim_gate.failed_claims
                            ],
                        },
                        "steers_applied": value.draft.steers_applied,
                        "token_count": value.draft.token_count,
                        "model_used": value.draft.model_used,
                        "input_tokens": value.draft.input_tokens,
                        "output_tokens": value.draft.output_tokens,
                    }
                card_db.evidence_data = evidence_data
        
        db.commit()
        return True
    finally:
        db.close()


@activity.defn(name="update_card")
async def update_card(input: UpdateCardInput) -> bool:
    """
    Update a review card.
    
    Idempotent: updates are applied by field.
    B3: Runs sync DB work via asyncio.to_thread.
    """
    activity.logger.info(
        f"Updating card {input.workflow_id} (key: {input.idempotency_key})"
    )
    
    # B3: Run sync DB work in thread pool
    result = await asyncio.to_thread(_update_card_sync, input)
    
    if not result:
        activity.logger.error(f"Card {input.workflow_id} not found")
    else:
        activity.logger.info(f"Updated card {input.workflow_id}")
    
    return result


def _get_card_sync(input: GetCardInput) -> Optional[Dict]:
    """Synchronous DB work for get_card (B3)."""
    db = get_db()
    try:
        card_db = db.query(ReviewCardDB).filter(
            ReviewCardDB.workflow_id == input.workflow_id
        ).first()
        
        if not card_db:
            return None
        
        # Return as dict
        return {
            "workflow_id": card_db.workflow_id,
            "post": card_db.post_data,
            "evidence": card_db.evidence_data,
            "status": card_db.status,
            "created_at": card_db.created_at,
            "updated_at": card_db.updated_at,
            "approved_by": card_db.approved_by,
            "posted_url": card_db.posted_url,
            "reply_post_id": card_db.reply_post_id,
            "skip_reason": card_db.skip_reason,
            "followup_replies": card_db.followup_replies if card_db.followup_replies else None,
        }
    finally:
        db.close()


@activity.defn(name="get_card")
async def get_card(input: GetCardInput) -> Optional[Dict]:
    """
    Get a review card by workflow ID.
    
    B3: Runs sync DB work via asyncio.to_thread.
    """
    activity.logger.info(f"Getting card {input.workflow_id}")
    
    # B3: Run sync DB work in thread pool
    result = await asyncio.to_thread(_get_card_sync, input)
    
    if result:
        activity.logger.info(f"Found card {input.workflow_id}")
    else:
        activity.logger.info(f"Card {input.workflow_id} not found")
    
    return result


def _delete_post_text_sync(input: DeletePostTextInput) -> bool:
    """Synchronous DB work for delete_post_text (B8)."""
    db = get_db()
    try:
        card_db = db.query(ReviewCardDB).filter(
            ReviewCardDB.workflow_id == input.workflow_id
        ).first()
        
        if not card_db:
            return False
        
        # Delete post text on 404 per X API compliance, keep structure
        post_data = card_db.post_data
        post_data["text"] = "[removed - post deleted]"
        card_db.post_data = post_data
        
        db.commit()
        return True
    finally:
        db.close()


@activity.defn(name="delete_post_text")
async def delete_post_text(input: DeletePostTextInput) -> bool:
    """
    Delete post text from card after 404 (B8 - X API compliance).
    
    Keeps the card structure but removes the post text.
    """
    activity.logger.info(
        f"Deleting post text for {input.workflow_id} (key: {input.idempotency_key})"
    )
    
    # B3: Run sync DB work in thread pool
    result = await asyncio.to_thread(_delete_post_text_sync, input)
    
    if result:
        activity.logger.info(f"Deleted post text for {input.workflow_id}")
    else:
        activity.logger.warning(f"Card {input.workflow_id} not found")
    
    return result


def get_all_cards() -> List[Dict]:
    """Get all cards (for API access)."""
    db = get_db()
    try:
        cards = db.query(ReviewCardDB).all()
        
        return [
            {
                "workflow_id": card.workflow_id,
                "post": card.post_data,
                "evidence": card.evidence_data,
                "status": card.status,
                "created_at": card.created_at,
                "updated_at": card.updated_at,
                "approved_by": card.approved_by,
                "posted_url": card.posted_url,
                "reply_post_id": card.reply_post_id,
                "skip_reason": card.skip_reason,
                "followup_replies": card.followup_replies if card.followup_replies else None,
            }
            for card in cards
        ]
    finally:
        db.close()
