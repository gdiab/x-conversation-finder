"""Offline research eval (issue #44).

Runs the research step of a Review (the research model's proposal, then the
code checks of backend/gate/reply_fit.py) on posts already stored in a local
database, with the reply agent it is given. It measures research-prompt
changes: how often the proposed problem quote is not in the post, how often
such a quote was copied from the brief, and how many posts pass the fit
rules.

What it does and does not do:
- It makes no X call: the post comes from the stored card. A card whose post
  text was removed (issue #9) is not used, and a removal recorded during a
  run stops that post's next model call.
- It reads the database through a read-only SQLite connection and never
  writes it.
- Every model call goes through the redraft harness's spend cap (SpendCap),
  which refuses a call whose worst-case cost would pass the cap.

The report holds model-written text that quotes real posts and the brief,
so it must be written outside version control (scripts/research_harness.py
refuses a path git does not ignore). summarize() gives aggregate numbers
only.
"""

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from temporalio.converter import value_to_type

from backend.eval.written_harness import (
    EXCLUDED_NO_SETTINGS,
    EXCLUDED_NO_TOPIC,
    EXCLUDED_REMOVED,
    EXCLUDED_UNREADABLE,
    PostRemovedDuringRun,
    SpendCap,
    SpendCapReached,
    WatchSettings,
    _account_of,
    _connect_read_only,
    _removed_post_ids,
    _stop_if_removed,
    post_id_of,
)
from backend.gate.reply_fit import assess_reply_fit, normalize_text
from backend.models import Post
from backend.ports.reply_agent import ReplyAgentPort

# Another card of a post already in the run under the same watch settings
EXCLUDED_DUPLICATE = "duplicate_post"

# A rejected quote shares a run of this many words with the brief: it was
# copied from the brief, also when the model cut or joined brief sentences
BRIEF_RUN_WORDS = 6


@dataclass
class ResearchPost:
    workflow_id: str
    topic: str
    post: Post
    settings_key: str


def load_research_posts(
    db_path: Path, review_topics: Mapping[str, str], settings: Mapping[str, WatchSettings],
) -> Tuple[List[ResearchPost], Counter]:
    """Every stored card whose post still has its text, oldest first, once
    per post and watch settings. Read-only. Returns the posts and a count of
    the cards left out, by reason.

    The research step runs before any skip rule, so cards skipped at research
    are used too (unlike the redraft harness, which needs a draft). The
    watch topic comes from the Review's start input (`review_topics`), as in
    the redraft harness; the card's matched_topic is model-written.
    """
    from backend.eval.written_harness import settings_key

    posts: List[ResearchPost] = []
    excluded: Counter = Counter()
    seen = set()
    conn = _connect_read_only(db_path)
    try:
        rows = conn.execute(
            "SELECT workflow_id, post_data FROM review_cards ORDER BY created_at, workflow_id"
        ).fetchall()
        removed_ids = _removed_post_ids(conn)
    finally:
        conn.close()
    for workflow_id, post_data in rows:
        try:
            post_dict = json.loads(post_data) if isinstance(post_data, str) else post_data
        except (TypeError, ValueError):
            excluded[EXCLUDED_UNREADABLE] += 1
            continue
        if not isinstance(post_dict, dict):
            excluded[EXCLUDED_UNREADABLE] += 1
            continue
        if post_dict.get("text_removed") or str(post_dict.get("id")) in removed_ids \
                or post_id_of(workflow_id) in removed_ids:
            excluded[EXCLUDED_REMOVED] += 1
            continue
        try:
            post = value_to_type(Post, post_dict)
        except Exception:
            excluded[EXCLUDED_UNREADABLE] += 1
            continue
        if not isinstance(post.text, str) or not post.text.strip():
            excluded[EXCLUDED_UNREADABLE] += 1
            continue
        topic = review_topics.get(workflow_id)
        if not topic:
            excluded[EXCLUDED_NO_TOPIC] += 1
            continue
        key = settings_key(_account_of(workflow_id), topic)
        if key not in settings:
            excluded[EXCLUDED_NO_SETTINGS] += 1
            continue
        if (post.id, key) in seen:
            excluded[EXCLUDED_DUPLICATE] += 1
            continue
        seen.add((post.id, key))
        posts.append(ResearchPost(workflow_id, topic, post, key))
    return posts, excluded


# --- Did a rejected quote come from the brief? ---


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9']+", normalize_text(text))


def brief_quote_kind(span: Optional[str], brief: str) -> str:
    """How a proposed problem quote relates to the brief: "verbatim" (the
    whole quote is in the brief, ignoring case, spaces and quote marks),
    "partial" (it shares a run of BRIEF_RUN_WORDS words with the brief) or
    "none". Used to measure issue #44, never to decide a skip."""
    if not isinstance(span, str) or not span.strip():
        return "none"
    if normalize_text(span) in normalize_text(brief):
        return "verbatim"
    span_words, brief_words = _words(span), _words(brief)
    n = BRIEF_RUN_WORDS
    if len(span_words) < n:
        return "none"
    runs = {tuple(brief_words[i:i + n]) for i in range(len(brief_words) - n + 1)}
    if any(tuple(span_words[i:i + n]) in runs for i in range(len(span_words) - n + 1)):
        return "partial"
    return "none"


# --- The run ---


@dataclass
class ResearchRecord:
    workflow_id: str
    post_id: str
    settings_key: str
    sample: int
    outcome: str  # researched | removed | error
    error: str = ""
    post_kind: Optional[str] = None
    span_check: Optional[str] = None
    accepted_span: Optional[str] = None  # post text (code-verified)
    rejected_span: Optional[str] = None  # model text: may quote the post or the brief
    brief_quote: str = "none"  # brief_quote_kind of the rejected span
    skip_rule: Optional[str] = None
    founder_materials: int = 0
    materials_dropped: int = 0
    confidence_score: Optional[float] = None


async def run_research(
    posts: Sequence[ResearchPost],
    settings: Mapping[str, WatchSettings],
    agent: ReplyAgentPort,
    *,
    cap: Optional[SpendCap] = None,
    samples: int = 1,
    limit: int = 0,
    is_removed: Optional[Callable[[str], bool]] = None,
    now: Optional[datetime] = None,
) -> Tuple[List[ResearchRecord], bool]:
    """Research each post `samples` times and apply the code checks, as the
    research_post activity does. Returns the records and whether the spend
    cap stopped the run. `limit` stops after N posts (0: all)."""
    now = now or datetime.now(timezone.utc)
    records: List[ResearchRecord] = []
    for index, item in enumerate(posts):
        if limit and index >= limit:
            break
        brief = settings[item.settings_key].product_brief
        for sample in range(1, samples + 1):
            record = ResearchRecord(item.workflow_id, item.post.id, item.settings_key, sample, "researched")
            try:
                _stop_if_removed(is_removed, item.post.id)
                if cap is not None:
                    cap.label = "research"
                evidence = await agent.research(
                    post=item.post, topic=item.topic, product_brief=brief,
                    idempotency_key=f"research-eval:{item.workflow_id}:{sample}",
                )
                evidence = assess_reply_fit(evidence, item.post, brief, now=now)
            except SpendCapReached:
                return records, True
            except PostRemovedDuringRun:
                record.outcome = "removed"
                records.append(record)
                break
            except Exception as e:  # a model or parse error is a result, not a crash
                record.outcome, record.error = "error", type(e).__name__
                records.append(record)
                continue
            fit = evidence.reply_fit
            record.post_kind = fit.post_kind
            record.span_check = fit.span_check
            record.accepted_span = fit.brief_problem_span
            record.rejected_span = fit.rejected_span
            record.brief_quote = brief_quote_kind(fit.rejected_span, brief)
            record.skip_rule = fit.skip_rule
            record.founder_materials = len(evidence.founder_materials)
            record.materials_dropped = fit.materials_dropped
            record.confidence_score = evidence.confidence_score
            records.append(record)
    return records, False


def redact_removed(records: Sequence[ResearchRecord], is_removed: Callable[[str], bool]) -> int:
    """Empty the text of every record whose post's removal is recorded, and
    mark it removed. Returns how many records changed."""
    changed = 0
    for r in records:
        if r.outcome != "removed" and is_removed(r.post_id):
            r.outcome = "removed"
            r.accepted_span = r.rejected_span = None
            changed += 1
    return changed


def _share(part: int, whole: int) -> Optional[float]:
    return round(part / whole, 3) if whole else None


def summarize(
    records: Sequence[ResearchRecord], excluded: Counter, cap: Optional[SpendCap], *, stopped_by_cap: bool = False,
) -> Dict[str, Any]:
    """Aggregate numbers only: no post, quote or brief text, no ids. This is
    what may be pasted into a pull request of the public repo."""
    done = [r for r in records if r.outcome == "researched"]
    not_in_post = [r for r in done if r.span_check == "not_in_post"]
    from_brief = [r for r in not_in_post if r.brief_quote != "none"]
    passed = [r for r in done if r.skip_rule is None]
    summary: Dict[str, Any] = {
        "posts": len({(r.post_id, r.settings_key) for r in records}),
        "runs": len(records),
        "excluded": dict(sorted(excluded.items())),
        "outcomes": dict(sorted(Counter(r.outcome for r in records).items())),
        "researched": len(done),
        "span_check": dict(sorted(Counter(str(r.span_check) for r in done).items())),
        "not_in_post": len(not_in_post),
        "not_in_post_share": _share(len(not_in_post), len(done)),
        "not_in_post_from_brief": len(from_brief),
        "not_in_post_from_brief_share": _share(len(from_brief), len(not_in_post)),
        "not_in_post_from_brief_kind": dict(sorted(Counter(r.brief_quote for r in from_brief).items())),
        "skip_rules": dict(sorted(Counter(str(r.skip_rule) for r in done).items())),
        "passed_fit_rules": len(passed),
        "passed_fit_rules_share": _share(len(passed), len(done)),
        "posts_passing_in_any_sample": len({(r.post_id, r.settings_key) for r in passed}),
        "stopped_by_spend_cap": stopped_by_cap,
    }
    if cap is not None:
        summary["calls"] = len(cap.calls)
        summary["input_tokens"] = sum(c.input_tokens for c in cap.calls)
        summary["output_tokens"] = sum(c.output_tokens for c in cap.calls)
        summary["cost_usd"] = round(cap.spent_usd, 6)
        summary["spend_cap_usd"] = cap.cap_usd
    return summary


def summary_markdown(summary: Dict[str, Any]) -> str:
    """The aggregate summary as a Markdown table, safe to paste (no text).
    The heading is fixed: the run label stays in the file path only."""
    lines = ["### Research eval run", "", "| Measure | Value |", "|---|---|"]
    for key, value in summary.items():
        shown = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        lines.append(f"| {key} | `{shown}` |")
    return "\n".join(lines) + "\n"


def report_json(
    label: str, records: Sequence[ResearchRecord], summary: Dict[str, Any], models: Sequence[str] = (),
) -> str:
    """The full report: the summary, the model names the provider reported,
    and every run's proposal and checks. It holds quotes of real posts and
    of the brief: keep it local."""
    return json.dumps(
        {"label": label, "summary": summary, "models": list(models), "runs": [asdict(r) for r in records]},
        indent=2, ensure_ascii=False,
    )
