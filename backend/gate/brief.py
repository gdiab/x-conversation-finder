"""The product brief as numbered sentences, and its feature terms (issue #12).

Written drafts (ADR-0006 amendment 2026-11-24) are checked against the brief:
the checker cites brief sentences by id, and code confirms the ids exist. This
module turns the founder's brief (Markdown or plain text) into that numbered
list, deterministically, and reads the feature terms the brief lists.

Rules:
- A heading sets the context of what follows and is not a sentence itself.
- Prose is split with the claim gate's splitter (split_into_sentences), then
  sentences are joined again wherever the boundary is unclear
  (merge_at_unclear_boundaries), so a brief sentence is never a fragment.
- A list item is one sentence. Its context is the heading it stands under
  and the lead-in line that ends in a colon ("Key features:"), when present.
- Ids are "b1", "b2", ... in document order.

Feature terms are the short items (at most MAX_FEATURE_TERM_WORDS words) of a
list whose lead-in or heading contains the word "feature" or "features", and
the comma-separated terms after such a lead-in on one line ("Features: a, b").
Nothing else in the brief is a feature term: the trigger must be predictable
from what the founder wrote.

Like every sentence splitter, this runs only inside activities, never in
workflow code (see ADR-0006, 2026-11-21 amendment, item 2).
"""

import re
from typing import List, Optional, Tuple

from backend.activities.claim_gate_activity import split_into_sentences
from backend.gate.sentence_boundary import merge_at_unclear_boundaries
from backend.models.written import BriefSentence

MAX_FEATURE_TERM_WORDS = 6

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*(.*?)\s*#*\s*$")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+•]|\d{1,3}[.)])\s+(.+?)\s*$")
_FEATURE_WORD_RE = re.compile(r"\bfeatures?\b", re.IGNORECASE)
# "Key features: a, b and c" on one line
_INLINE_FEATURES_RE = re.compile(r"^([^:]*\bfeatures?\b[^:]*):\s*(\S.*)$", re.IGNORECASE)
_INLINE_SEPARATOR_RE = re.compile(r"\s*[,;]\s*(?:and\s+)?|\s+and\s+", re.IGNORECASE)
# Where a list item's term ends and its description starts
_TERM_END_RE = re.compile(r":|\s[-–—]\s|\(|\.\s")
_MARKUP_RE = re.compile(r"[*_`]+")
_WORD_RE = re.compile(r"[^\W_]+")


def _clean_context(text: str) -> str:
    return " ".join(_MARKUP_RE.sub("", text).split()).rstrip(":").strip()


def _parse(product_brief: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Return ((text, context) units in order, feature term candidates)."""
    units: List[Tuple[str, str]] = []
    terms: List[str] = []
    heading = ""
    colon_lead: Optional[str] = None  # lead-in of the list being read
    pending_lead: Optional[str] = None  # a "...:" sentence that may introduce a list
    paragraph: List[str] = []
    in_list = False

    def flush_paragraph() -> None:
        nonlocal pending_lead
        if not paragraph:
            return
        text = " ".join(paragraph)
        paragraph.clear()
        inline = _INLINE_FEATURES_RE.match(text)
        if inline:
            terms.extend(_INLINE_SEPARATOR_RE.split(inline.group(2).rstrip(".")))
        sentences = merge_at_unclear_boundaries(split_into_sentences(text))
        if sentences and sentences[-1].rstrip().endswith(":"):
            pending_lead = sentences.pop()
        units.extend((s, heading) for s in sentences)

    def emit_pending_lead() -> None:
        nonlocal pending_lead
        if pending_lead is not None:
            units.append((pending_lead, heading))
            pending_lead = None

    for line in product_brief.splitlines():
        if not line.strip():
            flush_paragraph()
            continue
        heading_match = _HEADING_RE.match(line) if line.lstrip().startswith("#") else None
        item_match = _LIST_ITEM_RE.match(line)
        if heading_match:
            flush_paragraph()
            emit_pending_lead()
            heading = _clean_context(heading_match.group(1))
            colon_lead, in_list = None, False
        elif item_match:
            flush_paragraph()
            if pending_lead is not None:
                colon_lead, pending_lead = _clean_context(pending_lead), None
            elif not in_list:
                colon_lead = None
            in_list = True
            # Both, so an item under "Not supported > Integrations" keeps the "not"
            context = " > ".join(part for part in (heading, colon_lead) if part)
            item = item_match.group(1)
            units.append((item, context))
            if _FEATURE_WORD_RE.search(context):
                terms.append(item)
        else:
            if not paragraph:
                emit_pending_lead()
                colon_lead, in_list = None, False
            paragraph.append(line.strip())
    flush_paragraph()
    emit_pending_lead()
    return units, terms


def split_brief(product_brief: str) -> List[BriefSentence]:
    """The brief's sentences in order, with ids "b1", "b2", ..."""
    units, _ = _parse(product_brief or "")
    return [
        BriefSentence(id=f"b{i}", text=text, context=context)
        for i, (text, context) in enumerate(units, start=1)
    ]


def _clean_term(raw: str) -> str:
    term = _MARKUP_RE.sub("", raw)
    end = _TERM_END_RE.search(term)
    if end:
        term = term[:end.start()]
    return " ".join(term.split()).strip(" .,;:!?\"'“”‘’")


def brief_feature_terms(product_brief: str) -> List[str]:
    """Feature terms the brief lists (see the module docstring), in order, without repeats."""
    _, candidates = _parse(product_brief or "")
    terms: List[str] = []
    seen = set()
    for raw in candidates:
        term = _clean_term(raw)
        words = _WORD_RE.findall(term)
        if not 1 <= len(words) <= MAX_FEATURE_TERM_WORDS or not any(w.isalpha() for w in words):
            continue
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            terms.append(term)
    return terms


def render_brief_sentence(sentence: BriefSentence) -> str:
    """One brief sentence as prompts and failure reasons show it."""
    if sentence.context:
        return f"[{sentence.id}] ({sentence.context}) {sentence.text}"
    return f"[{sentence.id}] {sentence.text}"
