"""
XClientPort - abstraction for X API client.

Per B5 and ADR-0003: X client must be injected, not hard-wired.
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

from backend.models import Post


class XClientPort(ABC):
    """Port for X API operations."""
    
    @abstractmethod
    async def search_recent(
        self,
        query: str,
        since_id: Optional[str] = None,
        max_results: int = 10,
    ) -> Tuple[List[Post], Optional[str]]:
        """
        Search recent posts.
        
        Returns (posts, newest_id).
        """
        pass
    
    @abstractmethod
    async def lookup_post(self, post_id: str) -> Optional[Post]:
        """
        Lookup a single post by ID.
        
        Returns None if not found (404).
        """
        pass
