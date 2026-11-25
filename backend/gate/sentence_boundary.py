"""When does a period end a sentence? Shared, deterministic rules (issue #10).

Both sentence splitters use these rules: the claim gate's splitter for founder
passages and edited drafts (``split_into_sentences``, ADR-0006), and the reply
plan's question-span splitter for post text (``backend/planning``). A word that
ends in a single period ends a sentence unless it is one of the kinds below.

- Company suffixes (Inc. Ltd. Co. Corp.; QA R14): end a sentence when the next
  word starts with a capital letter.
- Prefix abbreviations (Dr. Mr. Mrs. Ms. Prof. vs. e.g. i.e. approx.): never
  end a sentence. Titles come before a name and the others introduce what
  follows, so a capitalized next word ("Dr. Smith", "Foo vs. It") belongs to
  the same sentence (QA R13).
- Abbreviations that can also end a sentence: etc., St., Ph.D., dotted
  initialisms (U.S., U.S.A., a.m.), single-letter initials (J., B.), and
  numbers (2021., 1.): end a sentence only when the next word clearly starts a
  new one, that is, it is a capitalized word from SENTENCE_STARTERS ("in the
  U.S. We ship" splits; "the U.S.A. market" and "J. Smith" do not; "Steps:
  1. Install" stays whole, "in 2021. We" splits). The number rule keeps the
  decimal fix (S-5: "99.99%" never splits) and list markers whole.

A period run ("..."), "!" and "?" are handled by the callers and always end a
sentence.

Changing these rules changes split points, and so the sentence indices that
claim segments reference. See ADR-0006 (2026-11-21 amendment, item 2) for what
that affects.
"""

import re
from typing import Optional

COMPANY_SUFFIXES = ("Inc.", "Ltd.", "Co.", "Corp.")
PREFIX_ABBREVIATIONS = ("Dr.", "Mr.", "Mrs.", "Ms.", "Prof.", "vs.", "e.g.", "i.e.", "approx.")
TERMINAL_ABBREVIATIONS = ("etc.", "St.", "Ph.D.")

# Capitalized words that clearly start a new sentence after an abbreviation
# that can also end one. Closed list: pronouns, determiners, connectives and
# question words. Words that are also common names ("Will", "May") are left
# out so "J. Will Smith" stays whole.
SENTENCE_STARTERS = frozenset(
    """
    i we you he she it they me us our ours my your their his her its
    i'm i've i'd i'll we're we've we'd we'll you're you've it's they're
    the this that these those there here a an
    and but so then however also plus still yet thus hence therefore
    what why how when where who which whose
    what's how's where's who's there's that's here's let's
    if in on at for from with without after before since while
    now today yes no please thanks hi hello hey
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
_NUMBER_RE = re.compile(r".*\d\.")
_LEADING_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")


def _core(word: str) -> str:
    """The word without leading opening or trailing closing quotes and brackets."""
    return word.lstrip(_OPENERS).rstrip(_CLOSERS)


def abbreviation_kind(word: str) -> Optional[str]:
    """Classify a word that ends in a single period.

    Returns "company", "prefix", "terminal", "number" or None (an ordinary
    word, whose period ends the sentence).
    """
    core = _core(word)
    if not core.endswith(".") or core.endswith(".."):
        return None
    if _COMPANY_RE.search(core):
        return "company"
    if _PREFIX_RE.search(core):
        return "prefix"
    if (
        _TERMINAL_RE.search(core)
        or _INITIALISM_RE.fullmatch(core)
        or _INITIAL_RE.fullmatch(core)
    ):
        return "terminal"
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


def period_ends_sentence(word: str, next_word: Optional[str]) -> bool:
    """Does the period at the end of `word` end the sentence?

    `word` is a whitespace-delimited word whose last character, before any
    closing quotes or brackets, is a period. `next_word` is the following
    whitespace-delimited word, or None at the end of the text.
    """
    if next_word is None:
        return True
    kind = abbreviation_kind(word)
    if kind is None:
        return True
    if kind == "company":
        core = next_word.lstrip(_OPENERS)
        return bool(core) and core[0].isupper()
    if kind == "prefix":
        return False
    return starts_new_sentence(next_word)


def ends_with_abbreviation(sentence: str) -> bool:
    """True if the sentence's last word is an abbreviation (any kind but a number).

    QA R13: such a sentence is not selectable as a claim. Trimming its
    trailing period for flow would alter the abbreviation ("U.S." to "U.S"),
    and the claim gate inserts sentences verbatim apart from that trim.
    """
    words = sentence.split()
    if not words:
        return False
    return abbreviation_kind(words[-1]) in ("company", "prefix", "terminal")
