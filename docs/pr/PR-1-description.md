# X Conversation Finder - MVP Slice 1

**Status:** ✅ Ready for review  
**HEAD:** `5eeb730` - Add complete E2E test starting from TopicWatch  
**Base:** `dfb08a6` - Initial commit (empty repository)  
**Tests:** 62/62 passing (0 skipped, 0 xfails)  
**Frontend:** ✅ Build passing

---

## Scope

This PR implements the first MVP slice per ADRs 0001-0006:

### Backend (Python + Temporal)
- ✅ `TopicWatch` workflow: durable polling loop with Continue-As-New
- ✅ `Review` workflow: research → draft → claim gate → human approval → follow-up timer
- ✅ Signals and Updates: `approve`, `skip`, `edit`, `nudge`, `confirm_posted`
- ✅ Query: `get_card` returns full evidence trail
- ✅ Activities: `search_recent`, `lookup_post`, `score_post`, `draft_reply`, `claim_gate`, `check_followup`, `store_card`, `update_card`
- ✅ Fake X and Reply adapters for testing (ADR-0003)
- ✅ Atomic budget system with daily spend tracking and kill switch
- ✅ Database: SQLite with SQLAlchemy ORM
- ✅ HTTP API: FastAPI with Temporal client integration

### Frontend (TypeScript + React)
- ✅ Review inbox with evidence trail display
- ✅ Actions: Approve, Edit, Skip, Nudge, Confirm Posted
- ✅ Budget meter
- ✅ Build passing with no TypeScript errors

### Evidence & Safety (ADR-0006)
- ✅ Claim gate: every product claim must match founder material
- ✅ Product capability sentences: detected and verified
- ✅ Computed confidence from evidence shape (never LLM-asserted)
- ✅ "Not enough evidence" as valid skip reason
- ✅ Evidence trail stored with every review card

### Testing
- ✅ 62 unit/integration tests (all passing)
- ✅ Real E2E test with Temporal dev server, fake adapters, 8-second durable timer
- ✅ Continue-As-New test verifies CONTINUED_AS_NEW event and status
- ✅ Concurrent budget test proves atomic spend tracking
- ✅ Claim gate tests: risky words, capability sentences, regeneration

---

## R4 QA Items Completed

All R4 QA items from the task are complete:

### 1. Claim Gate Product Capability Sentences ✅
**Commits:** `62118b8` (R4-SF1), `4b33304` (tests)
- ✅ Sentences mentioning product name or we/our/my + capability verb are detected
- ✅ Gate fails when capability sentences lack covering claims
- ✅ Tests added for "Our tool helps teams export to Notion" with no claim (blocked) and with matching claim (allowed)
- ✅ ADR-0006 constraint: capability sentences without covering evidence must fail gate

### 2. Follow-up Timer Documentation ✅
**Commits:** `2df5a12` (R4-SF2)  
- ✅ Documented in ADR-0002: "The follow-up timer (default: 3 days after approval) is a durable Temporal timer and cannot be cancelled once the workflow reaches the approved state."
- ✅ Code comment in `review.py` line 50: "R4-SF2: The follow-up timer cannot be cancelled once it starts"

### 3. lookup_post: Build URL from API Response ✅
**Commits:** `15b10db` (R4-SF3)
- ✅ Post URL built from `author.username` and `post.id` (no placeholder)
- ✅ Username from author expansion in API response
- ✅ Test added: `test_lookup_post_builds_url_from_response`

### 4. X Client 429 Handling ✅
**Commits:** `aa604ff` (R4-SF4)
- ✅ Raises typed `ApplicationError(type='RateLimitExceeded', non_retryable=False)`
- ✅ Carries `next_retry_delay` from `Retry-After` or reset header
- ✅ Test added: `test_x_client_429_raises_retryable_error`

### 5. NN5 and Nits ✅
**Commits:** `b349175` (R4-NN5)
- ✅ `get_card` query uses `_build_card` helper (no hand-built dict)
- ✅ Earlier nits (NN1-NN4) were completed in R3: double JSON encoding, try/finally, temporalio pin, unused settings

### 6. Real Local E2E Test ✅
**Commits:** `b82d119`, `716197a`, `5eeb730`
- ✅ Script: `scripts/e2e-full.sh`
- ✅ Starts Temporal dev server (localhost:7233)
- ✅ Worker with fake X and Reply adapters
- ✅ API server (localhost:8002)
- ✅ **Starts TopicWatch with immediate poll** (not Review directly)
- ✅ TopicWatch naturally spawns Review child workflow from FakeXClient fixture data
- ✅ Creates Review card with **pending** status (verified in DB and API)
- ✅ Approve and confirm_posted work
- ✅ **8-second durable timer** (not 0) fires automatically
- ✅ `check_followup` executes after timer
- ✅ Final status: **followup_due**
- ✅ Full test output: 34 seconds end-to-end
- ✅ See `docs/pr/e2e-output.txt` for complete output

### 7. PR Description Update ✅
**This document**
- ✅ Accurate scope
- ✅ Test counts: 62/62 passing
- ✅ Complete SHA map with real commit subjects
- ✅ Removed stale WIP lines

---

## Complete SHA Map (3973ae2 → 5eeb730)

Starting from base `3973ae2` (R4-B2: Implement real budget reconciliation for reads and tokens), all commits added in this PR:

1. **`62118b8`** - R4-SF1: Add product capability sentence detection to claim gate
2. **`2df5a12`** - R4-SF2: Document that follow-up timer cannot be cancelled
3. **`15b10db`** - R4-SF3: Remove placeholder username fallback in lookup_post
4. **`aa604ff`** - R4-SF4: Handle 429 rate limits with typed ApplicationError
5. **`b349175`** - R4-NN5: Fix get_card query to use _build_card helper
6. **`b82d119`** - Add followup_delay_seconds config for E2E testing with real durable timer
7. **`716197a`** - Update E2E script to use 8-second followup_delay_seconds
8. **`4b33304`** - Add capability sentence tests and fix gate logic
9. **`5eeb730`** - Add complete E2E test starting from TopicWatch (HEAD)

---

## Test Results

### Backend Tests
```
cd backend && source venv/bin/activate && pytest -q
======================== 62 passed, 2 warnings in 4.47s ========================
```

**Breakdown:**
- Model tests: 2/2 ✅
- Workflow tests: 13/13 ✅  
- Activity tests: 15/15 ✅
- Integration tests: 10/10 ✅
- Claim gate tests: 10/10 ✅  
- Budget tests: 7/7 ✅
- API tests: 5/5 ✅

**No skipped tests. No xfails.**

### Frontend Build
```
cd frontend && npm run build
✓ built in 455ms
```

### E2E Test
```
cd /agent/repos/x-conversation-finder
bash scripts/e2e-full.sh

=== E2E Test Complete ===
✓ TopicWatch started and polled
✓ Review child workflow spawned naturally
✓ Review card created with status 'pending'
✓ Review approved  
✓ Posted reply confirmed
✓ Follow-up timer fired (8s - real durable timer)
✓ check_followup activity executed
✓ Final status: followup_due

All operations used fake adapters (no real X API calls)
```

Full E2E output available in `docs/pr/e2e-output.txt`

---

## Architecture Decisions

This implementation follows ADRs 0001-0006:

- **ADR-0001:** Separate product from Outpost (own repo, shared patterns)
- **ADR-0002:** TopicWatch polling loop + Review child workflows with human pause
- **ADR-0003:** Plain Temporal spine, Agent Kit behind a port (reply agent only)
- **ADR-0004:** Founder posts by hand (no auto-posting)
- **ADR-0005:** Pay-per-use X API with polling and budget enforcement
- **ADR-0006:** Evidence trail, claim gate, computed confidence, baseline eval schema

---

## No Secrets Committed

This is a PUBLIC repository. No `.env` files, API keys, or real X data committed.
All E2E tests use fake adapters.

---

## Ready for Merge

All QA items complete. Tests passing. Frontend builds. E2E test proves end-to-end flow with real durable Temporal timer, starting from TopicWatch naturally spawning Review workflows.
