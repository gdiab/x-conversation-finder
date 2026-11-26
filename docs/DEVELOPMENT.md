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
- `test_written_switch_criteria.py`: The written-draft switch criteria (ADR-0006, 2026-11-24 amendment) on `fixtures/written_switch_criteria.json`: every must-REJECT case with the answer a correct checker must give. See "Draft modes" below for what these tests prove and what only a live model run can
- `test_written_check.py`, `test_written_triggers.py`, `test_brief_sentences.py`: The code side of the written-draft check: verdict rules, triggers (with the watch's key features) and brief sentences
- `test_written_workflow.py`: Written drafts through the Review workflow (redraft, skip, edit, nudge, removal) and replay of the histories recorded before written drafts
- `test_text_out_of_history.py`: issue #23. New TopicWatch and Review histories (both draft modes; edit, nudge, approve, confirm, follow-up, skip) hold no post text; after a removal the text is in neither the SQLite file nor its `-wal`; histories recorded on main 06db357 replay
- `test_removal_all_accounts.py`: issue #22. Two accounts' Reviews of the same post: a removal found by one stops the other at its next activity with no model call (researching or planning; text-free and with the post in the input), a Review started after the removal makes no model call, and the other's approve ends it as "post removed" without an X read
- `test_removal_author_edit.py`: issue #35. After a removal, no author name, handle, profile URL, post URL or edited draft text is left in the stored card, the stored post or the API's card, for this card and another account's card of the same post (both draft modes), nor in the SQLite file or its `-wal`
- `test_product_url.py`: issue #41. A passing draft that names the product ends with exactly one ` <url>` (both draft modes, also after a nudge and when the model wrote the URL itself); a draft that does not name the product, or a Review without a URL, has none; the check of an edit is the same with and without the link; the 256-character budget in the check, the writer prompt, the edit check and the redraft harness; text-free histories recorded on main d296ec3 replay
- `test_brief_suggestions.py`: issue #42. A suggestion whose quote is not on the page word for word is dropped (and short quotes, repeats, sentences the brief has, more than 8); flags name existing brief sentences, a contradiction only with a quote on the page; the API stores neither the page text nor the suggestions; a fetch failure, a non-https URL or a long brief is a clear error with no model call; the SSRF guard (private, loopback, link-local, mapped and embedded IPv4 addresses, redirects, port, user info) with a fake resolver and `httpx.MockTransport`; the page as a delimited data block; the call metered in the ledger, never above the shown estimate, refused by the kill switch
- `test_redraft_harness.py`: The offline redraft harness (issue #29) with fake adapters: no X calls, the database left unchanged, the workflow's redraft steps, the spend cap, a summary without text, and `--limit` counted after the skips (issue #34)

### E2E Scripts

Three scripts run the app end to end with fake adapters, so they call neither X nor OpenAI:

- `scripts/e2e-test.sh`: a TopicWatch finds a post, and its Review card is approved, confirmed and followed up
- `scripts/e2e-full.sh`: the same, but the draft is edited first. Approving the draft shown before the edit is refused (409 `draft_changed`), and then the edited draft is approved
- `scripts/e2e-review-direct.sh`: a Review is started directly, without a watch. The post is stored first and the Review gets only its id, as a watch starts it (issue #23)

Each script approves as the review app does. It reads the card (`GET /api/reviews/{id}`) and sends the card's `draft.text` and `edited_text_gate_state` (null if the draft was never edited). Each script then checks that the card reaches `followup_due`, with the confirmed reply's id and the fixture replies to it (`backend/tests/fixtures/e2e_x.json`) stored.

Run a script from the repo root, for example `bash scripts/e2e-full.sh`. It needs the Temporal CLI, `curl`, `lsof` and the backend dependencies. It uses `.venv/bin/python` if that exists, and otherwise `python3` (set `PYTHON` to choose another interpreter). Each script starts its own stack and stops it on exit:

- an in-memory Temporal dev server on port 7243, with its web UI on port 8243;
- a worker connected to that Temporal server;
- an API on port 8010 (`e2e-test.sh`), 8011 (`e2e-review-direct.sh`) or 8012 (`e2e-full.sh`);
- a SQLite file and logs in `data/e2e/`. The script deletes the SQLite file and creates it again at each run.

The scripts set `DATABASE_URL`, `TEMPORAL_HOST`, the adapters, the credentials and the confidence threshold themselves, so values in the environment do not apply to these settings. The worker, the API and the script's own Python steps run in a new empty temporary directory, so no `.env` is read. A script never uses a running app or its database. If one of its ports is already in use, the script stops before it starts anything. After each server is ready, the script also checks with `lsof` that the process that listens on the port is the one that it started. To choose other ports, set `E2E_TEMPORAL_PORT`, `E2E_TEMPORAL_UI_PORT` or `E2E_API_PORT`. The scripts run one at a time, because they share the Temporal ports. The last recorded runs are in `docs/pr/issue-27-*.txt`.

## Using the System

1. **Start a Topic Watch:**
   - Open the UI at `http://localhost:3000`
   - Fill in the "Start New Topic Watch" form. To find facts your brief is missing, click "Suggest brief additions" after the Product URL (see "Brief suggestions from the product page"). "Product name" and "Product URL" (an `https://` link) are required; "Also called" takes other names people use for the product, and "Key features" the short names of features, both optional and separated by commas (see "Draft modes" and "Product URL")
   - The watch will poll every 3 hours (configurable)
   - A running watch keeps the brief, product name, product URL, other names and key features it was started with. To change them, stop the watch (`temporal workflow signal --workflow-id "watch:<account>:<topic>" --name stop`) and start it again from the form

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
- `draft_reply`: Generate the draft, steered by the plan: assembled from segments, or written as free text (see "Draft modes")
- `claim_gate` / `verify_edited_text`: The claim gate of an assembled draft, and the re-gate of a founder edit of one
- `check_written_draft`: The product-claim check of a written draft, a nudged redraft or a founder edit of one
- `store_card` / `update_card`: Persist review cards

`backend/worker.py` (`build_activities`) registers all of them; a test compares the list with the activities the workflows call. Before issue #12 `verify_edited_text` was missing from the worker, so on a real worker every founder edit ended as "not_checked".

### Draft modes (ADR-0006 amendment 2026-11-24, issue #12)

`DRAFT_MODE` in `.env` selects how replies are drafted. Restart the worker after changing it.

| `DRAFT_MODE` | Draft | Check |
|---|---|---|
| `assembled` (default) | Fixed phrases plus at most one brief sentence, word for word (2026-11-21 amendment) | `claim_gate`: every sentence is a fixed phrase or a verbatim brief sentence |
| `written` | The model writes the reply as free text: at most 3 sentences, 280 characters and 1 product claim | `check_written_draft`: product claims are checked against the brief |

`assembled` stays the default until the amendment's switch criteria pass. Both paths stay in the code so the baseline eval can compare them.

**Which mode a Review uses.** The `plan_reply` activity fixes the mode when the Review plans its reply, and the Review keeps it through redrafts, nudges and edits. The mode is `written` only if `DRAFT_MODE=written` and the watch has a product name. A watch started before the "Product name" field existed has none: its Reviews stay assembled, and each card says so. To get written drafts from such a watch, stop it and start it again with a product name (see "Using the System"). Reviews already running keep the mode they were planned under.

**The written check** (steps 2 to 4 of the amendment; `backend/gate/written_check.py`):

1. Code splits the reply into sentences and runs the triggers. A sentence is checked as a product claim if it names the product (the "Product name" or an "Also called" name, never read from the brief), uses we/our/us/ours, contains a number, makes a comparison, or uses one of the watch's key features (the "Key features" field, never read from the brief).
2. If code can already reject the reply, it is not sent to the checker, and no call is paid for: no sentence, more than 3 sentences, more than 280 characters, more than one triggered sentence, a triggered question, a number that no brief sentence states, a sentence that may be two ("Acme ships.It retries."; a line break ends a sentence), a triggered sentence when the plan said not to mention the product, or a product claim that an empty brief could not support.
3. Otherwise one checker call sees the reply's sentences, the brief sentences, the product name and its other names, and which sentences code requires a verdict for (the triggered ones). It does not see the watch's key features themselves, the post, the plan, the drafting prompt or the brief sentences the drafter pointed at (the drafter's list is kept on the draft and shown on the card as the drafter's own note). It says which sentences are about the product, and for each of those and each triggered sentence returns `supported` with brief sentence ids, or `unsupported` with a reason.
4. Code decides. A checked sentence passes only with `supported`, at least one cited id, every cited id existing in the brief, and no number that the cited sentences lack. A checked sentence that is a question fails. A sentence that no trigger fired on passes in two cases: the checker cleanly says it is not about the product (no verdict, no citation), or the checker reads it as a product claim and it meets the conditions above. A missing, malformed, repeated or self-contradicting answer counts as not supported and is never repaired.

"Any number" is any digit and a closed list of number words read broadly (cardinals, ordinals, fractions and multipliers: "one", "first", "half", "twice", "single"), also where a reply uses them as plain words ("which one", "First, ..."): code cannot tell the uses apart, so the drafter is told to avoid them outside the claim. "At most 1 product claim" is counted per sentence. A sentence that may be two ("works.it retries", "Hmm... that depends") fails; names such as "Node.js" and "docs.example.com" are recognized by their ending (`SAFE_DOT_ENDINGS`).

A failed draft is redrafted with the reasons, then the post is skipped as "not enough evidence", as in assembled mode: one redraft by default (`CLAIM_GATE_MAX_ATTEMPTS=2` draft attempts, the value ADR-0006 specifies; the setting applies to both modes). A founder edit of a written draft goes through the same check; a failing edit needs "Post anyway". A Review keeps one check result per text, for model drafts and for edits: an identical redraft, a nudge that returns the same text, or the same edit saved again shows the same result and makes no second checker call (change the text to get a new check), so the draft and gate state approve names still identify one result. The `needs_written_drafts` skip rule (issue #5) is off in written mode.

**Writing the brief for written drafts.** The checker cites brief sentences by id (`b1`, `b2`, ... in document order). A list item is one sentence, together with its continuation lines. A heading is context: each sentence is shown to the drafter and the checker with the headings, lead-in lines ("Not offered yet:"; for a list, also the sentence right before it without a colon) and parent list items it stands under, so "Slack" under "Not supported yet > Integrations" is read as not supported. A one- or two-word sentence stays with the sentence before it ("We do not offer these yet. Slack. Teams." is one brief sentence). Context between full sentences of a paragraph is not carried, so write each sentence to stand on its own. Short factual sentences check most reliably. A number in a reply must appear in the brief.

**Key features.** To make a feature's name a trigger, enter it in the "Key features" field of the Start Watch form, for example `Signed deliveries, Delivery replay`. A reply sentence that uses one of these terms is checked as a product claim, so it needs a brief sentence that supports it: list only features the brief describes. A term is matched as whole words in the order entered, without regard to case or to punctuation between the words ("dead letter queue" for "Dead-letter queue"), and a regular plural matches its singular in both directions ("a signed delivery", "UIs" for "UI"). Any other spelling is a different term: enter it as its own key feature ("Realtime monitoring" next to "Real-time monitoring"). An entry is the short name of a feature: the API refuses an entry of more than 6 words or 80 characters, an entry with no letter or digit, and more than 20 entries. The brief is never read for key features, not even a list it names "Key features". A watch started without key features has none, and the card shows "Key features from your watch settings: none"; the other triggers and the checker still apply. To give a running watch key features, stop it and start it again (see "Using the System").

**Budget.** The checker call is one more inference call per draft, per nudge and per edit, metered like the others (`CHECK_MAX_OUTPUT_TOKENS`; see "Budget accounting").

**What the tests prove, and what they cannot.** `test_written_switch_criteria.py` runs every must-REJECT case with a fake checker that returns the answer a correct checker must give. That proves the code side: which sentences the triggers force into the check, that a forced sentence is rejected unless the checker cites brief sentences that exist, and which cases code rejects with no checker call. It does not prove that the real model returns the correct answer. Of the 24 must-REJECT cases, code alone rejects 6 before the checker is asked (questions that make a product claim, and numbers no brief sentence states); the other 18 are rejected only if the checker judges entailment correctly, and a wrong `supported` with a real citation would pass them. Measuring that needs a live run on the real model: the blind comparison of `assembled` against `written` on stored evidence (switch criterion 3), which is not built or run yet. The `FakeClaimChecker` used with fake adapters accepts only a brief sentence word for word that does not stand under a negating heading; it is a stand-in, not a paraphrase checker.

Switch criterion 1 covers the 2026-11-21 cases that make a product claim. The five that make none (for example 'I like the idea.') are allowed by design: they are outside the claim gate, and founder review catches their tone (George's decision of 2026-11-25, at the end of the ADR amendment). The test file pins both groups.

**The writer prompt (issue #29).** The first live run (2026-11-25) failed in two ways: a second sentence described the product through "it", so two sentences counted as product claims, and the product sentence paraphrased the brief into qualities it does not state. The written drafter's prompt (`build_written_draft_prompt` in `backend/adapters/plain_reply_agent.py`) now says that everything about the product goes in one sentence, written close to the wording of the cited brief sentences, and that every other sentence is about the poster and does not say in general what a tool or approach does (the checker reads such a sentence as about the product). A redraft after a failed check also lists each failed sentence in full with its reason and restates these rules. The workflow's steer, recorded in history, is unchanged; the prompt reads the failed check from the previous draft.

### Product URL (ADR-0006 amendment 2026-11-26, issue #41)

The Start Watch form's required "Product URL" is stored on the watch (`TopicWatchInput.product_url`) and passed to its Reviews. Code appends it to a draft; the model never writes it:

- **Which drafts.** After a draft passes its check, `check_written_draft` (written) or `claim_gate` (assembled) appends one space and the URL to the stored draft when the draft names the product: the reply plan says to mention the product, or a sentence is checked as a product claim (a written claim sentence, an assembled claim segment). Other drafts get no link. A trailing copy of the URL the model wrote anyway is taken off first, so the draft ends with exactly one; a draft whose sentences already hold the URL (a brief sentence that quotes it) gets no second copy. A quoted URL counts as typed in every length check (gate, edit check, edit box): only the appended trailing link counts as 23, which is stricter than X and never looser.
- **The check is unchanged.** The check runs on the sentences without the URL (`backend/gate/product_link.py`), so the URL never fires a trigger.
- **Length.** X counts any link as 23 characters, so with the link the sentences get 280 - 24 = 256 characters. The writer prompts of both modes ask for 256 when the plan says to mention the product. A draft that names the product and is longer fails its check ("at most 256 characters, because the product link is added after it") and is redrafted.
- **Edits and nudges.** The edit box starts from the draft with the link, and its counter counts as the check does: the watch's product URL as its last word as 23 characters plus the whitespace before it, everything else (other links too) as typed. The card names the URL in its evidence (`evidence.product_url`, recorded by `draft_reply` with each draft), on the queue and on a single card. The check of an edit takes off a trailing product URL (the text's last word, exactly the watch's URL) and checks the rest under 280 - 23 characters less the whitespace before the link (256 after one space); an edit without the link is checked as before (280). A nudge or redraft shows the drafter the current draft without the link; code adds it again after the check.
- **Card.** The card shows the draft with the link, and Copy Draft copies it. Approve names that text, as before.
- **Existing watches and Reviews.** A watch started before the field has no URL, and neither do its Reviews: their drafts get no link. Reviews already running keep their input. To give a watch the link, stop it and start it again with the same settings plus the URL (see "Using the System"). A Review whose input carries the post (recorded before issue #23) never has a URL.

### Brief suggestions from the product page (ADR-0006 amendment 2026-11-26, issue #42)

On the Start Watch form, **Suggest brief additions** (below the brief) reads the page at the Product URL and asks the model once for facts the brief does not state yet. The button is on once the Product URL is valid and the cost has loaded; next to it the form shows the call's worst case (`GET /api/brief/suggestions/cost`: the prompt with the page and the brief at their caps plus `SUGGEST_MAX_OUTPUT_TOKENS`, priced with the inference token prices; about $0.013 with `gpt-4o-mini`, rounded up; the README prompt and the web-page prompt are both covered).

- **Reading the page** (`backend/adapters/http_page_fetcher.py`). A `https://github.com/<owner>/<repo>` URL is read as the repository's README through `GET https://api.github.com/repos/{owner}/{repo}/readme` (unauthenticated: 60 requests an hour per IP; a used-up limit, a missing README or a private repository is a clear error; a query or fragment such as `?tab=readme-ov-file` does not matter). Any other https URL is read as a web page, and its text is taken from the HTML without scripts, styles and the head; a page built by JavaScript has no readable text and is refused. Only `https` on port 443, without a user name or password in the URL. The text is cut at `SUGGEST_MAX_PAGE_BYTES` (UTF-8, default 60000; the form says when it was cut).
- **SSRF guard.** Each request, redirects included (at most 3), resolves the host and is refused when any address is not public (private, loopback, link-local, carrier-grade NAT, multicast, reserved, unspecified; an IPv4-mapped, 6to4 or NAT64 IPv6 address is checked as its IPv4 address). The request then connects to the checked address with the host name as TLS SNI and `Host`, so the certificate is checked for the host and a second DNS answer cannot move the connection. Proxy settings from the environment are ignored. Timeouts: 5 s to connect, 10 s per read, 25 s in all; at most 2 MB is kept, counted after decompression: a gzip or deflate body is decompressed in bounded steps, so a small compressed body cannot expand past the cap in memory (other encodings are refused).
- **The model call** (`backend/adapters/plain_brief_suggester.py`, behind `BriefSuggesterPort`; `REPLY_AGENT_ADAPTER=fake` selects `FakeBriefSuggester`). One JSON-mode call with the app's OpenAI settings. The page text is untrusted: it stands between delimiters with a random marker per call (any copy of them in the page is deleted first), and the prompt says before and after it that it is data, never instructions. The model returns at most 8 suggestions (`sentence`, `quote`, `why`) and flags for brief sentences (by id, as the checker cites them) that the page contradicts or does not mention.
- **What code keeps** (`backend/brief_suggestions.py`). A suggestion only if its quote is on the page word for word (`quote_in_text` in `backend/gate/reply_fit.py`: only whitespace, NFKC forms, HTML entities, curly quotes and dashes, and Markdown `*` and backtick marks are ignored), with at least 3 words; its sentence one sentence (the claim gate's splitter) of at most 300 characters, not a repeat and not already in the brief. A flag only for an existing brief sentence, and a "contradicted" flag only with a quote on the page. The form says how many suggestions were dropped for their quote.
- **Nothing is added without a tick.** Each suggestion is a checklist item: a checkbox (off), the sentence (editable), its quote and why. **Add checked to brief** appends the ticked sentences to the brief textarea as list items under a level-1 `# From the product page` heading (it ends any earlier heading, nested heading or lead-in such as "Not supported yet:", so an added fact never stands under it). A later add continues that list only when the brief still ends with the heading and its plain list items; otherwise it starts a new heading. Unticked items are dropped: after the add the checklist is cleared, and a new click starts a new one. A new click also clears the earlier list before it asks. The brief changes only in the browser; the watch gets it when the founder starts the watch.
- **Nothing is stored.** The call runs in the API process, not in a workflow, so the page text is not in Temporal history. Neither the page text nor the suggestions are written to the database or logged; an error shows the provider error's type only (it can echo the prompt).
- **Budget.** The call is reserved and settled in the daily inference ledger like the workflows' calls (`backend.budget.meter_api_inference`; reservation key `api:brief-suggestions:<random run id>`), under the same cap and kill switch. A refused call is HTTP 429 `budget_exceeded`.
- **Errors.** `{"code", "detail"}` as the review endpoints: `url_invalid`, `url_refused` (a private or local address), `brief_too_long` (400); `page_not_found`, `unsupported_content` (422); `fetch_failed`, `model_answer_unusable`, `model_failed` (502); `rate_limited` (503); `budget_exceeded` (429). The form shows the message and stays usable.

### Redraft harness (issue #29)

`scripts/redraft_harness.py` measures writer-prompt changes on stored evidence. It runs the written path of a Review (draft, check, then redrafts with the workflow's steer, each checked: one redraft by default, as many as the watch's saved `claim_gate_max_attempts` allows) against the real model on the cards in the app's database. It never searches X. Before it drafts a post it looks the post up on X once per run (see **Re-fetch** below); apart from the X reads that lookup counts in the app's budget ledger and the removals it records, it reads the database read-only and changes no card. Its model calls do not write the app's budget ledger. Its own `--max-cost` cap (default $0.25 per run) covers the model calls and the X lookups, and refuses a call whose worst-case cost would pass it. The cap prices tokens with `INFERENCE_INPUT_TOKEN_COST_USD` and `INFERENCE_OUTPUT_TOKEN_COST_USD`, whose defaults are `gpt-4o-mini` prices: set them to your model's prices before you use another model, or the cap and the reported cost are wrong. X lookups are priced with `X_POST_READ_COST` (default $0.005).

Cards do not store the brief, product name, other names, key features or product URL, so export them first from each watch's start input in Temporal (read-only), keyed by account and topic, with the redraft limit the watch's Reviews keep (`claim_gate_max_attempts`), together with the watch topic of every Review from its start input (a card's `matched_topic` is written by the research model, so it is not used to pick settings). The export reads the watch's current run: if a watch was restarted with other settings, its older cards are drafted and checked with the new ones. Run both commands from the checkout that holds your `.env` (the OpenAI key and model come from the app's settings), with the script of the code you want to measure:

```bash
python <repo>/scripts/redraft_harness.py export-settings \
  --workflow-id "watch:<account>:<topic>" --out <repo>/eval-out/watch-settings.json
python <repo>/scripts/redraft_harness.py run --db x_conversation_finder.db \
  --settings <repo>/eval-out/watch-settings.json --out-root <repo>/eval-out/redraft \
  --label before --samples 3
```

- **Which cards.** Cards that reached drafting (a draft or a reply plan). Left out, and counted under `excluded` in the summary: cards skipped at research or by the confidence threshold (`not_drafted`), cards whose stored plan was invalid twice (`plan_skipped`; a card skipped only by the assembled-mode `needs_written_drafts` rule stays, since a written Review drafts it), cards whose post text was removed (`post_text_removed`, also checked again right before each model call and once more before the report is written; the harness then empties that post's drafts, sentences and reasons in the report and keeps only its counts and cost. Each run also deletes the cached plan of every post whose removal is recorded and applies recorded removals to the reports of earlier runs under the same `--out-root`, recounting their summaries without the removed posts; the run ends with an error if a report could not be checked), cards whose Review is not in the export (`no_review_topic`), cards with no exported settings for their account and topic (`no_watch_settings`), cards of a watch with no product name, which a live Review keeps assembled (`no_product_name`), and cards that do not decode (`unreadable`). Compare the `excluded` counts before comparing two runs. A card stored before the plan step has no plan: the harness plans it once and caches the plan in `<out-root>/plans.json` (an invalid plan too), so runs with different labels draft the same posts under the same plans.
- **Output.** `<out-root>/<label>/report.json` has, per post and sample, every draft, sentence, trigger, verdict and reason, the outcome, and the run's model calls and cost; `summary.md` (a fixed heading; the label is only the directory name) has the aggregate numbers only (pass rates, failure kinds, product-claim sentences per draft, calls, tokens, cost; the model names the provider reported are in `report.json` only) and is what goes into a pull request. The report holds real post and brief text: the script refuses an output path inside a git work tree that git does not ignore (`eval-out/` is ignored).
- **Re-fetch (issue #31).** Re-fetch is on by default. Before a post's first model call, the harness looks the post up once per run (not once per sample, and once for all cards of the same post) through the eval re-fetch "Post removal" describes (`refetch_stored_card_target`, with the app's X client and Temporal). It needs `X_CLIENT_ADAPTER=real` with `X_BEARER_TOKEN`, Temporal running, and `--db` must be the app's database (`DATABASE_URL`; a relative one is resolved against the current directory, the checkout that holds your `.env`, as the running app resolves it against its own checkout): a removal recorded in a copy would leave the text in the app's. The script then points the app code it reuses (budget ledger, removal) at that file, also when the script's code comes from another checkout. Outcomes, counted under `excluded` and `refetch_outcomes` in the summary:
  - The post exists: it is drafted.
  - X reports it gone (the outcomes the approve re-fetch treats as removal): it is skipped (`removed_at_refetch`), and the removal is recorded through `delete_post_text`, so its stored text, its working documents and every copy on its cards are redacted as the app does it. The harness also deletes its cached plan. If recording the removal fails (the database is locked) or its WAL checkpoint stays pending (a reader holds an old snapshot), the harness retries `delete_post_text` a few times without another X lookup. What is still unfinished at the end is counted: `refetch_removal_failed` (the removal was not recorded and the text is still stored; the post is skipped, under `removal_failed`) and `refetch_wal_checkpoint_pending` (the removal committed, but old pages can stay in the `-wal` file). The read of a gone post is released in the app's budget ledger even when recording its removal failed (X billed nothing). Each re-fetch run also tries a WAL checkpoint at start (a removal an earlier run left with its checkpoint pending is otherwise never revisited: its card is left out) and once more at the end when one is pending; a checkpoint still pending at the end counts in `refetch_wal_checkpoint_pending`. With anything unfinished the script exits with an error after writing the report. Recovery: for `removal_failed`, run it again (the next run looks the post up again and records the removal); for a pending checkpoint, run it again (it retries the checkpoint at start). A checkpoint completes once no reader holds an old snapshot; restart the API process if a rerun does not clear it.
  - The lookup failed (network, rate limit, the app's X budget or kill switch): the post is skipped without a removal (`lookup_failed`; `refetch_failure_types` names the error types).
  - The card's Review still runs: it is skipped without a lookup (`review_running`); it re-fetches when the founder approves.

  Each lookup reserves one X read in the app's budget ledger (about $0.005; a post found gone is not billed, issue #17) and is counted on the harness's cap; the summary's `calls.x_lookup` gives the count and cost. `--no-refetch` turns the lookups off and makes no X call; use it only on a database of synthetic posts (the summary then says `"refetch": "off"`).
- **Limit (issue #34).** `--limit N` stops the run once N cards are drafted, not after the first N cards: the cards are taken oldest first, and a card passed over does not count toward N: one left out at load (see **Which cards**: its removal is recorded, its stored plan was invalid twice, ...) or one the run skips (its Review still runs, its removal is found at re-fetch or recorded during its run, its cached or new plan is invalid, its lookup failed, or it has no watch settings). A card counts as drafted when one of its runs ends with a result (passed or skipped), or with an error after it made a model call or finished a draft attempt (an error before any call, such as the cap refusing the first one, does not count); a card whose plan is invalid (in any of its samples), or whose post's removal is recorded by the time its runs end (also after an earlier sample finished), does not count. The limit is counted also when the spend cap stops the run. Once N cards are drafted, the harness looks up no further post on X. The lookups made are at most one per card examined before the limit was reached (none for a card whose Review still runs or that has no watch settings; cards of the same post share one lookup). A post is looked up before its plan is checked, so a card skipped for an invalid plan also costs a lookup. With fewer eligible cards in the database, it drafts what there is. The summary reports `limit`, `limit_reached` (false when the database ran out first), `skipped_before_limit` (stored cards passed over, in order, before the run stopped, including those left out at load; their reasons are under `excluded`, which also counts the cards left out at load after the stop, except a card whose plan was invalid or whose run failed before any model call: those show as `plan_invalid` or `error` under `outcomes`) and `not_reached_by_limit` (stored cards after the card where the run stopped, never examined or looked up). A run without `--limit` has none of these keys.
- **Product URL (issue #41).** The export includes the watch's product URL (null for a watch started without one; settings exported before the field load as null). With a URL, the harness asks the writer for 256 characters when the plan says to mention the product, checks a draft that names the product under that budget, and checks the sentences without a trailing product URL, as a live Review does. The report keeps the checked text without the link.
- **Samples.** The drafter runs at temperature 0.7, so one run per post is noisy; `--samples 3` runs the written path three times per post. With `gpt-4o-mini` a sample of 21 posts costs about $0.02 in model calls; the re-fetch adds about $0.005 per post found (about $0.105 for 21), so a run of 21 posts needs a cap of about $0.13.

The harness is the first piece of switch criterion 3 (the blind comparison of `assembled` against `written` on stored evidence); the comparison and its scoring are not built yet.

### Post removal (ADR-0006, issue #9)

Post text is deleted when a re-fetch confirms that the post is gone (see below for what counts). Pending cards are not re-checked on a schedule (see the end of this section), so a deleted post's text stays until a re-fetch confirms its removal. The target post is re-fetched with `GET /2/tweets/:id`:

1. **When the founder approves.** The `approve` update calls `lookup_post` before approving. A post whose removal is already recorded (by another account's Review of it, or the eval re-fetch) counts as gone without an X read or a budget reservation (issue #22). If the post is gone, `delete_post_text` removes the stored text and every model-written copy of it (`redact_post_text_copies`: why it fits, the problem span, the plan's target point, ...), removes the post's author and URL and a founder's edit of the draft (issue #35), marks the card `post.text_removed`, and the Review ends as skipped with "Post removed". The update fails with type `PostRemoved` (HTTP 409 from the API), and the UI reloads the card. During the re-fetch, edits, nudges and other approvals are rejected.
2. **Before the eval replays a card** (the redraft harness does this for each post, issue #31). Call `backend.target_refetch.refetch_stored_card_target(x_client, workflow_id, run_key, review_is_running=temporal_review_is_running(temporal_client))` and skip the card unless it returns `"found"`. A card already marked removed returns `"removed"` first (after finishing any pending WAL checkpoint), whatever its workflow is doing. Otherwise it leaves a card whose Review workflow is still running alone and returns `"live"`: that workflow re-fetches at approve (a Review started before issue #23 holds its own copy of the evidence; a later one reads its evidence from SQLite). Liveness comes from Temporal, not the stored status, because Reviews skipped before issue #9 ended with their card still "pending".

If a skip or the review timeout arrives while approve's re-fetch is running, the workflow waits for the re-fetch (and any removal) to finish before it ends. A lookup counts as removed only when X's error body (on a 200 without the post, or on an HTTP 404) names this post as not found or not authorized; any other response fails the lookup and removes nothing. These response shapes come from the X API v2 docs and have not been seen live yet.

A removal applies to every stored card of the post (the same post can have a card per account). A Review started before issue #23 still holds the text in its own state when the removal happened elsewhere, so `GET /api/reviews/{id}` applies the stored removal to the `get_card` query result, `draft_reply` refuses to redraft (nudge) a card that records a removal, and `store_card` stores any later card of a removed post already redacted.

### Post text out of Temporal history (ADR-0006 amendment 2026-11-25, issue #23)

Workflows carry ids, not text. A TopicWatch search stores the posts it finds in the `stored_posts` table and returns their ids, and each Review starts with `ReviewInput(post=None, post_id=...)`. The Review's activities read the post from `stored_posts` and the Review's working documents from the `review_docs` table (by workflow id and slot: `evidence`, `draft:attemptN`, `nudge:<request id>`, check results, the founder's edit and nudge text) and return summaries (`backend/models/summaries.py`): counts, flags, a draft's SHA-256 digest, and code-written reasons. `backend/review_docs.py` holds the storage. So a history of such a Review has no post text, author handle, plan target point, draft, edit or nudge in any input, result, update or query.

- **Approve** names the draft the founder saw by digest: the API hashes `draft_text`.
- **Edit and nudge**: the API first stores the text with `review_docs.store_founder_text` (one slot per request id and text digest, never overwritten), then sends the update `["", client_request_id, {"slot", "sha256"}]`. The update returns the digest and the API answers with the stored draft. The API asks `get_card` first: a Review started before issue #23 (`text_free` absent) is sent the text, as before.
- **`GET /api/reviews/{id}`**: the query of such a Review returns its state (`"text_free": true`, status, approval fields); the API merges it with the stored card (`store_activities.text_free_card`).
- **Reasons that quote the draft** (a failed-check skip reason, a nudge rejection's reasons, `post_anyway_required`'s failing sentences) are on the card or in `review_docs`; the API adds them to its responses. A rejection names the stored result it is about (a slot), so a later edit cannot change what the founder is shown.
- **Failures**: an activity failure's message is recorded in history, so a text-free activity replaces any error but the code base's own `ApplicationError`s (a database error, whose message holds the statement's parameters, or a model provider's error, which can quote the request) with an `ApplicationError` that keeps the type name and drops the message (`review_docs.text_safe_errors`); the original goes to the worker log. An edit changes the Review's state only after its card write committed; a failed edit can be retried with the same request id, and a retry of an edit still running is refused as `busy`. Each working document records the activity (workflow run, activity id and attempt) that wrote it: within one run, a late attempt (a timed-out attempt whose thread finished after its retry) cannot overwrite what a later step wrote, and its card write is dropped. A Review started again under the same workflow id (a restarted watch finds the post again) is a new run: its research step, before its model call, takes ownership of the workflow id's documents (`review_docs.claim_run`), deletes the earlier run's, and retires that run, so a late activity of the earlier run writes nothing: no document, no card write.
- **Removal**: `delete_post_text` also replaces the stored post's text, records `text_removed` on it, and deletes every `review_docs` row of the post. Later required reads and writes of the post or a working document raise `PostRemoved` (an optional lookup, such as a cached check result, finds nothing; the follow-up's writer marker holds no text and is still written): a Review of the removed post that is still researching, planning or drafting stops at its next activity with the card "Post removed", before that activity's model call, in both modes. Two limits: an activity that already read its inputs before the removal was recorded may still make its model call (its result is then not stored); and a Review already waiting for the founder is not stopped by a removal recorded elsewhere: its card is redacted, its edits and nudges are refused, and it ends at the founder's decision or the review timeout (its approve finds the recorded removal without asking X, issue #22). SQLite runs with `secure_delete=ON`, and each removal ends with `PRAGMA wal_checkpoint(TRUNCATE)`, so freed pages are zeroed and the old pages do not stay in the `-wal` file. A reader holding an old snapshot (the API process) can keep the checkpoint from completing: `delete_post_text` (already committed, and idempotent) then fails with the retryable `WalCheckpointPending`, and Temporal retries it until the checkpoint completes. The eval's `refetch_stored_card_target` raises the same error to its caller, and a call for a card already marked removed runs the checkpoint again before it returns `"removed"`.
- **Replay**: a Review whose input carries the post, and a search result that carries posts, take the previous code path. Histories recorded before issue #23 keep their text until Temporal's retention deletes the closed workflow.

Card writes (`store_card`, `update_card`, `delete_post_text`) take SQLite's write lock, so a removal cannot interleave with a write, and any write to a card of a removed post is stored redacted.

A written draft (issue #12) is text a model wrote after reading the post, so a removal also redacts the draft text, the sentence and reason of each sentence check, the drafter's hints, the founder's nudges on it (they are written while looking at that text), and the edited-text results and "Post anyway" sentences of the card (`redact_removed_card` lists every field). The supporting brief sentences stay: they are the founder's own text. An assembled draft is code-inserted text and is kept, as before. In either mode (issue #35) a removal also deletes the post's author and URL (the removed post keeps its id, created time, counts, conversation id and reply setting) and a founder's edit of the draft with its check results and "Post anyway" sentences. `check_written_draft`, like `draft_reply`, refuses to send a text to the model once the card records a removal.

Reviews of the same post under *other* accounts (issue #22). A removal is recorded by post id: on the stored post (`stored_posts`, `text_removed`) and on every stored card of the post. Their stored cards are redacted, their nudges are refused, and their card writes are stored redacted. A Review that is still researching, planning or drafting stops at its next activity, before that activity's model call, and ends as skipped "Post removed", in both draft modes:

- A Review started since issue #23 reads its post and documents from the database, which refuses them once the removal is recorded.
- A Review started before issue #23 carries the post in its input. `research_post`, `plan_reply`, `draft_reply` and `check_written_draft` ask `post_removal_recorded(post_id)` (stored post or any card of the post) before the model call and refuse with `PostRemoved`. The workflow's handling of that refusal at research and planning is new, guarded by the patch `removal-skips-research-and-plan-v1`; at drafting it already ended the Review as "Post removed".
- At approve, `lookup_post` answers a recorded removal as gone without asking X, so a known removal costs no X read; the Review then ends as "Post removed" as after a 404.

The limit above still applies: an activity already inside its model call when the removal is recorded finishes that call. Today one account runs watches (ADR-0005), so this path needs a second account reviewing the same post.

Each re-fetch reserves one X read through `backend/budget.py` (about $0.005, ADR-0005). Over budget it raises and removes nothing. A re-fetch that finds the post gone is reconciled to 0 reads: X returned no post object (issue #17). A 404 on the founder's *reply* URL does not touch the target post's text.

Pending cards are **not** re-checked on a schedule, so X cost stays proportional to approvals and eval runs. A deleted post's text can therefore stay on a card that is never approved (pending, skipped or expired) until the eval re-fetches it.

### Review API errors (issues #15, #20)

The review endpoints (`approve`, `edit`, `nudge`, `confirm_posted`, `skip`, `GET /api/reviews/{id}`) answer an error with JSON `{"code": ..., "detail": <message>}`. The UI switches on `code`, never on the message. The Review workflow's validators raise `ApplicationError` with a type; `backend/api.py` (`REJECTION_CODES`) maps it:

| code | HTTP | meaning |
|---|---|---|
| `post_anyway_required` | 409 | the edited text failed the re-gate; also `gate_state` and `failing_sentences`. Approve again with `post_anyway=true` |
| `not_ready` | 409 | the pending card is not stored yet; try again in a moment. The card row is written inside `store_card` slightly before the workflow marks the card ready, so the UI can briefly show a card this rejects |
| `busy` | 409 | another edit, nudge or approval is running; try again after it |
| `draft_changed` | 409 | approve's `draft_text` and `draft_gate_state` (the draft the founder saw) are not the current draft and gate result; the UI reloads the card. Checked before `post_anyway`, so an override always acknowledges the shown draft's failures |
| `post_removed` | 409 | the target post is gone: approve's re-fetch found it gone and the review is skipped, or a nudge was refused because another Review of the post recorded its removal (`draft_reply` refuses, wrapped in `ActivityError`) |
| `invalid_state` / `invalid_request` | 409 / 400 | not allowed in this status / bad input (422 with FastAPI's `errors` for a malformed body) |
| `rejected` | 422 | an update handler refused it (a nudged draft failed the gate, a URL that is not a reply to the post) |
| `not_found` | 404 | no such workflow, or it has ended (updates and signals) |
| `server_error` | 500 | not the Review's decision: an activity failed, Temporal is unreachable or not connected |

Approve requires `draft_text` and `draft_gate_state` (null for a draft never edited): the draft the caller shows. The UI sends both with every approve. Known residual: a Review in which an edit was accepted before its card was stored, on code older than issue #20 (only a direct API call could do that), keeps the unedited draft on the stored card, so approving it from the UI returns `draft_changed` each time. Edit the draft once to bring the stored card and the workflow back in line.

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
  `DRAFT_MAX_OUTPUT_TOKENS`, and `CHECK_MAX_OUTPUT_TOKENS` for the written-draft
  claim checker, issue #12; sent as `max_completion_tokens`). The activity opens
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
- X reads are counted per post object X returns in `data` (ADR-0005: Posts
  Read is billed per resource; issue #17). Search returns a `SearchResult`
  whose `billable_reads` includes posts the adapter drops because their author
  is missing from `includes.users`. `newest_id` still covers converted posts
  only: a dropped post comes back (and is counted again) on later polls only
  until a newer post converts and `since_id` passes it.
- A reservation that is already closed refuses to authorize another request
  (`ReservationClosed`). `lookup_post` counts 1 read for an attempt that returns
  the post, or post data the adapter cannot convert (`MalformedLookupResponse`,
  raised; X billed it). It counts 0 when it returns None: the HTTP adapter
  returns None only when X's error (200 without data, or HTTP 404) names the
  post as gone (issue #9), and that response holds no post object. A 200 with
  neither the post nor such an error raises `LookupReturnedNoPost` and is
  released; a 404 that does not name the post is raised as the HTTP error and
  released. The eval re-fetch (`refetch_stored_card_target`) counts the same way.
- Output that hits the max output tokens (`finish_reason == "length"`) fails
  research, plan, draft and check without retry (`InferenceOutputTruncated`);
  the call stays counted.
- The claim checker (`PlainClaimChecker`, issue #12) reserves and settles its
  call through the same helper as the reply agent
  (`backend/adapters/metered_completion.py`), inside the meter the
  `check_written_draft` activity opens. A text that code rejects before the
  checker call costs nothing.
- The OpenAI client runs with `max_retries=0`: Temporal's retry policy owns
  retries, so each retried request is reserved and counted.
- `FakeReplyAgent` makes no paid calls and reserves nothing.

## Fake Adapters

For testing and CI, the system uses fake adapters:

- **FakeXClient:** Returns synthetic posts, no API calls
- **FakeReplyAgent:** Deterministic research and drafting (assembled and written)
- **FakeClaimChecker:** Deterministic written-draft checker: supports only a brief sentence word for word, and nothing under a heading that may negate it ("Not supported yet", "Roadmap")

These allow the full system to run without API keys.

## Production Considerations

To use real X and LLM APIs:

1. Set API keys in `.env`
2. Replace `FakeXClient` with a real X client in activities
3. Replace `FakeReplyAgent` with `PlainReplyAgent` (or Agent Kit adapter); `REPLY_AGENT_ADAPTER=plain` also selects `PlainClaimChecker`
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
