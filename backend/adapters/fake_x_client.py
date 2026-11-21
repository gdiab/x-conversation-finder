"""
Fake X API client for testing.

No real API calls, returns synthetic data.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
import hashlib
import json
from pathlib import Path

from backend.models import Post, Author
from backend.ports import XClientPort


class FakeXClient(XClientPort):
    """Fake X client for testing and CI."""
    
    # Explicit reply-to-parent mapping (replaces formula-based derivation)
    # Maps reply post IDs to their conversation/parent IDs
    REPLY_TO_PARENT_MAP = {
        # Test compatibility (used by test_review_workflow.py)
        "9999": "1001",
        # E2E test (used by scripts/e2e-review-direct.sh)
        "99999": "e2e_9999",
        # 10000-19999 range: replies to 1001-1009
        "10001": "1001", "10002": "1002", "10003": "1003", "10004": "1004", "10005": "1005",
        "10006": "1006", "10007": "1007", "10008": "1008", "10009": "1009",
        # 20000-29999 range: replies to 2001-2009
        "20001": "2001", "20002": "2002", "20003": "2003", "20004": "2004", "20005": "2005",
        # Add more as needed for specific test cases
    }
    
    def __init__(self, e2e_fixtures_path: Optional[str] = None):
        self.posts_database = self._generate_fake_posts()
        self.call_count = 0
        if e2e_fixtures_path:
            self.load_e2e_fixtures(e2e_fixtures_path)
    
    def load_e2e_fixtures(self, fixtures_path: str):
        """Load E2E-specific fixtures (followup posts, reply mappings) from JSON file."""
        path = Path(fixtures_path)
        if not path.exists():
            raise FileNotFoundError(f"E2E fixtures not found: {fixtures_path}")
        
        with open(path, 'r') as f:
            fixtures = json.load(f)
        
        # Load synthetic followup posts
        for post_data in fixtures.get("synthetic_followup_posts", []):
            author = Author(
                id=post_data["author"]["id"],
                username=post_data["author"]["username"],
                name=post_data["author"]["name"],
                followers_count=0,
                verified=False,
            )
            post = Post(
                id=post_data["id"],
                text=post_data["text"],
                author=author,
                created_at=post_data["created_at"],
                like_count=0,
                reply_count=0,
                repost_count=0,
                quote_count=0,
                conversation_id=post_data["conversation_id"],
                in_reply_to_user_id="founder",
                reply_settings="everyone",
            )
            self.posts_database.append(post)
        
        # Load reply-to-parent mappings if present
        for reply_id, parent_id in fixtures.get("reply_to_parent_map", {}).items():
            self.REPLY_TO_PARENT_MAP[reply_id] = parent_id
    
    async def search_recent(
        self,
        query: str,
        since_id: Optional[str] = None,
        max_results: int = 10,
    ) -> Tuple[List[Post], Optional[str]]:
        """
        Fake search returning synthetic posts.
        
        Supports conversation_id: query syntax for testing followup replies.
        Returns (posts, newest_id)
        """
        self.call_count += 1
        
        # Handle conversation_id:X query syntax
        if query.startswith("conversation_id:"):
            conversation_id = query.split(":", 1)[1].strip()
            matching = [
                p for p in self.posts_database
                if p.conversation_id == conversation_id
            ]
        else:
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
        
        # If not found, generate a synthetic reply post for testing
        # Use explicit mapping from REPLY_TO_PARENT_MAP instead of formula
        conversation_id = self.REPLY_TO_PARENT_MAP.get(post_id, post_id)
        
        return Post(
            id=post_id,
            text=f"This is a synthetic reply post {post_id}",
            author=Author(
                id="author_fake",
                username="fake_replier",
                name="Fake Replier",
                followers_count=100,
                verified=False,
            ),
            created_at=datetime.now(timezone.utc).isoformat(),
            like_count=0,
            reply_count=0,
            repost_count=0,
            quote_count=0,
            conversation_id=conversation_id,
            in_reply_to_user_id="original_author",
            reply_settings="everyone",
            url=f"https://x.com/fake_replier/status/{post_id}",
        )
    
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
