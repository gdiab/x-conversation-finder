"""Post and author models."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Author:
    """X post author."""
    
    id: str
    username: str
    name: str
    followers_count: int
    verified: bool = False


@dataclass
class Post:
    """X post data."""
    
    id: str
    text: str
    author: Author
    created_at: str
    like_count: int
    reply_count: int
    repost_count: int
    quote_count: int
    conversation_id: str
    in_reply_to_user_id: Optional[str] = None
    reply_settings: str = "everyone"
    url: str = ""
    
    def __post_init__(self):
        if not self.url:
            self.url = f"https://x.com/{self.author.username}/status/{self.id}"
    
    def get_created_at_datetime(self) -> datetime:
        """Parse created_at as datetime."""
        return datetime.fromisoformat(self.created_at)
