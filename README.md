# x-conversation-finder

Given a product and a few topics, this finds X posts where the founder could join the conversation, with an evidence trail for why each fits, a draft reply checked against the founder's own material, and a review app where the founder approves, edits, skips or steers each draft. The founder posts by hand from their own account; the product never posts. The backend is Python on Temporal (durable watches, signals, timers); the review app is TypeScript.

## Status

**First slice implemented** (2026-11-21). The system includes:

- ✅ Python backend with Temporal workflows (TopicWatch and Review)
- ✅ TypeScript review UI showing evidence and drafts
- ✅ Replay tests with fake adapters (no API keys needed)
- ✅ CI that runs tests
- ✅ Complete evidence trail and claim gate
- ✅ Computed confidence scoring
- ✅ Approve/Skip/Edit/Nudge actions
- ✅ Continue-As-New for long-lived watches

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 20+
- Temporal CLI

### Installation

```bash
# Install Temporal CLI
brew install temporal  # macOS
# or download from https://github.com/temporalio/cli/releases

# Install backend dependencies
cd backend
pip install -r requirements.txt

# Install frontend dependencies
cd ../frontend
npm install
```

### Running Locally

```bash
# Terminal 1: Start Temporal dev server
temporal server start-dev

# Terminal 2: Start worker
cd backend
python -m backend.worker

# Terminal 3: Start API
cd backend
python -m backend.api

# Terminal 4: Start UI
cd frontend
npm run dev
```

Then open `http://localhost:3000` to use the review UI.

## Architecture

### Workflows

- **TopicWatch**: Long-lived workflow (one per topic) that polls X search every few hours, tracks `since_id`, uses Continue-As-New
- **Review**: One per post with signals (`approve`, `skip`), updates (`edit`, `nudge`), query (`get_card`), and 48h expiry

### Evidence & Confidence

Per ADR-0006:
- Confidence is **computed from evidence shape**, never asserted by the model
- Every product claim must be supported by founder material (claim gate)
- "Not enough evidence" is a valid skip reason

### The Founder Posts by Hand (ADR-0004)

The system never posts to X. Approve means:
1. Copy the draft
2. Post it manually on X
3. Paste the posted URL back

## Testing

```bash
cd backend
pytest -v
```

All tests run with fake adapters (no API keys needed). Test results:

```
test_review_workflow.py::test_review_workflow_approve PASSED
test_review_workflow.py::test_review_workflow_skip PASSED
test_review_workflow.py::test_review_workflow_nudge PASSED
test_review_workflow.py::test_review_workflow_timeout PASSED
test_review_workflow.py::test_review_workflow_low_confidence PASSED
test_topic_watch_workflow.py::test_topic_watch_workflow_single_iteration PASSED
test_topic_watch_workflow.py::test_topic_watch_workflow_spawns_reviews PASSED
test_topic_watch_workflow.py::test_topic_watch_workflow_continue_as_new PASSED
test_models.py::test_evidence_compute_confidence PASSED
test_models.py::test_evidence_compute_confidence_low PASSED
```

## Documentation

- [Proposal](docs/proposal.md) - Original vision
- [ADRs](docs/adr/README.md) - Architecture decisions
- [Development Guide](docs/DEVELOPMENT.md) - Detailed setup and usage

## Security

This repo is **PUBLIC**. Never commit:
- API keys or `.env` files
- Real X data or handles from real posts
- Credentials of any kind

Use `.env.example` as a template and keep secrets in `.env` (gitignored).

## What's Next

This slice demonstrates the core workflow patterns. Future work:
- Real X client integration
- Agent Kit adapter (behind ReplyAgentPort)
- Follow-up timer after approval
- Baseline evaluation (ADR-0006)
- Persistent database (SQLite → Postgres)
