# x-conversation-finder

Given a product and a few topics, this finds X posts where the founder could join the conversation, with an evidence trail for why each fits, a draft reply checked against the founder's own material, and a review app where the founder approves, edits, skips or steers each draft. The founder posts by hand from their own account; the product never posts. The backend is Python on Temporal (durable watches, signals, timers); the review app is TypeScript.

**Status:** design only. The proposal and six accepted ADRs (2026-11-20) are written; there is no application code yet.

- Proposal: [docs/proposal.md](docs/proposal.md)
- Decisions: [docs/adr/](docs/adr/README.md)
