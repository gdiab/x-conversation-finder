"""The claim gate's sentence splitter and text normalization.

They live here, below the activities, so that both the claim gate activities
and the written-draft check (backend/gate/written_check.py, backend/gate/brief.py)
can use them without the gate importing the activities package.
backend.activities.claim_gate_activity re-exports both under their old names.

Like every sentence splitter, split_into_sentences runs only inside
activities, never in workflow code (ADR-0006, 2026-11-21 amendment, item 2).
"""

import re
import unicodedata
from typing import List

from backend.gate.sentence_boundary import END, LIKELY_END, sentence_terminator, word_boundary


def split_into_sentences(text: str) -> List[str]:
    """
    Split text into sentences (ADR-0006 claim gate contract).

    Deterministic rule: a word ending in '!' or '?' (before any closing quotes
    or brackets) ends a sentence when another word follows. A word ending in
    '.' ends one only where
    backend.gate.sentence_boundary.period_boundary says END or LIKELY_END:
    - QA R14: company suffixes (Inc. Ltd. Co. Corp.) end a sentence when the
      next word is capitalized (LIKELY_END).
    - QA R13: prefix abbreviations (Dr. Mr. Mrs. Ms. Prof. vs. e.g. i.e.
      approx.) never end a sentence before a lowercase word or a name
      ("Dr. Smith"); an initial (J.) never ends one before a lowercase word.
    - Issue #10: etc., St., Ph.D. and dotted initialisms (U.S.A.) end a
      sentence before a sentence-starter word (LIKELY_END); numbers (2021.)
      before any capitalized word. Decimals (S-5: 99.99%) never split; a list
      marker ("1.") at the start of a sentence stays with it.
    claim_selectability_error rejects any sentence touching an unclear
    (LIKELY_*) boundary, as it rejects one that ends in an abbreviation.

    Sentence indices are positions in this split (SPLITTER_VERSION). Changing
    a split point changes the indices claim segments reference; see ADR-0006
    (2026-11-21 amendment, item 2).

    Returns non-empty sentences.
    """
    result = []
    current = []

    # Words and the whitespace between them alternate in tokens.
    tokens = re.findall(r'\S+|\s+', text)

    # Whether the current sentence already has a word (constant time per token).
    has_word = False

    for i, token in enumerate(tokens):
        current.append(token)
        if token.isspace():
            continue
        sentence_start = not has_word
        has_word = True
        terminator = sentence_terminator(token)
        if not terminator:
            continue
        next_word = tokens[i + 2] if i + 2 < len(tokens) else None
        if next_word is None:
            # End of text: the remainder is added below.
            continue
        if word_boundary(token, next_word, sentence_start=sentence_start) not in (END, LIKELY_END):
            continue
        sentence = ''.join(current).strip()
        if sentence:
            result.append(sentence)
        current = []
        has_word = False

    # Add remaining
    if current:
        sentence = ''.join(current).strip()
        if sentence:
            result.append(sentence)

    return result


def normalize_text(text: str) -> str:
    """
    Normalize text for comparison: NFKC, confusables, casefold, collapse whitespace.
    Uses the same normalization as the closed-set module for consistency.
    """
    # Use the closed-set normalization (but keep trailing punctuation for this usage)
    # NFKC normalization (compatibility decomposition + canonical composition)
    text = unicodedata.normalize('NFKC', text)
    
    # Apply confusables fold
    from backend.gate.non_claim_set import _apply_confusables_fold
    text = _apply_confusables_fold(text)
    
    # Normalize quote characters to standard ASCII quotes
    text = text.replace('"', '"').replace('"', '"').replace(''', "'").replace(''', "'")
    
    # Casefold (more aggressive than lowercase for international text)
    text = text.casefold()
    
    # Collapse whitespace
    return ' '.join(text.split())
