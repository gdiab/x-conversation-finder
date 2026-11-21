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

**Changes:**

1. **Output contract:** `ReplyAgentPort.draft()` now returns structured segments instead of free text:
   ```python
   Segment(
       text: str,
       kind: Literal["claim", "non_claim"],  # str literal, not enum (Temporal activity boundary)
       source_passage_id: Optional[str],
       quote: Optional[str]
   )
   ```
   Activity payloads use plain `str` Literals, not `str`-Enums (str-Enum decodes as a char list at the Temporal activity boundary). Code joins segments into the draft text (no untagged text by construction), then re-splits the joined draft into sentences and rejects if any sentence doesn't map to a segment. Empty segment lists and empty segment text are rejected.

2. **Closed-set non_claim determination (code decides):** A segment is classified as `non_claim` ONLY if it matches one of these closed forms:
   - A question (ends with `?`)
   - A greeting or thanks (`thanks`, `thank you`, `hi`, `hello`, `happy to`, `glad to`, etc.)
   - An explicit first-person opinion frame (`I think`, `I believe`, `In my opinion`, `In my experience`, `I've found`, etc.)
   
   Anything that doesn't match a closed-set pattern must cite a passage, regardless of the model's tag. The model's `kind` field is only a proposal; code determines the effective kind using deterministic rules.

3. **Opinion/question frames don't launder claims:** If a sentence uses an opinion or question frame but mentions the product (`we`, `our`, `it`, `this`, `the product`, etc.) together with a claim indicator (comparatives, numbers, capability verbs, or competitor names), it is classified as a claim. Examples:
   - "I think it's 2x faster." → CLAIM (opinion frame + product + number)
   - "In my experience it cuts costs in half." → CLAIM (opinion frame + product + quantification)
   - "I think this is a hard problem." → NON_CLAIM (opinion without product claim)

4. **Near-verbatim support against the QUOTE (not the passage):** For claim segments, every content word, every number, and every negation in the segment text must appear in the quote field after NFKC normalization and casefolding. Light stemming is applied for matching (e.g., "provides"/"provide", "systems"/"system"). Negation must match in both directions: if the segment has a negation, the quote must have it; if the quote has a negation, the segment must have it.
   
   Coverage requirements:
   - `source_passage_id` must exist in this product's founder material
   - `quote` must be a normalized substring of that passage
   - Near-verbatim support: all content words, numbers, and negations in the segment must appear in the quote
   
   The previous 50%-of-passage support check has been removed. A model citation alone is not coverage. The gate verifies all conditions deterministically.

5. **Pattern backstop (always claims):** These patterns always classify a segment as a claim, overriding any `non_claim` tag:
   - Any digit or unit (10k, 99%, 5 GB, 24/7)
   - Compliance/security terms (SOC2, SSO, RBAC, HIPAA, GDPR, encrypted)
   - Availability terms (24/7, uptime, SLA, 99.9%)
   - Risky absolutes (always, never, guarantee, best, fastest, unlimited, instant, zero, exactly-once)
   
   If a segment matches a backstop pattern but lacks `source_passage_id`/`quote`, the gate fails. Model tags are never trusted on their own.

6. **On failure:** Exactly one redraft carrying the gate's per-sentence reasons as the steer, then gate again. A second failure ends the `Review` as `skipped: not enough evidence` with the reasons recorded in the evidence trail. A failed draft never reaches a card. Budget reservation and reconciliation cover the redraft call.

**Regression tests added:**
- 'We eliminate on-call pain.' → REJECTED (no quote)
- 'Teams ship twice as fast with it.' → REJECTED (no quote)
- 'Used by thousands of engineers.' → REJECTED (no quote)
- 'Cuts your infra bill in half.' → REJECTED (no quote)
- 'Beats Temporal on latency.' → REJECTED (no quote)
- 'Works on Kubernetes.' → REJECTED (no quote)
- 'Battle-tested in production.' → REJECTED (no quote)
- 'Ships with a free tier.' → REJECTED (no quote)
- 'I think it's 2x faster.' → REJECTED (opinion launders claim with product + number)
- 'In my experience it cuts costs in half.' → REJECTED (opinion launders claim)
- Real quote + '100 times faster' → REJECTED (number not in quote)
- Real quote + 'is SOC2 certified' → REJECTED (certification not in quote)
- One segment with two sentences where the second is uncovered → REJECTED
- 'Our product is not open source' quoting 'It is open source' → REJECTED (negation mismatch)
- 'Our product is open source' quoting 'It is not open source' → REJECTED (negation mismatch)
- A question → PASSES (closed-set non_claim)
- A thanks → PASSES (closed-set non_claim)
- 'I think this is a hard problem.' → PASSES (opinion without product claim)
- Claim whose content words all appear in its quote → PASSES

**Deferred items:**
- Independent verifier call as an option (b) alternative to near-verbatim matching is deferred to the ADR-0003 eval program. Near-verbatim matching is the baseline; a verifier call would be evaluated for precision/recall improvement.
- Founder-edit re-gate with explicit `post_anyway` override is approved in principle and will be implemented in a subsequent change. This allows the founder to override a gate failure with an explicit acknowledgment.

**Alternatives considered:**
- **Option (b): Independent verifier LLM call.** Instead of near-verbatim matching, call an LLM to judge whether the segment is supported by the quote. Rejected for the baseline because it adds cost, latency, and non-determinism. Deferred to eval as a potential improvement.
- **Looser stemming or semantic similarity.** Rejected: near-verbatim matching with light stemming provides a clear, testable boundary. Semantic similarity would make the gate unpredictable.
- **Allow model tags without code override.** Rejected: model tags are proposals only. Code must decide using deterministic rules to prevent circumvention.

**Why:** This design prevents the model from circumventing the gate through opinion/question laundering or misclassification, and ensures every product assertion is traceable to founder material through near-verbatim matching checked by code, not trust.
