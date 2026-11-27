"""Issue #49: the reach score (backend/reach.py, rule in backend/config/reach.py).

Reach is separate from confidence: it never changes confidence or which
posts get a card. It is computed from the stored post metrics by store_card
and by the startup backfill, stored with its inputs and REACH_RULE_VERSION
in review_cards.reach, and the API adds the highlight flag and the pending
queue's sort key. Synthetic data only.
"""

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from backend import reach as reach_mod
from backend.activities.store_activities import (
    DeletePostTextInput,
    StoreCardInput,
    _delete_post_text_sync,
    _store_card_sync,
    backfill_reach,
    get_all_cards,
    get_stored_card,
)
from backend.config import confidence as confidence_config
from backend.config.reach import (
    REACH_HIGHLIGHT_LEVEL,
    REACH_RULE_VERSION,
    REACH_SORT_WEIGHT,
    REACH_WEIGHTS,
)
from backend.models import Author, Evidence, ReviewCard
from backend.reach import compute_reach, queue_priority, reach_highlighted


def _post_data(likes=0, replies=0, reposts=0, quotes=0, followers=0, **extra):
    return {
        "id": "p1",
        "text": "Synthetic post.",
        "author": {"id": "a1", "username": "synthetic_author", "name": "Synthetic", "followers_count": followers},
        "like_count": likes,
        "reply_count": replies,
        "repost_count": reposts,
        "quote_count": quotes,
        **extra,
    }


# --- the formula -------------------------------------------------------------


def test_the_rule_is_dated_and_its_weights_sum_to_one():
    assert len(REACH_RULE_VERSION) == 10 and REACH_RULE_VERSION[4] == "-"
    assert sum(REACH_WEIGHTS.values()) == pytest.approx(1.0)
    assert 0 < REACH_HIGHLIGHT_LEVEL < 1 and 0 < REACH_SORT_WEIGHT < 1


def test_zero_counts_and_no_followers_score_zero():
    reach = compute_reach(_post_data())
    assert reach["score"] == 0.0
    assert reach["engagement"] == 0
    assert reach["engagement_score"] == 0.0 and reach["audience_score"] == 0.0


def test_the_record_holds_the_inputs_and_the_rule_version():
    reach = compute_reach(_post_data(likes=120, replies=23, reposts=3, quotes=0, followers=119_098))
    assert reach["inputs"] == {
        "like_count": 120, "reply_count": 23, "repost_count": 3, "quote_count": 0, "followers_count": 119_098,
    }
    assert reach["engagement"] == 120 + 2 * (23 + 3 + 0)
    assert reach["rule_version"] == REACH_RULE_VERSION
    assert reach["score"] == pytest.approx(
        REACH_WEIGHTS["engagement_score"] * reach["engagement_score"]
        + REACH_WEIGHTS["audience_score"] * reach["audience_score"],
        abs=1e-4,
    )


def test_a_huge_account_with_no_engagement_scores_only_the_audience_weight():
    reach = compute_reach(_post_data(followers=9_454_430))
    assert reach["audience_score"] == 1.0
    assert reach["score"] == pytest.approx(REACH_WEIGHTS["audience_score"])
    assert not reach_highlighted(reach)


def test_a_huge_account_with_a_few_likes_is_not_highlighted():
    # A 9.4M-follower account's reply with 3 likes (seen on the real cards)
    assert not reach_highlighted(compute_reach(_post_data(likes=3, followers=9_451_657)))


def test_counts_and_followers_beyond_full_are_capped_at_one():
    reach = compute_reach(_post_data(likes=10**9, replies=10**6, followers=10**10))
    assert reach["engagement_score"] == 1.0 and reach["audience_score"] == 1.0
    assert reach["score"] == 1.0


def test_followers_at_or_below_the_floor_count_as_no_audience():
    assert compute_reach(_post_data(followers=1))["audience_score"] == 0.0
    assert compute_reach(_post_data(followers=100))["audience_score"] == 0.0
    assert compute_reach(_post_data(followers=101))["audience_score"] > 0.0


def test_a_reply_or_repost_counts_more_than_a_like():
    like = compute_reach(_post_data(likes=1))["score"]
    for kind in ("replies", "reposts", "quotes"):
        assert compute_reach(_post_data(**{kind: 1}))["score"] > like


def test_reach_rises_with_each_input():
    base = compute_reach(_post_data(likes=2, replies=1, followers=400))["score"]
    for change in ({"likes": 3}, {"replies": 2}, {"reposts": 1}, {"quotes": 1}, {"followers": 4000}):
        args = {"likes": 2, "replies": 1, "followers": 400, **change}
        assert compute_reach(_post_data(**args))["score"] > base, change


def test_a_viral_post_from_a_small_account_is_highlighted():
    assert reach_highlighted(compute_reach(_post_data(likes=150, followers=50)))


def test_the_busiest_real_shaped_card_is_highlighted_and_a_typical_one_is_not():
    assert reach_highlighted(compute_reach(_post_data(likes=120, replies=23, reposts=3, followers=119_098)))
    assert not reach_highlighted(compute_reach(_post_data(likes=2, replies=4, followers=2109)))


@pytest.mark.parametrize(
    "post_data",
    [
        None,
        "not a dict",
        {k: v for k, v in _post_data().items() if k != "repost_count"},
        {k: v for k, v in _post_data().items() if k != "author"},
        _post_data(likes=None),
        _post_data(likes=-1),
        _post_data(likes=True),
        _post_data(likes=1.5),
        _post_data(likes="12"),
        {**_post_data(), "author": {"id": "a1"}},
        {**_post_data(), "author": "synthetic_author"},
    ],
)
def test_missing_or_malformed_metrics_give_no_reach(post_data):
    assert compute_reach(post_data) is None


def test_a_removed_post_has_no_reach():
    # issue #35: the follower count belongs to the removed author
    assert compute_reach(_post_data(likes=50, followers=10_000, text_removed=True)) is None


def test_the_highlight_level_is_inclusive():
    assert reach_highlighted({"score": REACH_HIGHLIGHT_LEVEL})
    assert not reach_highlighted({"score": REACH_HIGHLIGHT_LEVEL - 0.0001})
    for missing in (None, {}, {"score": None}, {"score": "0.9"}, {"score": True}, {"score": float("nan")}):
        assert not reach_highlighted(missing)


@pytest.mark.parametrize(
    "likes,replies,reposts,quotes,followers,engagement_score,audience_score,score",
    [
        # Values worked by hand from the documented formula:
        # engagement_score = log10(1 + e) / log10(101), audience = (log10(f) - 2) / 4
        (9, 0, 0, 0, 0, 0.4989, 0.0, 0.3243),        # e=9: log10(10)/log10(101)
        (0, 0, 0, 0, 10_000, 0.0, 0.5, 0.175),       # log10(1e4) = 4
        (0, 0, 0, 0, 1_000, 0.0, 0.25, 0.0875),
        (100, 0, 0, 0, 1_000_000, 1.0, 1.0, 1.0),
        (2, 4, 0, 0, 2109, 0.5195, 0.3311, 0.4536),  # a pending card on 2026-11-27
        (18, 0, 0, 0, 15_000, 0.6380, 0.5440, 0.6051),  # just above the highlight level
        (17, 0, 0, 0, 15_000, 0.6263, 0.5440, 0.5975),  # just below it
    ],
)
def test_known_values_of_the_formula(likes, replies, reposts, quotes, followers, engagement_score, audience_score, score):
    reach = compute_reach(_post_data(likes, replies, reposts, quotes, followers))
    assert reach["engagement_score"] == pytest.approx(engagement_score, abs=1e-4)
    assert reach["audience_score"] == pytest.approx(audience_score, abs=1e-4)
    assert reach["score"] == pytest.approx(score, abs=2e-4)


def test_the_highlight_boundary_of_the_known_values():
    assert reach_highlighted(compute_reach(_post_data(likes=18, followers=15_000)))
    assert not reach_highlighted(compute_reach(_post_data(likes=17, followers=15_000)))


# --- the queue order ---------------------------------------------------------


def test_queue_priority_is_confidence_plus_weighted_reach():
    assert queue_priority(0.7, {"score": 0.5}) == pytest.approx(0.7 + REACH_SORT_WEIGHT * 0.5)


def test_a_card_without_reach_sorts_by_confidence_alone():
    assert queue_priority(0.7, None) == 0.7
    assert queue_priority(0.7, {"score": None}) == 0.7
    assert queue_priority(None, None) == 0.0


def test_scores_one_step_apart_do_not_tie():
    # 15,000 and 15,020 followers store 0.1904 and 0.1905
    low = compute_reach(_post_data(followers=15_000))
    high = compute_reach(_post_data(followers=15_020))
    assert low["score"] < high["score"]
    assert queue_priority(0.7, low) < queue_priority(0.7, high)


def test_reach_breaks_near_ties_but_cannot_overturn_a_large_confidence_gap():
    far_reaching = queue_priority(0.74, {"score": 0.92})
    assert far_reaching > queue_priority(0.76, {"score": 0.21})
    assert far_reaching > queue_priority(0.85, None)
    # Full reach is worth at most REACH_SORT_WEIGHT of confidence
    assert queue_priority(0.6, {"score": 1.0}) < queue_priority(0.81, {"score": 0.0})


# --- confidence is unchanged -------------------------------------------------


def test_confidence_does_not_read_reach():
    """Reach never enters the confidence rule (issue #49 decision)."""
    assert confidence_config.CONFIDENCE_RULE_VERSION == "2026-11-24"
    assert set(confidence_config.CONFIDENCE_WEIGHTS) == {
        "kind_score", "asks_question", "brief_overlap", "author_reach", "direct_post",
    }
    evidence = Evidence(
        post_id="p1", matched_topic="t", why_it_fits="w", confidence_score=0.0,
        confidence_inputs={
            "lookup_succeeded": 1.0, "replies_open": 1.0, "has_founder_material": 1.0, "freshness": 1.0,
            "kind_score": 1.0, "asks_question": 1.0, "brief_overlap": 1.0, "author_reach": 0.5,
            "direct_post": 1.0, "reach": 1.0, "engagement_score": 1.0,
        },
        founder_materials=[],
    )
    without_extras = replace(evidence, confidence_inputs={
        k: v for k, v in evidence.confidence_inputs.items() if k not in ("reach", "engagement_score")
    })
    assert evidence.compute_confidence() == without_extras.compute_confidence()


# --- storage and the API -----------------------------------------------------


def _card(sample_post, workflow_id="review:test:reach", confidence=0.7, **post_changes):
    post = replace(sample_post, **post_changes) if post_changes else sample_post
    evidence = Evidence(
        post_id=post.id, matched_topic="synthetic topic", why_it_fits="synthetic reason",
        confidence_score=confidence, confidence_inputs={"author_reach": 0.6}, founder_materials=[],
    )
    return ReviewCard(
        workflow_id=workflow_id, post=post, evidence=evidence, status="pending",
        created_at="2026-11-27T00:00:00Z", updated_at="2026-11-27T00:00:00Z",
    )


def _store(card):
    assert _store_card_sync(StoreCardInput(card=card, idempotency_key=f"{card.workflow_id}:store"))


def test_store_card_stores_reach_and_leaves_confidence_alone(sample_post):
    card = _card(sample_post)
    _store(card)

    stored = get_stored_card(card.workflow_id)
    expected = compute_reach(stored["post"])
    assert stored["reach"] == expected
    assert stored["reach"]["inputs"]["followers_count"] == sample_post.author.followers_count
    assert stored["reach"]["rule_version"] == REACH_RULE_VERSION
    assert stored["evidence"]["confidence_score"] == 0.7
    assert stored["evidence"]["confidence_inputs"] == {"author_reach": 0.6}
    assert stored["reach_highlighted"] is reach_highlighted(expected)
    assert stored["queue_priority"] == queue_priority(0.7, expected)


def test_a_restore_recomputes_reach_from_the_card_post(sample_post):
    _store(_card(sample_post))
    _store(_card(sample_post, like_count=500, reply_count=40, author=Author(
        id="author_001", username="alice_dev", name="Alice Developer", followers_count=200_000,
    )))
    stored = get_stored_card("review:test:reach")
    assert stored["reach"]["inputs"]["like_count"] == 500
    assert stored["reach_highlighted"] is True


def test_list_reviews_returns_reach_fields_on_every_card(sample_post):
    from backend.api import list_reviews

    _store(_card(sample_post, workflow_id="review:test:low", confidence=0.76, like_count=0,
                 reply_count=1, repost_count=0, quote_count=0,
                 author=replace(sample_post.author, followers_count=463)))
    _store(_card(sample_post, workflow_id="review:test:high", confidence=0.74, like_count=120,
                 reply_count=23, repost_count=3, quote_count=0,
                 author=replace(sample_post.author, followers_count=119_098)))

    result = asyncio.run(list_reviews())
    cards = {c["workflow_id"]: c for c in result["cards"]}
    assert cards["review:test:high"]["reach_highlighted"] is True
    assert cards["review:test:low"]["reach_highlighted"] is False
    for card in cards.values():
        assert set(card["reach"]) == {
            "score", "inputs", "engagement", "engagement_score", "audience_score", "rule_version",
        }
    # The far-reaching card leads the queue although slightly less confident
    order = sorted(cards.values(), key=lambda c: c["queue_priority"], reverse=True)
    assert [c["workflow_id"] for c in order] == ["review:test:high", "review:test:low"]


def test_a_removal_drops_the_stored_reach(sample_post):
    card = _card(sample_post)
    _store(card)
    assert get_stored_card(card.workflow_id)["reach"] is not None

    assert _delete_post_text_sync(DeletePostTextInput(
        workflow_id=card.workflow_id, idempotency_key="remove", post_id=card.post.id,
    ))

    from backend.database import ReviewCardDB, get_db

    db = get_db()
    try:
        assert db.get(ReviewCardDB, card.workflow_id).reach is None
    finally:
        db.close()
    stored = get_stored_card(card.workflow_id)
    assert stored["reach"] is None and stored["reach_highlighted"] is False
    assert stored["queue_priority"] == 0.7
    # A late store of the same card (a retried attempt) does not put it back
    _store(card)
    assert get_stored_card(card.workflow_id)["reach"] is None


def test_a_card_read_before_the_removal_is_returned_without_reach(sample_post):
    """GET /api/reviews/{id} can read the stored card just before the worker
    records the removal; apply_stored_removal then redacts that earlier copy."""
    from backend.activities.store_activities import apply_stored_removal

    card = _card(sample_post)
    _store(card)
    read_before = get_stored_card(card.workflow_id)
    assert read_before["reach"]["inputs"]["followers_count"] == 5000

    _delete_post_text_sync(DeletePostTextInput(workflow_id=card.workflow_id, idempotency_key="r", post_id=card.post.id))

    returned = apply_stored_removal(card.workflow_id, read_before)
    assert returned["post"]["text_removed"] is True
    assert returned["reach"] is None and returned["reach_highlighted"] is False
    assert returned["queue_priority"] == 0.7
    assert "followers_count" not in str(returned)


def test_redact_removed_card_leaves_a_card_without_reach_fields_alone():
    """The workflow's get_card query has no reach fields; it stays that way."""
    from backend.activities.store_activities import redact_removed_card

    redacted = redact_removed_card({"post": _post_data(), "evidence": {"confidence_score": 0.7}, "skip_reason": None})
    assert not {"reach", "reach_highlighted", "queue_priority"} & set(redacted)


def test_the_backfill_waits_for_a_removal_holding_the_write_lock(test_db_isolation):
    """A removal that holds SQLite's write lock commits first; the backfill
    then sees the removed post and stores no follower count."""
    import sqlite3
    import threading
    from backend.database import ReviewCardDB, get_db

    _insert_legacy_row("legacy:race", _post_data(likes=40, followers=27_000))
    remover = sqlite3.connect(test_db_isolation, timeout=10)
    remover.execute("BEGIN IMMEDIATE")
    remover.execute(
        "UPDATE review_cards SET post_data = json_set(post_data, '$.text_removed', json('true')) "
        "WHERE workflow_id = 'legacy:race'"
    )
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("scored", backfill_reach()))
    worker.start()
    worker.join(0.5)
    assert worker.is_alive(), "the backfill must wait for the removal's lock"
    remover.commit()
    remover.close()
    worker.join(10)

    assert result["scored"] == 0
    db = get_db()
    try:
        assert db.get(ReviewCardDB, "legacy:race").reach is None
    finally:
        db.close()


def _insert_legacy_row(workflow_id, post_data, confidence=0.6):
    from backend.database import ReviewCardDB, get_db

    db = get_db()
    try:
        db.add(ReviewCardDB(
            workflow_id=workflow_id, post_data=post_data,
            evidence_data={"confidence_score": confidence, "confidence_inputs": {}},
            status="pending", created_at="2026-11-20T00:00:00Z", updated_at="2026-11-20T00:00:00Z",
        ))
        db.commit()
    finally:
        db.close()


def test_worker_start_backfills_cards_stored_before_reach_existed():
    from backend.database import init_db
    from backend.worker import prepare_database

    _insert_legacy_row("legacy:scored", _post_data(likes=40, replies=5, followers=27_000))
    _insert_legacy_row("legacy:no_metrics", {"id": "p2", "text": "Synthetic.", "author": {"followers_count": 10}})
    _insert_legacy_row("legacy:removed", {"id": "p3", "text": "[removed]", "text_removed": True,
                                          "like_count": 9, "reply_count": 0, "repost_count": 0, "quote_count": 0})

    # API start applies the removal rules but scores nothing: an older worker
    # still running would not clear a score when it removes a post
    init_db()
    assert get_stored_card("legacy:scored")["reach"] is None

    assert prepare_database() == 1

    scored = get_stored_card("legacy:scored")
    assert scored["reach"] == compute_reach(_post_data(likes=40, replies=5, followers=27_000))
    for workflow_id in ("legacy:no_metrics", "legacy:removed"):
        card = get_stored_card(workflow_id)
        assert card["reach"] is None and card["reach_highlighted"] is False
        assert card["queue_priority"] == 0.6
    # Idempotent: nothing left to score
    assert backfill_reach() == 0


def test_the_backfill_keeps_an_existing_reach_record():
    from backend.database import ReviewCardDB, get_db

    _insert_legacy_row("legacy:older_rule", _post_data(likes=40, followers=27_000))
    db = get_db()
    try:
        row = db.get(ReviewCardDB, "legacy:older_rule")
        row.reach = {"score": 0.1, "rule_version": "2000-01-01"}
        db.commit()
    finally:
        db.close()
    assert backfill_reach() == 0
    assert get_stored_card("legacy:older_rule")["reach"]["rule_version"] == "2000-01-01"


def test_a_row_holding_reach_on_a_removed_post_is_not_returned():
    from backend.database import ReviewCardDB, get_db

    _insert_legacy_row("legacy:stale", {**_post_data(), "text_removed": True})
    db = get_db()
    try:
        db.get(ReviewCardDB, "legacy:stale").reach = {"score": 0.9}
        db.commit()
    finally:
        db.close()
    card = get_stored_card("legacy:stale")
    assert card["reach"] is None and card["reach_highlighted"] is False
    # The startup removal rules clear it from the row too
    from backend.activities.store_activities import redact_recorded_removals

    assert redact_recorded_removals().changed == 1
    db = get_db()
    try:
        assert db.get(ReviewCardDB, "legacy:stale").reach is None
    finally:
        db.close()


def test_the_single_card_response_carries_the_same_reach_fields_as_the_list(sample_post):
    """GET /api/reviews/{id} for a text-free Review: text_free_card fills the
    card from the stored row, then apply_stored_removal (backend/api.py)."""
    from backend.activities.store_activities import apply_stored_removal, text_free_card

    card = _card(sample_post)
    _store(card)
    _insert_legacy_row("legacy:no_metrics", {"id": "p9", "text": "Synthetic.", "author": {"followers_count": 10}})
    listed = {c["workflow_id"]: c for c in get_all_cards()}

    for workflow_id in (card.workflow_id, "legacy:no_metrics"):
        state = {"text_free": True, "post_id": "x", "workflow_id": workflow_id, "status": "pending"}
        single = apply_stored_removal(workflow_id, text_free_card(workflow_id, state))
        for key in ("reach", "reach_highlighted", "queue_priority"):
            assert single[key] == listed[workflow_id][key], (workflow_id, key)
    assert listed["legacy:no_metrics"]["reach"] is None

    # A removal recorded between the stored-card read and the response
    state = {"text_free": True, "post_id": "x", "workflow_id": card.workflow_id, "status": "pending"}
    read = text_free_card(card.workflow_id, state)
    _delete_post_text_sync(DeletePostTextInput(workflow_id=card.workflow_id, idempotency_key="r", post_id=card.post.id))
    single = apply_stored_removal(card.workflow_id, read)
    assert single["reach"] is None and single["reach_highlighted"] is False
    assert single["queue_priority"] == 0.7


# --- the migration -----------------------------------------------------------


def _old_schema_db(tmp_path):
    path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE review_cards (workflow_id VARCHAR PRIMARY KEY, post_data JSON NOT NULL, "
            "evidence_data JSON NOT NULL, status VARCHAR NOT NULL, created_at VARCHAR NOT NULL, "
            "updated_at VARCHAR NOT NULL, approved_by VARCHAR, posted_url VARCHAR, reply_post_id VARCHAR, "
            "skip_reason VARCHAR, followup_replies JSON, edited_text_gate_results JSON, "
            "edited_text_gate_state VARCHAR, post_anyway BOOLEAN, post_anyway_failing_sentences JSON, "
            "edit_seq INTEGER DEFAULT 0)"
        ))
        conn.commit()
    engine.dispose()
    return path


def _columns(engine):
    with engine.connect() as conn:
        return [row[1] for row in conn.execute(text("PRAGMA table_info(review_cards)"))]


def test_an_existing_table_gets_the_reach_column_on_first_engine_use(tmp_path, monkeypatch):
    """The worker never calls init_db, so get_engine adds the column."""
    import backend.database as database
    from backend.config import get_settings

    path = _old_schema_db(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    get_settings.cache_clear()
    database._engine = None
    database._SessionLocal = None

    engine = database.get_engine()
    assert "reach" in _columns(engine)
    database._migrate_add_reach(engine)  # idempotent
    assert _columns(engine).count("reach") == 1


def test_the_migration_tolerates_losing_the_race_to_another_process(tmp_path):
    import backend.database as database

    engine = create_engine(f"sqlite:///{_old_schema_db(tmp_path)}")
    real_connect = engine.connect
    calls = {"n": 0}

    class RacingConnection:
        """Another process adds the column between the check and the ALTER."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._conn.__exit__(*exc)

        def execute(self, statement, *args, **kwargs):
            if str(statement).startswith("ALTER TABLE") and calls["n"] == 0:
                calls["n"] += 1
                with real_connect() as other:
                    other.execute(text("ALTER TABLE review_cards ADD COLUMN reach JSON"))
                    other.commit()
            return self._conn.execute(statement, *args, **kwargs)

    engine.connect = lambda: RacingConnection(real_connect().__enter__())
    database._migrate_add_reach(engine)
    engine.connect = real_connect
    assert calls["n"] == 1
    assert _columns(engine).count("reach") == 1


def test_the_migration_reraises_an_error_that_is_not_the_race(tmp_path):
    import backend.database as database

    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    with engine.connect() as conn:  # no review_cards table at all
        conn.execute(text("CREATE TABLE other (x INTEGER)"))
        conn.commit()
    with pytest.raises(OperationalError):
        database._migrate_add_reach(engine)


@pytest.mark.parametrize("first", ["api", "worker"])
def test_upgrading_a_populated_database_in_either_start_order(tmp_path, monkeypatch, first):
    """A database written before issue #49 (no reach column, real cards, one
    removed): after the API (init_db) and the worker (prepare_database) both
    start, in either order, the live card is scored and the removed one is not."""
    import json
    import backend.database as database
    from backend.config import get_settings
    from backend.database import init_db
    from backend.worker import prepare_database

    path = _old_schema_db(tmp_path)
    engine = create_engine(f"sqlite:///{path}")
    live = _post_data(likes=120, replies=23, reposts=3, followers=119_098)
    removed = {"id": "p3", "text": "[removed]", "text_removed": True,
               "like_count": 9, "reply_count": 0, "repost_count": 0, "quote_count": 0}
    with engine.connect() as conn:
        for workflow_id, post in (("old:live", live), ("old:removed", removed)):
            conn.execute(
                text("INSERT INTO review_cards (workflow_id, post_data, evidence_data, status, created_at, updated_at) "
                     "VALUES (:w, :p, :e, 'pending', 't', 't')"),
                {"w": workflow_id, "p": json.dumps(post), "e": json.dumps({"confidence_score": 0.74})},
            )
        conn.commit()
    engine.dispose()

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    get_settings.cache_clear()

    def start(process):
        # Each process has its own engine
        if database._engine is not None:
            database._engine.dispose()
        database._engine = None
        database._SessionLocal = None
        init_db() if process == "api" else prepare_database()

    for process in ((first, "worker" if first == "api" else "api")):
        start(process)

    scored = get_stored_card("old:live")
    assert scored["reach"] == compute_reach(live)
    assert scored["reach_highlighted"] is True
    assert scored["queue_priority"] == pytest.approx(0.74 + REACH_SORT_WEIGHT * scored["reach"]["score"])
    gone = get_stored_card("old:removed")
    assert gone["reach"] is None and gone["queue_priority"] == 0.74
