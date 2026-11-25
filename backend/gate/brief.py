"""The product brief as numbered sentences (issue #12).

Written drafts (ADR-0006 amendment 2026-11-24) are checked against the brief:
the checker cites brief sentences by id, and code confirms the ids exist. This
module turns the founder's brief (Markdown or plain text) into that numbered
list, deterministically.

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
  ("Not supported yet:") when present, and the items it is nested under
  ("Integrations we do not support > Slack").
- A line that ends in a colon is the context of the list or the one
  paragraph that follows it (with or without a blank line between), and is a
  sentence of its own only when nothing follows. Lead-ins chain ("Not
  supported yet:" then "Integrations:" then a list).
- The sentence right before a list is the context of its items even without
  a colon (a bold "Not supported yet" line, "We do not integrate with these
  tools"), and stays a sentence of its own.
- A prose "sentence" of one or two words is joined to the sentence before it
  ("We do not offer these yet. Slack. Teams." is one sentence): alone it
  would lose what the sentence before says about it.
- Text without a letter or digit (a horizontal rule) is not a sentence.
- Ids are "b1", "b2", ... in document order.

Nothing in the brief is a feature term. The feature-term trigger reads only
the watch's "Key features" field (George's decision of 2026-11-25; see
backend.gate.written_check.clean_feature_terms), as the name trigger reads
only "Product name" and "Also called".

Like every sentence splitter, this runs only inside activities, never in
workflow code (see ADR-0006, 2026-11-21 amendment, item 2).
"""

import re
from typing import List, Optional, Tuple

from backend.gate.sentence_split import split_into_sentences
from backend.gate.sentence_boundary import merge_at_unclear_boundaries
from backend.models.written import BriefSentence

# A prose "sentence" with fewer words is joined to the sentence before it
MIN_SENTENCE_WORDS = 3

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s*(.*?)\s*#*\s*$")
# A Setext underline: the paragraph above it is a heading (=== level 1, --- level 2)
_SETEXT_RE = re.compile(r"^\s{0,3}(=+|-{2,})\s*$")
_LIST_ITEM_RE = re.compile(r"^(\s*)(?:[-*+\u2022]|\d{1,3}[.)])\s+(.+?)\s*$")
_MARKUP_RE = re.compile(r"[*_`]+")
_WORD_RE = re.compile(r"[^\W_]+")


def _clean_context(text: str) -> str:
    return " ".join(_MARKUP_RE.sub("", text).split()).rstrip(":").strip()


def _indent(whitespace: str) -> int:
    return len(whitespace.expandtabs(4))


def _parse(product_brief: str) -> List[Tuple[str, str]]:
    """Return the brief's (text, context) units in order."""
    units: List[Tuple[str, str]] = []
    headings: List[Tuple[int, str]] = []  # (level, text): the open heading path
    colon_lead: Optional[str] = None  # lead-in of the list or paragraph being read
    pending_lead: Optional[str] = None  # a "...:" sentence that may introduce what follows
    pending_parent: Optional[str] = None  # the lead-in the pending lead-in itself stands under
    # The last sentence of the paragraph just read, with its own lead-in: if a
    # list comes next, this sentence introduces it
    intro: Optional[Tuple[Optional[str], str]] = None
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
        nonlocal pending_lead, pending_parent, colon_lead, intro
        if not paragraph:
            return
        text = " ".join(paragraph)
        paragraph.clear()
        sentences = merge_at_unclear_boundaries(split_into_sentences(text))
        lead, colon_lead = colon_lead, None  # a lead-in covers one paragraph
        if sentences and sentences[-1].rstrip().endswith(":"):
            # A lead-in under a lead-in keeps it: "Not supported yet:" then
            # "Integrations:" then a list
            pending_lead, pending_parent = sentences.pop(), lead
        # A one- or two-word "sentence" cannot stand alone as evidence: in
        # "We do not offer these yet. Slack. Teams." the names belong to the
        # sentence before them. Joining only gives the reader more context.
        whole: List[str] = []
        for sentence in sentences:
            if whole and len(_WORD_RE.findall(sentence)) < MIN_SENTENCE_WORDS:
                whole[-1] = f"{whole[-1]} {sentence}"
            else:
                whole.append(sentence)
        for sentence in whole:
            add_unit(sentence, context(lead))
        intro = (lead, whole[-1]) if whole and pending_lead is None else None

    def emit_pending_lead() -> None:
        """Nothing followed the lead-in: it is a sentence of its own."""
        nonlocal pending_lead, pending_parent
        if pending_lead is not None:
            add_unit(pending_lead, context(pending_parent))
            pending_lead = pending_parent = None

    def take_pending_lead() -> None:
        """The lead-in introduces what follows: it becomes its context."""
        nonlocal pending_lead, pending_parent, colon_lead
        if pending_lead is not None:
            colon_lead = " > ".join(
                part for part in (pending_parent, _clean_context(pending_lead)) if part
            )
            pending_lead = pending_parent = None

    def open_heading(level: int, text: str) -> None:
        nonlocal colon_lead, intro
        while headings and headings[-1][0] >= level:
            headings.pop()
        if text:
            headings.append((level, text))
        colon_lead = intro = None
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
            elif not items and intro is not None:
                # The sentence right before a list introduces it, with or
                # without a colon: a bold "Not supported yet" line, or "We
                # do not integrate with these tools" (it stays a sentence too)
                colon_lead = " > ".join(part for part in (intro[0], _clean_context(intro[1])) if part)
            intro = None
            indent, item = _indent(item_match.group(1)), item_match.group(2)
            while items and items[-1][0] >= indent:
                items.pop()
            parents = [_clean_context(open_item[1]) for open_item in items]
            # Everything the item stands under, so an item under
            # "Not supported > Integrations" keeps the "not"
            index = add_unit(item, context(colon_lead, *parents))
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
                colon_lead = intro = None
                take_pending_lead()
            paragraph.append(line.strip())
            if line.rstrip().endswith(":"):
                # A lead-in line ends its paragraph even without a blank line
                # after it, so "Not supported yet:" stays the context of the
                # lines below instead of being joined with their first sentence
                flush_paragraph()
    flush_paragraph()
    emit_pending_lead()
    return units


def split_brief(product_brief: str) -> List[BriefSentence]:
    """The brief's sentences in order, with ids "b1", "b2", ..."""
    units = _parse(product_brief or "")
    return [
        BriefSentence(id=f"b{i}", text=text, context=context)
        for i, (text, context) in enumerate(units, start=1)
    ]


def render_brief_sentence(sentence: BriefSentence) -> str:
    """One brief sentence as prompts and failure reasons show it."""
    if sentence.context:
        return f"[{sentence.id}] ({sentence.context}) {sentence.text}"
    return f"[{sentence.id}] {sentence.text}"
