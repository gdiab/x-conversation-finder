"""
Non-claim closed set determination.

Per Architect decision 2026-11-21:
Non-claim text is a closed set defined in code, versioned and dated.
Contains:
- Exact whole sentences after normalization (greetings, thanks, offers)
- Question or first-person opinion frames whose remainder contains only allowlisted words

Rules:
- No open-ended tails
- A frame never makes an assertion a non_claim
- NUMBERS ARE NEVER ALLOWED (digits or number words)
- Everything else must be a quoted claim

Version and provenance:
"""

VERSION = "1.0.0"
DATE = "2026-11-21"

import re
import unicodedata
from typing import Set

# Exact non-claim sentences (after normalization: NFKC + casefold + whitespace + trailing punctuation)
# These are complete, standalone non-claim sentences.
EXACT_NON_CLAIMS: Set[str] = {
    # Pure greetings
    "thanks",
    "thank you",
    "hi",
    "hello",
    "hey",
    "hi there",
    "hello there",
    
    # Thanks with common suffixes
    "thanks for sharing",
    "thanks for asking",
    "thanks for the question",
    "thanks for your help",
    "thank you for sharing",
    "thank you for asking",
    "thank you for the question",
    
    # Offers to help/share
    "happy to help",
    "happy to share",
    "happy to share more",
    "happy to share more details",
    "would be happy to help",
    "would be happy to share",
    "would be happy to share more",
    "would be happy to share more details",
    "glad to help",
    "glad to share",
    "pleased to help",
    
    # Thread appreciation (genuine non-claims from QA examples)
    "love this thread",
    "i love this thread",
    
    # Pure acknowledgment
    "appreciate it",
    "appreciate this",
}

# Allowlist of words that can appear in question/opinion frame remainders
# These are non-claim words that don't make assertions about the product
ALLOWLIST_WORDS: Set[str] = {
    # Question words
    "what", "which", "where", "when", "who", "how", "why",
    
    # Common verbs/linking words (non-assertive)
    "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "have", "has", "had",
    "can", "could", "would", "will", "should",
    "try", "tried", "work", "works", "worked",
    
    # Pronouns (non-product)
    "you", "your", "yours", "they", "them", "their",
    "i", "me", "my", "we",  # Note: "we/our/ours" in product context triggers backstop
    
    # Demonstratives (when not product references)
    "this", "that", "these", "those",
    
    # Common nouns (non-product, non-claim)
    "stack", "thread", "question", "approach", "problem",
    "setup", "discussion", "post", "comment", "idea",
    "topic", "issue", "context", "case", "situation",
    
    # Adjectives (non-comparative, non-absolute)
    "good", "great", "interesting", "helpful", "nice",
    "hard", "easy", "different", "similar", "same",
    "well",
    
    # Prepositions and connectors
    "on", "in", "at", "to", "from", "for", "with", "about",
    "by", "of", "and", "or", "but", "if",
    
    # Articles
    "a", "an", "the",
    
    # Other common non-claim words
    "really", "very", "quite", "just", "also", "too",
    "here", "there",
}

# Question frames: "What stack are you on?" is OK
# First-person opinion frames: "I think this is a hard problem." is OK
OPINION_FRAMES = [
    r'^i\s+think\b',
    r'^i\s+believe\b',
    r'^i\s+feel\b',
    r'^i\s+love\b',
    r'^i\s+like\b',
    r'^i\s+appreciate\b',
    r'^in\s+my\s+(?:opinion|experience|view)\b',
    r'^personally\b',
]


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


def _contains_number(text: str) -> bool:
    """
    Check if text contains any number (digit or number word).
    
    Numbers are NEVER allowed in non_claim text.
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


def _matches_backstop_pattern(text: str) -> bool:
    """
    Check if text matches any backstop pattern.
    
    Backstop patterns always indicate a claim, overriding any non_claim determination.
    These patterns include:
    - Numbers (already checked by _contains_number, but kept for completeness)
    - Compliance/security terms
    - Availability terms
    - Risky absolutes
    
    Applies confusables fold before matching.
    """
    # Apply confusables fold to catch homoglyphs
    text_folded = _apply_confusables_fold(text)
    
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


def _remainder_only_contains_allowlist(remainder: str) -> bool:
    """
    Check if the remainder (after stripping opinion frame) contains only allowlisted words.
    
    Returns True if all words in the remainder are in the allowlist.
    """
    # Tokenize: split on whitespace and punctuation, keep only alphanumeric words
    words = re.findall(r'\b\w+\b', remainder.lower())
    
    if not words:
        return True  # Empty remainder is OK
    
    for word in words:
        if word not in ALLOWLIST_WORDS:
            return False
    
    return True


def is_closed_set_non_claim(text: str) -> bool:
    """
    Determine if text is a closed-set non_claim.
    
    Per Architect decision 2026-11-21:
    A sentence is non_claim ONLY if:
    1. It matches an exact non-claim sentence (after normalization), OR
    2. It's a question (ends with ?) whose content contains only allowlisted words, OR
    3. It's a first-person opinion frame whose remainder contains only allowlisted words
    
    AND it doesn't contain numbers (digits or words) AND doesn't match backstop patterns.
    
    Rules:
    - No open-ended tails (exact matches only for non-frames)
    - A frame never makes an assertion a non_claim
    - NUMBERS ARE NEVER ALLOWED
    - Everything else must be a quoted claim
    
    Returns True if text is a closed-set non_claim, False otherwise.
    """
    # Normalize for matching
    normalized = normalize_for_closed_set(text)
    
    # Rule: Numbers are never allowed in non_claim text
    if _contains_number(normalized):
        return False
    
    # Rule: Backstop patterns override non_claim
    if _matches_backstop_pattern(normalized):
        return False
    
    # Check 1: Exact match to closed set
    if normalized in EXACT_NON_CLAIMS:
        return True
    
    # Check 2: Question with only allowlisted words
    if text.rstrip().endswith('?'):
        # Remove the question mark and check if remainder is allowlisted
        question_content = normalized.rstrip('?').strip()
        if _remainder_only_contains_allowlist(question_content):
            return True
    
    # Check 3: Opinion frame with allowlisted remainder
    for frame_pattern in OPINION_FRAMES:
        match = re.match(frame_pattern, normalized)
        if match:
            # Extract remainder after the frame
            remainder = normalized[match.end():].strip()
            if _remainder_only_contains_allowlist(remainder):
                return True
    
    # Not a closed-set non_claim
    return False
