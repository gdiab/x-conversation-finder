# ADR-0001: The SaaS is a separate product from Outpost

**Status:** Accepted (2026-11-20, George)

## Context
Outpost finds where a product's customers gather and produces an operator-approved posting plan. Its settled map (planning issue #1, https://github.com/gdiab/agentic-campaign-platform-planning/issues/1) is a local, bring-your-own-runtime CLI that drives the user's own `claude`/`codex`, with no hosted inference. Its ADRs follow from that:
- ADR-0008: Markdown is the only representation, so no database.
- ADR-0010: the harness seam is ACP over the operator's own subscription.
- ADR-0011: a Run never blocks on an answer.
- ADR-0014: the web view never runs anything.
- ADR-0020: a Run is forward-only.

The new MVP is a hosted, long-lived watcher with durable human pauses, a database and a web app. It contradicts every one of those. No Outpost ADR covers it.
Outpost ADR-0086 already rules X posts in as "conversation venues", and its research (`docs/research/x-search-keyless-surface.md`) is current as of 2026-09-29.

## Decision
- The MVP is its own repo, the GTM SaaS. It is not in the planning repo and not in outpost-prototype.
- It shares patterns and research with Outpost, never code or ADR numbering: claim gate, computed confidence (0079), baseline-judged evaluation (0078), evidence trail, "not enough evidence" as a valid skip, replay.
- Outpost's ADRs are referenced by link and never amended from here. Where this repo diverges, it says so.

## Alternatives considered
- **Inside the planning repo.** Rejected: that repo holds Outpost's settled decisions, and a hosted product would need an exception to five of them.
- **Inside outpost-prototype as a new mode.** Rejected for the same reason, and the prototype is parked awaiting its second operator.
- **Amend Outpost's ADRs to allow hosted runs.** Rejected: it reopens the "do not relitigate" constraints in issue #1 for a product Outpost's audience didn't ask for.
- **Share a library.** Deferred: the prototype is TypeScript and this backend may be Python (Q3). Port the ideas, not the code.

## Consequences
- Clean ownership: Outpost stays a local CLI; this product owns its own database and runtime.
- Duplicated concepts (claim gate, confidence) must be kept consistent by hand. Cross-link, and note drift.
- This repo needs its own ADR set and `CONTEXT.md` vocabulary before more features.
- **Decided (Q1, 2026-11-20, George):** a demo on George's own X key, built so each customer can bring their own key later. The X Developer Agreement forbids sharing keys and managed-service use (see the research note), so a product means per-customer keys or an enterprise deal. Keys are per-account config, never global.
- **Conflicts list** (Outpost ADRs this product does not inherit): 0008, 0010, 0011, 0014, 0020.
