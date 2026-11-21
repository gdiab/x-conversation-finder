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

**Context:** The original design had the model output claims with citations, then the gate checked them. This proved insufficient: a model can tag something as "non_claim" yet still make a product assertion, and citation alone doesn't prove coverage. Outpost ADR-0079 forbids trusting model self-attestation.

**Changes (Architect decision 2026-11-21):**

Claims are quoted, not paraphrased. A claim segment references a founder passage and a sentence index; code inserts that sentence verbatim (trailing punctuation aside). The model never authors claim wording. All other text must match the closed set of non-claim forms (questions, greetings or thanks, explicit first-person opinion or experience that does not mention the product with a comparative, number, capability or competitor), with the keyword backstop overriding any non_claim tag. A draft that fails is redrafted once with reasons, then skipped as 'not enough evidence'. Founder edits are re-gated; a failing edit needs an explicit 'post anyway' acknowledgement, which is recorded. Rejected: lexical support checks over paraphrased claims (bag-of-words, ordered subsequence), because they cannot see order, modality or dropped qualifiers (QA r9 and r10). Deferred: an independent verifier call per sentence, to be evaluated under ADR-0003 as a way to allow paraphrase later. Consequence: founder material should be written as short, standalone, claim-ready sentences.

**Implementation details:**

1. **Output contract:** `ReplyAgentPort.draft()` returns structured segments:
   - For claims: `Segment(kind="claim", source_passage_id=str, sentence_index=int)` with NO text
   - For non-claims: `Segment(kind="non_claim", text=str)`
   
   Activity payloads use plain `str` Literals, not `str`-Enums (str-Enum decodes as a char list at the Temporal activity boundary).

2. **Claim sentence insertion:** Code splits the founder passage into sentences using a deterministic splitter (fixed to not break on decimals like 99.99%), validates the sentence_index is in range, and inserts that exact sentence as the segment text. The only normalization allowed is trimming trailing punctuation for flow. No partial spans. An invalid passage id or out-of-range index fails the segment.

3. **Closed-set non_claim determination (WHOLE sentence, code decides):** A sentence is `non_claim` ONLY if, after stripping a leading greeting/thanks clause, the REMAINDER is:
   - Empty
   - A question (ends with `?`) with no product+comparative/number/capability/competitor
   - An explicit first-person opinion (`I think`, `I believe`, `In my opinion`, `In my experience`, etc.) that doesn't mention the product with a claim indicator
   
   Product words include: `we`, `our`, `ours`, `us`, `it`, `this`, `the tool/product/platform/service/app/system`. A trailing `?` must not make a claim-bearing sentence a question non_claim. Confusable homoglyphs (Cyrillic, etc.) are detected via a confusables skeleton fold before matching.

4. **Pattern backstop (overrides non_claim):** These patterns always classify text as a claim, regardless of the model's tag:
   - Any digit or unit (10k, 99%, 5 GB, 24/7, integrations count)
   - Compliance/security terms (SOC2, SSO, RBAC, HIPAA, GDPR, encrypted)
   - Availability terms (24/7, uptime, SLA, 99.9%)
   - Risky absolutes (always, never, guarantee, best, fastest, unlimited, instant, zero, exactly-once)

5. **Founder-edit re-gate:** When a founder edits the draft text, the edited text is split into sentences. Each sentence:
   - If it exactly matches a previously gated segment (after NFKC+casefold+whitespace normalization), keeps that verdict
   - If it's a verbatim passage sentence (normalized), passes as a claim
   - If it's a closed-set non_claim, passes
   - Otherwise, fails
   
   Per-sentence results are stored. This is not a hard block: if the edited text fails, the `approve` Update requires `post_anyway=True`. The validator rejects approve without it when failures exist, and the card records `post_anyway` and the failing sentences.

6. **On failure:** Exactly one redraft carrying the gate's per-sentence reasons as the steer, then gate again. A second failure ends the `Review` as `skipped: not enough evidence` with the reasons recorded in the evidence trail. A failed draft never reaches a card.

**Regression tests (must REJECT):**
- 'I think it's way cheaper.' → REJECTED (opinion launders claim with product + comparative)
- 'Thanks! It's way cheaper.' → REJECTED (greeting + claim with comparative)
- 'Hi! Ours is 10x quicker.' → REJECTED (greeting + product + number + comparative)
- 'Hello, many teams switched from Temporal to us.' → REJECTED (greeting + claim with competitor + capability)
- 'Thanks. The product has twenty integrations.' → REJECTED (greeting + claim with number)
- 'Is ours 10x faster than Kafka?' → REJECTED (trailing ? doesn't make claim-bearing sentence a non_claim)
- 'Thanks. Ѕupports ЅЅO.' → REJECTED (confusables skeleton detects Cyrillic bypass)
- Order swap: 'Kafka is faster than our product' vs passage quote 'Our product is faster than Kafka' → REJECTED (quoted order, not paraphrased)
- Dropped hedge: 'might support SSO in the future' in passage, model outputs 'supports SSO' → REJECTED (not verbatim)
- Modal swap: passage has 'can handle', model outputs 'handles' → REJECTED (not verbatim)
- Invalid passage id or out-of-range sentence_index → REJECTED

**Regression tests (must PASS):**
- 'Thanks for sharing!' → PASSES (closed-set non_claim: greeting/thanks)
- 'What stack are you on?' → PASSES (closed-set non_claim: pure question, no product+claim_indicator)
- 'I love this thread.' → PASSES (closed-set non_claim: opinion without product claim)
- Verbatim passage sentence via sentence_index → PASSES
- Edited sentence exactly matches gated segment (normalized) → PASSES (B4: exact match reuse)

**Why:** This design prevents circumvention through paraphrase (order swap, dropped qualifiers, modal changes) and opinion/question laundering, and ensures every product assertion is a verbatim quote from founder material, checked deterministically by code.
