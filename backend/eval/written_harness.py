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
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from temporalio.converter import value_to_type

from backend.budget import prompt_token_bound
from backend.gate.brief import split_brief
from backend.gate.written_check import clean_feature_terms, clean_names, judge, prepare_check
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
    watch's start input."""

    product_brief: str
    product_name: str
    product_aliases: List[str] = field(default_factory=list)
    key_features: Optional[List[str]] = None


@dataclass
class StoredReview:
    workflow_id: str
    topic: str
    post: Post
    evidence: Evidence


# Why a stored card is not used
EXCLUDED_REMOVED = "post_text_removed"
EXCLUDED_NOT_DRAFTED = "not_drafted"  # skipped before drafting (research rules, plan, confidence)
EXCLUDED_NO_SETTINGS = "no_watch_settings"  # no watch settings for its topic
EXCLUDED_UNREADABLE = "unreadable"


def load_stored_reviews(db_path: Path) -> Tuple[List[StoredReview], Counter]:
    """Read the cards that reached drafting, read-only. Returns the reviews
    and a count of the cards left out, by reason.

    A card reached drafting when it has a draft or a reply plan; a card
    skipped by the research rules, the confidence threshold or an invalid
    plan has neither. A card whose post text was removed is left out: its
    text must not be sent to a model.
    """
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    reviews: List[StoredReview] = []
    excluded: Counter = Counter()
    conn = sqlite3.connect(uri, uri=True)
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
        try:
            post = value_to_type(Post, post_dict)
            evidence = value_to_type(Evidence, evidence_dict)
        except Exception:
            excluded[EXCLUDED_UNREADABLE] += 1
            continue
        reviews.append(StoredReview(workflow_id, evidence.matched_topic, post, evidence))
    return reviews, excluded


# --- Spend cap ---


class SpendCapReached(Exception):
    """The next call could cost more than the harness's spend cap allows."""


@dataclass
class CallRecord:
    call: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class SpendCap:
    """Counts the harness's paid calls and refuses one that could pass the cap.

    Before each request the worst case (an upper bound on the prompt's tokens
    plus the max output tokens sent) is added to what was spent; if that is
    over the cap, the request is not sent. After it, the reported usage is
    recorded. A request that fails after it was sent is recorded at its worst
    case, as the app's budget does (a gateway may have billed it).
    """

    def __init__(self, cap_usd: float, input_cost_usd: float, output_cost_usd: float):
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
                if not isinstance(tokens_in, int) or not isinstance(tokens_out, int):
                    # No usable usage: keep the worst case counted
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
    outcome: str  # "passed_first" | "passed_redraft" | "skipped" | "plan_invalid" | "error"
    attempts: List[AttemptRecord] = field(default_factory=list)
    error: str = ""


# Failure kinds, read from the check's reasons (backend/gate/written_check.py)
FAILURE_KINDS = (
    ("too_many_claims", ("product claim, and",)),
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


def failure_kinds(result: WrittenCheckResult) -> List[str]:
    """The kinds of failure in a failed check, for the aggregate report."""
    reasons = [reason for _, reason in result.failures]
    kinds = []
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
) -> List[AttemptRecord]:
    """The written branch of ReviewWorkflow._draft_and_check, offline.

    Same inputs as the draft_reply activity builds (clean names and key
    features, the brief split in code), the same steer (redraft_steer), the
    same check (check_text) recorded on the draft the same way
    (record_written_check), and the same reuse of a check for a text already
    checked. Works on a copy of the stored evidence.
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
    attempts: List[AttemptRecord] = []
    for attempt in range(1, max_attempts + 1):
        steer = redraft_steer(last_failures) if attempt > 1 else None
        if cap is not None:
            cap.label = "draft" if attempt == 1 else "redraft"
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
    settings_by_topic: Dict[str, WatchSettings],
    agent: ReplyAgentPort,
    checker: ClaimCheckerPort,
    *,
    plans: Dict[str, Any],
    max_attempts: int,
    samples: int = 1,
    cap: Optional[SpendCap] = None,
    excluded: Optional[Counter] = None,
) -> Tuple[List[PostRecord], Counter, bool]:
    """Run every review `samples` times. Returns the per-post records, the
    count of reviews left out by reason, and whether the spend cap stopped
    the run early (the records then cover what finished)."""
    excluded = Counter(excluded or {})
    records: List[PostRecord] = []
    for review in reviews:
        settings = settings_by_topic.get(review.topic)
        if settings is None:
            excluded[EXCLUDED_NO_SETTINGS] += 1
            continue
        for sample in range(1, samples + 1):
            try:
                plan, source = await plan_for(review, agent, plans, cap)
                if plan is None:
                    records.append(PostRecord(review.workflow_id, review.topic, sample, source, None, "plan_invalid"))
                    break  # a plan that fails twice fails for every sample
                attempts = await run_written_path(
                    review, settings, plan, agent, checker,
                    max_attempts=max_attempts, sample=sample, cap=cap,
                )
                records.append(PostRecord(
                    review.workflow_id, review.topic, sample, source, plan.mention_product,
                    outcome_of(attempts), attempts,
                ))
            except SpendCapReached as e:
                records.append(PostRecord(
                    review.workflow_id, review.topic, sample, "", None, "error", error=str(e),
                ))
                return records, excluded, True
            except Exception as e:  # one post's failure does not end the run
                records.append(PostRecord(
                    review.workflow_id, review.topic, sample, "", None, "error",
                    error=f"{type(e).__name__}: {e}",
                ))
    return records, excluded, False


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
    drafted = [r for r in records if r.attempts]
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
    runs = len(drafted)
    summary: Dict[str, Any] = {
        "runs": len(records),
        "posts": len({r.workflow_id for r in records}),
        "excluded": dict(sorted(excluded.items())),
        "outcomes": dict(sorted(outcomes.items())),
        "pass_rate": round((outcomes["passed_first"] + outcomes["passed_redraft"]) / runs, 3) if runs else None,
        "first_draft_pass_rate": round(outcomes["passed_first"] / runs, 3) if runs else None,
        "drafts": drafts,
        "checker_calls": checker_calls,
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
        summary["models"] = sorted({c.model for c in cap.calls})
        summary["calls"] = by_call
        summary["cost_usd"] = round(cap.spent_usd, 6)
        summary["spend_cap_usd"] = cap.cap_usd
    return summary


def summary_markdown(label: str, summary: Dict[str, Any]) -> str:
    """The aggregate summary as a Markdown table, safe to paste (no text)."""
    lines = [f"### Redraft harness: {label}", "", "| Measure | Value |", "|---|---|"]
    for key, value in summary.items():
        shown = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        lines.append(f"| {key} | `{shown}` |")
    return "\n".join(lines) + "\n"


def report_json(label: str, records: Sequence[PostRecord], summary: Dict[str, Any]) -> str:
    """The full report: the summary plus every post's drafts and checks. It
    holds model-written text that can quote real posts: keep it local."""
    return json.dumps(
        {"label": label, "summary": summary, "posts": [asdict(r) for r in records]},
        indent=2,
        ensure_ascii=False,
    )
