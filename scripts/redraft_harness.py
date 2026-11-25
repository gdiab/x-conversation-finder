#!/usr/bin/env python3
"""Offline redraft harness for written drafts (issue #29).

Runs the written path (draft, check, one redraft) against the real model on
Reviews already stored in the local database, and writes a report. It never
searches X, never writes the database and never changes a stored card. See
backend/eval/written_harness.py and docs/DEVELOPMENT.md ("Redraft harness").

Run it from the checkout that holds your .env (the OpenAI key and model come
from the app's settings loader, which reads ./.env), with this repo's code:

    python <repo>/scripts/redraft_harness.py export-settings \\
        --workflow-id watch:<account>:<topic> --out eval-out/watch-settings.json
    python <repo>/scripts/redraft_harness.py run --db x_conversation_finder.db \\
        --settings eval-out/watch-settings.json --out-root eval-out/redraft --label before

Every output holds real post, brief or draft text and must stay out of the
public repo: the script refuses an output path inside a git work tree that
git does not ignore. summary.md holds the aggregate numbers only.
"""

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import backend  # noqa: E402

if Path(backend.__file__).resolve().parents[1] != REPO_ROOT:
    sys.exit(f"backend was imported from {backend.__file__}, not from {REPO_ROOT}")


def ensure_ignored(path: Path) -> None:
    """Refuse an output path that a git work tree would track."""
    probe = path.resolve()
    directory = probe if probe.is_dir() else probe.parent
    while not directory.exists():
        directory = directory.parent
    inside = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    )
    if inside.returncode != 0:
        if "not a git repository" in inside.stderr:
            return  # confirmed: not in a git work tree
        # Any other git error (an unsafe repository, a broken install) leaves
        # it unknown whether git would track the file: do not write it
        sys.exit(f"Refusing to write {probe}: git could not say whether it is in a work tree")
    if inside.stdout.strip() != "true":
        return  # inside a .git directory or a bare repository: nothing tracks it
    ignored = subprocess.run(
        ["git", "-C", str(directory), "check-ignore", "-q", str(probe)], capture_output=True, text=True,
    )
    if ignored.returncode == 128:
        sys.exit(f"Refusing to write {probe}: git check-ignore failed")
    if ignored.returncode != 0:
        sys.exit(
            f"Refusing to write {probe}: git does not ignore it, and the output holds real post text. "
            "Use a path under eval-out/ (ignored) or outside the repo."
        )


# Files the harness must never write over (the card database), set by run
PROTECTED: list = []


def safe_write(path: Path, text: str) -> None:
    """Write an output file: git must ignore it, it must not be a symlink
    (writing would follow it), and it must not be a protected file."""
    path = Path(path)
    if path.is_symlink():
        sys.exit(f"Refusing to write {path}: it is a symlink")
    resolved = path.resolve()
    if any(resolved == p for p in PROTECTED):
        sys.exit(f"Refusing to write {path}: it is the input database")
    ensure_ignored(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _settings():
    from backend.config import get_settings

    if not Path(".env").exists():
        print("note: no .env in the current directory; settings come from the environment only", file=sys.stderr)
    settings = get_settings()
    if not settings.openai_api_key:
        sys.exit("No OpenAI key configured (OPENAI_API_KEY); run from the checkout that holds your .env")
    return settings


async def export_settings(args) -> None:
    """Write the product brief, name, aliases, key features and redraft
    limit of each TopicWatch's current run, keyed by account and topic
    ("watches"), and the watch topic of every Review ("review_topics", from
    each Review's start input: a card's matched_topic is model-written).
    Reads Temporal only."""
    from temporalio.client import Client

    from backend.eval.written_harness import settings_key
    from backend.workflows.review import ReviewInput
    from backend.workflows.topic_watch import TopicWatchInput

    async def start_input(handle, input_type):
        history = await handle.fetch_history()
        started = history.events[0].workflow_execution_started_event_attributes
        (value,) = await client.data_converter.decode(started.input.payloads, [input_type])
        return value

    out = Path(args.out)
    ensure_ignored(out)
    from backend.config import get_settings

    settings = get_settings()
    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    result = json.loads(out.read_text()) if out.exists() else {}
    watches = result.setdefault("watches", {})
    review_topics = result.setdefault("review_topics", {})
    for workflow_id in args.workflow_id:
        watch = await start_input(client.get_workflow_handle(workflow_id), TopicWatchInput)
        watches[settings_key(watch.account, watch.topic)] = {
            "product_brief": watch.product_brief,
            "product_name": watch.product_name,
            "product_aliases": list(watch.product_aliases or []),
            "key_features": list(watch.key_features) if watch.key_features is not None else None,
            # The limit the watch's Reviews keep (ReviewConfig, saved at start)
            "claim_gate_max_attempts": watch.review_config.claim_gate_max_attempts,
        }
        print(f"{workflow_id}: topic {watch.topic!r}, product name {'set' if watch.product_name else 'missing'}")
    async for execution in client.list_workflows("WorkflowType='Review'"):
        handle = client.get_workflow_handle(execution.id, run_id=execution.run_id)
        review = await start_input(handle, ReviewInput)
        review_topics[execution.id] = review.topic
    print(f"{len(review_topics)} Review topics")
    safe_write(out, json.dumps(result, indent=2, ensure_ascii=False))
    print(f"wrote {out}")


def sweep_earlier_reports(out_root: Path, is_removed) -> bool:
    """Apply recorded removals to every report.json under out_root (and its
    summary.md). Each file on its own: one that cannot be read or written is
    named, and the others are still swept. Returns whether all were swept."""
    from backend.eval.written_harness import redact_removed_report, summary_markdown

    complete = True
    for earlier in sorted(out_root.glob("*/report.json")):
        try:
            report = json.loads(earlier.read_text())
            if not redact_removed_report(report, is_removed):
                continue
            safe_write(earlier, json.dumps(report, indent=2, ensure_ascii=False))
            if isinstance(report.get("summary"), dict):
                safe_write(earlier.parent / "summary.md", summary_markdown(report["summary"]))
            print(f"applied recorded removals to {earlier}")
        except (OSError, ValueError, TypeError, AttributeError, SystemExit) as e:
            complete = False
            print(f"could not apply recorded removals to {earlier}: {type(e).__name__}; check it by hand",
                  file=sys.stderr)
    return complete


async def run(args) -> None:
    from openai import AsyncOpenAI

    from backend.adapters.plain_claim_checker import PlainClaimChecker
    from backend.adapters.plain_reply_agent import PlainReplyAgent
    from backend.eval.written_harness import (
        SpendCap,
        WatchSettings,
        apply_removals,
        load_stored_reviews,
        prune_removed_plans,
        removal_recorded,
        safe_label,
        report_json,
        run_harness,
        summarize,
        summary_markdown,
    )

    if not safe_label(args.label):
        sys.exit("--label must be a short name: letters, digits, '.', '_' or '-' (it goes into summary.md)")
    out_root = Path(args.out_root)
    out_dir = out_root / args.label
    plans_path = out_root / "plans.json"
    for path in (out_dir / "report.json", out_dir / "summary.md", plans_path):
        ensure_ignored(path)

    settings = _settings()
    raw = json.loads(Path(args.settings).read_text())
    if not isinstance(raw.get("watches"), dict) or not isinstance(raw.get("review_topics"), dict):
        sys.exit("--settings is not an export-settings file (it needs \"watches\" and \"review_topics\")")
    by_key = {key: WatchSettings(**value) for key, value in raw["watches"].items()}
    db = Path(args.db)
    PROTECTED.append(db.resolve())

    def is_removed(post_id: str) -> bool:
        return removal_recorded(db, post_id)

    reviews, excluded = load_stored_reviews(db, raw["review_topics"])
    if args.limit:
        reviews = reviews[: args.limit]
    plans = json.loads(plans_path.read_text()) if plans_path.exists() else {}

    try:
        cap = SpendCap(
            args.max_cost, settings.inference_input_token_cost_usd, settings.inference_output_token_cost_usd,
        )
    except ValueError as e:
        sys.exit(f"--max-cost or INFERENCE_*_TOKEN_COST_USD: {e}")
    client = cap.wrap(AsyncOpenAI(
        api_key=settings.openai_api_key, base_url=settings.openai_base_url, max_retries=0,
    ))
    # The harness meters its own calls (SpendCap) and never opens the app's
    # budget ledger, so the adapters run without the activity meter.
    agent = PlainReplyAgent(openai_client=client, require_budget_meter=False)
    checker = PlainClaimChecker(openai_client=client, require_budget_meter=False)

    print(f"{len(reviews)} stored reviews, model {settings.openai_model}, cap ${args.max_cost:.2f}")
    try:
        records, excluded, stopped = await run_harness(
            reviews, by_key, agent, checker,
            plans=plans, max_attempts=settings.claim_gate_max_attempts,
            samples=args.samples, cap=cap, excluded=excluded,
            is_removed=is_removed,
        )
    finally:
        # Also when the run failed: no cached plan of a removed post is kept,
        # and earlier reports get the removals recorded since they were written
        prune_removed_plans(plans, is_removed)
        safe_write(plans_path, json.dumps(plans, indent=2, ensure_ascii=False))
        swept = sweep_earlier_reports(out_root, is_removed)
    # A removal recorded while the earlier reports were swept: apply it to
    # this run's records too, right before they are written
    apply_removals(records, plans, is_removed, excluded)
    summary = summarize(records, excluded, cap, stopped_by_cap=stopped)
    safe_write(out_dir / "report.json", report_json(args.label, records, summary, sorted({c.model for c in cap.calls})))
    safe_write(out_dir / "summary.md", summary_markdown(summary))
    print(summary_markdown(summary))
    # The new report has the removals already; sweep again in case one was
    # recorded while it was written
    if not (sweep_earlier_reports(out_root, is_removed) and swept):
        sys.exit("Some reports could not be checked for recorded removals (see above)")
    print(f"wrote {out_dir}/report.json and summary.md")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("export-settings", help="export watch settings from Temporal (read-only)")
    exp.add_argument("--workflow-id", action="append", required=True, help="a TopicWatch id; repeatable")
    exp.add_argument("--out", required=True, help="JSON file, keyed by account:topic (merged if it exists)")

    r = sub.add_parser("run", help="draft, check and redraft stored reviews against the real model")
    r.add_argument("--db", required=True, help="the SQLite database with the stored cards (opened read-only)")
    r.add_argument("--settings", required=True, help="the export-settings JSON file")
    r.add_argument("--out-root", required=True, help="output directory; plans.json is shared between labels")
    r.add_argument("--label", required=True, help="run name, e.g. before or after")
    r.add_argument("--samples", type=int, default=1, help="written-path runs per post (default 1)")
    r.add_argument("--limit", type=int, default=0, help="only the first N stored reviews")
    r.add_argument(
        "--max-cost", type=float, default=0.25,
        help="spend cap in USD for this run (default 0.25), priced with INFERENCE_*_TOKEN_COST_USD",
    )

    args = parser.parse_args()
    asyncio.run(export_settings(args) if args.command == "export-settings" else run(args))


if __name__ == "__main__":
    main()
