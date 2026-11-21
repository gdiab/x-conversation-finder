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
