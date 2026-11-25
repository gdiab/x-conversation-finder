"""The product brief as numbered sentences, and its feature terms (issue #12).

Written drafts (ADR-0006 amendment 2026-11-24) are checked against the brief:
the checker cites brief sentences by id, and code confirms the ids exist. This
module turns the founder's brief (Markdown or plain text) into that numbered
list, deterministically, and reads the feature terms the brief lists.

Rules:
- A heading sets the context of what follows and is not a sentence itself.
  Nested headings keep their parents ("Not supported yet > Integrations"),
  ATX ("## Title") and Setext (a title underlined with === or ---) alike.
- Prose is split with the claim gate's splitter (split_into_sentences), then
  sentences are joined again wherever the boundary is unclear
  (merge_at_unclear_boundaries), so a brief sentence is never a fragment.
- A list item is one sentence, with its continuation lines ("- Not
  supported:" followed by an indented "Slack and Teams." is one sentence).
  Its context is the heading path, the lead-in line that ends in a colon
  ("Key features:") when present, and the items it is nested under
  ("Integrations we do not support > Slack").
- A line that ends in a colon is the context of the list or paragraph that
  follows it, and is a sentence of its own only when nothing follows.
- Text without a letter or digit (a horizontal rule) is not a sentence.
- Ids are "b1", "b2", ... in document order.

Feature terms are the short items (at most MAX_FEATURE_TERM_WORDS words) of a
list whose nearest heading, lead-in line or parent item contains the word
"feature" or "features", and the comma-separated terms after such a lead-in
on one line ("Features: a, b"). Nothing else in the brief is a feature term:
the trigger must be predictable from what the founder wrote.

Like every sentence splitter, this runs only inside activities, never in
workflow code (see ADR-0006, 2026-11-21 amendment, item 2).
"""

import re
from typing import List, Optional, Tuple

from backend.gate.sentence_split import split_into_sentences
from backend.gate.sentence_boundary import merge_at_unclear_boundaries
from backend.models.written import BriefSentence

MAX_FEATURE_TERM_WORDS = 6

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s*(.*?)\s*#*\s*$")
# A Setext underline: the paragraph above it is a heading (=== level 1, --- level 2)
_SETEXT_RE = re.compile(r"^\s{0,3}(=+|-{2,})\s*$")
_LIST_ITEM_RE = re.compile(r"^(\s*)(?:[-*+\u2022]|\d{1,3}[.)])\s+(.+?)\s*$")
_FEATURE_WORD_RE = re.compile(r"\bfeatures?\b", re.IGNORECASE)
# "Key features: a, b and c" on one line
_INLINE_FEATURES_RE = re.compile(r"^([^:]*\bfeatures?\b[^:]*):\s*(\S.*)$", re.IGNORECASE)
_INLINE_SEPARATOR_RE = re.compile(r"\s*[,;]\s*(?:and\s+)?|\s+and\s+", re.IGNORECASE)
# Where a list item's term ends and its description starts
_TERM_END_RE = re.compile(r":|\s[-\u2013\u2014]\s|\(|\.\s")
_MARKUP_RE = re.compile(r"[*_`]+")
_WORD_RE = re.compile(r"[^\W_]+")


def _clean_context(text: str) -> str:
    return " ".join(_MARKUP_RE.sub("", text).split()).rstrip(":").strip()


def _indent(whitespace: str) -> int:
    return len(whitespace.expandtabs(4))


def _parse(product_brief: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Return ((text, context) units in order, feature term candidates)."""
    units: List[Tuple[str, str]] = []
    terms: List[str] = []
    headings: List[Tuple[int, str]] = []  # (level, text): the open heading path
    colon_lead: Optional[str] = None  # lead-in of the list or paragraph being read
    pending_lead: Optional[str] = None  # a "...:" sentence that may introduce what follows
    paragraph: List[str] = []
    # The open items of the list being read: [indent, text, index in units or None]
    items: List[list] = []
    blank_since_item = False

    def context(*inner: Optional[str]) -> str:
        path = [text for _, text in headings]
        return " > ".join(part for part in (*path, *inner) if part)

    def add_unit(text: str, where: str) -> Optional[int]:
        if not _WORD_RE.search(text):  # a horizontal rule or stray markup is not a sentence
            return None
        units.append((text, where))
        return len(units) - 1

    def flush_paragraph() -> None:
        nonlocal pending_lead, colon_lead
        if not paragraph:
            return
        text = " ".join(paragraph)
        paragraph.clear()
        inline = _INLINE_FEATURES_RE.match(text)
        if inline:
            terms.extend(_INLINE_SEPARATOR_RE.split(inline.group(2).rstrip(".")))
        sentences = merge_at_unclear_boundaries(split_into_sentences(text))
        lead, colon_lead = colon_lead, None  # a lead-in covers one paragraph
        if sentences and sentences[-1].rstrip().endswith(":"):
            pending_lead = sentences.pop()
        for sentence in sentences:
            add_unit(sentence, context(lead))

    def emit_pending_lead() -> None:
        """Nothing followed the lead-in: it is a sentence of its own."""
        nonlocal pending_lead
        if pending_lead is not None:
            add_unit(pending_lead, context())
            pending_lead = None

    def take_pending_lead() -> None:
        """The lead-in introduces what follows: it becomes its context."""
        nonlocal pending_lead, colon_lead
        if pending_lead is not None:
            colon_lead, pending_lead = _clean_context(pending_lead), None

    def open_heading(level: int, text: str) -> None:
        nonlocal colon_lead
        while headings and headings[-1][0] >= level:
            headings.pop()
        if text:
            headings.append((level, text))
        colon_lead = None
        items.clear()

    for line in product_brief.splitlines():
        if not line.strip():
            flush_paragraph()
            blank_since_item = True
            continue
        setext = _SETEXT_RE.match(line)
        if setext:
            if paragraph:
                # The paragraph above is this heading's text, not prose
                title = _clean_context(" ".join(paragraph))
                paragraph.clear()
                emit_pending_lead()
                open_heading(1 if setext.group(1).startswith("=") else 2, title)
            continue  # otherwise a horizontal rule
        heading_match = _HEADING_RE.match(line) if line.lstrip().startswith("#") else None
        item_match = _LIST_ITEM_RE.match(line)
        if heading_match:
            flush_paragraph()
            emit_pending_lead()
            open_heading(len(heading_match.group(1)), _clean_context(heading_match.group(2)))
        elif item_match:
            flush_paragraph()
            if pending_lead is not None:
                take_pending_lead()
                items.clear()
            indent, item = _indent(item_match.group(1)), item_match.group(2)
            while items and items[-1][0] >= indent:
                items.pop()
            parents = [_clean_context(open_item[1]) for open_item in items]
            # Everything the item stands under, so an item under
            # "Not supported > Integrations" keeps the "not"
            index = add_unit(item, context(colon_lead, *parents))
            nearest = (parents[-1:] or [colon_lead or ""]) + [headings[-1][1] if headings else ""]
            if any(_FEATURE_WORD_RE.search(part) for part in nearest):
                terms.append(item)
            items.append([indent, item, index])
            blank_since_item = False
        elif items and not paragraph and (
            not blank_since_item or _indent(line[: len(line) - len(line.lstrip())]) > items[-1][0]
        ):
            # A continuation line of the open list item (wrapped text, or an
            # indented paragraph inside it): one sentence with the item, so
            # "- Not supported:" keeps hold of the "Slack and Teams." below it
            open_item = items[-1]
            open_item[1] = f"{open_item[1]} {line.strip()}"
            if open_item[2] is None:
                open_item[2] = add_unit(open_item[1], context(colon_lead, *(
                    _clean_context(parent[1]) for parent in items[:-1]
                )))
            else:
                units[open_item[2]] = (open_item[1], units[open_item[2]][1])
        else:
            if not paragraph:
                items.clear()
                colon_lead = None
                take_pending_lead()
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
