# ADR-0002: Workflow shape and the founder review app

**Status:** Accepted (2026-11-20, George)

## Context
First slice (approved): given a product brief, topics and the founder's X handle, find X posts where the founder could join the conversation, and queue a few per day with a draft for the founder to approve. The system has a backend and, per George, a frontend. George wants to see the research and the drafts together, not just a draft.

Temporal fit: a durable loop per topic, a pause for a human, and follow-ups days later. Two Temporal facts shape the design: workflow code must be deterministic, so X and LLM calls are activities; and an activity whose result was not recorded may run again, so every effect needs an idempotency key (see the replay/duplicate-effect caveats in the 2nd-brain note `manetu-thread-is-the-workflow-temporal.md`).

## Decision
**Workflows**
- `TopicWatch` (one per account+topic; ID `watch:{account}:{topic}`): a loop that sleeps a durable timer of a few hours, calls `search_recent` with `since_id`, filters already-seen post IDs, and fans out per new post. It uses Continue-As-New after N iterations, carrying only `since_id` and the budget counters.
- `Review` (one per post, a child of `TopicWatch`; ID `review:{account}:{post_id}`): runs `lookup_post`, `score_post`, `draft_reply`, `claim_gate`; stores the card; then waits for the founder.
  - Duplicate IDs are rejected, so a post is never reviewed twice.
  - A skipped result (low computed confidence, or the claim gate fails with no clean draft) ends as `skipped: not enough evidence`.
  - Passing posts wait on a human with an overall timeout (for example 48 hours, then expire).
- After approval the founder pastes the URL of their posted reply. A durable timer then fires days later, and `check_followup` reads replies to it.

**Messages into `Review`**
- Signals: `approve`, `skip`.
- Updates (tracked request/response, validated): `edit(text)` and `nudge(instruction)`. Nudge re-runs `draft_reply` with a steer such as "shorter" or "less salesy", then re-runs the claim gate, and returns the new draft.
- Query: `get_card` returns the whole review card.
- Each human message carries a client request ID so a retried click is a no-op.

**Activities** are `search_recent`, `lookup_post`, `score_post`, `draft_reply`, `claim_gate`, `store_card`, `notify`, `check_followup`. Each takes an idempotency key (post ID plus input hash) and writes through the store with upsert.

**Frontend: a minimal review web app** (TypeScript, a thin client over a small HTTP API on the backend; it never talks to Temporal directly).
- Inbox of cards sorted by computed confidence.
- Each card shows the post, the **evidence trail and research** (matched topic, why it fits, computed confidence and its inputs, claim-gate result with any unsupported claims), and the draft alongside it.
- Actions: Approve (copy and open in X), Edit, Skip with an optional reason, and Nudge (a steer box that redrafts).
- Plus a "mark posted" field for the URL, and a read-only spend meter.
- No auth beyond a single-user session for the MVP.

## Alternatives considered
Two shapes were sketched before choosing.
- **A. Single workflow per topic holding all pending posts** (state: map of post to status, signals carry the post ID). Fewer workflows, but one history holds every review, the approval timeouts interleave, and a stuck review hurts the loop. Rejected.
- **B. Workflow per post, spawned by a thin watcher** (chosen). One owner per piece of state; a duplicate post ID is rejected by Temporal itself; reviews expire independently.
- **Cron or schedule instead of a loop.** A Temporal Schedule is a fine alternative for the poll. Deferred: the loop demonstrates timers plus Continue-As-New, but a Schedule is the simpler production answer, and the ADR can swap them without changing `Review`.
- **Signals only (no Updates) for edit and nudge.** Rejected: the UI needs the new draft or a validation error back, which is what Updates give.
- **Frontend talks straight to Temporal.** Rejected: it exposes workflow internals and credentials to the browser.
- **Slack or email approval instead of a web app.** Rejected: the research and evidence trail do not fit, and George explicitly wants to see them.

## Consequences
- The review card is the one place the evidence trail is rendered, so ADR-0006's schema is its contract.
- Many small workflows: fine for Temporal, but we need visibility via search attributes (account, topic, status).
- A human decision after the timeout is refused with a clear message in the UI.
- **Assumption for George:** "nudge-replies" was read as steering a redraft. If he meant "remind me later" or "refresh the post's replies", it's a small change (a snooze timer, or a `refresh_thread` activity).
- **Decided (Q3, 2026-11-20, George):** Python backend, TypeScript review app. The shapes above hold in both Temporal SDKs.
- **Caveat:** the Temporal behaviour described here comes from background knowledge and the 2nd-brain note; docs.temporal.io was not fetched.
- **Assumption to confirm:** nudge replies = steer a redraft (already stated above).
