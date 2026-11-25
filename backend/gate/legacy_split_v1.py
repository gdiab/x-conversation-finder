"""The claim gate's sentence split before issue #10 (SPLITTER_VERSION 1). Frozen.

Copied verbatim from main 9f4df4a. The claim gate uses it only to check a
claim segment drafted before splitter versions existed (splitter_version is
None): if this split and the current one give that index different text, the
segment fails and the draft is redrafted under the current split, instead of
silently quoting a different sentence (ADR-0006, 2026-11-21 amendment,
item 2). Such a segment can only exist in a Review whose draft_reply ran on a
worker older than issue #10 and whose claim_gate runs on a newer one. Do not
change this file; delete it, and the check in insert_claim_sentence, once no
worker older than issue #10 can still be draining work.
"""

import re
from typing import List


def split_into_sentences_v1(text: str) -> List[str]:
    """
    Split text into sentences.
    
    QA R13: NEVER split after a known abbreviation. A sentence ending in 
    a known abbreviation is NOT selectable as a claim.
    
    S4 (QA R14): Split after company suffixes (Inc, Ltd, Co, Corp) when next
    token is a capitalized word. Titles precede names, but company suffixes end sentences.
    
    Simple deterministic rule:
    - Never split after a period between digits (decimals like 99.9)
    - Split after company suffixes (Inc, Ltd, Co, Corp) when next token is capitalized
    - Never split after other abbreviations (Dr. Mr. Mrs. Ms. Prof. vs. e.g. i.e. etc. approx. St.)
    - Split on [.!?]+ followed by whitespace otherwise
    
    Returns non-empty sentences.
    """
    # S4 (QA R14): Company suffixes - split after these when next token is capitalized
    company_suffixes = ['Inc.', 'Ltd.', 'Co.', 'Corp.']
    
    # Other abbreviations - NEVER split after these
    non_split_abbrevs = ['Dr.', 'Mr.', 'Mrs.', 'Ms.', 'Prof.', 'vs.', 'e.g.', 'i.e.', 'etc.', 'approx.', 'St.']
    
    # Build case-insensitive patterns
    company_pattern = '|'.join(r'\b' + re.escape(a) for a in company_suffixes)
    abbrev_pattern = '|'.join(r'\b' + re.escape(a) for a in non_split_abbrevs)
    
    result = []
    current = []
    
    # Tokenize into words and spaces
    tokens = re.findall(r'\S+|\s+', text)
    
    i = 0
    while i < len(tokens):
        token = tokens[i]
        current.append(token)
        
        # Check if token ends with sentence-ending punctuation
        if re.search(r'[.!?]+$', token):
            # Check if it's a decimal (digit before period)
            if re.match(r'.*\d+\.\d*$', token):
                # It's a decimal, don't split
                i += 1
                continue
            
            # S4 (QA R14): Check if it's a company suffix
            is_company_suffix = re.search(r'(' + company_pattern + r')$', token, re.IGNORECASE)
            if is_company_suffix:
                # Split if next token is capitalized
                if i + 1 < len(tokens) and tokens[i + 1].strip() == '' and i + 2 < len(tokens):
                    next_content = tokens[i + 2]
                    # Check if next token starts with a capital letter (after stripping whitespace)
                    if next_content and next_content[0].isupper():
                        # Split here - company suffix ends sentence
                        sentence = ''.join(current).strip()
                        if sentence:
                            result.append(sentence)
                        current = []
                        i += 1
                        continue
                # Otherwise, don't split (no content after, or not capitalized)
                i += 1
                continue
            
            # Check if it's a non-split abbreviation - NEVER split
            if re.search(r'(' + abbrev_pattern + r')$', token, re.IGNORECASE):
                # It's an abbreviation, continue without splitting
                i += 1
                continue
            
            # Check if next token is whitespace followed by content
            if i + 1 < len(tokens) and tokens[i + 1].strip() == '':
                # Next is whitespace, split here if there's content after
                if i + 2 < len(tokens):
                    # We have more content, commit this sentence
                    sentence = ''.join(current).strip()
                    if sentence:
                        result.append(sentence)
                    current = []
        
        i += 1
    
    # Add remaining
    if current:
        sentence = ''.join(current).strip()
        if sentence:
            result.append(sentence)
    
    return result
