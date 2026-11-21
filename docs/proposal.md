# Proposal: X conversation-finder MVP on Temporal
*Replaces Outpost's short 3:20 PM PT note to George.*

## (a) Where it lives
**Recommendation: a new repo for the GTM SaaS, not the planning repo and not Outpost.**
- No Outpost ADR covers this MVP. Planning issue #1 settles Outpost as a local, bring-your-own runtime that drives `claude`/`codex` with no hosted inference ([map](https://github.com/gdiab/agentic-campaign-platform-planning/issues/1)). A hosted Temporal watcher breaks that, along with ADR-0008 (no database), ADR-0011 (a Run never blocks) and ADR-0020 (forward-only Runs).
- The SaaS has no ADR set yet, because George is still writing it. This slice should supply its first ADRs without pre-empting his backend architecture.
- Reuse from the Outpost prototype ([gdiab/outpost-prototype](https://github.com/gdiab/outpost-prototype), TypeScript): the claim gate, computed confidence, and replay. Also reuse the evidence trail per surfaced post, "not enough evidence" as a valid skip, and founder approval before anything is posted.
- Evaluate reply-worthiness the way Outpost does. Confidence is computed from the shape of the evidence and never asserted by the model (ADR-0079). Score against a single-prompt baseline using a blind-judged rubric of precision, traceability and honesty about uncertainty (ADR-0078).
- ADR-0086 and `research/x-search-keyless-surface.md` already cover X posts as venues, and are reusable.

## (b) First slice
- **In:** a product brief, 1–5 topics, and the founder's X handle.
- **Out:** a few queued posts a day, each with an evidence card and a draft reply. The founder posts by hand.
- **Workflows:**
  - `TopicWatch` is one long-lived workflow per topic. It loops on a timer every few hours and uses Continue-As-New.
  - It runs search → `score_post` → `draft_reply` → claim gate for each new post ID.
  - A post that passes starts a child `Review` workflow. It waits for an `approve|edit|skip` signal and answers a card query.
  - Approval records the pasted post URL. A durable timer then runs a follow-up check days later.
- **Rules:** LLM and X calls live in activities only, with stable IDs. Skipping is a valid outcome.
- **Agent Kit:** keep it behind a thin score-and-draft port, pinned to a version. Build the core on plain `temporalio`.
- **Store:** Postgres.
- **Approval UX:** a notification, then a page with the card, the draft, and Approve/Edit/Skip.
- **Out of scope for v1:** auto-posting, DMs, multiple accounts, other platforms, filtered stream, billing.

## (c) X cost
Verified against [pricing](https://docs.x.com/x-api/getting-started/pricing) on 2026-11-20:
- Posts Read costs $0.005 per resource. User Read costs $0.010. Post Create costs $0.015, or $0.200 with a URL.
- The cap is 3M Post reads a month. Charges are deduplicated within a 24-hour UTC day, as a "soft guarantee".
- 300 posts a day is about $45 a month. Recent search covers 7 days and is open to all developers ([docs](https://docs.x.com/x-api/posts/search/introduction)).
- **Recommended:** pay-per-use with a $25–50 monthly limit and auto-recharge off. Cap results per search.
- Filtered stream is on pay-per-use with 1 connection ([docs](https://docs.x.com/x-api/posts/filtered-stream/introduction)). I didn't confirm its per-post billing, so poll search instead.
- **Rules (third-party summaries, unconfirmed against X's own page):** keyword-only automated replies are banned. AI reply bots need X's written approval. Since 2026-02-23, self-serve API replies need the author to have summoned the account ([kairopost](https://kairopost.com/blog/x-automation-rules), [postmine](https://postmine.tech/en/blog/x-automation-tools-compared)). A human-posts design fits.

## (d) Questions for George
1. Is this a Temporal portfolio demo or a product? A product means other users' keys, because X's Developer Agreement bars key sharing and managed service use.
2. Who pays for LLM inference?
3. Python or TypeScript? The kit is Python-first.
4. Is the first user only you, or you and your partner?
5. Is Temporal a local dev server for now, or Cloud?

## (e) Suggested ADRs
1. The SaaS is separate from Outpost, and shares only patterns and X research.
2. Workflow shape: watch, review, follow-up, signals and Continue-As-New.
3. The Agent Kit sits behind a port and is pinned.
4. The founder posts by hand and approves every draft.
5. X access: pay-per-use, spend limit, polling.
6. The evidence and claim gate, with computed confidence and a baseline eval.
