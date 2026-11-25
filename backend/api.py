"""
FastAPI server for the review UI.

Provides endpoints for:
- Starting a topic watch
- Listing review cards
- Sending signals/updates to Review workflows
"""

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
from pydantic import BaseModel
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
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
)
from backend.activities.store_activities import apply_stored_removal, get_all_cards
from backend.database import init_db
from backend.budget import get_today_budget

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)
temporal_client: Optional[Client] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    global temporal_client
    settings = get_settings()
    
    # Startup: Initialize database
    init_db()
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


def review_error_response(exc: Exception, action: str) -> JSONResponse:
    """Map an exception from a Review update, signal or query to a response.

    A rejected update (validator or handler) is a WorkflowUpdateFailedError
    whose str() is only "Workflow update failed"; the reason is its cause.
    Only failures that are not the Review's own decision (an activity that
    failed, Temporal unreachable) are 500s.
    """
    if isinstance(exc, WorkflowUpdateFailedError) and isinstance(exc.cause, ApplicationError):
        cause = exc.cause
        status_code, code = REJECTION_CODES.get(cause.type, OTHER_REJECTION)
        extra = {}
        if code == "post_anyway_required":
            details = cause.details[0] if cause.details and isinstance(cause.details[0], dict) else {}
            extra = {
                "gate_state": details.get("gate_state"),
                "failing_sentences": list(details.get("failing_sentences") or []),
            }
        return error_response(status_code, code, cause.message, **extra)
    if isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND:
        # No such workflow, or (for updates and signals) it has already ended
        return error_response(404, "not_found", f"Review not found or no longer running: {exc.message}")
    reason = exc.cause if isinstance(exc, WorkflowUpdateFailedError) else exc
    logger.error(f"{action} failed: {reason}", exc_info=exc)
    return error_response(500, "server_error", f"{action} failed: {reason}")


class StartWatchRequest(BaseModel):
    """Request to start a topic watch."""
    account: str
    topic: str
    query: str
    product_brief: str


class StartWatchResponse(BaseModel):
    """Response from starting a watch."""
    workflow_id: str
    message: str


class ApproveRequest(BaseModel):
    """Request to approve a review."""
    posted_url: str
    client_request_id: str
    post_anyway: bool = False  # Per ADR-0006 Amendment E


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
            return StartWatchResponse(
                workflow_id=workflow_id,
                message=f"Watch already running for topic '{request.topic}'",
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
            args=[request.posted_url, request.client_request_id, request.post_anyway]
        )
        
        return {"message": result}

    except Exception as e:
        return review_error_response(e, "Approve")


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
        result = await handle.execute_update(
            "edit",
            args=[request.new_text, request.client_request_id]
        )
        
        return {"message": "Edited", "new_text": result}
    
    except Exception as e:
        return review_error_response(e, "Edit")


@app.post("/api/reviews/{workflow_id}/nudge")
async def nudge_review(workflow_id: str, request: NudgeRequest):
    """Send nudge update to a review workflow (errors: see REJECTION_CODES;
    a redraft the gate refuses is 422 "rejected" with its reasons)."""
    if not temporal_client:
        return temporal_not_connected()
    
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        result = await handle.execute_update(
            "nudge",
            args=[request.instruction, request.client_request_id]
        )
        
        return {"message": "Nudged", "new_draft": result}
    
    except Exception as e:
        return review_error_response(e, "Nudge")


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
        return review_error_response(e, "Confirm posted")


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
