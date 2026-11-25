"""When does a period end a sentence? Shared, deterministic rules (issue #10).

Both sentence splitters use these rules: the claim gate's splitter for founder
passages and edited drafts (``split_into_sentences``, ADR-0006), and the reply
plan's question-span splitter for post text (``backend/planning``).

period_boundary classifies a word-final period by the word and the next word:

- END: certainly ends the sentence. After an ordinary word (one that is not
  in the lists below) and after a number before a word ("in 2021. We",
  "in 2021. customers", "99.9. Latency"): English has no mid-sentence "2021."
  outside list markers.
- CONTINUE: certainly does not. After any abbreviation or initial before a
  lowercase word ("the U.S.A. market", "j. smith"); after a prefix
  abbreviation (titles and Latin abbreviations, which do not end sentences)
  before a digit or a capitalized word that is not a sentence starter
  ("Dr. Smith", "e.g. Postgres", "approx. 10"); after a list marker at the
  start of a sentence before a lowercase word ("1. install the CLI").
- LIKELY_END / LIKELY_CONTINUE: unclear. Every other abbreviation, initial or
  list marker before a capitalized word, digit or symbol ("Acme Inc. 2024
  revenue doubled"), a list marker inside a sentence, and a list marker
  before a capitalized word ("99. Is that good?" may be a score). "U.S. We ship" is probably two
  sentences, but "the U.S. The Netherlands, and France" is one; "J. Smith" is
  probably one, but "Plan B. Customers pay" is two; company suffixes (QA R14)
  are probably sentence ends. LIKELY_END after terminal abbreviations
  (etc. U.S. Jr. ...) and company suffixes before a SENTENCE_STARTERS word or
  any capitalized word respectively; LIKELY_CONTINUE otherwise.

Callers resolve the unclear cases in the safe direction for their use:

- The claim gate's splitter splits at END and LIKELY_END, so indices follow
  the likely reading. A sentence that touches an unclear boundary, at its
  start, its end or inside it, is not selectable as a claim
  (sentence_has_unclear_boundary). A claim is then never a fragment of a
  founder sentence and never two founder sentences counted as one.
- The reply plan's question spans split at everything but CONTINUE, so a
  statement before a question never counts as part of the question.

A word ends in a period when its last character before any closing quotes or
brackets is one ('U.S." Customers' is checked like 'U.S. Customers');
sentence_terminator gives that character. A period run ("..."), "!" and "?"
end a sentence, except that any terminator inside closing quotes before a
lowercase word is unclear ('"Is it fast?" asked a customer'; word_boundary).
A terminator with no space before a capitalized word ("fast.It saves") is
unclear too: the claim splitter does not split there, and the sentence is not
selectable. Decimals (S-5: "99.99%") contain no word-final period
and never split.

Known limits: abbreviations missing from the lists below are ordinary words,
so their period is END; the lists cover the common ones. A lowercase word
after an abbreviation or initial continues the sentence, as issue #10
requires for "Is the U.S.A. market ready?", so text that starts a sentence
in lowercase right after one ("the U.S.A. customers pay monthly") is read
as one sentence.

Changing these rules changes split points, and so the sentence indices that
claim segments reference. Bump SPLITTER_VERSION when a split changes; see
ADR-0006 (2026-11-21 amendment, item 2).
"""

import re
from typing import List, Literal, Optional

# Version of the claim gate's sentence split. Drafting adapters stamp it on
# claim segments; the claim gate refuses a segment numbered under another
# version. 1 = before issue #10 (backend/gate/legacy_split_v1.py).
SPLITTER_VERSION = 2

END = "end"
CONTINUE = "continue"
LIKELY_END = "likely_end"
LIKELY_CONTINUE = "likely_continue"
UNCLEAR = (LIKELY_END, LIKELY_CONTINUE)
Boundary = Literal["end", "continue", "likely_end", "likely_continue"]

COMPANY_SUFFIXES = ("Inc.", "Ltd.", "Co.", "Corp.")
# Never end a sentence: titles before a name, and Latin abbreviations and
# reference words that introduce what follows.
PREFIX_ABBREVIATIONS = (
    "Dr.", "Mr.", "Mrs.", "Ms.", "Prof.", "Mt.", "Gen.", "Col.", "Lt.", "Sgt.",
    "Gov.", "Sen.", "Rep.", "Rev.", "Hon.",
    "vs.", "e.g.", "i.e.", "approx.", "cf.", "Fig.", "Vol.", "pp.",
)
# Can end a sentence or sit inside one.
TERMINAL_ABBREVIATIONS = (
    "etc.", "St.", "Ph.D.", "Jr.", "Sr.", "Ave.", "Rd.", "Blvd.", "Dept.", "Univ.",
    "Jan.", "Feb.", "Mar.", "Apr.", "Aug.", "Sep.", "Sept.", "Oct.", "Nov.", "Dec.",
)

# Capitalized words that usually start a new sentence. Closed list: pronouns,
# determiners, connectives, question words, and the first word of every fixed
# non-claim phrase (backend/gate/non_claim_set.py; a test enforces this).
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
# Any Unicode letter ([^\W\d_]): "É. Dubois" is an initial like "J. Smith".
_LETTER = r"[^\W\d_]"
_INITIALISM_RE = re.compile(rf"(?:{_LETTER}\.){{2,}}")
_INITIAL_RE = re.compile(rf"{_LETTER}\.")
_LIST_MARKER_RE = re.compile(r"\d{1,3}\.")
_NUMBER_RE = re.compile(r".*\d\.")
_LEADING_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")


def _core(word: str) -> str:
    """The word without leading opening or trailing closing quotes and brackets."""
    return word.lstrip(_OPENERS).rstrip(_CLOSERS)


def sentence_terminator(word: str) -> str:
    """The word's last character before closing quotes and brackets if it is
    '.', '!' or '?'; otherwise the empty string."""
    last = word.rstrip(_CLOSERS)[-1:]
    return last if last in (".", "!", "?") else ""


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

    A word that is itself an initial ("I.", "A.") does not count.
    """
    core = next_word.lstrip(_OPENERS)
    if _INITIAL_RE.match(core):
        return False
    m = _LEADING_WORD_RE.match(core)
    if not m or not m.group()[0].isupper():
        return False
    return m.group().replace("’", "'").lower() in SENTENCE_STARTERS


def period_boundary(word: str, next_word: Optional[str], *, sentence_start: bool = False) -> Boundary:
    """Classify the period at the end of `word` (see the module docstring).

    `word` is a whitespace-delimited word whose terminator is a period.
    `next_word` is the following whitespace-delimited word, or None at the end
    of the text. `sentence_start` is True when `word` is the first word of its
    sentence.
    """
    if next_word is None:
        return END
    kind = abbreviation_kind(word)
    if kind is None:
        return END
    first = next_word.lstrip(_OPENERS)[:1]
    if kind == "number":
        # No mid-sentence "2021." in English: ends before a word of either case.
        return END if first.isalpha() else LIKELY_CONTINUE
    if kind == "list_marker":
        # "1. install the CLI" opens a list item; "99. Is that good?" may be a
        # score before a question, so a capitalized word is unclear.
        return CONTINUE if sentence_start and first.islower() else LIKELY_CONTINUE
    if first.islower():
        return CONTINUE
    if not first.isalpha():
        # A digit or symbol: "approx. 10", "Fig. 3"; unclear after the others
        # ("Acme Inc. 2024 revenue doubled").
        return CONTINUE if kind == "prefix" else LIKELY_CONTINUE
    if kind == "prefix":
        return LIKELY_CONTINUE if starts_new_sentence(next_word) else CONTINUE
    if kind == "company":
        return LIKELY_END
    if kind == "initial":
        return LIKELY_CONTINUE
    return LIKELY_END if starts_new_sentence(next_word) else LIKELY_CONTINUE


def word_boundary(word: str, next_word: Optional[str], *, sentence_start: bool = False) -> Boundary:
    """Classify the end of a word whose terminator is '.', '!' or '?'.

    A terminator inside closing quotes or brackets before a lowercase word is
    unclear: '"Is it fast?" asked a customer' continues, '"Ship it." Then'
    does not. Otherwise '!' and '?' end the sentence, and a period follows
    period_boundary.
    """
    if next_word is None:
        return END
    if word != word.rstrip(_CLOSERS) and next_word.lstrip(_OPENERS)[:1].islower():
        return LIKELY_CONTINUE
    if sentence_terminator(word) == ".":
        return period_boundary(word, next_word, sentence_start=sentence_start)
    return END


# A terminator with no space before a capitalized word ("fast.It saves"): a
# missing space, or a joined name. "Node.js", "ASP.NET" and "U.S.A" do not match.
_JOINED_SENTENCES_RE = re.compile(r"[a-z]{2}[.!?][A-Z][a-z]")


def _boundary_after(sentence: str, next_sentence: str) -> Boundary:
    """The boundary between two consecutive sentences of a split."""
    words = sentence.split()
    if not words or not sentence_terminator(words[-1]):
        return END
    following = next_sentence.split()
    return word_boundary(words[-1], following[0] if following else None, sentence_start=len(words) == 1)


def has_unclear_boundary_inside(sentence: str) -> bool:
    """True if a terminator inside the sentence might end a sentence."""
    if _JOINED_SENTENCES_RE.search(sentence):
        return True
    words = sentence.split()
    return any(
        sentence_terminator(word)
        and word_boundary(word, words[i + 1], sentence_start=i == 0) in UNCLEAR
        for i, word in enumerate(words[:-1])
    )


def sentence_has_unclear_boundary(sentences: List[str], index: int) -> bool:
    """True if sentences[index] starts, ends or contains an unclear boundary.

    `sentences` is one split of a passage. Such a sentence may be a fragment
    of a founder sentence or two founder sentences, so it is not selectable.
    """
    sentence = sentences[index]
    if has_unclear_boundary_inside(sentence):
        return True
    if index > 0 and _boundary_after(sentences[index - 1], sentence) in UNCLEAR:
        return True
    if index + 1 < len(sentences) and _boundary_after(sentence, sentences[index + 1]) in UNCLEAR:
        return True
    return False


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
