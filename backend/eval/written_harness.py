"""Offline redraft harness for written drafts (issue #29).

Runs the written path of a Review (draft, check, one redraft with the
workflow's steer, check) on Reviews already stored in a local database, with
the reply agent and claim checker it is given. It is the measuring tool for
writer-prompt changes, and the first piece of the ADR-0006 2026-11-24
amendment's switch criterion 3 (assembled against written on stored
evidence).

What it does not do:
- It never searches or reads X: the post and its research come from the
  stored card. A card whose post text was removed (issue #9) is not used.
- It never writes the database it reads. The cards are read through a
  read-only SQLite connection; nothing is stored, updated or redacted.
- It never opens the app's daily budget ledger. Every paid call goes through
  a client wrapper with its own spend cap (SpendCap), which refuses a call
  whose worst-case cost would pass the cap.

The report holds model-written text that can quote real posts, so it must be
written outside version control (scripts/redraft_harness.py refuses a path
git does not ignore). summarize() gives the aggregate numbers only.
"""

import copy
import json
import math
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from temporalio.converter import value_to_type

from backend.budget import prompt_token_bound
from backend.gate.brief import split_brief
from backend.gate.written_check import (
    MAX_PRODUCT_CLAIMS,
    clean_feature_terms,
    clean_names,
    judge,
    prepare_check,
)
from backend.models import Evidence, Post, ReplyPlan
from backend.models.written import DRAFT_MODE_WRITTEN, WrittenCheckResult
from backend.planning import validate_reply_plan
from backend.ports.claim_checker import ClaimCheckerPort
from backend.ports.reply_agent import ReplyAgentPort, WrittenDraftRequest
from backend.workflows.review import REPLY_PLAN_MAX_ATTEMPTS, record_written_check, redraft_steer

# --- Inputs ---


@dataclass
class WatchSettings:
    """What a TopicWatch was started with that the written path needs. Cards
    do not store these; scripts/redraft_harness.py exports them from the
    watch's start input, keyed by settings_key(account, topic). A watch
    restarted with other settings gives its older cards the new ones: the
    cards do not record which run they came from."""

    product_brief: str
    product_name: str
    product_aliases: List[str] = field(default_factory=list)
    key_features: Optional[List[str]] = None
    # The watch's saved ReviewConfig.claim_gate_max_attempts: a live Review
    # keeps the limit its watch was started with. None: the caller's default.
    claim_gate_max_attempts: Optional[int] = None


def settings_key(account: str, topic: str) -> str:
    """A TopicWatch is one per account and topic (watch:<account>:<topic>)."""
    return f"{account}:{topic}"


@dataclass
class StoredReview:
    workflow_id: str
    topic: str
    post: Post
    evidence: Evidence
    account: str = ""

    @property
    def settings_key(self) -> str:
        return settings_key(self.account, self.topic)


# Why a stored card is not used
EXCLUDED_REMOVED = "post_text_removed"
EXCLUDED_NOT_DRAFTED = "not_drafted"  # skipped before drafting (research rules, confidence)
EXCLUDED_PLAN_SKIPPED = "plan_skipped"  # a live Review skipped it at the plan stage
EXCLUDED_NO_TOPIC = "no_review_topic"  # its Review's watch topic is unknown (not in the export)
EXCLUDED_NO_SETTINGS = "no_watch_settings"  # no watch settings for its account and topic
EXCLUDED_NO_PRODUCT_NAME = "no_product_name"  # its watch has none: a live Review stays assembled
EXCLUDED_UNREADABLE = "unreadable"


def post_id_of(workflow_id: str) -> str:
    """review:<account>:<post_id>"""
    parts = workflow_id.split(":")
    return parts[2] if len(parts) >= 3 and parts[0] == "review" else ""


def _account_of(workflow_id: str) -> str:
    """review:<account>:<post_id>"""
    parts = workflow_id.split(":")
    return parts[1] if len(parts) >= 3 and parts[0] == "review" else ""


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)


def removal_recorded(db_path: Path, post_id: str) -> bool:
    """Does any stored card of the post record its removal (issue #9)? Read
    before each post's model calls, so a removal the app records during a
    run stops the harness from sending that post's text."""
    conn = _connect_read_only(db_path)
    try:
        rows = conn.execute("SELECT post_data FROM review_cards").fetchall()
    finally:
        conn.close()
    for (post_data,) in rows:
        try:
            post = json.loads(post_data) if isinstance(post_data, str) else post_data
        except (TypeError, ValueError):
            continue
        if isinstance(post, dict) and str(post.get("id")) == str(post_id) and post.get("text_removed"):
            return True
    return False


def load_stored_reviews(
    db_path: Path, review_topics: Mapping[str, str]
) -> Tuple[List[StoredReview], Counter]:
    """Read the cards that reached drafting, read-only. Returns the reviews
    and a count of the cards left out, by reason.

    A card reached drafting when it has a draft or a reply plan; a card
    skipped by the research rules or the confidence threshold has neither.
    A card with a plan that a live Review skipped (an invalid plan, or a
    plan-stage skip rule) is left out too. A card whose post text was
    removed is left out: its text must not be sent to a model.

    `review_topics` maps each Review's workflow id to the topic of the watch
    that started it (its ReviewInput.topic, exported from Temporal). The
    card's own matched_topic is written by the research model and is not
    used to pick settings; a card whose Review is not in the map is left out.
    """
    reviews: List[StoredReview] = []
    excluded: Counter = Counter()
    conn = _connect_read_only(db_path)
    try:
        rows = conn.execute(
            "SELECT workflow_id, post_data, evidence_data FROM review_cards ORDER BY created_at, workflow_id"
        ).fetchall()
    finally:
        conn.close()
    for workflow_id, post_data, evidence_data in rows:
        try:
            post_dict = json.loads(post_data) if isinstance(post_data, str) else post_data
            evidence_dict = json.loads(evidence_data) if isinstance(evidence_data, str) else evidence_data
        except (TypeError, ValueError):
            excluded[EXCLUDED_UNREADABLE] += 1
            continue
        if not isinstance(post_dict, dict) or not isinstance(evidence_dict, dict):
            excluded[EXCLUDED_UNREADABLE] += 1
            continue
        if post_dict.get("text_removed"):
            excluded[EXCLUDED_REMOVED] += 1
            continue
        if not evidence_dict.get("draft") and not evidence_dict.get("reply_plan"):
            excluded[EXCLUDED_NOT_DRAFTED] += 1
            continue
        if evidence_dict.get("reply_plan") and (
            evidence_dict.get("reply_plan_errors") or evidence_dict.get("reply_plan_skip_rule")
        ):
            excluded[EXCLUDED_PLAN_SKIPPED] += 1
            continue
        try:
            post = value_to_type(Post, post_dict)
            evidence = value_to_type(Evidence, evidence_dict)
        except Exception:
            excluded[EXCLUDED_UNREADABLE] += 1
            continue
        topic = review_topics.get(workflow_id)
        if not topic:
            excluded[EXCLUDED_NO_TOPIC] += 1
            continue
        reviews.append(StoredReview(workflow_id, topic, post, evidence, _account_of(workflow_id)))
    return reviews, excluded


# --- Spend cap ---


class SpendCapReached(Exception):
    """The next call could cost more than the harness's spend cap allows."""


class PostRemovedDuringRun(Exception):
    """The post's removal was recorded while the harness worked on it."""


def _stop_if_removed(is_removed: Optional[Callable[[str], bool]], post_id: str) -> None:
    """Asked right before each model call, as the live activities do."""
    if is_removed is not None and is_removed(post_id):
        raise PostRemovedDuringRun(post_id)


@dataclass
class CallRecord:
    call: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


def _usable_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class SpendCap:
    """Counts the harness's paid calls and refuses one that could pass the cap.

    Before each request the worst case (an upper bound on the prompt's tokens
    plus the max output tokens sent) is added to what was spent; if that is
    over the cap, the request is not sent. After it, the reported usage is
    recorded. A request that fails after it was sent is recorded at its worst
    case, as the app's budget does (a gateway may have billed it).
    """

    def __init__(self, cap_usd: float, input_cost_usd: float, output_cost_usd: float):
        for name, value in (("cap", cap_usd), ("input token price", input_cost_usd),
                            ("output token price", output_cost_usd)):
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or not math.isfinite(value) or value < 0:
                raise ValueError(f"the spend {name} must be a finite number of 0 or more")
        self.cap_usd = cap_usd
        self.input_cost_usd = input_cost_usd
        self.output_cost_usd = output_cost_usd
        self.calls: List[CallRecord] = []
        self.label = "unlabelled"  # set by the harness before each adapter call

    @property
    def spent_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return input_tokens * self.input_cost_usd + output_tokens * self.output_cost_usd

    def wrap(self, client: Any) -> Any:
        """A client whose chat.completions.create goes through this cap."""
        cap = self

        class _Completions:
            async def create(self, **kwargs):
                bound_in = prompt_token_bound(kwargs.get("messages") or [])
                bound_out = int(kwargs.get("max_completion_tokens") or 0)
                worst = cap.cost(bound_in, bound_out)
                if cap.spent_usd + worst > cap.cap_usd:
                    raise SpendCapReached(
                        f"{cap.label} call refused: ${cap.spent_usd:.4f} spent, up to ${worst:.4f} more "
                        f"would pass the ${cap.cap_usd:.2f} cap"
                    )
                model = str(kwargs.get("model") or "")
                try:
                    response = await client.chat.completions.create(**kwargs)
                except Exception:
                    cap.calls.append(CallRecord(cap.label, model, bound_in, bound_out, worst))
                    raise
                usage = getattr(response, "usage", None)
                tokens_in = getattr(usage, "prompt_tokens", None) if usage else None
                tokens_out = getattr(usage, "completion_tokens", None) if usage else None
                if not _usable_count(tokens_in) or not _usable_count(tokens_out) or (
                    tokens_in == 0 and tokens_out == 0
                ):
                    # No usable usage (as the app's meter reads it): keep the
                    # worst case counted
                    tokens_in, tokens_out = bound_in, bound_out
                cap.calls.append(CallRecord(
                    cap.label, getattr(response, "model", None) or model,
                    tokens_in, tokens_out, cap.cost(tokens_in, tokens_out),
                ))
                return response

        class _Chat:
            completions = _Completions()

        class _Client:
            chat = _Chat()

        return _Client()


# --- The written path ---


@dataclass
class SentenceRecord:
    sentence: str
    kind: str
    verdict: str
    passed: bool
    triggers: List[str]
    checker_flagged: bool
    reason: str


@dataclass
class AttemptRecord:
    attempt: int
    steer: Optional[str]
    text: str
    passed: bool
    checker_called: bool
    reused_check: bool  # the same text as an earlier attempt: its check is reused, as in the workflow
    sentences: List[SentenceRecord]
    plan_failures: List[str]
    reasoning: str
    failure_kinds: List[str]
    claim_sentences: int  # sentences checked as product claims


@dataclass
class PostRecord:
    workflow_id: str
    topic: str
    sample: int
    plan_source: str  # "stored" | "cached" | "planned" | "invalid"
    mention_product: Optional[bool]
    outcome: str  # "passed_first" | "passed_redraft" | "skipped" | "plan_invalid" | "removed" | "error"
    attempts: List[AttemptRecord] = field(default_factory=list)
    error: str = ""
    # What this run's model calls cost (plan, drafts, checks), from the spend cap
    model_calls: int = 0
    cost_usd: float = 0.0
    post_id: str = ""


# Failure kinds, read from the check's reasons (backend/gate/written_check.py)
FAILURE_KINDS = (
    ("unsupported", ("The brief does not support it",)),
    ("number_not_in_brief", ("number no brief sentence states", "number the cited brief sentences do not state")),
    ("question_claim", ("must be a statement, not a question",)),
    ("too_many_sentences", ("sentences (got",)),
    ("too_long", ("characters (got",)),
    ("joined_sentences", ("may be more than one sentence",)),
    ("empty", ("The reply is empty", "the draft is missing")),
    ("checker_answer", (
        "The checker returned no verdict", "The checker's answer", "cited no brief sentence",
        "brief sentence id(s) that do not exist", "The product-claim check did not run",
    )),
    ("empty_brief", ("brief has no sentences",)),
)


_CHECKER_TEXT_MARKER = "The brief does not support it:"


def _code_written(reason: str) -> str:
    """A reason without the checker's own words, which follow the marker:
    a phrase in them must not count as a failure kind."""
    head, marker, _ = reason.partition(_CHECKER_TEXT_MARKER)
    return head + marker


def failure_kinds(result: WrittenCheckResult) -> List[str]:
    """The kinds of failure in a failed check, for the aggregate report."""
    reasons = [_code_written(reason) for _, reason in result.failures]
    kinds = []
    # Counted from the sentence rows, not read from a reason: the checker's
    # own words can come before the code's "at most 1 product claim" text
    if sum(1 for c in result.sentences if c.kind == "claim") > MAX_PRODUCT_CLAIMS:
        kinds.append("too_many_claims")
    for kind, needles in FAILURE_KINDS:
        if any(needle in reason for reason in reasons for needle in needles):
            kinds.append(kind)
    if result.plan_failures:
        kinds.append("plan_product_decision")
    if not result.passed and not kinds:
        kinds.append("other")
    return kinds


async def check_text(
    text: str,
    settings: WatchSettings,
    mention_product: Optional[bool],
    checker: ClaimCheckerPort,
    key: str,
) -> WrittenCheckResult:
    """The check_written_draft activity's steps, without its activity context
    (the removal lookups: cards with removed text are never loaded here)."""
    prepared = prepare_check(
        text,
        split_brief(settings.product_brief),
        settings.product_name,
        settings.product_aliases,
        settings.key_features,
        mention_product,
    )
    if not prepared.needs_checker:
        return judge(prepared, None, mention_product)
    response = await checker.check(prepared.request(), key)
    return judge(
        prepared,
        response.verdicts,
        mention_product,
        checker_model=response.model_used,
        checker_input_tokens=response.input_tokens,
        checker_output_tokens=response.output_tokens,
    )


async def plan_for(
    review: StoredReview,
    agent: ReplyAgentPort,
    plans: Dict[str, Any],
    cap: Optional[SpendCap] = None,
    is_removed: Optional[Callable[[str], bool]] = None,
) -> Tuple[Optional[ReplyPlan], str]:
    """The reply plan to draft under, and where it came from.

    A stored valid plan is used as it is. A card stored before the plan step
    (issue #5) has none: a cached plan from an earlier harness run is reused,
    so runs before and after a prompt change draft under the same plan.
    Otherwise the agent plans it as the plan_reply activity would (validated
    in code, one re-request with the errors), and the plan is cached; a plan
    invalid twice is cached as {"invalid": [errors]}, and the post is left
    out of every run that uses the cache.
    """
    evidence = review.evidence
    if evidence.reply_plan is not None and not evidence.reply_plan_errors:
        return evidence.reply_plan, "stored"
    cached = plans.get(review.workflow_id)
    if isinstance(cached, dict):
        if "invalid" in cached:
            return None, "invalid"
        return value_to_type(ReplyPlan, cached), "cached"
    feedback: Optional[List[str]] = None
    for attempt in range(1, REPLY_PLAN_MAX_ATTEMPTS + 1):
        if cap is not None:
            cap.label = "plan"
        _stop_if_removed(is_removed, review.post.id)
        plan = await agent.plan(
            post=review.post,
            evidence=copy.deepcopy(evidence),
            idempotency_key=f"harness:{review.workflow_id}:plan:{attempt}",
            feedback=feedback,
        )
        errors = validate_reply_plan(plan, review.post.text)
        if not errors:
            plans[review.workflow_id] = asdict(plan)
            return plan, "planned"
        feedback = list(errors)
    # Cached too, so that every run with this cache covers the same posts
    plans[review.workflow_id] = {"invalid": feedback or []}
    return None, "invalid"


async def run_written_path(
    review: StoredReview,
    settings: WatchSettings,
    plan: ReplyPlan,
    agent: ReplyAgentPort,
    checker: ClaimCheckerPort,
    *,
    max_attempts: int,
    sample: int = 1,
    cap: Optional[SpendCap] = None,
    attempts: Optional[List[AttemptRecord]] = None,
    is_removed: Optional[Callable[[str], bool]] = None,
) -> List[AttemptRecord]:
    """The written branch of ReviewWorkflow._draft_and_check, offline.

    Same inputs as the draft_reply activity builds (clean names and key
    features, the brief split in code), the same steer (redraft_steer), the
    same check (check_text) recorded on the draft the same way
    (record_written_check), and the same reuse of a check for a text already
    checked. Works on a copy of the stored evidence.

    `attempts` collects each attempt as it finishes, so a caller keeps the
    finished ones when a later call fails or hits the spend cap.
    """
    evidence = copy.deepcopy(review.evidence)
    evidence.draft = None
    evidence.reply_plan = plan
    evidence.draft_mode = DRAFT_MODE_WRITTEN
    name, aliases = clean_names(settings.product_name, settings.product_aliases)
    brief_sentences = split_brief(settings.product_brief)
    feature_terms = clean_feature_terms(settings.key_features)

    checks: Dict[str, WrittenCheckResult] = {}
    last_failures: List[tuple] = []
    attempts = [] if attempts is None else attempts
    for attempt in range(1, max_attempts + 1):
        steer = redraft_steer(last_failures) if attempt > 1 else None
        if cap is not None:
            cap.label = "draft" if attempt == 1 else "redraft"
        _stop_if_removed(is_removed, review.post.id)
        evidence = await agent.draft_written(
            WrittenDraftRequest(
                post=review.post,
                evidence=evidence,
                plan=plan,
                brief_sentences=brief_sentences,
                product_name=name,
                product_aliases=aliases,
                feature_terms=feature_terms,
                steer=steer,
            ),
            idempotency_key=f"harness:{review.workflow_id}:{sample}:draft:{attempt}",
        )
        if not evidence.draft:
            last_failures = [("[reply]", "the draft is missing")]
            attempts.append(AttemptRecord(
                attempt, steer, "", False, False, False, [], [], "the draft is missing", ["empty"], 0,
            ))
            continue
        text = evidence.draft.text
        reused = text in checks
        if not reused:
            if cap is not None:
                cap.label = "check"
            _stop_if_removed(is_removed, review.post.id)
            checks[text] = await check_text(
                text, settings, plan.mention_product, checker,
                f"harness:{review.workflow_id}:{sample}:check:{attempt}",
            )
        result = checks[text]
        record_written_check(evidence.draft, result)
        last_failures = [("[reply plan]", r) for r in result.plan_failures] + [
            (sentence, reason) for sentence, reason in result.failures
        ]
        attempts.append(AttemptRecord(
            attempt=attempt,
            steer=steer,
            text=text,
            passed=result.passed,
            checker_called=result.info.checker_called and not reused,
            reused_check=reused,
            sentences=[
                SentenceRecord(
                    sentence=s.sentence,
                    kind=s.kind,
                    verdict=s.verdict,
                    passed=s.passed,
                    triggers=[t.trigger for t in s.triggers],
                    checker_flagged=s.checker_flagged,
                    reason=s.reason,
                )
                for s in result.sentences
            ],
            plan_failures=list(result.plan_failures),
            reasoning=result.reasoning,
            failure_kinds=failure_kinds(result),
            claim_sentences=sum(1 for s in result.sentences if s.kind == "claim"),
        ))
        if result.passed:
            break
    return attempts


def outcome_of(attempts: Sequence[AttemptRecord]) -> str:
    if attempts and attempts[-1].passed:
        return "passed_first" if len(attempts) == 1 else "passed_redraft"
    return "skipped"


async def run_harness(
    reviews: Sequence[StoredReview],
    settings_by_key: Dict[str, WatchSettings],
    agent: ReplyAgentPort,
    checker: ClaimCheckerPort,
    *,
    plans: Dict[str, Any],
    max_attempts: int,
    samples: int = 1,
    cap: Optional[SpendCap] = None,
    excluded: Optional[Counter] = None,
    is_removed: Optional[Callable[[str], bool]] = None,
) -> Tuple[List[PostRecord], Counter, bool]:
    """Run every review `samples` times. Returns the per-post records, the
    count of reviews left out by reason, and whether the spend cap stopped
    the run early (the records then cover what finished).

    `settings_by_key` maps settings_key(account, topic) to the watch's
    settings. `is_removed(post_id)` is asked right before each model call
    (removal_recorded on the database read); a post removed during its run
    is left out from then on, and its run is recorded as "removed".
    `max_attempts` is the default for a watch without a saved limit.
    """
    excluded = Counter(excluded or {})
    records: List[PostRecord] = []
    for review in reviews:
        settings = settings_by_key.get(review.settings_key)
        if settings is None:
            excluded[EXCLUDED_NO_SETTINGS] += 1
            continue
        if not clean_names(settings.product_name, settings.product_aliases)[0]:
            excluded[EXCLUDED_NO_PRODUCT_NAME] += 1
            continue
        attempt_limit = (
            max_attempts if settings.claim_gate_max_attempts is None else settings.claim_gate_max_attempts
        )
        for sample in range(1, samples + 1):
            attempts: List[AttemptRecord] = []
            source, mention = "", None
            calls_before = len(cap.calls) if cap is not None else 0

            def record(outcome: str, error: str = "") -> None:
                new_calls = cap.calls[calls_before:] if cap is not None else []
                records.append(PostRecord(
                    review.workflow_id, review.topic, sample, source, mention, outcome, attempts, error,
                    model_calls=len(new_calls), cost_usd=round(sum(c.cost_usd for c in new_calls), 6),
                    post_id=review.post.id,
                ))

            try:
                _stop_if_removed(is_removed, review.post.id)
                plan, source = await plan_for(review, agent, plans, cap, is_removed)
                if plan is None:
                    record("plan_invalid")
                    break  # a plan that fails twice fails for every sample
                mention = plan.mention_product
                await run_written_path(
                    review, settings, plan, agent, checker,
                    max_attempts=attempt_limit, sample=sample, cap=cap, attempts=attempts,
                    is_removed=is_removed,
                )
                record(outcome_of(attempts))
            except PostRemovedDuringRun:
                excluded[EXCLUDED_REMOVED] += 1
                if cap is not None and len(cap.calls) > calls_before or attempts:
                    # Its calls were paid for; the run is not a result, and
                    # apply_removals below drops its text
                    record("removed")
                break
            except SpendCapReached as e:
                # The attempts that finished are kept: their calls were paid
                record("error", str(e))
                apply_removals(records, plans, is_removed, excluded)
                return records, excluded, True
            except Exception as e:  # one post's failure does not end the run
                record("error", f"{type(e).__name__}: {e}")
    apply_removals(records, plans, is_removed, excluded)
    return records, excluded, False


def apply_removals(
    records: List[PostRecord],
    plans: Dict[str, Any],
    is_removed: Optional[Callable[[str], bool]],
    excluded: Counter,
) -> None:
    """Drop what the harness holds of a post whose removal is recorded
    (issue #9), before anything is written: every run of the post becomes
    "removed" with its drafts, sentences, reasons and steers emptied (they
    can quote the post), and its cached plan (whose target point is a span
    of the post) is deleted. Call counts, costs and failure kinds stay.
    Asked once per post after the run, so a removal recorded while the
    harness worked on other posts is honoured too. The whole plan cache is
    swept as well (prune_removed_plans), also plans of posts this run left
    out because their removal was already recorded.
    """
    if is_removed is None:
        return
    prune_removed_plans(plans, is_removed)
    removed = {r.post_id for r in records if r.post_id and is_removed(r.post_id)}
    for record in records:
        if record.post_id not in removed:
            continue
        if record.outcome != "removed" and not any(
            r.post_id == record.post_id and r.outcome == "removed" for r in records
        ):
            excluded[EXCLUDED_REMOVED] += 1  # once per post, when first found here
        record.outcome = "removed"
        record.error = ""
        plans.pop(record.workflow_id, None)
        for attempt in record.attempts:
            attempt.text = ""
            attempt.steer = None
            attempt.reasoning = ""
            for sentence in attempt.sentences:
                sentence.sentence = ""
                sentence.reason = ""


def prune_removed_plans(plans: Dict[str, Any], is_removed: Callable[[str], bool]) -> int:
    """Delete every cached plan whose post's removal is recorded (a plan's
    target point is a span of the post). Returns how many were deleted."""
    gone = [wid for wid in plans if post_id_of(wid) and is_removed(post_id_of(wid))]
    for wid in gone:
        del plans[wid]
    return len(gone)


def redact_removed_report(report: Dict[str, Any], is_removed: Callable[[str], bool]) -> bool:
    """Apply recorded removals to a report.json written by an earlier run:
    the same fields apply_removals empties. Returns whether it changed."""
    changed = False
    for post in report.get("posts") or []:
        if not isinstance(post, dict):
            continue
        post_id = str(post.get("post_id") or post_id_of(str(post.get("workflow_id") or "")))
        if not post_id or not is_removed(post_id):
            continue
        before = json.dumps(post, sort_keys=True)
        post["outcome"] = "removed"
        post["error"] = ""
        for attempt in post.get("attempts") or []:
            attempt.update({"text": "", "steer": None, "reasoning": ""})
            for sentence in attempt.get("sentences") or []:
                sentence.update({"sentence": "", "reason": ""})
        changed = changed or json.dumps(post, sort_keys=True) != before
    return changed


# --- Report ---


def summarize(
    records: Sequence[PostRecord],
    excluded: Counter,
    cap: Optional[SpendCap],
    *,
    stopped_by_cap: bool = False,
) -> Dict[str, Any]:
    """Aggregate numbers only: no post, draft or brief text, no ids. This is
    what may be pasted into a pull request of the public repo."""
    outcomes = Counter(r.outcome for r in records)
    # A removed post's runs are not results: they count only in "outcomes"
    drafted = [r for r in records if r.attempts and r.outcome != "removed"]
    first_kinds: Counter = Counter()
    redraft_kinds: Counter = Counter()
    claim_counts: Counter = Counter()
    checker_calls = 0
    drafts = 0
    for r in drafted:
        for a in r.attempts:
            drafts += 1
            checker_calls += int(a.checker_called)
            claim_counts[str(a.claim_sentences)] += 1
            (first_kinds if a.attempt == 1 else redraft_kinds).update(a.failure_kinds)
    # Pass rates count finished runs only (passed or skipped); a run cut off
    # by an error is in "outcomes" as "error", and its finished attempts
    # count in the failure kinds. The first-draft rate counts every run
    # whose first draft was checked.
    finished = outcomes["passed_first"] + outcomes["passed_redraft"] + outcomes["skipped"]
    first_checked = len(drafted)
    summary: Dict[str, Any] = {
        "runs": len(records),
        "posts": len({r.workflow_id for r in records}),
        "excluded": dict(sorted(excluded.items())),
        "outcomes": dict(sorted(outcomes.items())),
        "pass_rate": round((outcomes["passed_first"] + outcomes["passed_redraft"]) / finished, 3) if finished else None,
        "first_draft_pass_rate": round(
            sum(1 for r in drafted if r.attempts[0].passed) / first_checked, 3
        ) if first_checked else None,
        "drafts": drafts,
        # Sent checker requests (also failed ones) when the cap counted them
        "checker_calls": sum(1 for c in cap.calls if c.call == "check") if cap is not None else checker_calls,
        "first_draft_failure_kinds": dict(sorted(first_kinds.items())),
        "redraft_failure_kinds": dict(sorted(redraft_kinds.items())),
        "product_claim_sentences_per_draft": dict(sorted(claim_counts.items())),
        "plan_sources": dict(sorted(Counter(r.plan_source for r in records).items())),
        "mention_product": dict(sorted(Counter(str(r.mention_product) for r in drafted).items())),
        "stopped_by_spend_cap": stopped_by_cap,
    }
    if cap is not None:
        by_call: Dict[str, Dict[str, float]] = {}
        for c in cap.calls:
            entry = by_call.setdefault(c.call, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
            entry["calls"] += 1
            entry["input_tokens"] += c.input_tokens
            entry["output_tokens"] += c.output_tokens
            entry["cost_usd"] = round(entry["cost_usd"] + c.cost_usd, 6)
        summary["calls"] = by_call
        summary["cost_usd"] = round(cap.spent_usd, 6)
        summary["spend_cap_usd"] = cap.cap_usd
    return summary


# A name for the pasteable summary: a letter first, short, no spaces or
# colons (workflow ids have colons), and no run of 6 or more digits (post
# ids are long digit runs; a model date such as 2024-07-18 has none).
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._/-]{0,63}$")
_LONG_DIGITS_RE = re.compile(r"\d{6,}")


def _name_like(value: str, max_len: int) -> bool:
    return bool(
        isinstance(value, str) and len(value) <= max_len
        and _NAME_RE.match(value) and not _LONG_DIGITS_RE.search(value)
    )


def safe_label(label: str) -> bool:
    """A run label that can go into the pasteable summary: a short name."""
    return _name_like(label, 40) and "/" not in label


def summary_markdown(summary: Dict[str, Any]) -> str:
    """The aggregate summary as a Markdown table, safe to paste (no text).
    The heading is fixed: the run label is the developer's own text and
    stays in the file path only."""
    lines = ["### Redraft harness run", "", "| Measure | Value |", "|---|---|"]
    for key, value in summary.items():
        shown = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        lines.append(f"| {key} | `{shown}` |")
    return "\n".join(lines) + "\n"


def report_json(
    label: str, records: Sequence[PostRecord], summary: Dict[str, Any], models: Optional[Sequence[str]] = None,
) -> str:
    """The full report: the summary, the model names the provider reported
    (kept out of the pasteable summary: a gateway may return any string),
    and every post's drafts and checks. It holds model-written text that can
    quote real posts: keep it local."""
    return json.dumps(
        {"label": label, "summary": summary, "models": models or [], "posts": [asdict(r) for r in records]},
        indent=2,
        ensure_ascii=False,
    )
