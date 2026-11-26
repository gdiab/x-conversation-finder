"""The product-claim check of a written text, as a Temporal activity (issue #12).

ADR-0006 amendment 2026-11-24, steps 2 to 4. One activity checks a model
draft, a nudged redraft and a founder edit, so all three go through the same
steps. The sentence split, the triggers and the verdict rules run here, never
in workflow code, so the result is recorded in history and a later change to
the rules cannot make an in-flight Review replay differently.
"""

from dataclasses import dataclass, replace
from typing import Any, List, Optional, Union

from temporalio import activity
from temporalio.exceptions import ApplicationError

from backend.activities.store_activities import card_post_text_removed, post_removal_recorded
from backend.budget import meter_inference
from backend.gate.brief import split_brief
from backend.gate.product_link import PRODUCT_LINK_CHARS, append_product_link, linked_max_chars, strip_product_link
from backend.gate.written_check import MAX_CHARS, judge, prepare_check
from backend import review_docs
from backend.gate.failures import PLAN_FAILURE_MARKER, DraftFailures
from backend.models import ClaimGateResult, Evidence
from backend.models.summaries import GateSummary
from backend.models.written import WrittenCheckResult
from backend.ports.claim_checker import ClaimCheckerPort


def record_written_check(draft: Any, result: WrittenCheckResult) -> None:
    """Put a written draft's check result on the draft (issue #12).

    claim_gate mirrors the outcome, so everything that reads a draft's gate
    verdict (the card, the skip path) works for both modes. The drafter's
    own hints stay on the draft as provenance; the check never saw them.
    replace(): the result is kept for reuse, unchanged.
    """
    hints = list(draft.written.drafter_hints) if draft.written else []
    draft.sentence_checks = list(result.sentences)
    draft.written = replace(result.info, drafter_hints=hints)
    draft.plan_conformance_failures = list(result.plan_failures)
    draft.claim_gate = ClaimGateResult(
        passed=result.passed,
        segments=[],
        failed_sentences=[(sentence, reason) for sentence, reason in result.failures],
        reasoning=result.reasoning,
    )


def written_failure_rows(result: WrittenCheckResult) -> List[List[str]]:
    """The (sentence, reason) rows of a failed check, plan failures first."""
    return [[PLAN_FAILURE_MARKER, r] for r in result.plan_failures] + [
        [sentence, reason] for sentence, reason in result.failures
    ]


@dataclass
class CheckWrittenDraftInput:
    """Input for check_written_draft.

    The activity is given no post text and no reply plan, only the post's id
    (to ask whether its removal was recorded) and the plan's product
    decision, so nothing it passes to the checker can come from them.
    """

    text: str
    product_brief: str
    product_name: str
    idempotency_key: str
    product_aliases: Optional[List[str]] = None
    # The watch's "Key features": the only source of feature terms. None for
    # a watch started without the field: no feature terms.
    key_features: Optional[List[str]] = None
    # The reply plan's decision; None for a founder edit, which is not held to the plan
    mention_product: Optional[bool] = None
    # The target post's id, for the removal lookup only; never sent to the checker
    post_id: str = ""
    # --- Issue #23: text out of history. With source_slot set, the text is
    # read from the Review's documents: the draft of the evidence in
    # source_slot, or (source_is_founder_text) a founder's edit. The result
    # is kept per (kind, text digest), first finished check wins, and
    # recorded on the draft in record_slot when given. Returns a GateSummary.
    source_slot: str = ""
    source_is_founder_text: bool = False
    kind: str = "draft"
    record_slot: str = ""
    # Issue #41: the watch's "Product URL" (None for a watch started without
    # it). A model draft that passes and names the product gets it appended
    # when its check is recorded (record_slot), and its sentences must leave
    # room for it. A founder's text that ends with it is checked without it.
    # Text-free inputs only.
    product_url: Optional[str] = None


def names_product(result: WrittenCheckResult, mention_product: Optional[bool]) -> bool:
    """Whether a passing written draft gets the product link (issue #41):
    the plan says to mention the product, or a sentence was checked as a
    product claim (and, the draft having passed, supported)."""
    return result.passed and (
        mention_product is True or any(s.kind == "claim" for s in result.sentences)
    )


class WrittenCheckActivities:
    """Written-draft check with an injected claim checker."""

    def __init__(self, claim_checker: ClaimCheckerPort):
        self._claim_checker = claim_checker

    @activity.defn(name="check_written_draft")
    @review_docs.text_free_activity(lambda input: bool(input.source_slot))
    async def check_written_draft(self, input: CheckWrittenDraftInput) -> Union[WrittenCheckResult, GateSummary]:
        """
        Check a written text's product claims against the brief.

        1. Code splits the text and the brief, runs the triggers and applies
           everything it can decide alone (sentences, characters, more than
           one triggered sentence, a triggered question, a number no brief
           sentence states, a product claim the plan ruled out). A text that
           fails here is not sent to the checker: no paid call is made for it.
        2. Otherwise the independent checker sees the sentences, the brief
           sentences, the product name and aliases, and which sentences
           code requires a verdict for, and returns a verdict per sentence.
        3. Code decides (backend.gate.written_check.judge): the cited brief
           sentence ids must exist, every supported sentence must cite one,
           and a triggered sentence needs a supported verdict whatever the
           checker says about it.

        Budget (issue #8): the checker adapter reserves its paid call through
        the meter opened here, sized from its prompt, and reconciles it to the
        reported usage. Raises BudgetExceeded (non-retryable) at the cap.
        """
        if input.source_slot:
            return await self._check_text_free(input)
        return await self._check(input.text, input)

    async def _check(
        self, text: str, input: CheckWrittenDraftInput, max_chars: int = MAX_CHARS, product_link_chars: int = 0
    ) -> WrittenCheckResult:
        """The check itself; `max_chars` and `product_link_chars` are the
        length rule (issue #41, see prepare_check)."""
        activity.logger.info(
            f"Checking written text: {len(text)} chars (key: {input.idempotency_key})"
        )

        # Issue #9: once this Review's stored card records that the post was
        # removed, its draft may quote a deleted post: do not send it to a
        # model. Issue #12: likewise when any other card of the post records
        # it (another account's Review), before this Review has a card.
        if card_post_text_removed(activity.info().workflow_id) or (
            input.post_id and post_removal_recorded(input.post_id)
        ):
            raise ApplicationError(
                "Post removed: the target post was deleted or is no longer visible on X; not checking",
                type="PostRemoved",
                non_retryable=True,
            )

        prepared = prepare_check(
            text,
            split_brief(input.product_brief),
            input.product_name,
            input.product_aliases,
            input.key_features,
            input.mention_product,
            max_chars=max_chars,
            product_link_chars=product_link_chars,
        )
        if not prepared.needs_checker:
            result = judge(prepared, None, input.mention_product)
            activity.logger.info(f"Written check: FAIL before the checker call - {result.reasoning}")
            return result

        with meter_inference() as meter:
            response = await self._claim_checker.check(prepared.request(), input.idempotency_key)

        result = judge(
            prepared,
            response.verdicts,
            input.mention_product,
            checker_model=response.model_used,
            checker_input_tokens=response.input_tokens,
            checker_output_tokens=response.output_tokens,
        )
        activity.logger.info(
            f"Written check: {'PASS' if result.passed else 'FAIL'} - {result.reasoning}, {meter.summary()}"
        )
        return result

    async def _check_text_free(self, input: CheckWrittenDraftInput) -> GateSummary:
        """check_written_draft for a Review that keeps text out of its
        history (issue #23); see CheckWrittenDraftInput."""
        workflow_id = activity.info().workflow_id
        what = "not checking"
        if input.source_is_founder_text:
            text = review_docs.load_doc(workflow_id, input.post_id, input.source_slot, str, what)
        else:
            evidence = review_docs.load_doc(workflow_id, input.post_id, input.source_slot, Evidence, what)
            text = evidence.draft.text if evidence.draft else ""
        # Issue #41: the sentences are checked without a trailing product
        # link. A founder's text keeps its link and is checked under the
        # budget X leaves after it; its result is kept by the whole text, as
        # the card names it. A model draft gets the link only after it
        # passed: a link already on it (this activity's own output, read
        # again on a retry, or the model's) is taken off, and the result is
        # kept by the sentences, so the link changes no check.
        body, linked = strip_product_link(text, input.product_url)
        if input.source_is_founder_text:
            max_chars = linked_max_chars(text, input.product_url, body, MAX_CHARS) if linked else MAX_CHARS
            link_chars = 0
            digest = review_docs.text_digest(text)
        else:
            max_chars, link_chars = MAX_CHARS, (PRODUCT_LINK_CHARS if input.product_url else 0)
            digest = review_docs.text_digest(body)
        cache = review_docs.check_slot(input.kind, digest)
        result = review_docs.find_doc(workflow_id, input.post_id, cache, WrittenCheckResult)
        if result is None:
            checked = await self._check(body, input, max_chars, link_chars)
            # The first finished check of a text stands (another update may
            # have checked the same text meanwhile)
            result = review_docs.save_doc(workflow_id, input.post_id, cache, checked, first_wins=True)
        rows = written_failure_rows(result)
        draft_digest = digest
        if input.record_slot:
            target = review_docs.load_doc(workflow_id, input.post_id, input.record_slot, Evidence, what)
            if target.draft is not None:
                record_written_check(target.draft, result)
                if input.product_url:
                    # Issue #41: code, never the model, adds the link
                    target.draft.text = (
                        append_product_link(body, input.product_url)
                        if names_product(result, input.mention_product) else body
                    )
                draft_digest = review_docs.text_digest(target.draft.text)
                review_docs.save_doc(workflow_id, input.post_id, input.record_slot, target)
            review_docs.save_doc(
                workflow_id, input.post_id, review_docs.failures_slot(input.record_slot),
                DraftFailures(rows=rows, gate_reasoning=result.reasoning, gate_passed=result.passed),
            )
        return GateSummary(
            passed=result.passed,
            failure_count=len(rows),
            plan_failure_count=len(result.plan_failures),
            result_count=len(result.sentences),
            failing_count=sum(1 for r in result.sentences if not r.passed),
            text_sha256=digest,
            draft_sha256=draft_digest,
        )
