"""Deterministic checks for the reply plan (issue #5).

The model proposes a plan; code decides whether it is usable (the ADR-0006 /
ADR-0079 rule: no self-attestation). Validation runs inside the plan_reply
activity, so its result is recorded in workflow history and a later change to
these rules cannot make an in-flight Review workflow replay differently.
"""

import re
from typing import List, Optional, Tuple

from backend.gate.sentence_boundary import CONTINUE, is_decoration, period_boundary
from backend.models.evidence import Segment
from backend.models.reply_plan import REPLY_TYPES, ReplyPlan

MAX_REASON_CHARS = 200

# URLs can contain "?" in their query string; that is not a question. Trailing
# sentence punctuation after a URL ("Have you tried https://example.com?") is
# not part of the URL and stays visible.
_URL_RE = re.compile(r"https?://\S+?(?=[.,;:!?？)\]}»\"'”’]*(?:\s|$))", re.IGNORECASE)
# A sentence ends at a run of terminators (optionally followed by closing
# quotes or brackets) that is followed by whitespace or the end of the text, or
# at a newline. "Node.js" and "3.5" do not end a sentence; '?" ' does. A "?"
# or "!" run straight into a letter or digit ("working?Yes.", "agree?yes")
# also ends one; periods are excluded from that rule so "Node.js" stays whole.
# A single period after an abbreviation or number is a boundary unless the
# claim gate's rule says it clearly is not (CONTINUE: "Is the U.S.A. market
# ready?", "Dr. Smith, can you help?"). An AMBIGUOUS period ("in 2021.
# Canals or railways?") is a boundary here, so a statement before a question
# never counts as part of it (issue #10; backend.gate.sentence_boundary).
# A period with no space before a capitalized word ("We grew.Is that good?")
# is a boundary as well; "Node.js" and "ASP.NET" are not.
_BOUNDARY_RE = re.compile(
    r"[.!?？]+[\"'”’)\]}»]*(?=\s|$)|[!?？]+[\"'”’)\]}»]*(?=\w)|(?<=[a-z][a-z])\.(?=[A-Z][a-z])|\n"
)
_QUESTION_MARKS = ("?", "？")
_CLOSERS = "\"'”’)]}»"
_NEXT_WORD_RE = re.compile(r"\s*(\S+)")
_NON_SPACE_RE = re.compile(r"\S")


def _period_continues_sentence(text: str, m: "re.Match[str]", first_word_start: int) -> bool:
    """True if boundary m is a single period that clearly does not end the sentence.

    `first_word_start` is where the current sentence's first word begins. Work
    is bounded by the lengths of the word before m and the gap after it, so a
    long post stays linear.
    """
    if m.group().rstrip(_CLOSERS) != ".":
        return False
    if m.end() < len(text) and not text[m.end()].isspace():
        # The no-space alternative ("We grew.Is that good?") is always a
        # boundary; skipping the word scan keeps long unbroken tokens linear.
        return False
    word_start = m.start()
    while word_start > 0 and not text[word_start - 1].isspace():
        word_start -= 1
    word = text[word_start:m.end()]
    following = _NEXT_WORD_RE.match(text, m.end())
    next_word = following.group(1) if following else None
    return period_boundary(word, next_word, sentence_start=word_start <= first_word_start) == CONTINUE


def _sentence_spans(text: str) -> List[Tuple[int, int]]:
    spans = []
    start = 0
    first_word_start = None  # found once per sentence
    for m in _BOUNDARY_RE.finditer(text):
        if first_word_start is None:
            first = _NON_SPACE_RE.search(text, start)
            first_word_start = first.start() if first else len(text)
        if _period_continues_sentence(text, m, first_word_start):
            continue
        end = m.start() if m.group() == "\n" else m.end()
        if text[start:end].strip():
            spans.append((start, end))
        start = m.end()
        first_word_start = None
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


def question_spans(post_text: str) -> List[Tuple[int, int]]:
    """Return (start, end) character spans of the sentences in post_text that are questions.

    A question is a sentence whose terminators include "?" (or fullwidth "？").
    URLs are masked first, so "https://example.com/a?b=1" is not a question.
    """
    masked = _URL_RE.sub(lambda m: " " * len(m.group()), post_text)
    # Emoji after a terminator ("We launched!\U0001F680 Any tips?") must not hide the
    # boundary; one space per code point keeps the indices of post_text.
    masked = "".join(" " if is_decoration(ch) else ch for ch in masked)
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
