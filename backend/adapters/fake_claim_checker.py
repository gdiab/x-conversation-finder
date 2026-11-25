"""Fake claim checker for tests and CI (issue #12). No API calls.

A deterministic stand-in for the checker model, not a paraphrase checker: a
sentence is "supported" only when it is a brief sentence word for word (after
the claim gate's normalization, ignoring the final punctuation) and that
sentence does not stand under a heading that may negate it, and it is
"about the product" only when code already requires a verdict for it. The
real checker's judgement of paraphrase can be measured only with a live model
run (the ADR-0006 blind comparison).
"""

import re

from backend.gate.sentence_split import normalize_text
from backend.models.written import ClaimCheckRequest, ClaimCheckResponse, ClaimVerdict
from backend.ports.claim_checker import ClaimCheckerPort


# A heading or lead-in that may turn the sentences under it into their
# opposite ("Not supported yet", "Roadmap"). The fake cannot read context, so
# it supports nothing that stands under one.
_NEGATING_CONTEXT_RE = re.compile(
    r"\b(?:not|no|never|without|unsupported|unavailable|roadmap|planned|soon|later|future|yet)\b|n't",
    re.IGNORECASE,
)


class FakeClaimChecker(ClaimCheckerPort):
    """Exact-match checker."""

    async def check(self, request: ClaimCheckRequest, idempotency_key: str) -> ClaimCheckResponse:
        by_text = {}
        for brief_sentence in request.brief_sentences:
            if _NEGATING_CONTEXT_RE.search(brief_sentence.context or ""):
                continue  # fail closed: "Slack" under "Not supported yet" supports nothing
            by_text.setdefault(normalize_text(brief_sentence.text.rstrip(".!?")), brief_sentence.id)
        verdicts = []
        for index, sentence in enumerate(request.sentences, start=1):
            must_verify = index <= len(request.must_verify) and request.must_verify[index - 1]
            if not must_verify:
                verdicts.append(ClaimVerdict(index=index, about_product=False))
                continue
            cited = by_text.get(normalize_text(sentence.rstrip(".!?")))
            if cited:
                verdicts.append(ClaimVerdict(
                    index=index, about_product=True, verdict="supported", brief_sentence_ids=[cited]
                ))
            else:
                verdicts.append(ClaimVerdict(
                    index=index,
                    about_product=True,
                    verdict="unsupported",
                    reason="The fake checker accepts only a brief sentence word for word.",
                ))
        return ClaimCheckResponse(
            verdicts=verdicts,
            model_used="fake-checker",
            input_tokens=(sum(len(s) for s in request.sentences) + 400) // 4,
            output_tokens=20 * len(verdicts),
        )
