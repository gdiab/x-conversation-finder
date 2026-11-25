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
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return  # not in a git work tree
    ignored = subprocess.run(["git", "-C", str(directory), "check-ignore", "-q", str(probe)])
    if ignored.returncode != 0:
        sys.exit(
            f"Refusing to write {probe}: git does not ignore it, and the output holds real post text. "
            "Use a path under eval-out/ (ignored) or outside the repo."
        )


def _settings():
    from backend.config import get_settings

    if not Path(".env").exists():
        print("note: no .env in the current directory; settings come from the environment only", file=sys.stderr)
    settings = get_settings()
    if not settings.openai_api_key:
        sys.exit("No OpenAI key configured (OPENAI_API_KEY); run from the checkout that holds your .env")
    return settings


async def export_settings(args) -> None:
    """Write the product brief, name, aliases and key features of each
    TopicWatch's current run, keyed by topic. Reads Temporal only."""
    from temporalio.client import Client

    from backend.workflows.topic_watch import TopicWatchInput

    out = Path(args.out)
    ensure_ignored(out)
    from backend.config import get_settings

    settings = get_settings()
    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    result = json.loads(out.read_text()) if out.exists() else {}
    for workflow_id in args.workflow_id:
        handle = client.get_workflow_handle(workflow_id)
        history = await handle.fetch_history()
        started = history.events[0].workflow_execution_started_event_attributes
        (watch,) = await client.data_converter.decode(started.input.payloads, [TopicWatchInput])
        result[watch.topic] = {
            "product_brief": watch.product_brief,
            "product_name": watch.product_name,
            "product_aliases": list(watch.product_aliases or []),
            "key_features": list(watch.key_features) if watch.key_features is not None else None,
        }
        print(f"{workflow_id}: topic {watch.topic!r}, product name {'set' if watch.product_name else 'missing'}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"wrote {out}")


async def run(args) -> None:
    from openai import AsyncOpenAI

    from backend.adapters.plain_claim_checker import PlainClaimChecker
    from backend.adapters.plain_reply_agent import PlainReplyAgent
    from backend.eval.written_harness import (
        SpendCap,
        WatchSettings,
        load_stored_reviews,
        report_json,
        run_harness,
        summarize,
        summary_markdown,
    )

    out_root = Path(args.out_root)
    out_dir = out_root / args.label
    plans_path = out_root / "plans.json"
    for path in (out_dir / "report.json", plans_path):
        ensure_ignored(path)

    settings = _settings()
    raw = json.loads(Path(args.settings).read_text())
    by_topic = {topic: WatchSettings(**value) for topic, value in raw.items()}
    reviews, excluded = load_stored_reviews(Path(args.db))
    if args.limit:
        reviews = reviews[: args.limit]
    plans = json.loads(plans_path.read_text()) if plans_path.exists() else {}

    cap = SpendCap(args.max_cost, settings.inference_input_token_cost_usd, settings.inference_output_token_cost_usd)
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
            reviews, by_topic, agent, checker,
            plans=plans, max_attempts=settings.claim_gate_max_attempts,
            samples=args.samples, cap=cap, excluded=excluded,
        )
    finally:
        plans_path.parent.mkdir(parents=True, exist_ok=True)
        plans_path.write_text(json.dumps(plans, indent=2, ensure_ascii=False))
    summary = summarize(records, excluded, cap, stopped_by_cap=stopped)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(report_json(args.label, records, summary))
    (out_dir / "summary.md").write_text(summary_markdown(args.label, summary))
    print(summary_markdown(args.label, summary))
    print(f"wrote {out_dir}/report.json and summary.md")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("export-settings", help="export watch settings from Temporal (read-only)")
    exp.add_argument("--workflow-id", action="append", required=True, help="a TopicWatch id; repeatable")
    exp.add_argument("--out", required=True, help="JSON file, keyed by topic (merged if it exists)")

    r = sub.add_parser("run", help="draft, check and redraft stored reviews against the real model")
    r.add_argument("--db", required=True, help="the SQLite database with the stored cards (opened read-only)")
    r.add_argument("--settings", required=True, help="the export-settings JSON file")
    r.add_argument("--out-root", required=True, help="output directory; plans.json is shared between labels")
    r.add_argument("--label", required=True, help="run name, e.g. before or after")
    r.add_argument("--samples", type=int, default=1, help="written-path runs per post (default 1)")
    r.add_argument("--limit", type=int, default=0, help="only the first N stored reviews")
    r.add_argument("--max-cost", type=float, default=0.25, help="spend cap in USD for this run (default 0.25)")

    args = parser.parse_args()
    asyncio.run(export_settings(args) if args.command == "export-settings" else run(args))


if __name__ == "__main__":
    main()
