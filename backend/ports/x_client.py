"""
XClientPort - abstraction for X API client.

Per B5 and ADR-0003: X client must be injected, not hard-wired.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from backend.models import Post


@dataclass(frozen=True)
class SearchResult:
    """One recent-search response.

    billable_reads is what X bills for the response (issue #17): the number of
    post objects in its `data`, including posts the adapter could not convert
    and left out of `posts` (for example one whose author is missing from
    `includes.users`). X bills Posts Read per resource returned (ADR-0005).
    newest_id is the newest ID among `posts`, so a dropped newer post is
    fetched (and billed) again by the next poll rather than skipped for good.
    """

    posts: List[Post]
    newest_id: Optional[str]
    billable_reads: int

    def __post_init__(self):
        if self.billable_reads < len(self.posts):
            raise ValueError(
                f"billable_reads ({self.billable_reads}) is below the "
                f"{len(self.posts)} posts returned"
            )


class XClientPort(ABC):
    """Port for X API operations."""

    @abstractmethod
    async def search_recent(
        self,
        query: str,
        since_id: Optional[str] = None,
        max_results: int = 10,
    ) -> SearchResult:
        """
        Search recent posts.

        Returns the converted posts, the newest post ID and the billable read
        count (see SearchResult).
        """
        pass

    def billable_max_results(self, max_results: int) -> int:
        """The most posts a search_recent(max_results=...) call can be billed for.

        The activity reserves this many reads before the call (issue #8). An
        adapter that changes the requested count (the HTTP adapter clamps to
        X's 10-100 range) must override it.
        """
        return max_results

    @abstractmethod
    async def lookup_post(self, post_id: str) -> Optional[Post]:
        """
        Lookup a single post by ID.

        Returns the post, or None when X answered that the post is gone (not
        found, or no longer visible to us) and returned no post object. None
        makes the target re-fetch remove the post text (issue #9) and is not
        billed (issue #17: X bills per post object returned).

        Any other outcome raises. A response with post data the adapter cannot
        convert raises an error X billed (one read); see x_billed_nothing in
        backend/activities/x_activities.py for the errors counted as unbilled.
        """
        pass
