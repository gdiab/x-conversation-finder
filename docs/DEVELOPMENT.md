# Development Guide

## Local Setup

### Prerequisites

- Python 3.11+
- Node.js 20+
- Temporal CLI (for local dev server)

### 1. Install Temporal CLI

```bash
# macOS
brew install temporal

# Linux/WSL
curl -sSf https://temporal.download/cli.sh | sh

# Or download from https://github.com/temporalio/cli/releases
```

### 2. Start Temporal Dev Server

```bash
temporal server start-dev
```

This starts a local Temporal server on `localhost:7233` with a web UI at `http://localhost:8233`.

### 3. Backend Setup

```bash
cd backend

# Install dependencies
pip install -r requirements.txt

# Copy environment template
cp ../.env.example ../.env

# Edit .env and set your keys (optional for fake adapters)
# For testing with fakes, you can leave the API keys empty
```

### 4. Frontend Setup

```bash
cd frontend

# Install dependencies
npm install

# Run the frontend tests (Vitest + React Testing Library, needs Node 22.22.2+)
npm test
```

The frontend tests mock `src/api/client.ts` and build synthetic cards from its types
(`src/test/fixtures.ts`); they never call the backend. CI runs them in the `build-frontend` job.
Keep fixture data invented, because the repo is public.

## Running Locally

### Terminal 1: Temporal Dev Server

```bash
temporal server start-dev
```

### Terminal 2: Backend Worker

```bash
cd backend
python -m backend.worker
```

To upgrade the worker, stop the running worker before starting the new one. Do not run two worker versions on the task queue at once: an older worker can gate a draft numbered by a newer sentence split (ADR-0006, 2026-11-21 amendment, item 2).

### Terminal 3: Backend API

```bash
cd backend
python -m backend.api
```

The API will be available at `http://localhost:8000`.

### Terminal 4: Frontend Dev Server

```bash
cd frontend
npm run dev
```

The UI will be available at `http://localhost:3000`.

## Testing

### Backend Tests

```bash
cd backend
pytest -v
```

This runs all replay tests with fake adapters. No API keys needed.

### Test Coverage

- `test_review_workflow.py`: Tests Review workflow signals, updates, queries, and timeout
- `test_topic_watch_workflow.py`: Tests TopicWatch polling loop and Continue-As-New
- `test_models.py`: Tests confidence computation (rule v2, `backend/config/confidence.py`)
- `test_sentence_boundary.py`: Pins the claim gate's sentence splits after abbreviations, initials and numbers (issue #10). A changed expected split changes the sentence indices drafts reference: bump `SPLITTER_VERSION` in `backend/gate/sentence_boundary.py` and read ADR-0006 (2026-11-21 amendment, item 2) first
- `test_reply_fit.py`: Tests the should-we-reply rules on synthetic posts (`fixtures/reply_fit_posts.json`): skip reasons, confidence spread, and no draft call for a skipped post

## Using the System

1. **Start a Topic Watch:**
   - Open the UI at `http://localhost:3000`
   - Fill in the "Start New Topic Watch" form
   - The watch will poll every 3 hours (configurable)

2. **Review Cards Appear:**
   - As new posts are found, Review workflows start
   - Cards appear in the UI sorted by confidence

3. **Interact with Reviews:**
   - **Approve:** Copy the draft, post it manually on X, paste the URL
   - **Skip:** Mark as not relevant
   - **Edit:** Manually edit the draft text
   - **Nudge:** Give instructions to redraft (e.g., "make it shorter")

4. **Monitor Workflows:**
   - Temporal Web UI: `http://localhost:8233`
   - View workflow history, events, and state

## Architecture

### Workflows

- **TopicWatch** (`watch:{account}:{topic}`): Long-lived workflow that polls X search every few hours
- **Review** (`review:{account}:{post_id}`): One per post, waits for human decision

### Activities

- `search_recent`: Search X for recent posts
- `lookup_post`: Get full post details; also re-fetches the target post when the founder approves (see "Post removal" below)
- `research_post`: Analyze post and gather evidence; code then checks the proposed reply features, computes confidence and applies the skip rules (`backend/gate/reply_fit.py`), so a skipped post is never drafted
- `plan_reply`: Plan the reply before drafting (reply type, target point in the post, whether to mention the product); code validates the plan, re-requests it once, then skips the post. Runs only for posts that passed the research-stage skip rules
- `draft_reply`: Generate draft with claim gate, steered by the plan
- `store_card` / `update_card`: Persist review cards

### Post removal (ADR-0006, issue #9)

Post text is kept only while the post exists; a 404 on a re-fetch deletes it. The target post is re-fetched with `GET /2/tweets/:id`:

1. **When the founder approves.** The `approve` update calls `lookup_post` before approving. If the post is gone, `delete_post_text` removes the stored text and every model-written copy of it (`redact_post_text_copies`: why it fits, the problem span, the plan's target point, ...), marks the card `post.text_removed`, and the Review ends as skipped with "Post removed". The update fails with type `PostRemoved` (HTTP 409 from the API), and the UI reloads the card. During the re-fetch, edits, nudges and other approvals are rejected.
2. **Before the eval replays a card.** Call `backend.target_refetch.refetch_stored_card_target(x_client, workflow_id, run_key, review_is_running=temporal_review_is_running(temporal_client))` and skip the card unless it returns `"found"`. It leaves a card whose Review workflow is still running alone and returns `"live"`: that workflow holds its own copy of the evidence and re-fetches at approve. Liveness comes from Temporal, not the stored status, because Reviews skipped before issue #9 ended with their card still "pending".

If a skip or the review timeout arrives while approve's re-fetch is running, the workflow waits for the re-fetch (and any removal) to finish before it ends. A lookup counts as removed only when X's error body (on a 200 without the post, or on an HTTP 404) names this post as not found or not authorized; any other response fails the lookup and removes nothing. These response shapes come from the X API v2 docs and have not been seen live yet.

A removal applies to every stored card of the post (the same post can have a card per account). The Review workflow's own state still holds the text when the removal happened elsewhere, so `GET /api/reviews/{id}` applies the stored removal to the `get_card` query result, `draft_reply` refuses to redraft (nudge) a card that records a removal, and `store_card` stores any later card of a removed post already redacted.

Card writes (`store_card`, `update_card`, `delete_post_text`) take SQLite's write lock, so a removal cannot interleave with a write, and any write to a card of a removed post is stored redacted.

Limit: Reviews of the same post under *other* accounts are covered only partly. Their stored cards are redacted, their nudges are refused, and their card writes are stored redacted. However, a Review that is still researching, planning or drafting the post sends it to the model, and one that approves later is not stopped by the recorded removal; it finds out at its own approve re-fetch. Today one account runs watches (ADR-0005), so this path needs a second account reviewing the same post.

Each re-fetch reserves one X read through `backend/budget.py` (about $0.005, ADR-0005). Over budget it raises and removes nothing. A 404 on the founder's *reply* URL does not touch the target post's text.

Pending cards are **not** re-checked on a schedule, so X cost stays proportional to approvals and eval runs. A deleted post's text can therefore stay on a card that is never approved (pending, skipped or expired) until the eval re-fetches it.

### Review API errors (issues #15, #20)

The review endpoints (`approve`, `edit`, `nudge`, `confirm_posted`, `skip`, `GET /api/reviews/{id}`) answer an error with JSON `{"code": ..., "detail": <message>}`. The UI switches on `code`, never on the message. The Review workflow's validators raise `ApplicationError` with a type; `backend/api.py` (`REJECTION_CODES`) maps it:

| code | HTTP | meaning |
|---|---|---|
| `post_anyway_required` | 409 | the edited text failed the re-gate; also `gate_state` and `failing_sentences`. Approve again with `post_anyway=true` |
| `not_ready` | 409 | the pending card is not stored yet; try again in a moment. The card row is written inside `store_card` slightly before the workflow marks the card ready, so the UI can briefly show a card this rejects |
| `busy` | 409 | another edit, nudge or approval is running; try again after it |
| `post_removed` | 409 | approve's re-fetch found the target post gone; the review is skipped |
| `invalid_state` / `invalid_request` | 409 / 400 | not allowed in this status / bad input (422 with FastAPI's `errors` for a malformed body) |
| `rejected` | 422 | an update handler refused it (a nudged draft failed the gate, a URL that is not a reply to the post) |
| `not_found` | 404 | no such workflow, or it has ended (updates and signals) |
| `server_error` | 500 | not the Review's decision: an activity failed, Temporal is unreachable or not connected |

Approve, edit and nudge are rejected as `not_ready` until the pending card is stored, so no update writes to a card row that does not exist yet. Until a card row (pending or skipped) is stored, `GET /api/reviews/{id}` returns `{"status": "researching" | "planning" | "drafting", "in_progress": true, "evidence": null, ...}` instead of a 404 (`status` is `skipped` after a founder skip during research), with no post or evidence: a removal recorded by another Review of the post can be applied only against the stored row.

Tests that start a Review wait for its card with `backend/tests/review_waits.py` (`wait_for_card_ready`), not `env.sleep(1)`: on the time-skipping server that sleep is a real-time race against the activity chain.

### Key Constraints (ADRs)

- **ADR-0002:** Workflow shape with signals/updates/queries
- **ADR-0003:** Agent Kit behind a port (plain adapter is default)
- **ADR-0004:** Founder posts by hand (no X write operations)
- **ADR-0005:** Polling with spend limits
- **ADR-0006:** Evidence trail and computed confidence

### Budget accounting (issue #8)

`backend/budget.py` keeps the ADR-0005 daily caps and kill switch. Each paid
call is counted per Temporal activity attempt:

- Reservations are keyed by workflow, run, activity and **attempt** (and a call
  index for inference). A retry after a lost completion makes a new reservation,
  so both paid calls are counted.
- Inference calls reserve inside the adapter, right before the request, sized
  from an upper bound on the prompt's tokens (`prompt_token_bound`: its UTF-8
  bytes, since a byte-level BPE token covers at least one byte) plus the call's
  max output tokens (`RESEARCH_MAX_OUTPUT_TOKENS`, `PLAN_MAX_OUTPUT_TOKENS`,
  `DRAFT_MAX_OUTPUT_TOKENS`, sent as `max_completion_tokens`). The activity opens
  the meter with `meter_inference()`; an adapter that makes a paid call reserves
  through `current_inference_meter()`. Each call is then reconciled to the
  reported usage; missing or partial usage keeps the full reservation.
- `PlainReplyAgent` refuses a call made outside a metered activity attempt
  (`UnmeteredInferenceCall`); adapter unit tests with stub clients opt out with
  `require_budget_meter=False`. An injected `AsyncOpenAI` client gets
  `max_retries=0` too.
- A failed inference request stays counted at its reservation, whatever the
  error: `OPENAI_BASE_URL` may be a gateway, so a status proves nothing about
  upstream billing. An X request that X refused with a 4xx status (including
  429, except 408) is released: X bills per post returned and the X base URL is
  fixed in code. A request whose connection was never established
  (`httpx.ConnectError`/`ConnectTimeout`, also as the cause of an OpenAI SDK
  error) is released for both. 408, 5xx, read timeouts and crashes stay counted.
- X search reserves `billable_max_results(max_results)`: the HTTP adapter clamps
  the request to X's 10-100 range, and the reservation covers what is sent.
- A reservation that is already closed refuses to authorize another request
  (`ReservationClosed`). `lookup_post` counts 1 read per attempt even when it returns None:
  the HTTP adapter returns None only when X's error names the post as gone
  (issue #9), and raises `MalformedLookupResponse` (also counted, X billed it)
  for post data it cannot convert. A 404 that does not name the post is raised
  as the HTTP error and released.
- Output that hits the max output tokens (`finish_reason == "length"`) fails
  research, plan and draft without retry (`InferenceOutputTruncated`); the call
  stays counted.
- The OpenAI client runs with `max_retries=0`: Temporal's retry policy owns
  retries, so each retried request is reserved and counted.
- `FakeReplyAgent` makes no paid calls and reserves nothing.

## Fake Adapters

For testing and CI, the system uses fake adapters:

- **FakeXClient:** Returns synthetic posts, no API calls
- **FakeReplyAgent:** Deterministic research and drafting

These allow the full system to run without API keys.

## Production Considerations

To use real X and LLM APIs:

1. Set API keys in `.env`
2. Replace `FakeXClient` with a real X client in activities
3. Replace `FakeReplyAgent` with `PlainReplyAgent` (or Agent Kit adapter)
4. Monitor spend limits in the config

## Troubleshooting

### Temporal Connection Errors

Ensure the Temporal dev server is running:

```bash
temporal server start-dev
```

### Worker Not Processing

Check that the worker is running and connected to the right task queue:

```bash
cd backend
python -m backend.worker
```

### Frontend Can't Connect

Ensure the backend API is running on port 8000:

```bash
cd backend
python -m backend.api
```
