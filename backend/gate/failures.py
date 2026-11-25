"""Text built from a failed draft's (sentence, reason) rows.

The Review workflow built these strings itself while it held the draft. A
Review that keeps text out of its history (issue #23) has its activities
build them from the stored rows instead; both use these functions, so the
two paths say the same thing. Pure and deterministic: the workflow calls
them while replaying old histories.

A row's sentence is the failing draft sentence, or a [marker] such as
"[reply plan]" for a failure that is not about one sentence.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

PLAN_FAILURE_MARKER = "[reply plan]"
MISSING_DRAFT_REASON = "the draft is missing"


@dataclass
class DraftFailures:
    """The stored failures of one draft (issue #23): the rows the steer and
    the skip reason are built from, and the claim gate's own reasoning (a
    nudge rejection shows it when no sentence failed)."""

    rows: List[List[str]] = field(default_factory=list)
    gate_reasoning: str = ""
    gate_passed: bool = True


def gate_steer(rows: Sequence[Sequence[str]]) -> Optional[str]:
    """The steer for the redraft after a failed attempt (ADR-0006
    amendment 2026-11-21: one redraft with the per-sentence reasons)."""
    if not rows:
        return None
    reasons = [f"'{sentence[:50]}...': {reason}" for sentence, reason in list(rows)[:3]]
    return f"Previous draft failed claim gate. Fix these issues: {'; '.join(reasons)}"


def gate_skip_reason(rows: Sequence[Sequence[str]]) -> str:
    """The skip reason after the last attempt failed."""
    if not rows:
        return "Not enough evidence - claim gate failed after regeneration"
    summary = "; ".join(f"{sentence[:30]}... ({reason})" for sentence, reason in list(rows)[:3])
    return f"Not enough evidence: {summary}"


def nudge_rejection_reasons(failures: DraftFailures) -> List[str]:
    """Why a nudged draft was refused, as the nudge update used to word it:
    plan failures as they are, sentence failures as "sentence: reason", and
    the gate's reasoning when the gate failed without a sentence failure."""
    plan = [reason for sentence, reason in failures.rows if sentence == PLAN_FAILURE_MARKER]
    sentences = [
        f"{sentence[:50]}: {reason}" for sentence, reason in failures.rows if sentence != PLAN_FAILURE_MARKER
    ]
    reasons = plan + sentences
    if not failures.gate_passed and not sentences and failures.gate_reasoning:
        reasons.append(failures.gate_reasoning)
    return reasons


NUDGE_REJECTED_PREFIX = "Nudged draft rejected; the current draft is unchanged"


def nudge_rejection_message(reasons: Sequence[str]) -> str:
    return f"{NUDGE_REJECTED_PREFIX}: " + "; ".join(list(reasons)[:3])
