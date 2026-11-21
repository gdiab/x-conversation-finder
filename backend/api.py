"""
FastAPI server for the review UI.

Provides endpoints for:
- Starting a topic watch
- Listing review cards
- Sending signals/updates to Review workflows
"""

import logging
from typing import List, Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from temporalio.client import Client

from backend.config import get_settings
from backend.workflows import TopicWatchInput, ReviewInput
from backend.workflows.topic_watch import TopicWatchWorkflow
from backend.workflows.review import ReviewWorkflow
from backend.activities.store_activities import get_all_cards
from backend.database import init_db
from backend.budget import get_today_budget


app = FastAPI(title="X Conversation Finder API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger(__name__)
temporal_client: Optional[Client] = None


@app.on_event("startup")
async def startup():
    """Connect to Temporal and initialize database on startup."""
    global temporal_client
    settings = get_settings()
    
    # Initialize database
    init_db()
    logger.info("Database initialized")
    
    logger.info(f"Connecting to Temporal at {settings.temporal_host}")
    
    temporal_client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
    )


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
    
    try:
        await temporal_client.start_workflow(
            TopicWatchWorkflow.run,
            TopicWatchInput(
                account=request.account,
                topic=request.topic,
                query=request.query,
                product_brief=request.product_brief,
                poll_interval_hours=settings.watch_poll_interval_hours,
                max_results=settings.watch_max_results,
                confidence_threshold=settings.confidence_threshold,
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
    
    return {
        "cards": [card.to_dict() for card in cards],
        "count": len(cards),
    }


@app.post("/api/reviews/{workflow_id}/approve")
async def approve_review(workflow_id: str, request: ApproveRequest):
    """Send approve signal to a review workflow."""
    if not temporal_client:
        raise HTTPException(status_code=500, detail="Temporal client not connected")
    
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.signal(
            "approve",
            args=[request.posted_url, request.client_request_id]
        )
        
        return {"message": "Approved"}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/reviews/{workflow_id}/skip")
async def skip_review(workflow_id: str, request: SkipRequest):
    """Send skip signal to a review workflow."""
    if not temporal_client:
        raise HTTPException(status_code=500, detail="Temporal client not connected")
    
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.signal(
            "skip",
            args=[request.reason, request.client_request_id]
        )
        
        return {"message": "Skipped"}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/reviews/{workflow_id}/edit")
async def edit_review(workflow_id: str, request: EditRequest):
    """Send edit update to a review workflow."""
    if not temporal_client:
        raise HTTPException(status_code=500, detail="Temporal client not connected")
    
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        result = await handle.execute_update(
            "edit",
            args=[request.new_text, request.client_request_id]
        )
        
        return {"message": "Edited", "new_text": result}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/reviews/{workflow_id}/nudge")
async def nudge_review(workflow_id: str, request: NudgeRequest):
    """Send nudge update to a review workflow."""
    if not temporal_client:
        raise HTTPException(status_code=500, detail="Temporal client not connected")
    
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        result = await handle.execute_update(
            "nudge",
            args=[request.instruction, request.client_request_id]
        )
        
        return {"message": "Nudged", "new_draft": result}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/reviews/{workflow_id}")
async def get_review(workflow_id: str):
    """Get a review card by workflow ID."""
    if not temporal_client:
        raise HTTPException(status_code=500, detail="Temporal client not connected")
    
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        result = await handle.query("get_card")
        
        return result
    
    except Exception as e:
        raise HTTPException(status_code=404, detail="Review not found")


@app.post("/api/reviews/{workflow_id}/confirm_posted")
async def confirm_posted_review(workflow_id: str, request: ConfirmPostedRequest):
    """Send confirm_posted update to a review workflow."""
    if not temporal_client:
        raise HTTPException(status_code=500, detail="Temporal client not connected")
    
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        result = await handle.execute_update(
            "confirm_posted",
            args=[request.reply_url, request.client_request_id]
        )
        
        return {"message": "Confirmed", "result": result}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
