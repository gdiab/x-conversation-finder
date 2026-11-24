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
```

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
- `lookup_post`: Get full post details
- `research_post`: Analyze post and gather evidence; code then checks the proposed reply features, computes confidence and applies the skip rules (`backend/gate/reply_fit.py`), so a skipped post is never drafted
- `draft_reply`: Generate draft with claim gate
- `store_card` / `update_card`: Persist review cards

### Key Constraints (ADRs)

- **ADR-0002:** Workflow shape with signals/updates/queries
- **ADR-0003:** Agent Kit behind a port (plain adapter is default)
- **ADR-0004:** Founder posts by hand (no X write operations)
- **ADR-0005:** Polling with spend limits
- **ADR-0006:** Evidence trail and computed confidence

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
