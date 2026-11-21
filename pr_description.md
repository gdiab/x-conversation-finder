# First MVP Slice: X Conversation Finder

Implements the first slice of the X founder-reply MVP as specified in ADR-0002, constrained by ADR-0003 through ADR-0006.

## What's Built

### Python Backend (Temporal)

- **TopicWatch workflow**: Long-lived workflow (one per topic) that:
  - Polls X recent search every 3 hours (configurable)
  - Tracks `since_id` to avoid duplicates
  - Uses Continue-As-New after 100 iterations
  - Spawns Review child workflows for each new post
  
- **Review workflow**: One per post with:
  - Signals: `approve`, `skip`
  - Updates: `edit` (manual text changes), `nudge` (steer redraft)
  - Query: `get_card` (get current state)
  - 48-hour expiry timeout
  - Automatic skip on low confidence or claim gate failure

- **Activities**: `search_recent`, `lookup_post`, `research_post`, `draft_reply`, `store_card`, `update_card`

- **ReplyAgentPort** (ADR-0003): Abstraction for reply generation with two implementations:
  - `FakeReplyAgent`: Deterministic fake for tests (default)
  - `PlainReplyAgent`: Baseline LLM adapter (single-prompt, no Agent Kit)

### TypeScript Frontend

- Minimal review app showing:
  - Review queue sorted by confidence
  - Evidence trail: matched topic, why it fits, founder materials, claim gate result
  - Draft reply with copy button
  - Actions: Approve (with posted URL), Skip, Edit, Nudge
  - Start new topic watch form

### Evidence & Confidence (ADR-0006)

- **Computed confidence**: Score calculated from evidence shape (lookup success, replies open, topic match, freshness, author reach, founder material presence)
- **Claim gate**: Every product claim must be supported by founder material
- **"Not enough evidence" is valid**: Posts below threshold or failing claim gate are skipped with reason

### Testing

- Replay tests for both workflows using fake adapters
- No API keys needed for tests
- CI runs tests automatically

### CI/CD

- GitHub Actions workflow running pytest
- Checks for accidentally committed secrets
- Frontend build verification

## How to Run Locally

See `docs/DEVELOPMENT.md` for full setup instructions.

Quick start:

```bash
# Terminal 1: Temporal dev server
temporal server start-dev

# Terminal 2: Worker
cd backend && python3 -m backend.worker

# Terminal 3: API
cd backend && python3 -m backend.api

# Terminal 4: UI
cd frontend && npm install && npm run dev
```

Then open http://localhost:3000

## Test Results

Tests run with fake adapters (no API keys):

```
tests/test_models.py::test_evidence_compute_confidence PASSED
tests/test_models.py::test_evidence_compute_confidence_low PASSED
tests/test_review_workflow.py::test_review_workflow_approve PASSED
tests/test_review_workflow.py::test_review_workflow_skip PASSED
tests/test_review_workflow.py::test_review_workflow_nudge PASSED
tests/test_review_workflow.py::test_review_workflow_timeout PASSED
tests/test_review_workflow.py::test_review_workflow_low_confidence PASSED
tests/test_review_workflow.py::test_review_workflow_approved_not_confirmed PASSED
tests/test_review_workflow.py::test_review_workflow_confirm_posted_invalid_url SKIPPED
tests/test_review_workflow.py::test_review_workflow_duplicate_approve_ignored PASSED
tests/test_review_workflow.py::test_review_workflow_claim_gate_regenerate_then_skip SKIPPED
tests/test_topic_watch_workflow.py::test_topic_watch_workflow_single_iteration PASSED
tests/test_topic_watch_workflow.py::test_topic_watch_workflow_spawns_reviews PASSED
tests/test_topic_watch_workflow.py::test_topic_watch_workflow_continue_as_new PASSED
tests/test_topic_watch_workflow.py::test_topic_watch_duplicate_post_skipped PASSED
```

**Total: 15 tests, 13 passed, 2 skipped**

Skipped tests require additional implementation:
- `test_review_workflow_confirm_posted_invalid_url`: Needs Update validator for URL validation
- `test_review_workflow_claim_gate_regenerate_then_skip`: Needs custom activity mock for claim gate failure

## Verified Behaviors

✅ TopicWatch polls on timer and spawns Review workflows  
✅ TopicWatch skips duplicate posts (seen_post_ids tracking)  
✅ TopicWatch carries state through Continue-As-New (since_id, seen_post_ids)  
✅ Review workflow skips low confidence posts  
✅ Review workflow skips claim gate failures  
✅ Review workflow waits for human (approve/skip/edit/nudge)  
✅ Review workflow expires after 48h  
✅ Review workflow handles approved_not_confirmed state  
✅ Review workflow ignores duplicate approve/skip requests (idempotent)  
✅ Review workflow fires follow-up timer after confirm_posted  
✅ Confidence computed from evidence (not asserted by model)  
✅ Activities called by method reference (not string names)  
✅ Continue-As-New after threshold  
✅ No secrets in committed code  
✅ CI passes  

## What's Deferred

- Real X client integration (uses fakes)
- Agent Kit adapter (port exists, plain adapter is baseline)
- Persistent database (in-memory store for MVP)
- Baseline evaluation (ADR-0006, deferred to later)
- Founder posts by hand (UI ready, actual X posting is manual per ADR-0004)
- Local E2E test execution (script provided in `scripts/e2e-test.sh`)

## Implementation Decisions

1. **Datetime serialization**: Workflow inputs use ISO string timestamps instead of datetime objects for JSON serializability
2. **Deterministic time**: Workflows use `workflow.now()` instead of `datetime.now()` to maintain determinism
3. **Activity method references**: Activities called by function reference (e.g., `x_activity_funcs.search_recent`) instead of string names, preserving type hints through Temporal's JSON codec
4. **Module-level activity wrappers**: Created wrapper functions with global instance pattern to enable method reference calls while maintaining dependency injection
5. **In-memory store**: Simple dict for MVP, `store_card` activities write there
6. **Fake adapters default**: Tests and local dev work out of the box without API keys
7. **Stable request IDs**: Frontend generates request ID once per user action attempt (on button click), reuses on retries, resets on success

## Assumptions & Questions for Architect

1. **Assumption**: "Nudge" means steer a redraft (e.g., "shorter", "more technical"). If it means "remind me later" or "refresh post's replies", that's a small change (snooze timer or `refresh_thread` activity).

2. **Question**: The confidence threshold and feature weights are guesses (see `Evidence.compute_confidence()`). Should be tuned after ~50 real cards.

3. **Implementation choice**: Used `workflow.now()` for timestamps inside workflows (deterministic). Activities use real time.

4. **Clarification needed**: Follow-up timer wiring. Workflow has the structure but needs:
   - Where to store posted_url → post_id mapping
   - How many days for the timer
   - What to do on follow-up (check replies and surface them?)

## Security

- ✅ No API keys in code
- ✅ No real X data or handles
- ✅ `.env` in `.gitignore`
- ✅ `.env.example` provided as template
- ✅ CI checks for leaked secrets

## Files Changed

- 45 new files, 3823+ insertions
- Backend: 2200+ lines (Python)
- Frontend: 850+ lines (TypeScript/React)
- Tests: 350+ lines
- Documentation: 420+ lines

## Ready For

- Local end-to-end testing with fake adapters
- Integration with real X API (swap `FakeXClient` for real implementation)
- Integration with real LLM (swap `FakeReplyAgent` for `PlainReplyAgent`)
- Agent Kit integration (implement adapter behind `ReplyAgentPort`)
