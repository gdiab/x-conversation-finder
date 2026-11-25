"""
Non-claim closed set - fixed phrase list.

Per Architect decision 3 (2026-11-21):
Drafts are assembled, not written. A non_claim segment is {phrase_id} from a fixed,
versioned phrase list. The model authors no free text in a draft.

Phrases are short and generic. They must not mention any product, capability,
comparison, evaluation or number.

Version and provenance:
  VERSION: 2.1.0
  DATE: 2026-11-21
  SOURCE: QA r14
  CHANGE (v2.1.0): Removed presumptive phrases (great_question, for_asking, etc.),
    streamlined offers, added composition rules (order, no repeats, one per category,
    no opener-only drafts). 15 phrases: 4 openers, 1 thanks, 4 offers, 6 questions.
  CHANGE (v2.0.0): Removed frames entirely. Fixed phrase list with stable IDs.
    Model emits phrase_id, code inserts text. Rejected: open-ended frames and
    frame-plus-remainder allowlists, which admitted assertions in QA r11-r13.
"""

VERSION = "2.1.0"
DATE = "2026-11-21"

import re
import unicodedata
from typing import Dict, List, Optional
from dataclasses import dataclass

# Issue #9: identifiers come from the model. Gate errors flow into stored skip
# reasons and redraft steers, where the removal duty cannot find a post quote
# hidden in them, so a model-supplied identifier is echoed or kept only when
# it is a known one (a listed phrase, or a passage of the founder material).
UNKNOWN_IDENTIFIER = "<unknown>"


@dataclass
class NonClaimPhrase:
    """A fixed non-claim phrase with stable ID and category."""
    id: str
    text: str
    category: str  # opener, thanks, offer, question_to_poster


# Fixed phrase list - curated, short, generic phrases
# Each entry has a stable readable ID
# 15 phrases total (v2.1.0): 4 openers, 1 thanks, 4 offers, 6 questions
NON_CLAIM_PHRASES: List[NonClaimPhrase] = [
    # Openers / greetings (4)
    NonClaimPhrase(id="opener.hi", text="Hi!", category="opener"),
    NonClaimPhrase(id="opener.hello", text="Hello!", category="opener"),
    NonClaimPhrase(id="opener.hey", text="Hey!", category="opener"),
    NonClaimPhrase(id="opener.thanks", text="Thanks!", category="opener"),
    
    # Thanks / appreciation (1)
    NonClaimPhrase(id="thanks.for_sharing", text="Thanks for sharing.", category="thanks"),
    
    # Offers to help/share (4)
    NonClaimPhrase(id="offer.happy_to_help", text="Happy to help.", category="offer"),
    NonClaimPhrase(id="offer.share_details", text="Happy to share details.", category="offer"),
    NonClaimPhrase(id="offer.love_to_hear", text="I'd love to hear more.", category="offer"),
    NonClaimPhrase(id="offer.feel_free", text="Feel free to reach out.", category="offer"),
    
    # Questions to poster (generic, no product mentions) (6)
    NonClaimPhrase(id="q.current_tool", text="What are you using today?", category="question_to_poster"),
    NonClaimPhrase(id="q.stack", text="What stack are you on?", category="question_to_poster"),
    NonClaimPhrase(id="q.use_case", text="What's your use case?", category="question_to_poster"),
    NonClaimPhrase(id="q.context", text="Can you share more context?", category="question_to_poster"),
    NonClaimPhrase(id="q.looking_for", text="What are you looking for?", category="question_to_poster"),
    NonClaimPhrase(id="q.problem", text="What problem are you solving?", category="question_to_poster"),
]

# Build lookup maps
_PHRASE_BY_ID: Dict[str, NonClaimPhrase] = {p.id: p for p in NON_CLAIM_PHRASES}
_PHRASE_BY_TEXT: Dict[str, NonClaimPhrase] = {}  # Populated below


def get_phrase_by_id(phrase_id: str) -> Optional[NonClaimPhrase]:
    """
    Look up a phrase by its ID.
    
    Returns the phrase object, or None if the ID is not found.
    """
    return _PHRASE_BY_ID.get(phrase_id)


def known_phrase_id(phrase_id) -> str:
    """phrase_id itself if it is in the fixed phrase list, else UNKNOWN_IDENTIFIER."""
    return phrase_id if isinstance(phrase_id, str) and get_phrase_by_id(phrase_id) else UNKNOWN_IDENTIFIER


def get_phrase_text(phrase_id: str) -> Optional[str]:
    """
    Get the text for a phrase ID.
    
    Returns the phrase text, or None if the ID is not found.
    """
    phrase = _PHRASE_BY_ID.get(phrase_id)
    return phrase.text if phrase else None


def is_valid_phrase_id(phrase_id: str) -> bool:
    """Check if a phrase ID is valid."""
    return phrase_id in _PHRASE_BY_ID


def _apply_confusables_fold(text: str) -> str:
    """
    Apply confusables skeleton fold to detect homoglyphs.
    
    Expanded to include:
    - Cyrillic lookalikes (А, В, Е, К, М, Н, О, Р, С, Т, Х, Ѕ, etc.)
    - Greek lookalikes (Α, Β, Ε, Ζ, Η, Ι, Κ, Μ, Ν, Ο, Ρ, Τ, Υ, Χ, etc.)
    - Fullwidth forms (handled by NFKC)
    - Mathematical alphanumerics (handled by NFKC)
    """
    confusables = {
        # Cyrillic
        'А': 'A', 'В': 'B', 'Е': 'E', 'К': 'K', 'М': 'M', 'Н': 'H',
        'О': 'O', 'Р': 'P', 'С': 'C', 'Т': 'T', 'Х': 'X', 'Ѕ': 'S',
        'а': 'a', 'е': 'e', 'о': 'o', 'р': 'p', 'с': 'c', 'х': 'x',
        'У': 'Y', 'у': 'y',
        
        # Greek
        'Α': 'A', 'Β': 'B', 'Ε': 'E', 'Ζ': 'Z', 'Η': 'H', 'Ι': 'I',
        'Κ': 'K', 'Μ': 'M', 'Ν': 'N', 'Ο': 'O', 'Ρ': 'P', 'Τ': 'T',
        'Υ': 'Y', 'Χ': 'X',
        'α': 'a', 'β': 'b', 'γ': 'g', 'ε': 'e', 'ζ': 'z', 'η': 'h',
        'ι': 'i', 'κ': 'k', 'μ': 'u', 'ν': 'v', 'ο': 'o', 'ρ': 'p',
        'τ': 't', 'υ': 'y', 'χ': 'x',
    }
    
    result = []
    for char in text:
        result.append(confusables.get(char, char))
    
    return ''.join(result)


def normalize_for_closed_set(text: str) -> str:
    """
    Normalize text for closed-set matching.
    
    Steps:
    1. NFKC normalization
    2. Confusables fold (Cyrillic, Greek, fullwidth, mathematical alphanumerics)
    3. Casefold
    4. Collapse whitespace
    5. Strip trailing punctuation
    
    Returns normalized text for exact matching.
    """
    # NFKC normalization (handles fullwidth, mathematical alphanumerics, etc.)
    text = unicodedata.normalize('NFKC', text)
    
    # Confusables fold (expanded beyond Cyrillic to include Greek and others)
    text = _apply_confusables_fold(text)
    
    # Casefold
    text = text.casefold()
    
    # Collapse whitespace
    text = ' '.join(text.split())
    
    # Strip trailing punctuation
    text = text.rstrip('.!?')
    
    return text


# Populate normalized phrase text lookup (must be after normalize_for_closed_set is defined)
for phrase in NON_CLAIM_PHRASES:
    normalized = normalize_for_closed_set(phrase.text)
    _PHRASE_BY_TEXT[normalized] = phrase


def is_closed_set_non_claim(text: str) -> bool:
    """
    Determine if text matches a fixed phrase (v2.0.0 - no frames).
    
    Per Architect decision 3 (2026-11-21):
    A text is a closed-set non_claim ONLY if it exactly matches one of the
    fixed phrases (after normalization). No frames, no open-ended tails.
    
    This function is used for:
    - Founder edits in the review UI (edit path)
    - TEST-ONLY validation that all phrases pass the backstop
    
    It is NOT used for model-drafted segments, which emit phrase_id only.
    
    Returns True if text exactly matches a fixed phrase, False otherwise.
    """
    # Normalize for matching
    normalized = normalize_for_closed_set(text)
    
    # Exact match only
    return normalized in _PHRASE_BY_TEXT


def find_phrase_by_text(text: str) -> Optional[NonClaimPhrase]:
    """
    Find a phrase by its text (after normalization).
    
    Returns the phrase object, or None if no match.
    """
    normalized = normalize_for_closed_set(text)
    return _PHRASE_BY_TEXT.get(normalized)


# ==================== TEST-ONLY VALIDATION ====================
# The keyword and number backstop becomes TEST-ONLY.
# Every listed phrase must pass the backstop and contain no number.
# Remove the runtime backstop from the model-draft path (unreachable).
# Keep it wherever edit paths still use it.


def _test_only_contains_number(text: str) -> bool:
    """
    TEST-ONLY: Check if text contains any number (digit or number word).
    
    This is not used in the model-draft path, only in tests to validate
    that all phrases are number-free.
    """
    # Check for digits (including decimals, percentages, etc.)
    if re.search(r'\d', text):
        return True
    
    # Check for number words
    number_words = [
        r'\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten)\b',
        r'\b(?:eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)\b',
        r'\b(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)\b',
        r'\b(?:hundred|thousand|million|billion|trillion)\b',
        r'\b(?:dozens?|hundreds?|thousands?|millions?|billions?)\b',
    ]
    
    for pattern in number_words:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    
    return False


def _test_only_matches_backstop(text: str) -> bool:
    """
    TEST-ONLY: Check if text matches any backstop pattern.
    
    This is not used in the model-draft path, only in tests to validate
    that all phrases pass the backstop.
    """
    # Apply NFKC normalization (handles fullwidth, mathematical alphanumerics)
    text_folded = unicodedata.normalize('NFKC', text)
    # Apply confusables fold to catch homoglyphs
    text_folded = _apply_confusables_fold(text_folded)
    
    backstop_patterns = [
        # Compliance/security terms
        r'\b(?:soc2|sso|rbac|hipaa|gdpr|encrypted?|encryption|compliance|compliant|certified|certification|audit(?:ing|s| logs)?|role-based access control)\b',
        # Availability terms
        r'\b(?:24/7|uptime|availability|downtime|sla|99\.?\d*%)\b',
        # Risky absolutes
        r'\b(?:always|never|guarantee[sd]?|best|fastest|slowest|most|only|all|every|unlimited|instant(?:ly)?|zero|exactly-once)\b',
        # Numbers (needs to catch "10k", "20x", etc.)
        r'\b\d+[kKMBx%]\b',
    ]
    
    for pattern in backstop_patterns:
        if re.search(pattern, text_folded, re.IGNORECASE):
            return True
    
    return False


def validate_all_phrases() -> List[str]:
    """
    TEST-ONLY: Validate that all phrases pass the backstop and contain no numbers.
    
    Returns a list of validation errors, or empty list if all pass.
    """
    errors = []
    for phrase in NON_CLAIM_PHRASES:
        if _test_only_contains_number(phrase.text):
            errors.append(f"Phrase {phrase.id} contains a number: {phrase.text}")
        if _test_only_matches_backstop(phrase.text):
            errors.append(f"Phrase {phrase.id} matches backstop: {phrase.text}")
    return errors


# ==================== COMPOSITION RULES (v2.1.0, QA r14) ====================

@dataclass
class CompositionViolation:
    """A violation of draft composition rules."""
    reason: str


def validate_draft_composition(segments: List[dict]) -> Optional[CompositionViolation]:
    """
    Validate draft composition rules (v2.1.0, QA r14).
    
    Rules:
    1. Segment order by category: opener, then thanks, then claim, then offer or question_to_poster.
       Openers come first, and offers and questions come last.
    2. No phrase_id may repeat.
    3. At most one segment per category, where claim counts as a category and offer and 
       question_to_poster are each their own category.
    4. A draft with no claim is allowed, but needs at least one non-opener segment, 
       so "Hi!" alone or "Hi! Hello!" fails.
    
    Args:
        segments: List of segment dicts with 'kind' and either 'phrase_id' or 'source_passage_id'
    
    Returns:
        CompositionViolation if any rule is violated, None if all pass.
    """
    if not segments:
        return CompositionViolation("Draft must have at least one segment")
    
    # Track categories and phrase_ids
    categories_seen = []
    phrase_ids_seen = set()
    has_non_opener = False
    
    # Define category order (lower index = earlier)
    category_order = {
        "opener": 0,
        "thanks": 1,
        "claim": 2,
        "offer": 3,
        "question_to_poster": 4,
    }
    
    for segment in segments:
        kind = segment.get("kind")
        
        # Determine category
        if kind == "claim":
            category = "claim"
            has_non_opener = True
        elif kind == "non_claim":
            phrase_id = segment.get("phrase_id")
            if not phrase_id:
                return CompositionViolation("Non-claim segment missing phrase_id")
            
            # Check for repeats
            if phrase_id in phrase_ids_seen:
                return CompositionViolation(f"Phrase {known_phrase_id(phrase_id)} repeats in draft")
            phrase_ids_seen.add(phrase_id)
            
            # Look up category
            phrase = get_phrase_by_id(phrase_id)
            if not phrase:
                return CompositionViolation("Unknown phrase_id (not in the fixed phrase list)")
            
            category = phrase.category
            if category != "opener":
                has_non_opener = True
        else:
            return CompositionViolation("Invalid segment kind (must be 'claim' or 'non_claim')")
        
        # Check order
        if categories_seen:
            last_category = categories_seen[-1]
            last_order = category_order.get(last_category, 999)
            current_order = category_order.get(category, 999)
            if current_order < last_order:
                return CompositionViolation(
                    f"Segment order violation: {category} cannot follow {last_category}"
                )
        
        # Check at most one per category
        if category in categories_seen:
            return CompositionViolation(f"Multiple segments with category {category}")
        
        categories_seen.append(category)
    
    # Check opener-only rule
    if not has_non_opener:
        return CompositionViolation("Draft has only opener(s), needs at least one non-opener segment")
    
    return None
