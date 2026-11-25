"""When does a period end a sentence? Shared, deterministic rules (issue #10).

Both sentence splitters use these rules: the claim gate's splitter for founder
passages and edited drafts (``split_into_sentences``, ADR-0006), and the reply
plan's question-span splitter for post text (``backend/planning``).

A word that ends in a single period ends the sentence (END) unless it is an
abbreviation or a number. Then the next word decides: CONTINUE when the
period clearly does not end the sentence, END when it clearly does, and
AMBIGUOUS when it might do either.

==================  =========================  ========================  ==========
word                next word lowercase        capitalized starter       other
                                               (SENTENCE_STARTERS)       capitalized
==================  =========================  ========================  ==========
company suffix      CONTINUE                   END                       END
(Inc. Ltd. Co. Corp., QA R14)
prefix              CONTINUE                   AMBIGUOUS                 CONTINUE
(Dr. Mr. Mrs. Ms. Prof. vs. e.g. i.e. approx.)
initial (J. B.)     CONTINUE                   AMBIGUOUS                 AMBIGUOUS
terminal            CONTINUE                   END                       AMBIGUOUS
(etc. St. Ph.D. U.S. U.S.A. a.m.)
number (2021. $5.)  CONTINUE                   END                       AMBIGUOUS
list marker (1.)    CONTINUE                   AMBIGUOUS, or CONTINUE as the first word
                                               of a sentence ("1. The product ...")
==================  =========================  ========================  ==========

A single capital letter before a capitalized word reads both as a name
initial ("J. Smith") and as a sentence end ("Plan B. Customers pay"), so it
is AMBIGUOUS: such a founder sentence is not selectable, and post text splits
there.

A word counts as ending in a period when its last character before any
closing quotes or brackets is one ('We ship in the U.S." Customers' is
checked like 'U.S. Customers'); sentence_terminator gives that character.

A next word starting with a digit or symbol counts as lowercase, except after
a terminal abbreviation, number or list marker, where it is AMBIGUOUS. The
number rules keep the decimal fix (S-5): "99.99%" contains no word-final
period and never splits.

AMBIGUOUS is resolved by the caller, in the safe direction for its use:

- The claim gate does not split there, and a sentence containing an
  AMBIGUOUS boundary is not selectable as a claim (has_ambiguous_boundary).
  A claim is then never a fragment of a founder sentence and never two
  founder sentences counted as one.
- The reply plan's question spans do split there, so a statement before a
  question never counts as part of the question.

A period run ("..."), "!" and "?" are handled by the callers and always end a
sentence.

Changing these rules changes split points, and so the sentence indices that
claim segments reference. Bump SPLITTER_VERSION when a split changes; see
ADR-0006 (2026-11-21 amendment, item 2).
"""

import re
from typing import Literal, Optional

# Version of the claim gate's sentence split. Drafting adapters stamp it on
# claim segments; the claim gate refuses a segment numbered under another
# version. 1 = before issue #10 (backend/gate/legacy_split_v1.py).
SPLITTER_VERSION = 2

END = "end"
CONTINUE = "continue"
AMBIGUOUS = "ambiguous"
Boundary = Literal["end", "continue", "ambiguous"]

COMPANY_SUFFIXES = ("Inc.", "Ltd.", "Co.", "Corp.")
PREFIX_ABBREVIATIONS = ("Dr.", "Mr.", "Mrs.", "Ms.", "Prof.", "vs.", "e.g.", "i.e.", "approx.")
TERMINAL_ABBREVIATIONS = ("etc.", "St.", "Ph.D.")

# Capitalized words that clearly start a new sentence. Closed list: pronouns,
# determiners, connectives, question words, and the first word of every fixed
# non-claim phrase (backend/gate/non_claim_set.py; a test enforces this), so a
# claim ending in a number followed by a phrase splits in the assembled draft.
# Words that are also common names ("Will", "May") are left out.
SENTENCE_STARTERS = frozenset(
    """
    i we you he she it they me us our ours my your their his her its
    i'm i've i'd i'll we're we've we'd we'll you're you've it's they're
    the this that these those there here a an
    and but so then however also plus still yet thus hence therefore
    what why how when where who which whose
    what's how's where's who's there's that's here's let's
    if in on at for from with without after before since while
    now today yes no please thanks hi hello hey happy feel
    is are was were do does did can could would should have has had
    """.split()
)

_OPENERS = "\"'([{<“‘«¿¡"
_CLOSERS = "\"')]}>”’»"


def _named(abbrevs) -> "re.Pattern[str]":
    # \b before the abbreviation: "Acme Inc." and "(Inc." match, "Zinc." does not.
    return re.compile(r"\b(?:" + "|".join(re.escape(a) for a in abbrevs) + r")$", re.IGNORECASE)


_COMPANY_RE = _named(COMPANY_SUFFIXES)
_PREFIX_RE = _named(PREFIX_ABBREVIATIONS)
_TERMINAL_RE = _named(TERMINAL_ABBREVIATIONS)
_INITIALISM_RE = re.compile(r"(?:[A-Za-z]\.){2,}")
_INITIAL_RE = re.compile(r"[A-Z]\.")
_LIST_MARKER_RE = re.compile(r"\d{1,3}\.")
_NUMBER_RE = re.compile(r".*\d\.")
_LEADING_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")


def _core(word: str) -> str:
    """The word without leading opening or trailing closing quotes and brackets."""
    return word.lstrip(_OPENERS).rstrip(_CLOSERS)


def abbreviation_kind(word: str) -> Optional[str]:
    """Classify a word that ends in a single period.

    Returns "company", "prefix", "initial", "terminal", "list_marker",
    "number" or None (an ordinary word, whose period ends the sentence).
    """
    core = _core(word)
    if not core.endswith(".") or core.endswith(".."):
        return None
    if _COMPANY_RE.search(core):
        return "company"
    if _PREFIX_RE.search(core):
        return "prefix"
    if _INITIAL_RE.fullmatch(core):
        return "initial"
    if _TERMINAL_RE.search(core) or _INITIALISM_RE.fullmatch(core):
        return "terminal"
    if _LIST_MARKER_RE.fullmatch(core):
        return "list_marker"
    if _NUMBER_RE.fullmatch(core):
        return "number"
    return None


def starts_new_sentence(next_word: str) -> bool:
    """True if next_word is a capitalized word from SENTENCE_STARTERS.

    A word that is itself an initial ("I.", "A.") does not count, so "J. A.
    Smith" stays whole.
    """
    core = next_word.lstrip(_OPENERS)
    if re.match(r"[A-Za-z]\.", core):
        return False
    m = _LEADING_WORD_RE.match(core)
    if not m or not m.group()[0].isupper():
        return False
    return m.group().replace("’", "'").lower() in SENTENCE_STARTERS


def period_boundary(word: str, next_word: Optional[str], *, sentence_start: bool = False) -> Boundary:
    """Classify the period at the end of `word` as END, CONTINUE or AMBIGUOUS.

    `word` is a whitespace-delimited word whose last character, before any
    closing quotes or brackets, is a period. `next_word` is the following
    whitespace-delimited word, or None at the end of the text.
    `sentence_start` is True when `word` is the first word of its sentence.
    """
    if next_word is None:
        return END
    kind = abbreviation_kind(word)
    if kind is None:
        return END
    following = next_word.lstrip(_OPENERS)
    first = following[:1]
    if first.islower() or (not first.isalpha() and kind in ("company", "prefix", "initial")):
        return CONTINUE
    if not first.isalpha():
        return AMBIGUOUS
    if kind == "company":
        return END
    if kind == "initial":
        return AMBIGUOUS
    starter = starts_new_sentence(next_word)
    if kind == "prefix":
        return AMBIGUOUS if starter else CONTINUE
    if kind == "list_marker":
        return CONTINUE if sentence_start else AMBIGUOUS
    return END if starter else AMBIGUOUS


def sentence_terminator(word: str) -> str:
    """The word's last character before closing quotes and brackets if it is
    '.', '!' or '?'; otherwise the empty string."""
    last = word.rstrip(_CLOSERS)[-1:]
    return last if last in (".", "!", "?") else ""


def has_ambiguous_boundary(sentence: str) -> bool:
    """True if a period inside the sentence might end a sentence (AMBIGUOUS).

    The claim gate's splitter does not split at an AMBIGUOUS period, so such a
    sentence may be two founder sentences; it is not selectable as a claim.
    """
    words = sentence.split()
    return any(
        sentence_terminator(word) == "." and period_boundary(word, words[i + 1], sentence_start=i == 0) == AMBIGUOUS
        for i, word in enumerate(words[:-1])
    )


def ends_with_abbreviation(sentence: str) -> bool:
    """True if the sentence's last word is an abbreviation or initial (not a number).

    QA R13: such a sentence is not selectable as a claim. Trimming its
    trailing period for flow would alter the abbreviation ("U.S." to "U.S"),
    and the claim gate inserts sentences verbatim apart from that trim.
    """
    words = sentence.split()
    if not words:
        return False
    return abbreviation_kind(words[-1]) in ("company", "prefix", "initial", "terminal")
