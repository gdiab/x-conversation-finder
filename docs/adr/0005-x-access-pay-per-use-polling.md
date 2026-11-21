# ADR-0005: X access: pay-per-use, spend limit, polling

**Status:** Accepted (2026-11-20, George)

## Context
Verified 2026-11-20 against https://docs.x.com/x-api/getting-started/pricing:
- Posts Read $0.005 per resource; User Read $0.010; Post Create $0.015 ($0.200 with a URL).
- Cap 3M Post reads per month. Charges deduplicated within a 24-hour UTC day, a "soft guarantee".
- Recent search covers 7 days and is open to all developers (https://docs.x.com/x-api/posts/search/introduction).
- Filtered stream is on pay-per-use with 1 connection and 1,000 rules (https://docs.x.com/x-api/posts/filtered-stream/introduction). Its per-post billing is **not confirmed**.

300 posts a day is about $45 a month. Outpost's research (`x-search-keyless-surface.md`, ADR-0021/0086) found no sanctioned keyless route, so a key is required. It also notes that X's Developer Agreement bars sharing keys and managed-service use.

## Decision
- Poll recent search from `TopicWatch` every few hours with `since_id` and `max_results` capped; request author data through the `author_id` expansion rather than separate User reads.
- Pay-per-use credits with a **$25-50/month spending limit set in the X console and auto-recharge off.**
- Enforce a software budget as well: a `spend` ledger updated by each X activity, and a per-day cap checked in the activity before the call. At the cap the watch records "budget reached" and sleeps until the next UTC day.
- Reads are idempotent through the 24-hour UTC dedup plus our own seen-post table, so a retry or replay costs nothing extra in the same UTC day. A lookup crossing midnight UTC can be billed twice; accept it.
- Use the account's key from config per account, never a global.

## Alternatives considered
- **Filtered stream.** Rejected for v1: needs a long-lived connection outside Temporal's activity model, per-post billing unconfirmed, and polling at a few hours is enough for a human-in-the-loop product.
- **Activity API webhooks** ($0.005 per `post.create` event, per the pricing page). Rejected: events for a topic aren't a search, and billing scales with volume we do not control.
- **Enterprise plan.** Not needed at this volume; revisit only for a product with many users.
- **Scraping or unofficial readers.** Rejected: against X's terms; nitter is dead per the research note.
- **Software cap only.** Rejected: the console limit is the real backstop if our ledger has a bug.

## Consequences
- Predictable cost with a hard ceiling; a busy day yields fewer posts rather than a bill.
- Search is up to hours stale, and capped by a 7-day window.
- A key per account fits a later per-user-key product.
- Pricing changes: the page says prices are subject to change; the spend ledger reads rates from config and a unit test pins the numbers.
- **Decided (Q1, 2026-11-20, George):** George's own key, a demo, with per-account keys so each customer can bring one later.
- **Decided (Q4, 2026-11-20, George):** George is the first user; his partner would be a second operator on their own key.
- **Caveat (unverified):** filtered-stream per-post billing was not confirmed; this ADR does not depend on it.
