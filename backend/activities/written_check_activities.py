"""The product-claim check of a written text, as a Temporal activity (issue #12).

ADR-0006 amendment 2026-11-24, steps 2 to 4. One activity checks a model
draft, a nudged redraft and a founder edit, so all three go through the same
steps. The sentence split, the triggers and the verdict rules run here, never
in workflow code, so the result is recorded in history and a later change to
the rules cannot make an in-flight Review replay differently.
"""

from dataclasses import dataclass
from typing import List, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

from backend.activities.store_activities import card_post_text_removed, post_removal_recorded
from backend.budget import meter_inference
from backend.gate.brief import split_brief
from backend.gate.written_check import judge, prepare_check
from backend.models.written import WrittenCheckResult
from backend.ports.claim_checker import ClaimCheckerPort


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


class WrittenCheckActivities:
    """Written-draft check with an injected claim checker."""

    def __init__(self, claim_checker: ClaimCheckerPort):
        self._claim_checker = claim_checker

    @activity.defn(name="check_written_draft")
    async def check_written_draft(self, input: CheckWrittenDraftInput) -> WrittenCheckResult:
        """
        Check a written text's product claims against the brief.

        1. Code splits the text and the brief, runs the triggers and applies
           everything it can decide alone (sentences, characters, more than
           one triggered sentence, a triggered question, a number no brief
           sentence states, a product claim the plan ruled out). A text that
           fails here is not sent to the checker: no paid call is made for it.
        2. Otherwise the independent checker sees the sentences, the brief
           sentences and the product name, and returns a verdict per sentence.
        3. Code decides (backend.gate.written_check.judge): the cited brief
           sentence ids must exist, every supported sentence must cite one,
           and a triggered sentence needs a supported verdict whatever the
           checker says about it.

        Budget (issue #8): the checker adapter reserves its paid call through
        the meter opened here, sized from its prompt, and reconciles it to the
        reported usage. Raises BudgetExceeded (non-retryable) at the cap.
        """
        activity.logger.info(
            f"Checking written text: {len(input.text)} chars (key: {input.idempotency_key})"
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
            input.text,
            split_brief(input.product_brief),
            input.product_name,
            input.product_aliases,
            input.key_features,
            input.mention_product,
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
