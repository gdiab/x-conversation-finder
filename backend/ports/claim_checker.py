"""ClaimCheckerPort - the independent check of product sentences (issue #12).

ADR-0006 amendment 2026-11-24, step 3: a separate model call with its own
prompt verifies each product sentence of a written draft against the brief.
George's decision (2026-11-24): the checker may use the same model and
provider as the drafter, but it is a separate session, a fresh call with no
drafting context, like a subagent.

That isolation is this port's signature. A checker receives a
ClaimCheckRequest and nothing else: the reply's sentences, the brief
sentences, the product name and aliases, which sentences code requires a
verdict for, and the brief sentence ids the drafter suggested. It never
receives the post, the reply plan, the evidence or the drafting prompt, so an
adapter cannot pass them to the model.

Budget (issue #8): an adapter that makes a paid call reserves it through
backend.budget.current_inference_meter() right before the request and settles
it after, as the reply agent's adapters do
(backend.adapters.metered_completion). The check_written_draft activity opens
the meter.
"""

from abc import ABC, abstractmethod

from backend.models.written import ClaimCheckRequest, ClaimCheckResponse


class ClaimCheckerPort(ABC):
    """Port for the independent product-claim checker."""

    @abstractmethod
    async def check(self, request: ClaimCheckRequest, idempotency_key: str) -> ClaimCheckResponse:
        """
        Classify and verify the request's sentences against its brief sentences.

        Returns one ClaimVerdict per sentence (index is the 1-based position):
        - about_product: whether the sentence says anything about the product;
        - verdict: "supported" or "unsupported", required for every sentence
          with must_verify set and for every sentence about the product;
        - brief_sentence_ids: for "supported", the ids that entail it;
        - reason: for "unsupported", one short line.

        The verdicts are the checker's proposal. Code decides
        (backend.gate.written_check.judge): it confirms the cited ids exist,
        and a missing or malformed verdict counts as not supported. Adapters
        must not raise on malformed model output: return what was parsed.
        Output cut off at the max output tokens is a configuration error and
        may raise (issue #8).
        """
        pass
