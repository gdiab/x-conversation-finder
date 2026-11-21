"""
Fake X API client for testing.

No real API calls, returns synthetic data.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional
import hashlib

from backend.models import Post, Author


class FakeXClient:
    """Fake X client for testing and CI."""
    
    def __init__(self):
        self.posts_database = self._generate_fake_posts()
        self.call_count = 0
    
    async def search_recent(
        self,
        query: str,
        since_id: Optional[str] = None,
        max_results: int = 10,
    ) -> tuple[List[Post], Optional[str]]:
        """
        Fake search returning synthetic posts.
        
        Returns (posts, newest_id)
        """
        self.call_count += 1
        
        matching = [
            p for p in self.posts_database
            if any(term.lower() in p.text.lower() for term in query.split())
        ]
        
        if since_id:
            matching = [
                p for p in matching
                if int(p.id) > int(since_id)
            ]
        
        matching.sort(key=lambda p: p.id, reverse=True)
        results = matching[:max_results]
        
        newest_id = results[0].id if results else since_id
        
        return results, newest_id
    
    async def lookup_post(self, post_id: str) -> Optional[Post]:
        """Lookup a post by ID."""
        self.call_count += 1
        
        for post in self.posts_database:
            if post.id == post_id:
                return post
        
        return None
    
    def _generate_fake_posts(self) -> List[Post]:
        """Generate a set of fake posts for testing."""
        base_time = datetime.now(timezone.utc)
        
        posts = []
        
        post_templates = [
            {
                "id": "1001",
                "text": "Struggling with distributed systems reliability. How do you folks handle eventual consistency?",
                "username": "alice_dev",
                "name": "Alice Developer",
                "followers": 5000,
                "hours_ago": 2,
            },
            {
                "id": "1002",
                "text": "Just shipped our new API. Lessons learned about reliability and monitoring.",
                "username": "bob_engineer",
                "name": "Bob Engineer",
                "followers": 12000,
                "hours_ago": 6,
            },
            {
                "id": "1003",
                "text": "What are people using for system reliability these days? Need recommendations.",
                "username": "charlie_cto",
                "name": "Charlie CTO",
                "followers": 25000,
                "hours_ago": 12,
            },
            {
                "id": "1004",
                "text": "Coffee and code. Perfect Friday.",
                "username": "dana_random",
                "name": "Dana Random",
                "followers": 500,
                "hours_ago": 1,
            },
            {
                "id": "1005",
                "text": "Our infrastructure keeps failing. Looking for better monitoring solutions for reliability.",
                "username": "eve_devops",
                "name": "Eve DevOps",
                "followers": 8000,
                "hours_ago": 18,
            },
        ]
        
        for template in post_templates:
            author = Author(
                id=hashlib.md5(template["username"].encode()).hexdigest()[:16],
                username=template["username"],
                name=template["name"],
                followers_count=template["followers"],
                verified=template["followers"] > 10000,
            )
            
            created_at = base_time - timedelta(hours=template["hours_ago"])
            
            post = Post(
                id=template["id"],
                text=template["text"],
                author=author,
                created_at=created_at.isoformat(),
                like_count=(int(template["id"]) * 3) % 100,
                reply_count=(int(template["id"]) * 2) % 50,
                repost_count=(int(template["id"])) % 30,
                quote_count=(int(template["id"])) % 20,
                conversation_id=template["id"],
                reply_settings="everyone",
            )
            
            posts.append(post)
        
        return posts
