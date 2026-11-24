"""Deterministic checks for the reply plan (issue #5).

The model proposes a plan; code decides whether it is usable (the ADR-0006 /
ADR-0079 rule: no self-attestation). Validation runs inside the plan_reply
activity, so its result is recorded in workflow history and a later change to
these rules cannot make an in-flight Review workflow replay differently.
"""

import re
from typing import List, Optional, Tuple

from backend.models.evidence import Segment
from backend.models.reply_plan import REPLY_TYPES, ReplyPlan

MAX_REASON_CHARS = 200

# URLs can contain "?" in their query string; that is not a question. Trailing
# sentence punctuation after a URL ("Have you tried https://example.com?") is
# not part of the URL and stays visible.
_URL_RE = re.compile(r"https?://\S+?(?=[.,;:!?？)\]\"'”’]*(?:\s|$))", re.IGNORECASE)
# A sentence ends at a run of terminators (optionally followed by closing
# quotes or brackets) that is followed by whitespace or the end of the text, or
# at a newline. "Node.js" and "3.5" do not end a sentence; '?" ' does. A "?"
# or "!" run straight into a letter or digit ("working?Yes.", "agree?yes")
# also ends one; periods are excluded from that rule so "Node.js" stays whole.
_BOUNDARY_RE = re.compile(
    r"[.!?？]+[\"'”’)\]]*(?=\s|$)|[!?？]+[\"'”’)\]]*(?=\w)|\n"
)
_QUESTION_MARKS = ("?", "？")


def _sentence_spans(text: str) -> List[Tuple[int, int]]:
    spans = []
    start = 0
    for m in _BOUNDARY_RE.finditer(text):
        end = m.start() if m.group() == "\n" else m.end()
        if text[start:end].strip():
            spans.append((start, end))
        start = m.end()
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


def question_spans(post_text: str) -> List[Tuple[int, int]]:
    """Return (start, end) character spans of the sentences in post_text that are questions.

    A question is a sentence whose terminators include "?" (or fullwidth "？").
    URLs are masked first, so "https://example.com/a?b=1" is not a question.
    """
    masked = _URL_RE.sub(lambda m: " " * len(m.group()), post_text)
    return [
        (start, end)
        for start, end in _sentence_spans(masked)
        if any(q in masked[start:end] for q in _QUESTION_MARKS) and masked[start:end].strip(" ?？")
    ]


def _occurrences(text: str, sub: str) -> List[int]:
    starts = []
    i = text.find(sub)
    while i != -1:
        starts.append(i)
        i = text.find(sub, i + 1)
    return starts


def validate_reply_plan(plan: ReplyPlan, post_text: str) -> List[str]:
    """Check a proposed plan against the post. Returns failure reasons; empty means valid.

    Rules:
    - reply_type is one of REPLY_TYPES.
    - target_point is non-empty, contains a letter or digit, and is an exact
      substring of the post text.
    - answer_question requires a question in the post, and the target_point
      must lie within the post's question sentences (it is the question being
      answered, or part of it).
    - mention_product is a boolean.
    - mention_product_reason is one non-empty line of at most MAX_REASON_CHARS.
    """
    errors: List[str] = []

    if plan.reply_type not in REPLY_TYPES:
        # The model's value is not echoed: errors are stored on the card and
        # in skip reasons, and model text can quote the post (removal duty).
        errors.append(f"reply_type must be one of {', '.join(REPLY_TYPES)}")

    target = plan.target_point if isinstance(plan.target_point, str) else ""
    target_found = False
    if not target.strip():
        errors.append("target_point is empty; copy the exact span of the post the reply responds to")
    elif not any(ch.isalnum() for ch in target):
        errors.append("target_point must contain words from the post")
    elif target not in post_text:
        errors.append(
            "target_point is not an exact substring of the post; copy it character for character"
        )
    else:
        target_found = True

    if plan.reply_type == "answer_question":
        spans = question_spans(post_text)
        if not spans:
            errors.append("reply_type answer_question requires a question in the post, and the post has none")
        elif target_found:
            def inside_questions(start: int) -> bool:
                return all(
                    ch.isspace() or any(q_start <= i < q_end for q_start, q_end in spans)
                    for i, ch in enumerate(target, start)
                )

            if not any(inside_questions(start) for start in _occurrences(post_text, target)):
                errors.append(
                    "for answer_question, target_point must be the question being answered (or part of it)"
                )

    if not isinstance(plan.mention_product, bool):
        errors.append("mention_product must be true or false")

    reason = plan.mention_product_reason if isinstance(plan.mention_product_reason, str) else ""
    if not reason.strip():
        errors.append("mention_product_reason is empty; give a one-line reason")
    elif "\n" in reason.strip():
        errors.append("mention_product_reason must be one line")
    elif len(reason.strip()) > MAX_REASON_CHARS:
        errors.append(f"mention_product_reason must be at most {MAX_REASON_CHARS} characters")

    return errors


def plan_conformance_failures(segments: List[Segment], plan: Optional[ReplyPlan]) -> List[str]:
    """Check that an assembled draft follows the plan's product decision.

    Returns failure reasons; empty means the draft conforms (or there is no
    plan). The draft_reply activity records the result on the draft, and the
    Review workflow treats a non-conforming draft like a claim-gate failure:
    one redraft with the reasons as the steer, then skip. The draft is never
    altered here, and the claim gate is unchanged.

    Under assembled drafts the product is mentioned only through a claim
    segment: ADR-0006 defines a claim as a product claim (a verbatim sentence
    from the founder's material), and the fixed phrases never name the
    product. So:
    - mention_product=True requires a claim segment;
    - mention_product=False forbids claim segments.
    """
    if plan is None:
        return []
    if not segments:
        return ["the draft has no segments"]
    has_claim = any(s.kind == "claim" for s in segments)
    if plan.mention_product is False and has_claim:
        return ["the reply plan says not to mention the product, but the draft has a claim segment"]
    if plan.mention_product is True and not has_claim:
        return ["the reply plan says to mention the product, but the draft has no claim segment"]
    return []
