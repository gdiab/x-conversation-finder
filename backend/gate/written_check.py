"""Product-claim check for written drafts (issue #12; ADR-0006 amendment 2026-11-24).

The model writes the reply as free text. This module is the code side of the
check, steps 2 to 4 of the amendment:

2. Code decides which sentences are product claims, conservatively. A
   sentence is checked as a product claim if a deterministic trigger fires
   (find_triggers) or the independent checker classifies it as about the
   product. Triggers can only add sentences to the checked set.
3. The checker returns `supported` with brief sentence ids, or `unsupported`
   with a reason. Code confirms that the cited ids exist and that every
   supported sentence cites at least one.
4. Sentences outside the checked set are not gated against the brief.

Everything here is deterministic: the checker's verdicts are an input
(ClaimVerdict), so the rules are tested without a model. Rules that go beyond
the amendment's wording, each of which can only add a rejection:
- A product claim must be a statement. A checked sentence that is a question
  fails, whatever the checker says ("Did you know ours does X?").
- A number in a supported claim must appear in a cited brief sentence, and a
  number that no brief sentence states fails before the checker is asked.
- The checker's answer fails closed: a missing or malformed verdict is read
  as "not supported".
- A sentence that may be two (a terminator inside it that might end a
  sentence, as in "Acme ships.It retries." or "works.it retries") fails, so
  the sentence and claim limits cannot be dodged by leaving out a space. A
  line break ends a sentence, and so does a fullwidth terminator.

The text limits are the amendment's: at most MAX_SENTENCES sentences, at most
MAX_CHARS characters, at most MAX_PRODUCT_CLAIMS product claim. A claim is
counted per sentence: one sentence that says two things about the product is
one claim, and the checker must find all of it in the brief.

A text that code can already reject is not sent to the checker
(PreparedCheck.needs_checker): no paid call is made for it.

Like every sentence splitter, this runs only inside activities, never in
workflow code.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from backend.gate.sentence_split import normalize_text, split_into_sentences
from backend.gate.sentence_boundary import SPLITTER_VERSION, has_unclear_boundary_inside
from backend.models.written import (
    VERDICT_NOT_CHECKED,
    VERDICT_NOT_PRODUCT,
    VERDICT_SUPPORTED,
    VERDICT_UNSUPPORTED,
    BriefSentence,
    ClaimCheckRequest,
    ClaimVerdict,
    SentenceCheck,
    TriggerHit,
    WrittenCheckResult,
    WrittenDraftInfo,
)

MAX_SENTENCES = 3
MAX_CHARS = 280
MAX_PRODUCT_CLAIMS = 1
MAX_REASON_CHARS = 200

# Version of the trigger rules below. Stored with each check, so a card says
# which rules decided it. Bump it when a trigger changes.
TRIGGER_RULES_VERSION = "1.0.0"  # 2026-11-24, issue #12

TRIGGER_PRODUCT_NAME = "product_name"
TRIGGER_FIRST_PERSON_PLURAL = "first_person_plural"
TRIGGER_NUMBER = "number"
TRIGGER_COMPARISON = "comparison"
TRIGGER_FEATURE_TERM = "feature_term"

NO_MENTION_PLAN_FAILURE = "the reply plan says not to mention the product, but the draft has a product claim"
MENTION_PLAN_FAILURE = "the reply plan says to mention the product, but the draft has no product claim"

TRIGGER_LABELS = {
    TRIGGER_PRODUCT_NAME: "names the product",
    TRIGGER_FIRST_PERSON_PLURAL: "uses we/our/us/ours",
    TRIGGER_NUMBER: "contains a number",
    TRIGGER_COMPARISON: "makes a comparison",
    TRIGGER_FEATURE_TERM: "uses a feature term from the brief",
}

FIRST_PERSON_PLURAL = frozenset({"we", "our", "ours", "us", "ourselves"})

# Number words; any digit counts too. The list is closed and read broadly,
# because the amendment says "any number": cardinals, ordinals, fractions and
# multipliers. "one", "first" and "second" are included although a reply often
# uses them as plain words ("which one", "First, ..."): code cannot tell the
# two uses apart, and the drafter is told to keep them out of every sentence
# but the claim.
NUMBER_WORDS = frozenset(
    """
    zero one two three four five six seven eight nine ten
    eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen
    twenty thirty forty fifty sixty seventy eighty ninety
    hundred thousand million billion trillion dozen
    hundreds thousands millions billions trillions dozens
    first second third fourth fifth sixth seventh eighth ninth tenth
    eleventh twelfth twentieth hundredth thousandth
    half halves quarter quarters thirds fourths fifths tenths
    twice thrice double doubles doubled triple triples tripled quadruple
    single couple
    """.split()
)

# Comparatives and superlatives that are comparisons on their own.
COMPARISON_WORDS = frozenset(
    """
    than vs versus unlike
    better worse best worst
    faster fastest slower slowest quicker quickest
    cheaper cheapest pricier
    easier easiest simpler simplest harder
    smaller smallest bigger biggest larger largest
    lighter lightest heavier safer safest stronger strongest
    superior inferior outperform outperforms outperformed
    """.split()
)
_COMPARISON_PHRASE_RE = re.compile(
    r"\bcompared (?:to|with)\b|\bin comparison\b|\b(?:the )?same as\b"
    r"|\bas (?!well\b|soon\b|long\b|far\b)[a-z]+ as\b"
)

_WORD_RE = re.compile(r"[^\W_]+")
_DIGIT_RUN_RE = re.compile(r"\d+")


def _fold(text: str) -> str:
    """Text for trigger matching: the claim gate's normalization (NFKC,
    look-alike letters, casefold, single spaces) without invisible format
    characters, so "our" with a zero-width space inside still reads "our"."""
    visible = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return normalize_text(visible)


def _word_spans(folded: str) -> List[Tuple[str, int, int]]:
    return [(m.group(), m.start(), m.end()) for m in _WORD_RE.finditer(folded)]


def _singular(word: str) -> str:
    """Fold a plain plural, so "guarantee" matches the term "guarantees" and
    "delivery" matches "deliveries". A tolerance, not a stemmer."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("xes", "zes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _phrase_matches(words: Sequence[Tuple[str, int, int]], phrase: str, *, plural: bool) -> List[Tuple[int, int]]:
    """Character spans where the phrase's words occur as consecutive whole words."""
    wanted = _WORD_RE.findall(_fold(phrase))
    if not wanted:
        return []
    if plural:
        wanted = [_singular(w) for w in wanted]
    spans = []
    for i in range(len(words) - len(wanted) + 1):
        window = [w for w, _, _ in words[i:i + len(wanted)]]
        if plural:
            window = [_singular(w) for w in window]
        if window == wanted:
            spans.append((words[i][1], words[i + len(wanted) - 1][2]))
    return spans


def clean_names(product_name: str, product_aliases: Optional[Sequence[str]]) -> Tuple[str, List[str]]:
    """The product name and aliases as entered, trimmed, without empties or repeats."""
    name = " ".join((product_name or "").split())
    aliases: List[str] = []
    seen = {name.casefold()}
    for alias in product_aliases or []:
        alias = " ".join(alias.split()) if isinstance(alias, str) else ""
        if alias and alias.casefold() not in seen:
            seen.add(alias.casefold())
            aliases.append(alias)
    return name, aliases


def find_triggers(
    sentence: str,
    product_name: str,
    product_aliases: Optional[Sequence[str]] = None,
    feature_terms: Optional[Sequence[str]] = None,
) -> List[TriggerHit]:
    """The deterministic triggers that fire on one sentence (amendment step 2).

    The name trigger reads only `product_name` and `product_aliases` (the
    watch's "Product name" and "Also called" fields), never the brief.
    A digit inside the product name ("Relay2") is part of the name, not a
    number.
    """
    folded = _fold(sentence)
    words = _word_spans(folded)
    hits: List[TriggerHit] = []
    seen = set()

    def add(trigger: str, match: str) -> None:
        key = (trigger, match.casefold())
        if match and key not in seen:
            seen.add(key)
            hits.append(TriggerHit(trigger=trigger, match=match))

    name, aliases = clean_names(product_name, product_aliases)
    name_spans: List[Tuple[int, int]] = []
    for label in [name, *aliases]:
        if not label:
            continue
        spans = _phrase_matches(words, label, plural=False)
        if not spans and not _WORD_RE.search(_fold(label)) and _fold(label) in folded:
            # A name made of symbols only has no words to match on
            start = folded.index(_fold(label))
            spans = [(start, start + len(_fold(label)))]
        if spans:
            add(TRIGGER_PRODUCT_NAME, label)
            name_spans.extend(spans)

    def inside_name(start: int, end: int) -> bool:
        return any(a <= start and end <= b for a, b in name_spans)

    for word, start, end in words:
        if word in FIRST_PERSON_PLURAL:
            add(TRIGGER_FIRST_PERSON_PLURAL, word)
        if inside_name(start, end):
            continue
        if _DIGIT_RUN_RE.search(word) or word in NUMBER_WORDS:
            add(TRIGGER_NUMBER, word)
        if word in COMPARISON_WORDS:
            add(TRIGGER_COMPARISON, word)
    for m in _COMPARISON_PHRASE_RE.finditer(folded):
        add(TRIGGER_COMPARISON, m.group())

    for term in feature_terms or []:
        if _phrase_matches(words, term, plural=True):
            add(TRIGGER_FEATURE_TERM, term)
    return hits


def is_question(sentence: str) -> bool:
    """A question mark anywhere, in any width or doubled form (NFKC folds them to "?")."""
    return "?" in unicodedata.normalize("NFKC", sentence)


_THOUSANDS_SEPARATOR_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


def _numbers(text: str, product_name: str, product_aliases: Sequence[str]) -> List[str]:
    """The numbers a text states, outside the product name: each digit run
    ("10" for "10x" and for "10ms", "1000" for "1,000") and each number word."""
    numbers: List[str] = []
    for hit in find_triggers(_THOUSANDS_SEPARATOR_RE.sub("", text), product_name, product_aliases):
        if hit.trigger == TRIGGER_NUMBER:
            numbers.extend(_DIGIT_RUN_RE.findall(hit.match) or [hit.match])
    return numbers


def _one_line(text: object, limit: int = MAX_REASON_CHARS) -> str:
    line = " ".join(str(text).split()) if isinstance(text, str) else ""
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _trigger_summary(hits: Sequence[TriggerHit]) -> str:
    """ 'uses we/our/us/ours ("our"), makes a comparison ("quicker", "than")' """
    matches: Dict[str, List[str]] = {}
    for hit in hits:
        matches.setdefault(hit.trigger, []).append(f'"{hit.match}"')
    return ", ".join(
        f"{TRIGGER_LABELS.get(trigger, trigger)} ({', '.join(found)})" for trigger, found in matches.items()
    )


@dataclass
class PreparedCheck:
    """A text split and scanned by code, before any checker call."""

    text: str
    sentences: List[str]
    triggers: List[List[TriggerHit]]
    brief_sentences: List[BriefSentence]
    product_name: str
    product_aliases: List[str]
    feature_terms: List[str]
    # Failures code found without the checker: (marker or sentence, reason)
    structural_failures: List[List[str]] = field(default_factory=list)
    # The reply plan's product decision, where code alone can see it is broken
    plan_failures: List[str] = field(default_factory=list)

    @property
    def needs_checker(self) -> bool:
        """False when the text already failed in code: no paid call is made."""
        return not self.structural_failures and not self.plan_failures and bool(self.sentences)

    def request(self) -> ClaimCheckRequest:
        """Everything the checker is given: nothing from the post, the plan or the drafter."""
        return ClaimCheckRequest(
            sentences=list(self.sentences),
            brief_sentences=list(self.brief_sentences),
            product_name=self.product_name,
            product_aliases=list(self.product_aliases),
            must_verify=[bool(hits) for hits in self.triggers],
        )


# Sentence terminators the claim gate's splitter does not know: fullwidth and
# ideographic stops and marks, and the doubled marks. A reply that uses one
# without a space after it still ends a sentence there.
_OTHER_TERMINATORS = "".join(map(chr, (0x3002, 0xFF0E, 0xFF61, 0xFF01, 0xFF1F, 0x203C, 0x2047, 0x2048, 0x2049)))
_OTHER_TERMINATOR_RE = re.compile(f"([{_OTHER_TERMINATORS}]+)(?=\\S)")


# What may follow a dot inside one word without the dot ending a sentence: a
# file extension or a domain ending ("Node.js", "docs.example.com"). English
# words that are also endings ("it", "is", "in", "me", "so") are left out on
# purpose: "works.it" is two sentences.
SAFE_DOT_ENDINGS = frozenset(
    """
    js ts jsx tsx mjs py rb rs php java kt swift cpp sql sh
    io ai com org net dev app
    md json yaml yml toml html css txt csv xml
    """.split()
)
_LETTER = r"[^\W\d_]"
_JOINED_TERMINATOR_RE = re.compile(rf"{_LETTER}([.!?\u2026]+){_LETTER}")


def has_joined_sentences(sentence: str) -> bool:
    """True if a word of the reply joins two sentences without a space.

    The claim gate's rule (has_unclear_boundary_inside) sees a join only
    before a capital letter ("fast.It"). In a reply the lowercase join counts
    too, or "Acme works.it retries.it scales." would be one sentence and one
    claim. Dotted abbreviations ("e.g", "U.S.A") and names with a known
    ending ("Node.js") are not joins.
    """
    for word in sentence.split():
        joins = _JOINED_TERMINATOR_RE.findall(word)
        if not joins:
            continue
        if any(join != "." for join in joins):
            return True  # "agree?yes", "wait...what"
        labels = [label for label in re.split(r"\.", word.strip("\"'()[]{}<>,;:!?")) if label]
        if all(len(label) == 1 for label in labels):
            continue  # "e.g", "U.S.A"
        if re.sub(r"\W+$", "", labels[-1]).casefold() in SAFE_DOT_ENDINGS:
            continue  # "Node.js", "docs.example.com"
        return True
    return False


def split_reply(text: str) -> List[str]:
    """The sentences of a reply. A line break ends a sentence, and so does a
    fullwidth or ideographic terminator; within a line the claim gate's
    splitter decides."""
    sentences: List[str] = []
    for line in _OTHER_TERMINATOR_RE.sub("\\1\n", text or "").splitlines():
        sentences.extend(split_into_sentences(line))
    return sentences


def prepare_check(
    text: str,
    brief_sentences: Sequence[BriefSentence],
    product_name: str,
    product_aliases: Optional[Sequence[str]] = None,
    feature_terms: Optional[Sequence[str]] = None,
    mention_product: Optional[bool] = None,
) -> PreparedCheck:
    """Split the text, run the triggers and apply everything code can decide alone.

    `mention_product` is the reply plan's decision (None for a founder edit).
    """
    name, aliases = clean_names(product_name, product_aliases)
    terms = list(feature_terms or [])
    sentences = split_reply(text)
    triggers = [find_triggers(s, name, aliases, terms) for s in sentences]
    failures: List[List[str]] = []
    if not sentences:
        failures.append(["[reply]", "The reply is empty; it must have at least 1 sentence"])
    if len(sentences) > MAX_SENTENCES:
        failures.append(
            ["[reply]", f"The reply must have at most {MAX_SENTENCES} sentences (got {len(sentences)})"]
        )
    if len((text or "").strip()) > MAX_CHARS:
        failures.append(
            ["[reply]", f"The reply must be at most {MAX_CHARS} characters (got {len(text.strip())})"]
        )
    triggered = [i for i, hits in enumerate(triggers) if hits]
    if len(triggered) > MAX_PRODUCT_CLAIMS:
        failures.append([
            "[reply]",
            f"The reply must have at most {MAX_PRODUCT_CLAIMS} product claim, and "
            f"{len(triggered)} sentences are checked as product claims: "
            + "; ".join(f"sentence {i + 1} {_trigger_summary(triggers[i])}" for i in triggered),
        ])
    for sentence in sentences:
        if has_unclear_boundary_inside(sentence) or has_joined_sentences(sentence):
            failures.append([
                sentence,
                "This may be more than one sentence: a period, question mark or ellipsis inside it "
                "might end a sentence. Write each sentence on its own, with a space after it",
            ])
    brief_numbers = {
        n for b in brief_sentences for n in _numbers(f"{b.context} {b.text}", name, aliases)
    }
    for i in triggered:
        why = f"Checked as a product claim ({_trigger_summary(triggers[i])})."
        if is_question(sentences[i]):
            failures.append([sentences[i], f"{why} A product claim must be a statement, not a question."])
            continue
        if not brief_sentences:
            failures.append([sentences[i], f"{why} The brief has no sentences, so nothing can support it."])
            continue
        # Whatever the checker cites, the number rule would reject these
        unstated = [n for n in _numbers(sentences[i], name, aliases) if n not in brief_numbers]
        if unstated:
            failures.append([
                sentences[i],
                f"{why} It contains a number no brief sentence states: "
                + ", ".join(f'"{n}"' for n in dict.fromkeys(unstated)),
            ])
    plan_failures: List[str] = []
    if mention_product is False and triggered:
        # Known before the checker is asked: a triggered sentence is a product claim
        plan_failures.append(NO_MENTION_PLAN_FAILURE)
    return PreparedCheck(
        text=(text or "").strip(),
        sentences=sentences,
        triggers=triggers,
        brief_sentences=list(brief_sentences),
        product_name=name,
        product_aliases=aliases,
        feature_terms=terms,
        structural_failures=failures,
        plan_failures=plan_failures,
    )


def _judge_sentence(
    sentence: str,
    hits: List[TriggerHit],
    verdict: Optional[ClaimVerdict],
    brief_by_id: Dict[str, BriefSentence],
    product_name: str,
    product_aliases: Sequence[str],
) -> SentenceCheck:
    """Decide one sentence from its triggers and the checker's verdict."""
    # Only an explicit False takes a sentence out of the checked set, and only
    # when no trigger fired. A missing or malformed answer counts as flagged.
    flagged = verdict is None or verdict.about_product is not False or verdict.malformed
    checked = bool(hits) or flagged
    if not checked:
        return SentenceCheck(
            sentence=sentence, passed=True, kind="non_claim", verdict=VERDICT_NOT_PRODUCT
        )

    why = _trigger_summary(hits) if hits else "the checker read it as a statement about the product"
    check = SentenceCheck(
        sentence=sentence,
        passed=False,
        kind="claim",
        triggers=list(hits),
        checker_flagged=bool(verdict is not None and verdict.about_product is True),
        verdict=VERDICT_UNSUPPORTED,
    )

    def fail(reason: str) -> SentenceCheck:
        check.reason = f"Checked as a product claim ({why}). {reason}"
        return check

    if is_question(sentence):
        return fail("A product claim must be a statement, not a question.")
    if verdict is None:
        return fail("The checker returned no verdict for it.")
    cited = verdict.brief_sentence_ids
    if verdict.malformed or not isinstance(cited, list) or any(not isinstance(i, str) for i in cited):
        # Not repaired: a citation list with one bad entry is not a citation list
        return fail("The checker's answer for it was malformed.")
    if verdict.verdict != VERDICT_SUPPORTED:
        stated = _one_line(verdict.reason) or "The checker did not find it in the brief."
        return fail(f"The brief does not support it: {stated}")
    if not cited:
        return fail("The checker called it supported but cited no brief sentence.")
    unknown = [i for i in cited if i not in brief_by_id]
    if unknown:
        # The ids are model output: say how many, never echo them.
        return fail(f"The checker cited {len(unknown)} brief sentence id(s) that do not exist.")
    supporting = [brief_by_id[i] for i in dict.fromkeys(cited)]
    cited_numbers = {
        n
        for b in supporting
        for n in _numbers(f"{b.context} {b.text}", product_name, product_aliases)
    }
    added = [n for n in _numbers(sentence, product_name, product_aliases) if n not in cited_numbers]
    if added:
        return fail(
            "It contains a number the cited brief sentences do not state: "
            + ", ".join(f'"{n}"' for n in dict.fromkeys(added))
        )
    check.passed = True
    check.verdict = VERDICT_SUPPORTED
    check.supporting = supporting
    return check


def plan_product_failures(checks: Sequence[SentenceCheck], mention_product: Optional[bool]) -> List[str]:
    """Does the checked text follow the plan's product decision (issue #5)?

    In written mode the product is mentioned through a product claim
    sentence, as it is through a claim segment in assembled mode.
    """
    claims = sum(1 for c in checks if c.kind == "claim")
    if mention_product is False and claims:
        return [NO_MENTION_PLAN_FAILURE]
    if mention_product is True and not claims:
        return [MENTION_PLAN_FAILURE]
    return []


def judge(
    prepared: PreparedCheck,
    verdicts: Optional[Sequence[ClaimVerdict]],
    mention_product: Optional[bool] = None,
    *,
    checker_model: str = "",
    checker_input_tokens: int = 0,
    checker_output_tokens: int = 0,
) -> WrittenCheckResult:
    """Combine code's findings with the checker's verdicts into the final result.

    `verdicts` is None when the checker was not called (the text had already
    failed in code). `mention_product` is the reply plan's decision, or None
    for a founder edit, which is not held to the plan.
    """
    info = WrittenDraftInfo(
        product_name=prepared.product_name,
        product_aliases=list(prepared.product_aliases),
        feature_terms=list(prepared.feature_terms),
        brief_sentence_count=len(prepared.brief_sentences),
        splitter_version=SPLITTER_VERSION,
        trigger_rules_version=TRIGGER_RULES_VERSION,
        checker_called=verdicts is not None,
        checker_model=checker_model,
        checker_input_tokens=checker_input_tokens,
        checker_output_tokens=checker_output_tokens,
    )
    failures: List[List[str]] = [list(f) for f in prepared.structural_failures]

    if verdicts is None:
        # Not sent to the checker: the text failed in code. No sentence passes;
        # each shows its own failure, or the failure of the reply as a whole.
        plan_failures = list(prepared.plan_failures)
        if not failures and not plan_failures:
            failures.append(["[reply]", "The product-claim check did not run"])
        whole = failures[0][1] if failures else f"The draft does not follow the reply plan: {plan_failures[0]}"
        checks = [
            SentenceCheck(
                sentence=s,
                passed=False,
                kind="claim" if hits else "non_claim",
                reason=next((f[1] for f in failures if f[0] == s), whole),
                triggers=list(hits),
                verdict=VERDICT_NOT_CHECKED,
            )
            for s, hits in zip(prepared.sentences, prepared.triggers)
        ] or [
            # An empty text still gets one failing row, so a failed check
            # always names something that failed (the approve validator and
            # the card list failing sentences)
            SentenceCheck(
                sentence="[reply]", passed=False, kind="non_claim",
                reason=whole, verdict=VERDICT_NOT_CHECKED,
            )
        ]
        count = len(failures) + len(plan_failures)
        return WrittenCheckResult(
            passed=False,
            sentences=checks,
            failures=failures,
            reasoning=f"{count} failure(s) found before the checker call: {whole}",
            plan_failures=plan_failures,
            info=info,
        )

    by_index: Dict[int, ClaimVerdict] = {}
    for v in verdicts:
        # The first answer for a sentence stands; a repeat cannot overrule it
        if isinstance(v.index, int) and not isinstance(v.index, bool):
            by_index.setdefault(v.index, v)
    brief_by_id = {b.id: b for b in prepared.brief_sentences}
    checks = [
        _judge_sentence(
            sentence, hits, by_index.get(i), brief_by_id, prepared.product_name, prepared.product_aliases
        )
        for i, (sentence, hits) in enumerate(zip(prepared.sentences, prepared.triggers), start=1)
    ]
    claims = [c for c in checks if c.kind == "claim"]
    triggered = sum(1 for hits in prepared.triggers if hits)
    if len(claims) > MAX_PRODUCT_CLAIMS and triggered <= MAX_PRODUCT_CLAIMS:
        # (More than one triggered sentence is already a structural failure.)
        # The failure is recorded on each claim sentence, so a failed check
        # always has a failing sentence.
        too_many = (
            f"The reply must have at most {MAX_PRODUCT_CLAIMS} product claim, and "
            f"{len(claims)} sentences are checked as product claims."
        )
        for claim in claims:
            claim.reason = f"{claim.reason} {too_many}".strip() if not claim.passed else too_many
            claim.passed = False
    failures += [[c.sentence, c.reason] for c in checks if not c.passed]
    plan_failures = plan_product_failures(checks, mention_product)
    passed = not failures and not plan_failures
    if passed:
        reasoning = (
            f"{len(checks)} sentence(s) checked: {len(claims)} product claim(s) supported by "
            f"cited brief sentences, {len(checks) - len(claims)} not about the product"
        )
    elif failures:
        reasoning = f"{len(failures)} failure(s): {failures[0][1]}"
    else:
        reasoning = f"The draft does not follow the reply plan: {plan_failures[0]}"
    return WrittenCheckResult(
        passed=passed,
        sentences=checks,
        failures=failures,
        reasoning=reasoning,
        plan_failures=plan_failures,
        info=info,
    )
