# ADR-0006: Evidence trail, claim gate, computed confidence, baseline eval

**Status:** Accepted (2026-11-20, George)

## Context
A reply that mentions the product in the wrong place is worse than no reply. The system must show why a post was surfaced and refuse to guess. Outpost solved the same problem for venues:
- ADR-0079: confidence is computed from the shape of the evidence and never asserted by a role.
- ADR-0078: the baseline is fixed before the run, and a blind judge scores precision, traceability and honest uncertainty, never volume.
- ADR-0086: a post is attested by a lookup (`GET /2/tweets/:id`) rather than trusted from search results, and its text is held only under X's removal duty (a 404 on re-fetch removes it).
- Plus the prototype's claim gate and its habit of recording a "Gap" instead of inventing an answer.

George wants the review UI to show the research alongside the draft.

## Decision
- **Evidence card per post** (the ADR-0002 contract), written by activities, never by the model free-hand:
  - post and author facts from the lookup (id, text kept under the removal rule, like/reply counts, followers, who may reply);
  - matched topic and a short "why it fits" quoting the post;
  - the founder-material passages the draft relies on;
  - computed confidence and its inputs;
  - claim-gate result;
  - draft and any steers applied.
- **Confidence is computed**, from a fixed rule over evidence shape: lookup succeeded, replies open, topic match strength, freshness, author reach, and presence of supporting founder material. The model proposes features; code sets the number and the threshold. No post gets a number the model typed.
- **Claim gate:** every product claim in a draft must match a passage in the founder's own material, or the gate fails and the draft is regenerated at most once. No quote-shopping: the draft may not cherry-pick the post's words to flatter it.
- **"Not enough evidence" is a valid output.** Below threshold or gate-failed, the `Review` ends as a skip with the reason recorded and no card to approve.
- **Baseline eval:** a single-prompt baseline ("here is the post and the product, write a reply") is the comparison. A blind judge scores both arms on precision (would the founder reply), traceability (each claim has a source) and uncertainty honesty (does it skip when it should), using a held-out set of past posts. Scoring replays stored evidence so runs are repeatable (ADR-0074's idea).
- Store the post text only while the post exists; a 404 on a re-fetch deletes it (ADR-0086).

## Alternatives considered
- **LLM self-rated confidence.** Rejected: precisely what ADR-0079 forbids; it is uncalibrated and gamable.
- **Engagement-only ranking.** Rejected: X search cannot sort by engagement (Outpost research), and a hot post is not a good place to mention a product.
- **No claim gate; rely on a good prompt.** Rejected: invented product claims are the main failure, and a prompt cannot be tested like a gate.
- **Human reviewer as the only gate.** Rejected as the only control: it removes the system's value and fails on fatigue. Keep it as the last gate.
- **Eval by founder click-through only.** Rejected as the sole signal: too slow and sparse; keep as a longer-term signal.

## Consequences
- Every approved reply has a traceable record, which the review UI renders.
- More skips than a naive tool: this is intended and is measured, not hidden.
- The evaluation set and judge must be built before the claim "better than a single prompt" is made.
- The threshold and feature weights are guesses at first; record them in config with a date and revisit after the first 50 cards.
- **Caveat:** the confidence weights, threshold and eval design are untested proposals.
- Deviation from Outpost: Outpost's claims live in Markdown with Anchors; here they live in the database. Keep the vocabulary (Observation, Judgment, Gap) so the two can be compared.

## Amendment 2026-11-21: Claim gate contract redesign

**Context:** The original design had the model output claims with citations, then the gate checked them. This proved insufficient: a model can tag something as "non_claim" yet still make a product assertion, and citation alone doesn't prove coverage. Outpost ADR-0079 forbids trusting model self-attestation. QA r11-r13 revealed that frames with allowlists still admitted assertions.

**Changes (Architect decisions 2 and 3, 2026-11-21):**

Drafts are assembled, not written. A claim segment references a founder passage and a sentence index; code inserts that sentence verbatim (trailing punctuation aside). A non-claim segment references a phrase_id from a fixed, versioned phrase list; code looks up and inserts that text. The model authors no free text anywhere in a draft. A draft has 1-3 segments with at most one claim. A draft that fails the gate is redrafted once with reasons, then skipped as 'not enough evidence'. Founder edits are re-gated; a failing edit needs an explicit 'post anyway' acknowledgement, which is recorded. Rejected: lexical support checks over paraphrased claims (bag-of-words, ordered subsequence), and open-ended frames with allowlists. Deferred: an independent verifier call per sentence, to be evaluated under ADR-0003 as a way to allow paraphrase later. Consequence: founder material should be written as short, standalone, claim-ready sentences; v1 replies are templated by design.

**Implementation details:**

1. **Output contract:** `ReplyAgentPort.draft()` returns structured segments (Architect decision 3):
   - For claims: `Segment(kind="claim", source_passage_id=str, sentence_index=int)` with NO text
   - For non-claims: `Segment(kind="non_claim", phrase_id=str)` with NO text
   
   The model authors no free text. Code inserts text: for claims, the exact sentence from the passage; for non-claims, the phrase text from the fixed list by phrase_id.
   
   Activity payloads use plain `str` Literals, not `str`-Enums (str-Enum decodes as a char list at the Temporal activity boundary).

2. **Claim sentence insertion:** Code splits the founder passage into sentences using a deterministic splitter (fixed to not break on decimals like 99.99%; QA R14 splits after company suffixes Inc/Ltd/Co/Corp when next token is capitalized), validates the sentence_index is in range, and inserts that exact sentence as the segment text. The only normalization allowed is trimming trailing punctuation for flow (QA R14: questions preserve '?'). No partial spans. An invalid source_passage_id or out-of-range index fails the segment.

3. Drafts are assembled, not written. The reply agent selects segments by reference only: a claim is {source_passage_id, sentence_index} and code inserts that founder sentence verbatim; a non-claim is {phrase_id} from a fixed, versioned phrase list in code (`backend/gate/non_claim_set.py`, v2.1.0, pinned here). The model authors no free text in a draft. A draft has 1-3 segments with at most one claim. Composition rules (v2.1.0): segment order by category (opener, thanks, claim, offer/question), no phrase_id repeats, at most one segment per category, no opener-only drafts. Rejected: open-ended frames and frame-plus-remainder allowlists, which admitted assertions in QA r11-r13. Deferred to ADR-0003 evaluation: post-echo questions, an independent verifier, and scoping the hard gate to product-referencing sentences with founder review as the backstop. Consequence: v1 replies are templated by design; the baseline eval measures the cost in precision and naturalness.

4. **Pattern backstop (TEST-ONLY):** The keyword and number backstop is now TEST-ONLY validation: every listed phrase must pass the backstop and contain no number. The runtime backstop is removed from the model-draft path (unreachable with phrase_id). It remains in edit paths for founder-edited free text.

5. **Founder-edit re-gate:** When a founder edits the draft text, the edited text is split into sentences. Each sentence:
   - If it exactly matches a previously gated segment (after NFKC+casefold+whitespace normalization, question vs statement type), keeps that verdict
   - If it's a verbatim passage sentence (normalized), passes as a claim IF it also passes the referent rule, abbreviation-ending rule, and selectability checks (same checks as model path, QA R14)
   - If it's a closed-set non_claim phrase (v2.1.0 fixed phrase list), passes IF terminal punctuation type matches
   - Otherwise, fails
   
   Edited text is also validated against the composition rules (v2.1.0). Any violation fails the edit or requires post_anyway, same as any other failure (QA R14).
   
   Per-sentence results are stored. This is not a hard block: if the edited text fails, the `approve` Update requires `post_anyway=True`. The validator rejects approve without it when failures exist, and the card records `post_anyway` and the failing sentences.
   
   **Concurrent edits** (QA R12): Resolve as last-writer-wins by finish order. Each edit runs its gate independently; the last to complete sets the final draft.text and gate state. The _edit_in_flight_count counter prevents approve while any edit is running, but does not serialize edits themselves.
   
   **Stale card writes** (QA R13): A monotonically increasing edit_seq in workflow state is incremented at the start of each edit and passed to update_card. The store activity ignores any write whose edit_seq is lower than the stored value, preventing an earlier edit's update_card that lands last from overwriting a later edit's data. SQLAlchemy's flag_modified is used for JSON columns to ensure changes are persisted.

6. **On failure:** Exactly one redraft carrying the gate's per-sentence reasons as the steer, then gate again. A second failure ends the `Review` as `skipped: not enough evidence` with the reasons recorded in the evidence trail. A failed draft never reaches a card.

**Regression tests for model-draft path (must REJECT):**
- Unknown phrase_id → REJECTED (phrase_id not in v2.1.0 fixed list)
- Invalid segment kind (not 'claim' or 'non_claim') → REJECTED (QA R14)
- Non_claim segment with text field → REJECTED (phrase_id only, no text)
- 0 segments → REJECTED (must have at least 1)
- 4 segments → REJECTED (must have at most 3)
- 2 claim segments → REJECTED (must have at most 1)
- Invalid source_passage_id or out-of-range sentence_index → REJECTED
- Composition violations → REJECTED (QA R14 v2.1.0):
  - Opener after claim (order violation)
  - Repeated phrase_id
  - Multiple segments per category
  - Opener-only draft

**Regression tests for edit path (must REJECT, current bypass classes QA r8-r14):**
- 'It supports retries.' → REJECTED (unresolved referent)
- 'We are Acme Inc.' → REJECTED (ends with abbreviation, not selectable)
- 'Our product helps developers build reliable systems?' → REJECTED (passage sentence, wrong punctuation type)
- 'What stack are you on.' / 'What stack are you on!' → REJECTED (phrase, wrong punctuation type)
- 'I think it's way cheaper.' → REJECTED (assertion not in fixed phrases or passages)
- 'Thanks! It's way cheaper.' → REJECTED (multiple sentences, second is assertion)
- 'Hi! Ours is 10x quicker.' → REJECTED (multiple sentences, second is assertion)
- 'Hello, many teams switched from Temporal to us.' → REJECTED (assertion not in passages)
- 'Thanks. The product has twenty integrations.' → REJECTED (assertion with number)
- 'Is ours 10x faster than Kafka?' → REJECTED (question with product claim)
- 'I think this works well.' → REJECTED (assertion not in fixed phrases)
- 'I think they are the same.' → REJECTED (assertion not in fixed phrases)
- 'I like the idea.' → REJECTED (assertion not in fixed phrases)
- 'I think you are the problem.' → REJECTED (not a fixed phrase or passage sentence)
- 'Why is the issue the same?' → REJECTED (assertion in question form)
- 'Thanks for sharing. Our product supports SSO. Hi!' → REJECTED (composition violation: opener after claim)

**Regression tests (must PASS):**
- Valid phrase_id from fixed list → PASSES (model-draft path)
- Verbatim passage sentence via sentence_index → PASSES (model-draft path)
- 'Thanks for sharing!' → PASSES (edit path: exact phrase match)
- 'What stack are you on?' → PASSES (edit path: exact phrase match)
- 'I love this thread.' → PASSES (edit path: exact phrase match)
- Edited sentence exactly matches gated segment (normalized, same type) → PASSES (edit path: reuse)

**Why:** This design prevents circumvention through paraphrase (order swap, dropped qualifiers, modal changes) and opinion/question laundering, and ensures every product assertion is a verbatim quote from founder material, checked deterministically by code.
