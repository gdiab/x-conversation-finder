# ADR-0004: The founder posts by hand and approves every draft

**Status:** Accepted (2026-11-20, George)

## Context
The product surfaces posts and drafts replies. The founder decides what is posted, which keeps it organic.
X's rules (third-party summaries; confirm against X's own Help Center before launch, https://kairopost.com/blog/x-automation-rules, https://postmine.tech/en/blog/x-automation-tools-compared):
- Automated replies based on keyword searches alone are not permitted.
- AI reply bots need X's prior written approval.
- Scripting the X website risks suspension.
- Since 2026-02-23, self-serve API replies only work when the author summoned the account.

The API could post (Post: Create is $0.015, $0.200 with a URL per https://docs.x.com/x-api/getting-started/pricing), so the constraint is policy, not capability. Outpost made the same call (planning issue #1: v1 refuses to reply on the user's behalf).

## Decision
- The product never posts, replies, likes, follows or DMs. No code path holds a write-scoped token; the X credential is read-only by construction.
- "Approve" means "I will post this": the UI offers copy-and-open-in-X.
- The founder pastes the posted reply URL. The system verifies it with a post lookup (idempotent by post ID), records it, and starts the follow-up timer. Without a pasted URL, the post is marked "approved, not confirmed" and expires.
- Drafts are never batch-approved; one card, one decision. The draft may carry no product claim the founder's material does not support (ADR-0006).
- Out of scope for v1: auto-posting, DMs, multiple accounts.

## Alternatives considered
- **Post via API on approval.** Rejected: keeps a human click but the account would still post programmatically; write cost and reply-summon limits apply, and it invites policy trouble.
- **Browser automation to post.** Rejected: non-API scripting risks suspension.
- **Seek X's written approval for an AI reply bot.** Rejected for v1: slow, uncertain, and unnecessary for a drafts-only tool.
- **Skip confirmation entirely.** Rejected: follow-ups and outcome learning need the posted URL.

## Consequences
- The product is safe against the main automation bans, but is not hands-free; value depends on a founder who reviews daily.
- The read-only credential makes "never posts" checkable in a test.
- Posting failures are not our failures; reconciliation is by URL.
- If George later wants auto-post, that needs a new ADR and X's approval, not a flag.
- **Caveat (unverified):** the X automation rules above come from third-party summaries, not X's own Help Center; confirm before launch.
