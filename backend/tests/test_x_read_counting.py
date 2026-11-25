"""
Issue #17: X reads are counted as X bills them, per post object returned.

- A search post the adapter drops (its author is missing from
  includes.users) was returned and billed, so it counts.
- A lookup that finds the post gone (HTTP 404 or 200 with a post-gone error)
  returned no post object, so it counts 0 reads; a 200 with post data the
  adapter cannot convert counts 1.

X is stubbed with httpx.MockTransport using the X API v2 shapes the adapter
and the issue #9 tests already use. All data is synthetic.
"""

import dataclasses

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from backend.activities.x_activities import (
    CheckFollowupInput,
    LookupPostInput,
    SearchRecentInput,
    XActivities,
    x_billed_nothing,
)
from backend.adapters.http_x_client import HttpXClient
from backend.budget import get_today_budget
from backend.config import get_settings
from backend.database import (
    BudgetLedgerDB,
    BudgetReconciliationDB,
    BudgetReservationDB,
    get_db,
)
from backend.models import Author, Post
from backend.ports import SearchResult

POST_ID = "9001"
GONE_ERROR = {
    "value": POST_ID,
    "detail": f"Could not find tweet with id: [{POST_ID}].",
    "title": "Not Found Error",
    "resource_type": "tweet",
    "parameter": "id",
    "resource_id": POST_ID,
    "type": "https://api.twitter.com/2/problems/resource-not-found",
}


@pytest.fixture(autouse=True)
def empty_budget_tables():
    db = get_db()
    try:
        for model in (BudgetLedgerDB, BudgetReservationDB, BudgetReconciliationDB):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()


def _env(attempt: int = 1) -> ActivityEnvironment:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info,
        attempt=attempt,
        activity_id="7",
        workflow_id="review:synthetic",
        workflow_run_id="run-1",
    )
    return env


def _serve(monkeypatch, handler):
    """Route the adapter's httpx.AsyncClient through a MockTransport."""
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_async_client(*a, transport=httpx.MockTransport(handler), **kw),
    )


def _tweet(post_id: str, author_id: str) -> dict:
    return {
        "id": post_id,
        "text": f"Synthetic post {post_id} about reliability.",
        "author_id": author_id,
        "created_at": "2026-11-24T00:00:00.000Z",
        "conversation_id": post_id,
        "reply_settings": "everyone",
        "public_metrics": {"like_count": 0, "reply_count": 0, "retweet_count": 0, "quote_count": 0},
    }


def _user(author_id: str) -> dict:
    return {
        "id": author_id,
        "username": f"synthetic_{author_id}",
        "name": "Synthetic User",
        "verified": False,
        "public_metrics": {"followers_count": 10},
    }


# One post whose author is in includes.users, and two (one of them the
# newest) whose author is missing
SEARCH_BODY = {
    "data": [_tweet("9003", "missing_a"), _tweet("9002", "a1"), _tweet("9001", "missing_b")],
    "includes": {"users": [_user("a1")]},
    "meta": {"newest_id": "9003", "oldest_id": "9001", "result_count": 3},
}


def _reads() -> float:
    return get_today_budget()["x_reads"]


def _reconciliations():
    db = get_db()
    try:
        return db.query(BudgetReconciliationDB).filter(BudgetReconciliationDB.budget_type == "x_reads").all()
    finally:
        db.close()


# ---------- search: dropped posts are billed reads ----------


@pytest.mark.asyncio
async def test_http_search_counts_posts_dropped_for_a_missing_author(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(200, json=SEARCH_BODY, request=request))

    result = await HttpXClient("synthetic-token").search_recent("reliability", max_results=10)

    assert [p.id for p in result.posts] == ["9002"]
    assert result.billable_reads == 3
    # Unchanged cursor: the newest converted post, so the next poll can still
    # pick up the dropped 9003 (and counts it again)
    assert result.newest_id == "9002"


@pytest.mark.asyncio
async def test_search_post_without_author_id_fails_and_keeps_the_reservation(monkeypatch):
    """A post without author_id fails the search (unchanged), so the poll is
    retried rather than its cursor moving past the post; X may have billed
    the response, so the reservation stays counted."""
    tweet = _tweet("9004", "a1")
    del tweet["author_id"]
    body = {"data": [tweet, _tweet("9005", "a1")], "includes": {"users": [_user("a1")]}}
    _serve(monkeypatch, lambda request: httpx.Response(200, json=body, request=request))
    activities = XActivities(HttpXClient("synthetic-token"))

    with pytest.raises(KeyError):
        await _env().run(
            activities.search_recent,
            SearchRecentInput(query="reliability", max_results=10, idempotency_key="k"),
        )
    assert _reads() == 10


@pytest.mark.asyncio
async def test_http_search_without_data_bills_nothing(monkeypatch):
    body = {"meta": {"result_count": 0}}
    _serve(monkeypatch, lambda request: httpx.Response(200, json=body, request=request))

    result = await HttpXClient("synthetic-token").search_recent("reliability")

    assert result == SearchResult(posts=[], newest_id=None, billable_reads=0)


@pytest.mark.asyncio
async def test_search_activity_counts_the_dropped_posts(monkeypatch):
    """Acceptance: a search response with a post whose author is missing
    counts that post's read."""
    _serve(monkeypatch, lambda request: httpx.Response(200, json=SEARCH_BODY, request=request))
    activities = XActivities(HttpXClient("synthetic-token"))

    output = await _env().run(
        activities.search_recent,
        SearchRecentInput(query="reliability", max_results=10, idempotency_key="k"),
    )

    assert [p.id for p in output.posts] == ["9002"]
    assert output.newest_id == "9002"
    budget = get_today_budget()
    assert budget["x_reads"] == 3
    assert budget["x_cost_usd"] == pytest.approx(3 * get_settings().x_post_read_cost)
    assert output.cost_usd == pytest.approx(3 * get_settings().x_post_read_cost)
    (recon,) = _reconciliations()
    assert (recon.reserved_amount, recon.actual_amount) == (10, 3)


@pytest.mark.asyncio
async def test_check_followup_counts_the_dropped_posts(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(200, json=SEARCH_BODY, request=request))
    activities = XActivities(HttpXClient("synthetic-token"))

    replies = await _env().run(
        activities.check_followup,
        CheckFollowupInput(reply_post_id="8001", idempotency_key="k"),
    )

    assert [p.id for p in replies] == ["9002"]
    assert _reads() == 3


def test_search_result_cannot_bill_less_than_its_posts():
    posts = [
        Post(
            id=post_id,
            text="Synthetic post.",
            author=Author(id="a1", username="synthetic_user", name="Synthetic", followers_count=10, verified=False),
            created_at="2026-11-24T00:00:00+00:00",
            like_count=0, reply_count=0, repost_count=0, quote_count=0,
            conversation_id=post_id,
            reply_settings="everyone",
        )
        for post_id in ("9001", "9002")
    ]
    with pytest.raises(ValueError, match="billable_reads"):
        SearchResult(posts=posts, newest_id="9002", billable_reads=1)


# ---------- lookup: a gone post is not billed, unconvertible data is ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 200])
async def test_lookup_of_a_gone_post_counts_no_read(monkeypatch, status):
    """Acceptance: a 404 lookup counts 0 reads (and so does X's 200 with a
    post-gone error, issue #9). The activity still returns None, which makes
    the target re-fetch remove the post text."""
    body = {"errors": [GONE_ERROR]}
    _serve(monkeypatch, lambda request: httpx.Response(status, json=body, request=request))
    activities = XActivities(HttpXClient("synthetic-token"))

    result = await _env().run(activities.lookup_post, LookupPostInput(post_id=POST_ID, idempotency_key="k"))

    assert result is None
    budget = get_today_budget()
    assert budget["x_reads"] == 0
    assert budget["x_cost_usd"] == pytest.approx(0.0)
    (recon,) = _reconciliations()
    assert (recon.reserved_amount, recon.actual_amount) == (1, 0)


@pytest.mark.asyncio
async def test_unconvertible_lookup_counts_one_read_per_attempt(monkeypatch):
    """Acceptance: an unconvertible 200 (the post without its author
    expansion) counts 1 read. The lookup raises and removes nothing (issue #9),
    and each retried attempt is billed again (issue #8)."""
    body = {"data": _tweet(POST_ID, "missing_a")}
    _serve(monkeypatch, lambda request: httpx.Response(200, json=body, request=request))
    activities = XActivities(HttpXClient("synthetic-token"))
    arg = LookupPostInput(post_id=POST_ID, idempotency_key="k")

    for attempt in (1, 2):
        with pytest.raises(ApplicationError) as excinfo:
            await _env(attempt).run(activities.lookup_post, arg)
        assert excinfo.value.type == "MalformedLookupResponse"
        assert _reads() == attempt
    assert not x_billed_nothing(excinfo.value)


@pytest.mark.asyncio
async def test_found_lookup_counts_one_read(monkeypatch):
    body = {"data": _tweet(POST_ID, "a1"), "includes": {"users": [_user("a1")]}}
    _serve(monkeypatch, lambda request: httpx.Response(200, json=body, request=request))
    activities = XActivities(HttpXClient("synthetic-token"))

    post = await _env().run(activities.lookup_post, LookupPostInput(post_id=POST_ID, idempotency_key="k"))

    assert post.id == POST_ID
    assert _reads() == 1
    (recon,) = _reconciliations()
    assert (recon.reserved_amount, recon.actual_amount) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"errors": [{"title": "Invalid Request", "type": "https://api.twitter.com/2/problems/invalid-request"}]},
        # A not-found error about another resource
        {"errors": [dict(GONE_ERROR, resource_id="999", value="999")]},
    ],
)
async def test_lookup_200_without_any_post_is_released_and_removes_nothing(monkeypatch, body):
    """A 200 with neither the post nor a post-gone error for it returned no
    post object: it fails the lookup (no removal, issue #9) and its read is
    released."""
    _serve(monkeypatch, lambda request: httpx.Response(200, json=body, request=request))
    activities = XActivities(HttpXClient("synthetic-token"))

    with pytest.raises(ApplicationError) as excinfo:
        await _env().run(activities.lookup_post, LookupPostInput(post_id=POST_ID, idempotency_key="k"))

    assert excinfo.value.type == "LookupReturnedNoPost"
    assert not excinfo.value.non_retryable
    assert _reads() == 0


@pytest.mark.asyncio
async def test_404_not_naming_the_post_is_released(monkeypatch):
    """Unchanged from issue #8/#9: a 404 whose body does not name this post
    fails as the HTTP error and is released."""
    body = {"errors": [dict(GONE_ERROR, resource_id="999", value="999")]}
    _serve(monkeypatch, lambda request: httpx.Response(404, json=body, request=request))
    activities = XActivities(HttpXClient("synthetic-token"))

    with pytest.raises(httpx.HTTPStatusError):
        await _env().run(activities.lookup_post, LookupPostInput(post_id=POST_ID, idempotency_key="k"))
    assert _reads() == 0
