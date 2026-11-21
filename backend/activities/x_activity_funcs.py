"""X API activity functions (module-level for workflow imports)."""

from temporalio import activity

from backend.activities.x_activities import (
    SearchRecentInput,
    SearchRecentOutput,
    LookupPostInput,
    CheckFollowupInput,
    XActivities,
)
from backend.models import Post
from typing import Optional, List

# Global instance - will be set by worker
_x_activities: Optional[XActivities] = None


def set_x_activities(instance: XActivities):
    """Set the XActivities instance (called by worker)."""
    global _x_activities
    _x_activities = instance


@activity.defn(name="search_recent")
async def search_recent(input: SearchRecentInput) -> SearchRecentOutput:
    """Search recent posts - delegates to XActivities instance."""
    if _x_activities is None:
        raise RuntimeError("XActivities not initialized")
    return await _x_activities.search_recent(input)


@activity.defn(name="lookup_post")  
async def lookup_post(input: LookupPostInput) -> Optional[Post]:
    """Lookup a post by ID - delegates to XActivities instance."""
    if _x_activities is None:
        raise RuntimeError("XActivities not initialized")
    return await _x_activities.lookup_post(input)


@activity.defn(name="check_followup")
async def check_followup(input: CheckFollowupInput) -> List[Post]:
    """Check for follow-up replies - delegates to XActivities instance."""
    if _x_activities is None:
        raise RuntimeError("XActivities not initialized")
    return await _x_activities.check_followup(input)
