# Remaining QA R1 Work

## Status: B1-B5, B7 DONE (6/9 blockers fixed)

Commits: ea80930, 940af89, 8a1cd03, 958082e, 3091eb2

## B8 - Follow-up Flow (IN PROGRESS)

Need to:
- [ ] Import and call claim_gate activity in Review workflow after draft_reply
- [ ] Regenerate once on claim gate failure, then skip
- [ ] Complete confirm_posted Update (already exists, verify lookup works)
- [ ] Ensure 48h approved_not_confirmed timeout (already exists)
- [ ] Ensure 3d follow-up timer fires (already exists)
- [ ] Verify check_followup is capped (max_results=10 already set)
- [ ] Verify followup_due status gets set (already exists)
- [ ] Add paste-URL action in TS app
- [ ] Add followup_due view in TS app

## B9 - Handler Idempotency (NOT STARTED)

Need to:
- [ ] Per-handler request_id deduplication (not shared across handlers)
- [ ] Add validators to Updates (check state before executing)
- [ ] Wait for all_handlers_finished before CAN/completion
- [ ] Make frontend generate stable request IDs (not per-call)

## Should-Fix (S1-S9)

- [ ] S1: Refactor card building (one helper function)
- [ ] S2: Move magic numbers to config (48h timeout, etc.)
- [ ] S3: Fix seen_post_ids (ordered list, not set)
- [ ] S4: Don't swallow search failures
- [ ] S5: Type update_card patches
- [ ] S6: Remove user text from workflow logs
- [ ] S7: Unify card schema (query/API/TS)
- [ ] S8: API hygiene (CORS, errors, lifespan)
- [ ] S9: Add tests (replay, duplicate post, claim failure)

## Nits (N1-N5)

- [ ] N1: Remove unused imports
- [ ] N2-N5: Small fixes

## Final Steps

- [ ] Run full test suite - report exact counts
- [ ] TypeScript build
- [ ] Local E2E test with Temporal dev server
- [ ] Update PR description with accurate info
- [ ] Comment in PR mapping B1-B9 to SHAs
- [ ] Clean up scratch files

## Next Action

Continue with systematic fixes of B8, B9, then batch S1-S9 and N1-N5.
