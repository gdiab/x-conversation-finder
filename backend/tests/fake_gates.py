"""Helpers for test fakes of verify_edited_text (issue #23).

A Review started with only a post id sends verify_edited_text the slot of
the founder's edit, not the text, and expects the results stored and a
GateSummary back (backend.activities.claim_gate_activity). A fake that
controls timing or results uses these to stay compatible with both inputs.
"""

from temporalio import activity

from backend import review_docs
from backend.models.summaries import GateSummary


def edited_text(input: dict) -> str:
    """The edit's text, from the input or (text-free input) its slot."""
    if not input.get("text_slot"):
        return input["edited_text"]
    return review_docs.load_doc(activity.info().workflow_id, input["post_id"], input["text_slot"], str)


def gate_result(input: dict, results=()):
    """What the fake returns: the results, or (text-free input) a summary
    after storing them where the real activity stores them."""
    results = list(results)
    if not input.get("text_slot"):
        return results
    review_docs.save_doc(activity.info().workflow_id, input["post_id"], input["results_slot"], results)
    failing = sum(1 for r in results if not getattr(r, "passed", r.get("passed") if isinstance(r, dict) else True))
    return GateSummary(
        passed=failing == 0,
        result_count=len(results),
        failing_count=failing,
        text_sha256=review_docs.text_digest(edited_text(input)),
    )
