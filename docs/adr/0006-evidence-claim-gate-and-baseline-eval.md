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

   **Abbreviations (issue #10, 2026-11-24).** The period rule lives in `backend/gate/sentence_boundary.py` and is shared with the reply plan's question-span check. A word-final period (closing quotes, brackets or emoji after it do not hide it) is classified by the word and the next word. Certain end: after an ordinary word, and after a number before a word of either case ("in 2021. Customers"). Certain continuation: after any abbreviation or initial before a lowercase word ("Is the U.S.A. market ready?", "j. smith"); after a prefix abbreviation (titles such as Dr. Mt. Prof., and vs. e.g. i.e. approx.; these do not end sentences) before a digit or a capitalized word that is not a sentence starter ("Dr. Smith", "approx. 10"); after a list marker that opens a sentence, before a lowercase word ("1. install the CLI"). Every other abbreviation, initial or list marker before a capitalized word, digit or symbol is unclear ("Acme Inc. 2024 revenue doubled"), and so is a list marker inside a sentence or before a capitalized word ("99. Is that good?" may be a score), which makes numbered list items unselectable. Two more cases are unclear: any terminator inside closing quotes before a lowercase word ('"Is it fast?" asked a customer'), an ellipsis ("..." or "…") before a lowercase word, and a terminator with no space before a capital letter ("fast.It saves", "AI.It saves"; "Node.js" and "ASP.NET" are not affected). In general: "the U.S. We ship" is probably two sentences but "the U.S. The Netherlands, and France" is one; "J. Smith" is probably one but "Plan B. Customers pay" is two. For numbering, the claim gate splits an unclear case when the next word is a sentence starter (a closed list of pronouns, determiners, connectives, question words and the fixed phrases' first words) or the word is a company suffix (QA R14), and joins it otherwise. A sentence that starts, ends or contains an unclear boundary is not selectable, on the model path, the edit re-gate's passage match, and the numbered prompt list. So a claim is not a fragment of a founder sentence or two founder sentences counted as one, and the question-span check, which splits at every period except a certain continuation, does not count a statement before a question as part of it, within two known limits. Abbreviations missing from the lists are read as ordinary words; the lists cover the common ones. A lowercase word after an abbreviation or initial continues the sentence, as this issue requires for "the U.S.A. market", so a sentence that starts in lowercase right after one ("the U.S.A. customers pay monthly") is joined to it. A sentence ending in an abbreviation (not a number) is not selectable, as before. Before this change the splitter cut "We sell in the U.S.A. market today." after "U.S.A." and the first piece was selectable, which inserted a partial span.

   **Split changes and stored indices.** Changing a split point changes sentence indices, so the split is versioned (`SPLITTER_VERSION`, 2 since issue #10). The splitters run only inside activities (`draft_reply`, `claim_gate`, `verify_edited_text`, `plan_reply`), never in workflow code (a test enforces this), so replaying a Review history uses the recorded results and cannot diverge. The drafting adapters stamp `splitter_version` on each claim segment, and `claim_gate` refuses a segment numbered under another version. A segment without a version was drafted by a worker older than issue #10; the gate accepts it only if the frozen pre-#10 split (`backend/gate/legacy_split_v1.py`) gives its index the same text, so a worker restart between `draft_reply` and `claim_gate` cannot quote a different sentence from the one drafted. A refused segment fails the gate: the first draft is redrafted once under the current split, and a nudge on a Review that ran the plan step is rejected with the current draft kept. (A nudge on a Review started before the plan step does not run `claim_gate` at all; that is unchanged.) A card stores each claim's inserted text next to its index. The edit re-gate reuses a stored claim's verdict by that text only if the claim was gated under the current version, or its text is still a whole, selectable sentence in the current split; a pre-#10 fragment such as "We sell in the U.S.A" is checked again and fails. New passage sentences are matched against a fresh split. A stored index is provenance against the card's passage snapshot. Next time the split changes, bump the version and keep the previous split for this check. A worker older than issue #10 ignores `splitter_version`, so do not run old and new workers on the task queue together: stop the old worker before starting the new one (the documented setup runs one worker).

3. Drafts are assembled, not written. The reply agent selects segments by reference only: a claim is {source_passage_id, sentence_index} and code inserts that founder sentence verbatim; a non-claim is {phrase_id} from a fixed, versioned phrase list in code (`backend/gate/non_claim_set.py`, v2.1.0, pinned here). The model authors no free text in a draft. A draft has 1-3 segments with at most one claim. Composition rules (v2.1.0): segment order by category (opener, thanks, claim, offer/question), no phrase_id repeats, at most one segment per category, no opener-only drafts. Rejected: open-ended frames and frame-plus-remainder allowlists, which admitted assertions in QA r11-r13. Deferred to ADR-0003 evaluation: post-echo questions, an independent verifier, and scoping the hard gate to product-referencing sentences with founder review as the backstop. Consequence: v1 replies are templated by design; the baseline eval measures the cost in precision and naturalness.

4. **Pattern backstop (TEST-ONLY):** The keyword and number backstop is now TEST-ONLY validation: every listed phrase must pass the backstop and contain no number. The runtime backstop is removed from the model-draft path (unreachable with phrase_id). It remains in edit paths for founder-edited free text.

5. **Founder-edit re-gate:** When a founder edits the draft text, the edited text is split into sentences. Each sentence:
   - If it exactly matches a previously gated segment (after NFKC+casefold+whitespace normalization, question vs statement type), keeps that verdict
   - If it's a verbatim passage sentence (normalized), passes as a claim IF it also passes the referent rule, abbreviation-ending rule, and selectability checks (same checks as model path, QA R14)
   - If it's a closed-set non_claim phrase (v2.1.0 fixed phrase list), passes IF terminal punctuation type matches
   - Otherwise, fails
   
   Edited text is also validated against the composition rules (v2.1.0). Any violation fails the edit or requires post_anyway, same as any other failure (QA R14).
   
   QA R15: Edits are capped at 1-3 sentences (same as model path) and must have at most 1 claim. A 4-sentence edit or an edit with 2+ claims fails with a specific reason recorded.
   
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

## Amendment 2026-11-24: Written drafts, product claims checked against the brief

**Status:** Accepted (2026-11-24, George, by merging PR #3). Supersedes the 2026-11-21 amendment's "drafts are assembled, not written" rule for the model-draft path once the switch criteria below pass. Everything else in that amendment stays. (Clarified 2026-11-25: for a written draft, step 5 below also replaces that amendment's edit-path rules, so a founder edit of a written draft is checked by steps 2-4 and not against the fixed phrases; an assembled draft and its edits keep the 2026-11-21 rules unchanged.)

**Context:** The first live runs (2 watches, 20 posts, 2026-11-23) showed the cost the 2026-11-21 amendment predicted ("v1 replies are templated by design"). Every draft was a fixed opener, one verbatim brief sentence and a fixed question, for example "Thanks for sharing. <brief sentence>. What problem are you solving?" The drafts cannot react to what the post says, so they read as a product pitch dropped into a thread. George judged none of the 20 postable as written. The assembled format removed invented claims, but it also removed the conversation, which is the reason to reply at all.

The 2026-11-21 amendment deferred two options to allow more than verbatim quotes: an independent verifier call per sentence, and scoping the hard gate to product-referencing sentences with founder review as the backstop. This amendment takes up both.

**Decision:**

1. **The model writes the reply as free text**: at most 3 sentences, at most 280 characters, at most 1 product claim (unchanged limits). It also returns, for each sentence that says anything about the product, the id of the brief sentence(s) it says support it. That list is provenance: it is kept on the draft and shown on the card. It is never sent to the checker and is never a verdict (decision (a) below).
2. **Code decides which sentences are product claims, conservatively.** A sentence is checked as a product claim if either:
   - a deterministic trigger fires: the product name or a declared alias, first-person-plural words about our side ("we", "our", "us", "ours"), any number, a comparison (step 4), or a key-feature term from the watch settings (the Start Watch form's optional "Key features" list, decision (c) below); or
   - the independent checker (step 3) classifies it as being about the product.
   Triggers can only add sentences to the checked set; nothing the drafting model says can remove one. This answers ADR-0079's ban on self-attestation: the drafting model never decides what gets checked.
3. **An independent checker call verifies each product sentence against the brief.** It is a separate model call with its own prompt. It sees only the reply's sentences, the brief sentences, the product name and its aliases, and which sentences code requires a verdict for (decision (a) below), not the post, not the drafting prompt and not the drafter's list from step 1. For each sentence it returns `supported` with the brief sentence ids that entail it, or `unsupported` with a reason. Code then confirms the cited ids exist and that each `supported` sentence cites at least one. Paraphrase is allowed only where the checker finds entailment; added specifics (numbers, integrations, customers, comparisons) that the cited sentences do not state are `unsupported`.
4. **Non-product sentences are not gated against the brief.** They respond to the post: agreement, a question, an observation about the poster's point. Founder review is the backstop for them, as the 2026-11-21 amendment allowed. They may not contain numbers or comparisons (trigger in step 2 sends those to the checker).
5. **Failure handling is unchanged:** one redraft with the checker's reasons as the steer, then skip as "not enough evidence" with reasons recorded. Founder edits run through the same steps 2-4; a failing edit still needs `post_anyway`, which is recorded.
6. **The evidence card shows the check.** For each sentence: product claim or not, why it was checked (which trigger or the checker), verdict, and the supporting brief sentences. This replaces the per-segment display.
7. **Both draft modes stay behind a setting** (`DRAFT_MODE=assembled|written`) until the switch criteria below pass, so the baseline eval can compare them.

**Switch criteria (must pass before `written` becomes the default):**
- Every must-REJECT case in the 2026-11-21 amendment's edit-path list that makes a product claim is rejected by steps 2-4. (Reworded 2026-11-25; see "Decided: switch criterion 1" at the end of this amendment.)
- A new adversarial set is rejected: paraphrases that add a qualifier, drop a qualifier, change a modal ("can" to "will"), swap a comparison, attribute a brief claim to a different feature, or hide a claim in a question ("Did you know ours does X?").
- A blind comparison of `assembled` against `written` on the same stored evidence (ADR-0006 eval), scored on would-the-founder-reply and traceability.

**Alternatives considered:**
- **Keep assembled drafts and improve the phrase list.** Rejected: more phrases cannot make a draft respond to the post's specific point.
- **Lexical support checks over paraphrase.** Still rejected, for the reasons in the 2026-11-21 amendment.
- **Let the drafting model label its own product sentences.** Rejected: self-attestation (ADR-0079). The drafter's own list is kept as provenance only (step 1).
- **Use the same model call to draft and to check.** Rejected: the checker must not see the post or the drafting goal, so it has no reason to be lenient.

**Consequences:**
- Replies can engage with the post. The gate's guarantee changes from "every product claim is a verbatim brief sentence" to "every product claim was judged entailed by cited brief sentences by an independent call, and code checked the citations". This is weaker than verbatim and is the price of natural replies; the adversarial set measures it.
- Each draft costs one more model call (the checker), under the ADR-0005 daily budget.
- The brief no longer has to be written as standalone, claim-ready sentences, though short factual sentences still check more reliably.
- **Questions decided by George** ((a) and (b) on 2026-11-24 in issue #12; (c) on 2026-11-25):
  - (a) *Should the checker use a different model or provider from the drafter?* No: the same model and provider are fine. The checker must be a separate session: a fresh call with its own prompt and no drafting context (no post, no drafting conversation), like a subagent. In code the checker is its own port (`ClaimCheckerPort`). Its request holds the reply's sentences, the brief sentences, the product name and aliases, and which sentences code requires a verdict for, and nothing else. The drafter's list of supporting brief sentences (step 1) is drafting context, and a suggestion would anchor a check that has to be independent, so the checker never sees it. The list is kept on the draft as provenance and shown on the card, labelled as the drafter's own note. George confirmed this on 2026-11-25; step 1 first called the list "a hint for the checker" and was reworded to match.
  - (b) *Do the product-name aliases live in the brief or in watch settings?* In watch settings. The Start Watch form has a required "Product name" field and an optional "Also called" list. Both are stored on the TopicWatch input and passed to each Review. The deterministic name trigger reads only these fields, never the brief text. The card shows which draft sentences matched the name or an alias.
  - (c) *Do feature terms come from the brief or from watch settings?* From watch settings (George, 2026-11-25). The Start Watch form has an optional "Key features" list next to "Product name" and "Also called". It is stored on the TopicWatch input and passed to each Review exactly like the aliases. The deterministic feature-term trigger reads only that field, never the brief text. A watch started without key features has no feature terms, and the card says so.
  - *Existing watches.* A watch started before these fields existed has no product name, and a running watch keeps the input it was started with. Its Reviews stay in assembled mode whatever `DRAFT_MODE` says, with a note on the card saying why. Written mode is unavailable to that watch until it is stopped and started again with a product name. A Review keeps the draft mode it was planned under: the `plan_reply` activity fixes the mode and records it in workflow history.

**Implementation notes (issue #12, 2026-11-24).** The code is `backend/gate/written_check.py` (triggers and verdict rules), `backend/gate/brief.py` (brief sentences) and the `check_written_draft` activity. `docs/DEVELOPMENT.md` ("Draft modes") describes how to run it.

- *One checker call per text.* The checker sees all sentences of the reply in one call, so it can read a sentence in the context of the others ("By a wide margin." after a product sentence), and each draft costs one more model call as stated above. A text that code can already reject is not sent to the checker: no sentence, more than 3 sentences, more than 280 characters, more than one triggered sentence, a triggered question, a number that no brief sentence states, a sentence that may be two, a triggered sentence when the plan said not to mention the product, or a product claim that an empty brief could not support.
- *Triggers.* The step 2 triggers. A comparison is "than", "vs", "compared to", "same as", "as fast as" or a word of a closed list of comparatives and superlatives. "Any number" means any digit and any word of a closed list read broadly: cardinals, ordinals, fractions and multipliers ("one", "first", "second", "half", "twice", "single"). Code cannot tell the pronoun ("which one") or the discourse word ("First, ...") from the quantity, so all of them are checked, and the drafter is told to keep them out of every sentence but the claim. A digit inside the product name is part of the name. Text is normalized as in the claim gate (NFKC, look-alike letters, case) before matching.
- *Feature terms.* A feature term is one entry of the watch's "Key features" list (decision (c)): a comma-separated form field, stored on the TopicWatch input and passed to each Review with the aliases. The brief is never read for feature terms, not even a list the brief itself names "Key features", so the trigger is predictable from what the founder entered. An entry is the short name of a feature, not a sentence about it. The API drops blanks and repeats, and refuses an entry of more than 6 words or 80 characters, an entry with no letter or digit, and a list of more than 20 entries. A term fires when its words stand in a reply sentence as consecutive whole words in the same order, after the same normalization as the other triggers. Punctuation between the words is ignored ("dead letter queue" matches "Dead-letter queue"), and a regular plural matches its singular in both directions ("a signed delivery" matches "Signed deliveries", "UIs" matches "UI"). The plural tolerance has no word list, so it also matches a few unrelated words ("it" and "its"); that only adds a sentence to the checked set. Any other wording or spelling of a feature is a different term ("realtime" for "real-time", "and" for "&", a plural that changes the stem): the founder enters it as its own key feature, as other names go into "Also called". The trigger only forces the check: the sentence still needs a brief sentence that supports it, so a key feature the brief says nothing about makes a sentence that uses it fail. A watch started without key features (the field left empty, or a watch that predates the field) has no feature terms; the card says so, and the other triggers and the checker still apply. A running watch keeps the key features it was started with, as it keeps its product name.
- *Brief sentences.* Prose is split with the claim gate's splitter and joined again at unclear boundaries, so a brief sentence is never a fragment. A list item is one sentence. A list item's continuation lines belong to it ("- Not supported:" with "Slack and Teams." on the next line is one sentence). A heading is not a sentence: each sentence is shown with the headings, lead-in line and parent items it stands under, so an item under "Not supported yet > Integrations" keeps its meaning. A line that ends in a colon is the context of the list or the one paragraph after it, and lead-ins chain. The sentence right before a list is the context of its items even without a colon (a bold "Not supported yet" line). A one- or two-word "sentence" is joined to the sentence before it, so "We do not offer these yet. Slack. Teams." stays one sentence. What this cannot carry is context between full sentences of a paragraph ("We do not offer these yet. Slack is on the list."): the checker sees the brief sentences in order and is told to read each with its neighbours, and standalone sentences remain the safest way to write a brief. Ids are `b1`, `b2`, ... in document order.
- *Rules in code beyond steps 2-4.* Each can only add a rejection. (1) A product claim must be a statement: a sentence in the checked set that is a question fails, whatever the checker says. This rejects "Did you know ours does X?" even when the brief supports X. (2) A number in a supported claim must appear in a cited brief sentence. (3) The checker's answer fails closed. Only two answers pass a sentence: a clean "not about the product" (no verdict, no citation) for a sentence no trigger fired on, and a clean "supported" with citations that all exist. A missing, malformed, repeated or self-contradicting answer fails the sentence; nothing is repaired or read charitably. (4) A sentence that may be two fails: a terminator inside a word with a letter or digit after it in that word ("Acme ships.It retries.", "works.it retries", also with an emoji or a quote between), except in a number, a dotted abbreviation or a name with a known ending ("3.5", "U.S.", "Node.js"). A line break and a fullwidth terminator end a sentence, and the drafter's line breaks are kept until the check. So the sentence and claim limits cannot be dodged by how sentences are joined.
- *Counting claims.* "At most 1 product claim" is counted per sentence. One sentence that says two things about the product ("X does A, and it does B") is one claim, and the checker must find all of it in the brief: packing statements into one sentence cannot get one past the support check, only past the count.
- *Triggered sentences need support.* A triggered sentence must get `supported` with existing brief sentence ids, including one that makes no real claim ("We would love to hear how it goes."). The drafter is told to keep "we", numbers and comparisons out of every sentence but the claim.
- *One result per text.* The checker is a model, so asking it twice about the same text could give two answers. A Review keeps the first finished check of each text, separately for model drafts (with nudged redrafts) and for founder edits, and reuses it whenever that text comes back: an identical redraft, a nudge that returns the same text, an edit saved again. So the draft text and gate state that approve names (issue #15) still identify one check result, and a repeated text costs no second call. A check that could not run is not kept; that text is checked again.
- *Removal duty.* A written draft is model-written text that can quote the post, so a removal redacts the draft text, each sentence check's sentence and reason, and the edited-text results (`redact_removed_card`). The supporting brief sentences are the founder's own text and stay.

**Decided: switch criterion 1 covers the cases that make a product claim (George, 2026-11-25).** Five cases of the 2026-11-21 edit-path must-REJECT list make no product claim: 'What stack are you on.', 'What stack are you on!', 'I like the idea.', 'I think you are the problem.' and 'Why is the issue the same?'. In 2026-11-21 they failed because they were not fixed phrases. They are out of scope for the claim gate by design: under step 4 a sentence that is not a product claim is not gated against the brief, and the tone of such a sentence is caught by founder review. Criterion 1 as first worded ("every must-REJECT case ... is rejected by steps 2-4") could not hold for them, so it now names the cases that make a product claim. Those are the other twelve, and steps 2-4 reject each of them. `backend/tests/test_written_switch_criteria.py` pins both groups: the twelve as rejected, the five as allowed by design.

## Amendment 2026-11-25: Post text kept out of Temporal history (issue #23)

**Context.** Issue #9 made a removal redact the stored cards. Two copies of the post text survived it: Temporal history (workflow inputs and activity payloads held the post, the evidence and the drafts until history retention deleted the closed workflow; the local dev server keeps histories in `.temporal-dev.db`), and SQLite residue (old pages in the `-wal` file and in free pages).

**Decision (George, 2026-11-25: option A).** Workflows carry ids only. The text lives in SQLite, where a removal can delete it:

1. A TopicWatch search stores the posts it finds (`stored_posts`) and returns their ids; each Review starts with the post id only.
2. A Review's activities read the post and the Review's working documents (`review_docs`: the evidence, each draft candidate, check results, the founder's edit and nudge text) from SQLite and return summaries: counts, flags, the draft's SHA-256 digest, and reasons that code writes without post text (should-we-reply rules, the confidence threshold, plan validation errors, "post removed").
3. **Drafts stay out of history too.** A draft is model output that can quote the post, and approve names the draft the founder saw (issue #15). Approve now names it by digest: the API hashes `draft_text` and sends `{"text_sha256", "gate_state"}`. An edit's text and a nudge's instruction are stored by the API first; the update carries the slot and the digest. Edit and nudge return the digest, and the API answers with the stored draft. A failed-check skip reason and a nudge rejection's reasons quote the draft, so they are stored, and the API adds them to its responses.
4. The `get_card` query of such a Review returns its state only (status, approval fields, digest); the API reads the card from SQLite and applies that state.
5. A removal (`delete_post_text`) also records the removal on the stored post, replaces its text, and deletes every working document of the post. Any later required read or write of them raises `PostRemoved`, so a Review of a removed post that is still being prepared stops at its next activity, research and planning included, in both draft modes, before that activity's model call. An activity that read its inputs before the removal was recorded is not cancelled and may still make its model call, and a Review already waiting for the founder ends at the founder's decision or the review timeout (its card is redacted and its edits and nudges are refused).
6. SQLite: `PRAGMA secure_delete=ON` on every connection, and `PRAGMA wal_checkpoint(TRUNCATE)` after each removal.

**Replay.** A Review whose input carries the post (every history recorded before this change) runs the previous code path unchanged; a search result that carries posts starts its Reviews with the post as before. The mode follows from recorded data, so replay always takes the same path. Fixtures recorded on main 06db357 replay in tests.

**What stays in history.** The founder's own text: the brief, the watch query, product name, aliases and key features, and a founder's skip reason. Ids: post, conversation, reply ids and the founder's reply URL. Code-written reasons. Histories recorded before this change keep their text until Temporal's retention deletes them.

**Not changed.** The removal duty's own rules from issue #9 and issue #12: a card keeps the post's author fields, and a founder's edit of an assembled draft is kept as the founder's words (both changed by the issue #35 amendment below). Worker logs are not history and can still print draft sentences. Activity failure messages are history: text-free activities replace other libraries' error messages (a database's statement parameters, a model provider's echo of the request) with the error's type name.

## Amendment 2026-11-25: A removal also deletes the author and the founder's edit (issue #35)

**Context.** After the issue #9, #12 and #23 rules, a removed post's cards still kept the author's name, username (handle) and profile link, the post URL (which holds the handle), and, in assembled mode, the founder's edited draft, which can quote or paraphrase the deleted post.

**Decision (George, 2026-11-25).** A removal deletes both, on every stored card of the post, on the stored post, and in the `get_card` result:

1. The removed post keeps only its id, created time, counts, conversation id and reply setting (`REMOVED_POST_KEPT_FIELDS`, an allowlist), with the text placeholder and `text_removed`. The author (id, name, username, follower count, verified flag), the URL and the id of the user it replied to go. A later search that finds the post again stores it the same way.
2. A founder's edit of the draft goes in either draft mode (the draft text when `steers_applied` holds `manual_edit`), with the edit's check results (sentence and reason) and the "Post anyway" sentences. An unedited assembled draft and the founder's nudges on it stay: code-inserted text and the founder's own words.
3. The card keeps its ids, status and timestamps. The UI's "Post removed" card shows no author line, follower count or "View on X" link.
4. Removals recorded before this change get the same rules: `init_db` (at API start) runs `redact_recorded_removals`, which rewrites only the rows that change and then checkpoints the WAL. If a reader keeps the checkpoint from completing, the API retries it every 30 s until it does (`retry_wal_checkpoint`).

**Not changed.** Follow-up replies are other posts and keep their authors. A Review recorded before issue #23 holds the post in its workflow state; when another Review or the eval records the removal, that Review's raw `get_card` query is not redacted, and `GET /api/reviews/{id}` redacts it from the stored removal (`apply_stored_removal`), as for issue #9. No workflow command changes: the redaction is in the activities and in the pure `redact_removed_card`, so no patch is needed.

## Amendment 2026-11-26: Drafts that name the product end with the product URL (issue #41)

**Context.** Drafts never contained a link to the product, so a reply that names the product gave the reader nowhere to go, and the founder added the link by hand before posting. The founder wants the copied reply to paste into X ready to post (George, 2026-11-26: required).

**Decision.**

1. **Watch setting.** The Start Watch form has a required "Product URL" field: an `https://` link to a site, without spaces. It is stored on the TopicWatch input with the other founder settings (product name, aliases, key features) and passed to each Review. A running watch keeps the URL it was started with.
2. **Code appends the link, never the model.** After a draft passes its check, the activity that recorded the verdict (`check_written_draft` for a written draft, `claim_gate` for an assembled one) appends one space and the URL to the stored draft. The writer is never asked to write the URL. A trailing copy of the URL the model wrote anyway is taken off first, so the draft ends with exactly one. A draft whose sentences already hold the URL (a brief sentence that quotes it) gets no second copy. Such a quoted URL counts as typed in every length check; only the appended link counts as 23 (stricter than X, never looser).
3. **Only drafts that name the product** (default; the founder can change it): the reply plan's `mention_product` is true, or the passing draft has a sentence checked as a product claim (a written draft's claim sentence, an assembled draft's claim segment). Other drafts get no link.
4. **The check is unchanged.** The written check and the claim gate run on the sentences without the URL, so the URL never fires the number, name or feature triggers, and a text checks the same with or without the link.
5. **Length.** X counts any link as 23 characters. With the link and the space before it, the sentences get at most 280 - 24 = 256 characters. The writer prompts (both modes) ask for 256 when the plan says to mention the product, and say why. The check applies 256 to a draft that names the product: before the checker call when the plan says to mention it or a trigger fired, and after the verdicts when the checker found a claim. A draft over 256 that names the product fails and is redrafted, like any failed check.
6. **Edits.** An edited draft keeps the link unless the founder deletes it. The check of the edit (both modes) takes off a trailing product URL (the text's last word, exactly the watch's URL) and checks the rest; the link is not required. With the link the text before it gets 280 - 23 characters less the whitespace typed before the link (256 after one space); without it the rules are as before. A changed URL, or one anywhere else in the text, is checked as part of the text.
7. **Card.** The draft shown on the card is the text with the link, and Copy Draft copies it. Approve names that text by digest (issue #15, #23): the activities return the stored draft's digest (`GateSummary.draft_sha256`) and the workflow keeps it.
8. **Existing work.** Only new drafts get the link (default; the founder can change it). Running watches have no URL; after this change, restart both Tightbeam watches with the same settings plus the URL.

**Temporal.** The new input fields (`TopicWatchInput.product_url`, `ReviewInput.product_url`, and the activity inputs) are optional with a `None` default, and `GateSummary.draft_sha256` defaults to empty, so running workflows and recorded histories decode and replay. No workflow command is added, removed or reordered: the link is appended inside activities, and the workflow only reads which digest to keep (`draft_sha256`, or `text_sha256` for a result recorded before the field, which is the same draft). So no `workflow.patched()` marker is needed. Text-free histories recorded on main d296ec3 (a TopicWatch in flight, a written Review edited, nudged and approved, an assembled Review through its follow-up) replay in tests. A Review whose input carries the post (recorded before issue #23) never gets a URL.

**Not changed.** The claim gate's and the written check's rules, the steer and skip reasons (a length failure is one more failure row), and the removal rules: a written draft's text, the link with it, is redacted on removal; an unedited assembled draft keeps its text and link.

## Amendment 2026-11-26: Brief suggestions from the product page (issue #42)

**Context.** The brief is the only source of facts a draft may state about the product. A founder can leave out facts that would make good replies; a manual comparison of one product's README with its brief (2026-11-26) found several.

**Decision (George, 2026-11-26: a checklist).**

1. The Start Watch form reads the page at the Product URL (for a GitHub repository, its README through the GitHub API) and asks the model once for at most 8 sentences the brief lacks, each with the exact page quote that supports it and why it helps replies, plus flags for brief sentences the page contradicts or does not mention (information, not errors: the founder may have other sources).
2. Code keeps a suggestion only if its quote appears on the page word for word.
3. **Scraped text never becomes a claim source without the founder's choice.** Each suggestion is a checkbox, off by default, with an editable sentence. A ticked suggestion is appended to the brief when the founder clicks "Add checked to brief", and can be edited first; an unticked one is dropped. Added sentences go under a level-1 "From the product page" heading, so no earlier heading or lead-in (such as "Not supported yet") changes their meaning. Nothing is added without a tick. The claim gate and the written check are unchanged: they read only the brief the watch is started with.
4. The page text is untrusted data in the prompt (delimited, marked as data), is not stored, and does not enter Temporal history: the call runs in the API, not in a workflow (the issue #23 rules).
5. Cost: one model call per click, metered in the daily inference ledger; its worst case is shown before the click.
6. The fetch refuses anything but https on port 443 and any host that resolves to a non-public address, also after a redirect (SSRF guard).

**Not changed.** Watches, Reviews and their histories: no workflow or activity changes. Details: `docs/DEVELOPMENT.md`, "Brief suggestions from the product page".

## Amendment 2026-11-27: Reach score, separate from confidence (issue #49)

**Context.** Posts with activity, or from authors with many followers, give the most return per reply. Only `author_reach` (log10 followers / 6, weight 0.05) affected confidence; likes, replies, reposts and quotes, which X returns with each searched post, were unused.

**Decision (George, 2026-11-27: a separate score).**

1. Code computes a reach score in [0, 1] from the stored post metrics (engagement and author followers), as the rule in `backend/config/reach.py` with a `REACH_RULE_VERSION` date sets it. The model proposes nothing for it. The inputs and the version are stored with each card.
2. Reach does not change confidence, the threshold or which posts get a card. Confidence answers "is a reply welcome and on-topic?"; reach answers "how many people will see it?".
3. The pending queue sorts by confidence + 0.2 × reach; cards at or above reach 0.6 are highlighted and show the raw counts.

This does not reverse the rejected "engagement-only ranking" alternative above: no post is surfaced, kept or skipped for its engagement, and confidence still leads the order. The counts are from the moment of search; re-fetching them would cost X reads and is out of scope.

**Not changed.** Workflows and their histories: reach is computed by the `store_card` activity and a startup backfill. Details: `docs/DEVELOPMENT.md`, "Reach score".
