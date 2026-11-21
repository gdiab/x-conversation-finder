# QA R1 Progress

## Commits

1. **ea80930** - B1, B2: workflow.sleep and signal/update args fix
2. **940af89** - B3, B4, B6: SQLite persistence, non-blocking reviews, budget enforcement  
3. **8a1cd03** - B7: Deterministic claim gate activity

## Blockers Fixed

✅ **B1** - TopicWatch poll sleep: Wrap wait_condition with try/except TimeoutError
✅ **B2** - Signal/update args: Use args=[...] syntax everywhere
✅ **B3** - SQLite persistence: Add database.py, ReviewCardDB, BudgetLedgerDB
✅ **B4** - Non-blocking reviews: Use start_child_workflow with ABANDON policy
✅ **B6** - Budget enforcement: Add budget.py with daily ledger, check/record on all X calls
✅ **B7** - Deterministic claim gate: Add claim_gate_activity.py with code-based verification

## Blockers Remaining

🔄 **B5** - Inject adapters: Need XClientPort, inject in worker.py, make real adapters reachable
🔄 **B8** - Follow-up flow: Integrate claim_gate into review workflow, complete confirm_posted
🔄 **B9** - Handler idempotency: Add per-handler dedup, validators, all_handlers_finished

## Should-Fix Remaining

- S1: Refactor card building duplication
- S2: Move magic numbers to config
- S3: Fix non-deterministic set truncation
- S4: Don't swallow search failures
- S5: Type update_card properly
- S6: Remove user text from logs
- S7: Unify card schema across query/API/TS
- S8: API hygiene (CORS, errors, auth)
- S9: Add more tests (replay, duplicate, claim failure)

## Nits Remaining

- N1: Remove unused imports
- N2-N5: Various small fixes

## Next Actions

1. B5: Create XClientPort, inject adapters
2. B8: Wire claim_gate into review workflow  
3. B9: Add handler deduplication
4. Run tests to ensure all 10+ pass
5. Fix should-fix items in bulk
6. Fix nits
7. Run local E2E test
8. Update PR description
9. Reply in PR with per-item changes
