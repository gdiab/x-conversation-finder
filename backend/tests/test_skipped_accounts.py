"""Issue #48: skipped accounts. The search_recent activity adds `-from:`
operators within X's 512-character query limit and drops posts written by a
listed handle; the list is edited through the API and read at each search."""

import dataclasses
import json
import logging

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from backend import api
from backend.activities.x_activities import SearchRecentInput, XActivities
from backend.adapters.fake_x_client import FakeXClient
from backend.database import GlobalSettingsDB, get_db
from backend.models import Author, Post
from backend.ports import SearchResult
from backend.review_docs import stored_post_data
from backend.skipped_accounts import (
    MAX_SKIPPED_ACCOUNTS,
    SETTING_KEY,
    X_QUERY_MAX_CHARS,
    drop_skipped_authors,
    get_skipped_accounts,
    normalize_handles,
    query_with_skips,
    set_skipped_accounts,
)


def _post(post_id: str, username: str, text: str = "Synthetic post about webhook retries.") -> Post:
    return Post(
        id=post_id,
        text=text,
        author=Author(id=f"u{post_id}", username=username, name="Synthetic", followers_count=10),
        created_at="2026-11-27T00:00:00.000Z",
        like_count=0,
        reply_count=0,
        repost_count=0,
        quote_count=0,
        conversation_id=post_id,
    )


class RecordingXClient(FakeXClient):
    """Returns fixed posts whatever the query (so the fallback filter is what
    drops a listed author) and records each query it was sent."""

    def __init__(self, posts):
        super().__init__()
        self.fixed_posts = posts
        self.queries = []

    async def search_recent(self, query, since_id=None, max_results=10):
        self.queries.append(query)
        return SearchResult(
            posts=list(self.fixed_posts),
            newest_id=self.fixed_posts[0].id if self.fixed_posts else since_id,
            billable_reads=len(self.fixed_posts),
        )


def _env(activity_id: str = "1") -> ActivityEnvironment:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info, activity_id=activity_id, workflow_id="watch:synthetic_founder:webhooks", workflow_run_id="run-1",
    )
    return env


def _search(client, query="webhook retries", activity_id="1", **kwargs):
    return _env(activity_id).run(
        XActivities(client).search_recent,
        SearchRecentInput(query=query, idempotency_key="synthetic:webhooks:0", store_posts=True, **kwargs),
    )


# --- Handles ---

def test_handles_are_trimmed_lowercased_and_deduplicated_in_order():
    assert normalize_handles([" @Grok ", "", "openai", "GROK", "@ OpenAI_Dev"]) == ["grok", "openai", "openai_dev"]


@pytest.mark.parametrize("value", ["grok bot", "x.com/grok", "@", "a" * 16, "grök"])
def test_a_value_that_is_not_an_x_handle_is_refused(value):
    if value == "@":
        assert normalize_handles([value]) == []  # a lone "@" is a blank
        return
    with pytest.raises(ValueError, match="is not an X handle"):
        normalize_handles([value])


def test_the_list_has_a_maximum():
    with pytest.raises(ValueError, match=f"At most {MAX_SKIPPED_ACCOUNTS}"):
        normalize_handles([f"user{i}" for i in range(MAX_SKIPPED_ACCOUNTS + 1)])


# --- Query ---

def test_no_handles_leave_the_query_unchanged():
    assert query_with_skips("webhook retries", []) == ("webhook retries", [])


def test_each_handle_becomes_a_from_operator_after_the_parenthesized_query():
    # Parentheses keep an OR in the founder's query from binding to the operator
    assert query_with_skips("webhooks OR retries", ["grok", "openai"]) == (
        "(webhooks OR retries) -from:grok -from:openai",
        [],
    )


def test_operators_are_added_only_while_the_query_fits_in_512_characters():
    handles = [f"handle_{i:08d}" for i in range(60)]  # 15 characters each; about 22 fit
    query, left_out = query_with_skips("webhook retries", handles)

    assert len(query) <= X_QUERY_MAX_CHARS
    added = [h for h in handles if f"-from:{h}" in query]
    assert added and left_out
    assert added + left_out == handles  # every handle is added or left out
    assert len(query) + len(f" -from:{left_out[0]}") > X_QUERY_MAX_CHARS


def test_a_shorter_handle_after_one_that_did_not_fit_is_still_added():
    base = "w" * (X_QUERY_MAX_CHARS - 2 - len(" -from:abc"))  # room for exactly " -from:abc"
    query, left_out = query_with_skips(base, ["longer_handle", "abc"])

    assert query == f"({base}) -from:abc"
    assert len(query) == X_QUERY_MAX_CHARS
    assert left_out == ["longer_handle"]


def test_a_query_with_no_room_for_any_operator_is_sent_unchanged():
    base = "w" * (X_QUERY_MAX_CHARS - 5)
    assert query_with_skips(base, ["grok"]) == (base, ["grok"])


# --- Fallback filter ---

def test_posts_written_by_a_listed_handle_are_dropped_case_insensitively():
    posts = [_post("3", "Grok"), _post("2", "synthetic_user"), _post("1", "GROK")]
    assert [p.id for p in drop_skipped_authors(posts, ["grok"])] == ["2"]


def test_a_post_that_mentions_a_listed_handle_is_kept():
    mention = _post("4", "synthetic_user", text="@grok is this true about webhook retries?")
    assert drop_skipped_authors([mention], ["grok"]) == [mention]


# --- Stored list ---

def test_the_list_starts_empty_and_is_stored_normalized():
    assert get_skipped_accounts() == []
    assert set_skipped_accounts(["@Grok", "grok"]) == ["grok"]
    assert get_skipped_accounts() == ["grok"]
    assert set_skipped_accounts([]) == []
    assert get_skipped_accounts() == []


def test_an_unreadable_stored_list_skips_no_accounts(caplog):
    set_skipped_accounts(["grok"])
    db = get_db()
    try:
        db.query(GlobalSettingsDB).filter(GlobalSettingsDB.key == SETTING_KEY).one().value = "{not json"
        db.commit()
    finally:
        db.close()

    with caplog.at_level(logging.ERROR):
        assert get_skipped_accounts() == []
    assert "unreadable" in caplog.text


# --- search_recent activity ---

@pytest.mark.asyncio
async def test_search_sends_the_from_operators_and_drops_listed_authors():
    set_skipped_accounts(["grok"])
    mention = _post("9003", "synthetic_user", text="@grok is this true about webhook retries?")
    client = RecordingXClient([mention, _post("9002", "Grok"), _post("9001", "other_user")])

    result = await _search(client)

    assert client.queries == ["(webhook retries) -from:grok"]
    assert result.post_ids == ["9003", "9001"]  # the mention is kept
    db = get_db()
    try:
        assert stored_post_data(db, "9002") is None  # the skipped post is not stored
    finally:
        db.close()
    assert result.newest_id == "9003"


@pytest.mark.asyncio
async def test_since_id_advances_past_a_result_of_only_skipped_posts():
    set_skipped_accounts(["grok"])
    result = await _search(RecordingXClient([_post("9005", "grok")]))

    assert result.post_ids == []
    assert result.newest_id == "9005"


@pytest.mark.asyncio
async def test_an_edit_applies_at_the_next_search():
    client = RecordingXClient([_post("9001", "grok")])

    first = await _search(client)
    set_skipped_accounts(["grok"])
    second = await _search(client, activity_id="2")  # the next iteration's search

    assert client.queries == ["webhook retries", "(webhook retries) -from:grok"]
    assert first.post_ids == ["9001"]
    assert second.post_ids == []


@pytest.mark.asyncio
async def test_handles_left_out_of_the_query_are_logged_and_still_filtered(caplog):
    handles = [f"handle_{i:08d}" for i in range(60)]
    set_skipped_accounts(handles)
    last = handles[-1]
    client = RecordingXClient([_post("9002", last.upper()), _post("9001", "other_user")])

    with caplog.at_level(logging.WARNING):
        result = await _search(client)

    assert len(client.queries[0]) <= X_QUERY_MAX_CHARS
    assert f"-from:{last}" not in client.queries[0]
    assert "left out of the X query" in caplog.text and last in caplog.text
    assert result.post_ids == ["9001"]


@pytest.mark.asyncio
async def test_the_fake_client_honors_from_operators_as_x_does():
    """The e2e adapter must not treat `(webhook` or `-from:grok` as search terms."""
    client = FakeXClient()
    client.posts_database = [
        _post("9002", "grok", text="webhook retries explained"),
        _post("9001", "synthetic_user", text="our webhook retries keep failing"),
    ]
    set_skipped_accounts(["grok"])

    result = await _search(client)

    assert result.post_ids == ["9001"]


# --- API ---

@pytest.fixture
def http():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test")


@pytest.mark.asyncio
async def test_api_reads_and_replaces_the_list(http):
    async with http:
        assert (await http.get("/api/settings/skipped-accounts")).json() == {"handles": []}

        saved = await http.put("/api/settings/skipped-accounts", json={"handles": ["@Grok", " openai "]})
        assert saved.status_code == 200
        assert saved.json() == {"handles": ["grok", "openai"]}
        assert (await http.get("/api/settings/skipped-accounts")).json() == {"handles": ["grok", "openai"]}

    assert json.loads(_stored_value()) == ["grok", "openai"]


@pytest.mark.asyncio
async def test_api_refuses_a_value_that_is_not_a_handle_and_keeps_the_list(http):
    set_skipped_accounts(["grok"])
    async with http:
        refused = await http.put("/api/settings/skipped-accounts", json={"handles": ["grok", "not a handle"]})

    assert refused.status_code == 422
    assert refused.json()["code"] == "invalid_request"
    assert "is not an X handle" in refused.json()["errors"][0]["msg"]
    assert get_skipped_accounts() == ["grok"]


def _stored_value() -> str:
    db = get_db()
    try:
        return db.query(GlobalSettingsDB).filter(GlobalSettingsDB.key == SETTING_KEY).one().value
    finally:
        db.close()
