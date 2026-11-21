"""Reply agent activity functions (module-level for workflow imports)."""

from temporalio import activity
from typing import Optional

from backend.activities.reply_activities import (
    ResearchPostInput,
    DraftReplyInput,
    ReplyActivities,
)
from backend.models import Evidence

# Global instance - will be set by worker
_reply_activities: Optional[ReplyActivities] = None


def set_reply_activities(instance: ReplyActivities):
    """Set the ReplyActivities instance (called by worker)."""
    global _reply_activities
    _reply_activities = instance


@activity.defn(name="research_post")
async def research_post(input: ResearchPostInput) -> Evidence:
    """Research a post - delegates to ReplyActivities instance."""
    if _reply_activities is None:
        raise RuntimeError("ReplyActivities not initialized")
    return await _reply_activities.research_post(input)


@activity.defn(name="draft_reply")
async def draft_reply(input: DraftReplyInput) -> Evidence:
    """Draft a reply - delegates to ReplyActivities instance."""
    if _reply_activities is None:
        raise RuntimeError("ReplyActivities not initialized")
    return await _reply_activities.draft_reply(input)
