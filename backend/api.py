"""
FastAPI server for the review UI.

Provides endpoints for:
- Starting a topic watch
- Listing review cards
- Sending signals/updates to Review workflows
"""

import asyncio
import logging
import sys
from typing import List, Optional
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from backend.config import get_settings
from backend.workflows import TopicWatchInput, ReviewInput
from backend.workflows.topic_watch import TopicWatchWorkflow
from backend.workflows.review import (
    ReviewWorkflow,
    BUSY_ERROR,
    INVALID_REQUEST_ERROR,
    INVALID_STATE_ERROR,
    NOT_READY_ERROR,
    POST_ANYWAY_REQUIRED_ERROR,
    POST_REMOVED_ERROR,
    DRAFT_CHANGED_ERROR,
    NUDGE_REJECTED_ERROR,
)
from backend.activities.store_activities import (
    POST_TEXT_REMOVED,
    apply_stored_removal,
    card_post_text_removed,
    get_all_cards,
    get_stored_card,
    text_free_card,
)
from backend import review_docs
from backend.gate.failures import DraftFailures, nudge_rejection_message, nudge_rejection_reasons
from backend.database import init_db
from backend.gate.product_link import product_url_error
from backend.gate.written_check import MAX_FEATURE_TERM_WORDS, clean_feature_terms, feature_term_words
from backend.budget import get_today_budget

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)
temporal_client: Optional[Client] = None


# issue #35: how often a WAL checkpoint the startup backfill could not
# complete is retried
WAL_CHECKPOINT_RETRY_SEC = 30.0


async def retry_wal_checkpoint(interval_sec: float = WAL_CHECKPOINT_RETRY_SEC) -> None:
    """Retry the WAL checkpoint until it completes (issue #35): the startup
    backfill redacted rows of removed posts, but a reader holding an old
    snapshot kept their old pages in the -wal file."""
    from backend.database import checkpoint_wal

    while True:
        await asyncio.sleep(interval_sec)
        try:
            done = await asyncio.to_thread(checkpoint_wal)
        except Exception:
            # A transient database error must not end the retries
            logger.exception("WAL checkpoint after the removal backfill failed; retrying")
            continue
        if done:
            logger.info("WAL checkpoint after the removal backfill completed")
            return


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    global temporal_client
    settings = get_settings()
    
    # Startup: Initialize database
    checkpoint_task = asyncio.create_task(retry_wal_checkpoint()) if init_db() else None
    logger.info("Database initialized")
    
    # Startup: Connect to Temporal
    logger.info(f"Connecting to Temporal at {settings.temporal_host}")
    temporal_client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
    )
    logger.info("Connected to Temporal")
    
    yield
    
    # Shutdown
    if checkpoint_task is not None:
        checkpoint_task.cancel()
    logger.info("Shutting down")


app = FastAPI(
    title="X Conversation Finder API",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware (dev only - restrict in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],  # Vite dev server
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Catch unhandled exceptions and return 500."""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"code": "server_error", "detail": "Internal server error"},
    )


# --- Review update errors (issue #15) ---
# Every error from a review endpoint is JSON {"code": ..., "detail": <message>}
# plus code-specific fields; the UI switches on "code", never on the message.
# The Review workflow's validators raise ApplicationError with a type
# (backend/workflows/review.py); the type picks the status and code here.
REJECTION_CODES = {
    NOT_READY_ERROR: (409, "not_ready"),  # card not stored yet: retry shortly
    BUSY_ERROR: (409, "busy"),  # another change is in flight: retry after it
    POST_ANYWAY_REQUIRED_ERROR: (409, "post_anyway_required"),  # + gate_state, failing_sentences
    POST_REMOVED_ERROR: (409, "post_removed"),  # review now skipped; reload it
    DRAFT_CHANGED_ERROR: (409, "draft_changed"),  # the draft shown is not current; reload it
    INVALID_STATE_ERROR: (409, "invalid_state"),
    INVALID_REQUEST_ERROR: (400, "invalid_request"),
}
# Any other ApplicationError the Review raises is a business rejection from an
# update handler (a nudged draft the gate refused, a reply URL that is not a
# reply to the post): the request was understood and refused.
OTHER_REJECTION = (422, "rejected")


def error_response(status_code: int, code: str, detail: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code, "detail": detail, **extra})


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request, exc: RequestValidationError):
    """A malformed request body: FastAPI's 422, with a code like every other error."""
    return error_response(422, "invalid_request", "Invalid request body", errors=jsonable_encoder(exc.errors()))


def temporal_not_connected() -> JSONResponse:
    return error_response(500, "server_error", "Temporal client not connected")


# A refusal can quote the draft: a rejected nudge names the failing sentence
# and the checker's reason, and post_anyway_required lists failing sentences.
REFUSAL_DETAILS_REMOVED = "The request was refused. Its details were removed with the post."


def review_error_response(exc: Exception, action: str, workflow_id: Optional[str] = None) -> JSONResponse:
    """Map an exception from a Review update, signal or query to a response.

    A rejected update (validator or handler) is a WorkflowUpdateFailedError
    whose str() is only "Workflow update failed"; the reason is its cause.
    Only failures that are not the Review's own decision (an activity that
    failed, Temporal unreachable) are 500s.

    With `workflow_id`, a refusal that can quote the draft ("rejected",
    "post_anyway_required") is returned without those quotes once the stored
    card records that the post was removed (issue #12; the same rule as the
    edit and nudge success responses).
    """
    cause = exc.cause if isinstance(exc, WorkflowUpdateFailedError) else None
    # issue #23: the API stores an edit or nudge text before the update and
    # is refused once the post's removal is recorded
    if isinstance(exc, ApplicationError) and exc.type == POST_REMOVED_ERROR:
        cause = exc
    # An activity can refuse on purpose too: draft_reply refuses to redraft
    # (nudge) a card whose post was removed by another Review of it. That
    # refusal arrives wrapped in ActivityError; any other activity failure
    # stays a 500.
    if (
        isinstance(cause, ActivityError)
        and isinstance(cause.cause, ApplicationError)
        and cause.cause.type == POST_REMOVED_ERROR
    ):
        cause = cause.cause
    if isinstance(cause, ApplicationError):
        status_code, code = REJECTION_CODES.get(cause.type, OTHER_REJECTION)
        extra = {}
        if code == "post_anyway_required":
            details = cause.details[0] if cause.details and isinstance(cause.details[0], dict) else {}
            extra = {
                "gate_state": details.get("gate_state"),
                "failing_sentences": list(details.get("failing_sentences") or []),
            }
            if details.get("results_ref") and workflow_id:
                # issue #23: a text-free Review does not hold the sentences;
                # it names the stored results of the check it refused on
                extra["failing_sentences"] = stored_failing_sentences(workflow_id, details.get("results_ref"))
        detail = cause.message
        if cause.type == NUDGE_REJECTED_ERROR and workflow_id:
            # issue #23: the reasons quote the redraft and are stored, not in history
            detail = stored_nudge_rejection(workflow_id, cause) or detail
        if code in ("rejected", "post_anyway_required") and workflow_id and card_post_text_removed(workflow_id):
            if code == "rejected":
                detail = REFUSAL_DETAILS_REMOVED
            if "failing_sentences" in extra:
                extra["failing_sentences"] = [POST_TEXT_REMOVED for _ in extra["failing_sentences"]]
        return error_response(status_code, code, detail, **extra)
    if isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND:
        # No such workflow, or (for updates and signals) it has already ended
        return error_response(404, "not_found", f"Review not found or no longer running: {exc.message}")
    reason = exc.cause if isinstance(exc, WorkflowUpdateFailedError) else exc
    logger.error(f"{action} failed: {reason}", exc_info=exc)
    return error_response(500, "server_error", f"{action} failed: {reason}")


def stored_failing_sentences(workflow_id: str, results_ref: Optional[dict] = None) -> List[str]:
    """The failing sentences of the edit check a text-free Review's
    post_anyway_required names (issue #23): those stored results exactly,
    so a later edit cannot change what the founder is shown. No reference
    (a "not_checked" edit) means no sentences; after a removal, none."""
    from backend.activities.claim_gate_activity import SentenceGateResult
    from backend.models.written import WrittenCheckResult

    card = get_stored_card(workflow_id)
    if not isinstance(results_ref, dict) or not card:
        return []
    post_id = card["post"]["id"]
    results = None
    if isinstance(results_ref.get("results_slot"), str):
        results = review_docs.find_doc(workflow_id, post_id, results_ref["results_slot"], List[SentenceGateResult])
    elif isinstance(results_ref.get("check_slot"), str):
        check = review_docs.find_doc(workflow_id, post_id, results_ref["check_slot"], WrittenCheckResult)
        results = check.sentences if check else None
    return [r.sentence for r in results or [] if not r.passed]


def stored_nudge_rejection(workflow_id: str, cause: ApplicationError) -> Optional[str]:
    """A text-free nudge rejection's message with its stored reasons (issue #23)."""
    details = cause.details[0] if cause.details and isinstance(cause.details[0], dict) else {}
    card = get_stored_card(workflow_id)
    slot = details.get("slot")
    if not card or not isinstance(slot, str):
        return None
    failures = review_docs.find_doc(
        workflow_id, card["post"]["id"], review_docs.failures_slot(slot), DraftFailures
    )
    return nudge_rejection_message(nudge_rejection_reasons(failures)) if failures else None


async def send_founder_text(handle, workflow_id: str, update: str, kind: str, text: str, client_request_id: str):
    """Send an edit or a nudge (issue #23).

    A Review that keeps text out of its history (its get_card says
    "text_free") is sent a reference: the text is stored in the database
    first, and the update names its slot and digest. Any other Review is
    sent the text, as before. Returns (update result, text_free).
    """
    state = await handle.query("get_card")
    if not state.get("text_free"):
        return await handle.execute_update(update, args=[text, client_request_id]), False
    text_ref = None
    if text and text.strip():
        slot = review_docs.store_founder_text(workflow_id, state["post_id"], kind, client_request_id, text)
        text_ref = {"slot": slot, "sha256": review_docs.text_digest(text)}
    return await handle.execute_update(update, args=["", client_request_id, text_ref]), True


def stored_draft_text(workflow_id: str) -> Optional[str]:
    """The draft text on the stored card (issue #23: a text-free Review's
    edit and nudge return a digest)."""
    card = get_stored_card(workflow_id) or {}
    draft = (card.get("evidence") or {}).get("draft") or {}
    return draft.get("text")


MAX_PRODUCT_NAME_CHARS = 80
MAX_PRODUCT_ALIASES = 10
MAX_KEY_FEATURES = 20


class StartWatchRequest(BaseModel):
    """Request to start a topic watch."""
    account: str
    topic: str
    query: str
    product_brief: str
    # issue #12 (George's decision, 2026-11-24): the Start Watch form's
    # "Product name" (required) and "Also called" (optional) fields. The
    # written-draft name trigger reads only these, never the brief text.
    product_name: str
    product_aliases: List[str] = Field(default_factory=list)
    # issue #12 (George's decision, 2026-11-25): the form's optional "Key
    # features" list. The written-draft feature-term trigger reads only this
    # field, never the brief text.
    key_features: List[str] = Field(default_factory=list)
    # issue #41 (George's decision, 2026-11-26): the form's required "Product
    # URL" (https). Code appends it to drafts that name the product.
    product_url: str

    @field_validator("product_url")
    @classmethod
    def product_url_is_https(cls, value: str) -> str:
        url = value.strip()
        error = product_url_error(url)
        if error:
            raise ValueError(error)
        return url

    @field_validator("product_name")
    @classmethod
    def product_name_is_set(cls, value: str) -> str:
        name = " ".join(value.split())
        if not name:
            raise ValueError("Product name is required")
        if len(name) > MAX_PRODUCT_NAME_CHARS:
            raise ValueError(f"Product name must be at most {MAX_PRODUCT_NAME_CHARS} characters")
        return name

    @field_validator("product_aliases")
    @classmethod
    def product_aliases_are_names(cls, value: List[str]) -> List[str]:
        aliases: List[str] = []
        for alias in value:
            alias = " ".join(alias.split())
            if not alias or alias.casefold() in (a.casefold() for a in aliases):
                continue  # blanks and repeats from the comma-separated field
            if len(alias) > MAX_PRODUCT_NAME_CHARS:
                raise ValueError(f"Each alias must be at most {MAX_PRODUCT_NAME_CHARS} characters")
            aliases.append(alias)
        if len(aliases) > MAX_PRODUCT_ALIASES:
            raise ValueError(f"At most {MAX_PRODUCT_ALIASES} aliases")
        return aliases

    @field_validator("key_features")
    @classmethod
    def key_features_are_short_terms(cls, value: List[str]) -> List[str]:
        """A key feature is the short name of a feature, not a sentence about
        it. An entry the trigger could not use is refused, so the founder is
        told, instead of being dropped."""
        for feature in value:
            feature = " ".join(feature.split())
            if not feature:
                continue  # a blank from the comma-separated field
            if not feature_term_words(feature):
                raise ValueError(f'Key feature "{feature}" has no letter or digit')
            if len(feature.split()) > MAX_FEATURE_TERM_WORDS:
                raise ValueError(
                    f"Each key feature must be at most {MAX_FEATURE_TERM_WORDS} words "
                    f'(got {len(feature.split())}: "{feature}")'
                )
            if len(feature) > MAX_PRODUCT_NAME_CHARS:
                raise ValueError(f"Each key feature must be at most {MAX_PRODUCT_NAME_CHARS} characters")
        features = clean_feature_terms(value)  # trimmed, without blanks and repeats
        if len(features) > MAX_KEY_FEATURES:
            raise ValueError(f"At most {MAX_KEY_FEATURES} key features")
        return features


class StartWatchResponse(BaseModel):
    """Response from starting a watch."""
    workflow_id: str
    message: str


class ApproveRequest(BaseModel):
    """Request to approve a review."""
    posted_url: str
    client_request_id: str
    post_anyway: bool = False  # Per ADR-0006 Amendment E
    # The draft the founder saw (issue #15 review): its text and its
    # edited-text gate state (null if never edited). A mismatch is 409
    # draft_changed. Both required (draft_gate_state may be null), so no
    # caller approves a draft unchecked. (The workflow argument stays
    # optional: approvals recorded before it replay with three arguments,
    # and validators do not run on replay.)
    draft_text: str
    draft_gate_state: Optional[str]


class SkipRequest(BaseModel):
    """Request to skip a review."""
    reason: str
    client_request_id: str


class EditRequest(BaseModel):
    """Request to edit a draft."""
    new_text: str
    client_request_id: str


class NudgeRequest(BaseModel):
    """Request to nudge a draft."""
    instruction: str
    client_request_id: str


class ConfirmPostedRequest(BaseModel):
    """Request to confirm posted reply."""
    reply_url: str
    client_request_id: str


@app.post("/api/watch/start", response_model=StartWatchResponse)
async def start_watch(request: StartWatchRequest):
    """Start a topic watch workflow."""
    if not temporal_client:
        raise HTTPException(status_code=500, detail="Temporal client not connected")
    
    settings = get_settings()
    workflow_id = f"watch:{request.account}:{request.topic}"
    
    # Create configs for determinism
    from backend.workflows.review import ReviewConfig
    from backend.workflows.topic_watch import TopicWatchConfig
    review_config = ReviewConfig.from_settings(settings)
    # S-3: Allow sub-floor poll interval only with fake adapter (test-only)
    allow_sub_floor = (
        settings.watch_poll_interval_hours == 0 and 
        settings.x_client_adapter == 'fake'
    )
    topic_watch_config = TopicWatchConfig(
        poll_interval_hours=settings.watch_poll_interval_hours,
        activity_timeout_medium_sec=settings.activity_timeout_medium_sec,
        activity_max_attempts=settings.activity_max_attempts,
        allow_sub_floor_poll=allow_sub_floor,
    )
    
    try:
        await temporal_client.start_workflow(
            TopicWatchWorkflow.run,
            TopicWatchInput(
                account=request.account,
                topic=request.topic,
                query=request.query,
                product_brief=request.product_brief,
                max_results=settings.watch_max_results,
                confidence_threshold=settings.confidence_threshold,
                config=topic_watch_config,
                review_config=review_config,
                product_name=request.product_name,
                product_aliases=request.product_aliases,
                key_features=request.key_features,
                product_url=request.product_url,
            ),
            id=workflow_id,
            task_queue="x-conversation-finder",
        )
        
        return StartWatchResponse(
            workflow_id=workflow_id,
            message=f"Started watch for topic '{request.topic}'",
        )
    
    except Exception as e:
        if "already exists" in str(e).lower():
            # A running watch keeps the brief, product name, other names, key
            # features and product URL it was started with (issues #12, #41:
            # stop it first to change them)
            return StartWatchResponse(
                workflow_id=workflow_id,
                message=(
                    f"Watch already running for topic '{request.topic}'. Its product name, other "
                    "names, key features, product URL and brief were not changed; stop the watch "
                    "first to change them."
                ),
            )
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/reviews")
async def list_reviews():
    """List all review cards."""
    cards = get_all_cards()
    
    # get_all_cards() returns dicts directly (NN1: no double JSON encoding)
    return {
        "cards": cards,
        "count": len(cards),
    }


@app.post("/api/reviews/{workflow_id}/approve")
async def approve_review(workflow_id: str, request: ApproveRequest):
    """
    Send approve update to a review workflow.
    
    Per ADR-0006 Amendment E: Now an Update (not Signal) with validation.
    Requires post_anyway=true if edited text failed re-gate: otherwise 409
    with code "post_anyway_required", "gate_state" and "failing_sentences"
    (issue #15). 409 "post_removed" means the target post was gone on the
    re-fetch and the review is skipped (issue #9); 409 "not_ready" means the
    card is not stored yet (issue #20). See REJECTION_CODES.
    """
    if not temporal_client:
        return temporal_not_connected()
    
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        result = await handle.execute_update(
            "approve",
            args=[
                request.posted_url,
                request.client_request_id,
                request.post_anyway,
                # issue #23: the draft's digest, not its text, enters the history
                {
                    "text_sha256": review_docs.text_digest(request.draft_text),
                    "gate_state": request.draft_gate_state,
                },
            ]
        )
        
        return {"message": result}

    except Exception as e:
        return review_error_response(e, "Approve", workflow_id)


@app.post("/api/reviews/{workflow_id}/skip")
async def skip_review(workflow_id: str, request: SkipRequest):
    """Send skip signal to a review workflow."""
    if not temporal_client:
        return temporal_not_connected()
    
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.signal(
            "skip",
            args=[request.reason, request.client_request_id]
        )
        
        return {"message": "Skipped"}
    
    except Exception as e:
        return review_error_response(e, "Skip")


@app.post("/api/reviews/{workflow_id}/edit")
async def edit_review(workflow_id: str, request: EditRequest):
    """Send edit update to a review workflow (errors: see REJECTION_CODES)."""
    if not temporal_client:
        return temporal_not_connected()
    
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        result, text_free = await send_founder_text(
            handle, workflow_id, "edit", "edit", request.new_text, request.client_request_id
        )
        if text_free:
            result = stored_draft_text(workflow_id)
        
        # As for a nudge below: if the post's removal was recorded while the
        # edit was checked, the stored card is redacted and the text is not
        # echoed back either
        if card_post_text_removed(workflow_id):
            result = POST_TEXT_REMOVED
        
        return {"message": "Edited", "new_text": result}
    
    except Exception as e:
        return review_error_response(e, "Edit", workflow_id)


@app.post("/api/reviews/{workflow_id}/nudge")
async def nudge_review(workflow_id: str, request: NudgeRequest):
    """Send nudge update to a review workflow (errors: see REJECTION_CODES;
    a redraft the gate refuses is 422 "rejected" with its reasons)."""
    if not temporal_client:
        return temporal_not_connected()
    
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        result, text_free = await send_founder_text(
            handle, workflow_id, "nudge", "nudge", request.instruction, request.client_request_id
        )
        if text_free:
            result = stored_draft_text(workflow_id)
        
        # issue #9 / #12: another Review of the post can record its removal
        # while this redraft runs. The card write is stored redacted; the
        # redraft returned here is model text that can quote the post, so it
        # is withheld too (as GET /api/reviews/{id} applies a stored removal).
        if card_post_text_removed(workflow_id):
            result = POST_TEXT_REMOVED
        
        return {"message": "Nudged", "new_draft": result}
    
    except Exception as e:
        return review_error_response(e, "Nudge", workflow_id)


@app.get("/api/reviews/{workflow_id}")
async def get_review(workflow_id: str):
    """Get a review card by workflow ID.

    issue #20: while the Review is still researching, planning or drafting,
    the result is the in-progress state from get_card ("in_progress": true,
    "status" names the stage, "evidence": null), not a 404. 404 only when
    there is no such workflow.
    """
    if not temporal_client:
        return temporal_not_connected()
    
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        result = await handle.query("get_card")
    except Exception as e:
        return review_error_response(e, "Get review")
    if result.get("text_free"):
        # issue #23: the content is on the stored card
        result = text_free_card(workflow_id, result)

    # issue #9: a removal recorded outside this workflow (eval re-fetch,
    # another account's Review of the post) applies to the query too
    return apply_stored_removal(workflow_id, result)


@app.post("/api/reviews/{workflow_id}/confirm_posted")
async def confirm_posted_review(workflow_id: str, request: ConfirmPostedRequest):
    """Send confirm_posted update to a review workflow (errors: see
    REJECTION_CODES; a URL that is not a reply to the post is 422 "rejected")."""
    if not temporal_client:
        return temporal_not_connected()
    
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        result = await handle.execute_update(
            "confirm_posted",
            args=[request.reply_url, request.client_request_id]
        )
        
        return {"message": "Confirmed", "result": result}
    
    except Exception as e:
        return review_error_response(e, "Confirm posted", workflow_id)


@app.get("/api/budget")
async def get_budget():
    """Get today's budget usage for the spend meter."""
    return get_today_budget()


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    
    logging.basicConfig(level=logging.INFO)
    
    uvicorn.run(
        "backend.api:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=True,
    )
