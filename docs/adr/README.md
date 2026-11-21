# ADRs: X conversation-finder MVP

All six are **Accepted** (2026-11-20, George: "looks good to me"). The GTM SaaS has no other ADR set yet, so numbering starts at 0001 here. Source: [`../proposal.md`](../proposal.md), approved by George on a voice call 2026-11-20.

| # | Title | Open items |
|---|---|---|
| [0001](0001-separate-product-from-outpost.md) | The SaaS is a separate product from Outpost | none |
| [0002](0002-workflow-shape-and-review-app.md) | Workflow shape and the founder review app | none |
| [0003](0003-agent-kit-behind-a-port.md) | The Agent Kit sits behind a port, and plain temporalio owns the spine | none |
| [0004](0004-founder-posts-by-hand.md) | The founder posts by hand and approves every draft | none |
| [0005](0005-x-access-pay-per-use-polling.md) | X access: pay-per-use, spend limit, polling | none |
| [0006](0006-evidence-claim-gate-and-baseline-eval.md) | Evidence trail, claim gate, computed confidence, baseline eval | none |

## Decisions recorded (2026-11-20, George)
| Q | Decision |
|---|---|
| Q1 | Demo on George's own X key, built so each customer can bring a key later. |
| Q2 | George pays for inference, via a provider-agnostic port with a hard daily budget. |
| Q3 | Python backend, TypeScript review app. |
| Q4 | George is the first user; his partner would be second, on their own key. |
| Q5 | Local Temporal dev server. |

## Assumption to confirm
"Nudge replies" = steer a redraft ("shorter", "less salesy"), not "remind me later". See ADR-0002.

## Caveats (unverified)
- X automation rules come from third-party summaries, not X's own Help Center.
- Filtered-stream per-post billing is unconfirmed.
- Temporal docs were not fetched; Temporal behaviour is from background knowledge and the 2nd-brain note.
- Confidence weights and the eval design are untested.
