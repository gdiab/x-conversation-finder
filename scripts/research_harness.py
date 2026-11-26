#!/usr/bin/env python3
"""Offline research eval (issue #44).

Runs the research step of a Review (the research model, then the code's fit
checks) against the real model on posts already stored in the local
database, and writes a report. It makes no X call and changes none of the
database's data. See backend/eval/research_harness.py and docs/DEVELOPMENT.md
("Research eval").

Run it from the checkout that holds your .env (the OpenAI key and model come
from the app's settings loader, which reads ./.env), with this repo's code,
on watch settings exported by the redraft harness:

    python <repo>/scripts/redraft_harness.py export-settings \\
        --workflow-id watch:<account>:<topic> --out <repo>/eval-out/watch-settings.json
    python <repo>/scripts/research_harness.py --db x_conversation_finder.db \\
        --settings <repo>/eval-out/watch-settings.json --out-root <repo>/eval-out/research --label before

Every output holds real post or brief text and must stay out of the public
repo: the script refuses an output path inside a git work tree that git does
not ignore. summary.md holds the aggregate numbers only.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

# The redraft harness's output safety (git-ignored paths only, no symlinks,
# never the database) and settings loader; it also checks that backend is
# imported from this checkout
from redraft_harness import PROTECTED, _settings, ensure_ignored, safe_write  # noqa: E402


async def run(args) -> None:
    from openai import AsyncOpenAI

    from backend.adapters.plain_reply_agent import PlainReplyAgent
    from backend.eval.research_harness import (
        load_research_posts,
        redact_removed,
        report_json,
        run_research,
        summarize,
        summary_markdown,
    )
    from backend.eval.written_harness import SpendCap, WatchSettings, removal_recorded, safe_label

    if not safe_label(args.label):
        sys.exit("--label must be a short name: letters, digits, '.', '_' or '-' (it goes into summary.md)")
    if args.samples < 1 or args.limit < 0:
        sys.exit("--samples must be 1 or more and --limit 0 (no limit) or more")
    out_dir = Path(args.out_root) / args.label
    for path in (out_dir / "report.json", out_dir / "summary.md"):
        ensure_ignored(path)

    settings = _settings()
    raw = json.loads(Path(args.settings).read_text())
    if not isinstance(raw.get("watches"), dict) or not isinstance(raw.get("review_topics"), dict):
        sys.exit("--settings is not an export-settings file (it needs \"watches\" and \"review_topics\")")
    by_key = {key: WatchSettings(**value) for key, value in raw["watches"].items()}
    db = Path(args.db)
    if not db.is_file():
        sys.exit(f"--db {db} is not a file")
    for suffix in ("", "-wal", "-shm"):
        PROTECTED.append(Path(f"{db.resolve()}{suffix}"))

    def is_removed(post_id: str) -> bool:
        return removal_recorded(db, post_id)

    posts, excluded = load_research_posts(db, raw["review_topics"], by_key)
    try:
        cap = SpendCap(
            args.max_cost, settings.inference_input_token_cost_usd, settings.inference_output_token_cost_usd,
        )
    except ValueError as e:
        sys.exit(f"--max-cost or INFERENCE_*_TOKEN_COST_USD: {e}")
    client = cap.wrap(AsyncOpenAI(
        api_key=settings.openai_api_key, base_url=settings.openai_base_url, max_retries=0,
    ))
    # The eval meters its own calls (SpendCap) and never opens the app's
    # budget ledger, so the adapter runs without the activity meter.
    agent = PlainReplyAgent(openai_client=client, require_budget_meter=False)
    print(f"{len(posts)} stored posts, model {settings.openai_model}, cap ${args.max_cost:.2f}, "
          f"{args.samples} sample(s), {f'first {args.limit} posts' if args.limit else 'all posts'}, no X calls")
    records, stopped = await run_research(
        posts, by_key, agent, cap=cap, samples=args.samples, limit=args.limit, is_removed=is_removed,
    )
    # A removal recorded during the run: its quotes stay out of the report
    redact_removed(records, is_removed)
    swept = sweep_earlier_reports(Path(args.out_root), is_removed)
    summary = summarize(records, excluded, cap, stopped_by_cap=stopped)
    models = sorted({c.model for c in cap.calls})
    safe_write(out_dir / "report.json", report_json(args.label, records, summary, models))
    safe_write(out_dir / "summary.md", summary_markdown(summary))
    print(summary_markdown(summary))
    print(f"wrote {out_dir}/report.json and summary.md")
    # Once more, for a removal recorded while this report was written
    if not (sweep_earlier_reports(Path(args.out_root), is_removed) and swept):
        sys.exit("Some reports could not be checked for recorded removals (see above)")


def sweep_earlier_reports(out_root: Path, is_removed) -> bool:
    """Apply recorded removals to every research report.json under out_root:
    the quotes of a removed post are emptied. Each file on its own; one that
    cannot be read or written is named. Returns whether all were swept."""
    from backend.eval.research_harness import redact_report

    complete = True
    for earlier in sorted(out_root.glob("*/report.json")):
        try:
            report = json.loads(earlier.read_text())
            if redact_report(report, is_removed):
                safe_write(earlier, json.dumps(report, indent=2, ensure_ascii=False))
                print(f"applied recorded removals to {earlier}")
        except (OSError, ValueError, TypeError, AttributeError, SystemExit) as e:
            complete = False
            print(f"could not apply recorded removals to {earlier}: {type(e).__name__}; check it by hand",
                  file=sys.stderr)
    return complete


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="the app's SQLite database with the stored cards (read-only)")
    parser.add_argument("--settings", required=True, help="the redraft harness's export-settings JSON file")
    parser.add_argument("--out-root", required=True, help="output directory (git-ignored or outside the repo)")
    parser.add_argument("--label", required=True, help="run name, e.g. before or after")
    parser.add_argument("--samples", type=int, default=1, help="research runs per post (default 1)")
    parser.add_argument("--limit", type=int, default=0, help="research only the first N posts (default 0: all)")
    parser.add_argument(
        "--max-cost", type=float, default=0.25,
        help="spend cap in USD for this run (default 0.25), priced with INFERENCE_*_TOKEN_COST_USD",
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
