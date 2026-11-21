# ADR-0003: The Agent Kit sits behind a port, and plain temporalio owns the spine

**Status:** Accepted (2026-11-20, George)

## Context
George asked whether this is a compelling enough use of the Temporal Agent Kit or too small a slice, and part of the motivation is a showcase for a possible Temporal role.
Facts, checked 2026-11-20:
- The kit (https://github.com/temporal-community/temporal-agent-kit, MIT, Python primary plus TypeScript) calls itself "Experimental. APIs change between releases. Pin versions."
- It is at Python 0.12.1 and npm 0.6.1, both pre-releases from 2026-11-19, with near-daily commits.
- It turns an AI-SDK agent (OpenAI Agents SDK, Pydantic AI, Strands, Gemini) into a durable workflow with typed messages, durable tool activities, tool-call approval, subagents, "Code Mode" and a console.
- Its approval gates *tool calls*. Our approval gates the *final reply*, a product decision.

## Decision
- **Spine in plain `temporalio`:** `TopicWatch`, `Review`, timers, signals, Updates, Continue-As-New (ADR-0002). None of these need the kit, and showing them directly is the most legible Temporal.
- **The kit powers one thing: the reply agent.** `ReplyAgent` runs the scoring-and-drafting loop with durable tools: `lookup_post`, `fetch_thread`, `read_founder_material`, `check_claim`. It runs as a child workflow, called through a thin `ReplyAgentPort` (`research(post, topic) -> Evidence`, `draft(evidence, steer) -> Draft`). Only the port's adapter imports the kit.
- Pin the kit exactly, keep a plain-activity adapter (a single LLM call, no loop) as the fallback and as the baseline for ADR-0006, and replay-test the workflows in CI.

### Honest judgment on kit fit
- **As a whole product, the slice is too small to need the kit.** The watch loop, the human pause and the follow-up timer are ordinary Temporal; the kit adds nothing there, and its approval feature does not match ours.
- **The kit adds real value in the agent loop:** an LLM that decides which tools to call (look up the post, read the thread, check the founder's material), each call a retried, observable activity, with the whole run in history and a streaming console. A single prompt cannot show that.
- **So: not compelling as the centerpiece, compelling as the inner agent.** The demo is stronger if the headline is idiomatic Temporal with the kit as one well-fenced integration, rather than "an app built on the kit".
- What makes it showcase Temporal well:
  1. **Signals and Updates:** approve, skip and edit/nudge on a paused `Review`.
  2. **Durable timers:** the multi-hour poll, the 48-hour expiry and the days-later follow-up.
  3. **Continue-As-New:** keeping a months-long `TopicWatch` bounded.
  4. **Replay of a scoring run:** two meanings, both shown. Temporal replay (the Replayer re-executes saved histories to catch non-determinism) runs in CI. The product-level replay re-runs scoring over stored evidence to explain or compare a past decision (Outpost ADR-0074's idea), and doubles as the baseline eval.
  5. **Idempotent effects, visibility and failure recovery** (kill the worker mid-review in the demo).

## Alternatives considered
- **Build everything on the kit, including the loop.** Rejected: ties the spine to an experimental API and obscures the Temporal primitives a reviewer wants to see.
- **No kit at all.** Reasonable, and the fallback. Rejected as the default because the agent loop with durable tools is where the kit is strongest, and it's a relevant skill to show.
- **A different agent framework (LangGraph with a checkpointer, etc.).** Rejected: durability would be the framework's, not Temporal's, defeating the demo's point.
- **Code Mode.** Not used in v1; it is a good later experiment for fanning out scoring.

## Consequences
- Kit upgrades are contained to one adapter; breaking changes cost one file, and workflows that replay old histories need a version policy (Temporal patching/versioning) at each upgrade.
- Two adapters to maintain (kit and plain). Keep the plain one tiny.
- The Temporal-job angle is served by what the reviewer sees in the code, so keep the spine free of kit imports.
- **Decided (Q2, 2026-11-20, George):** George pays for inference, through a provider-agnostic port with a hard daily budget. Outpost's no-hosted-inference rule (ADR-0010) doesn't apply here.
- **Decided (Q3, 2026-11-20, George):** Python, since the kit and its AI-SDK integrations are Python-first; the npm package has no Node server yet (per the 2nd-brain note).
- **Decided (Q5, 2026-11-20, George):** a local Temporal dev server. The code is identical on Cloud.
- **Caveat:** Temporal docs were not fetched for this ADR, and the kit is experimental.
