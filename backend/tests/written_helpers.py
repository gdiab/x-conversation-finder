"""Shared helpers for the written-draft tests (issue #12). Synthetic data only.

The checker fakes here are deterministic stand-ins for the checker model:
- ScriptedChecker answers each sentence with the verdict a test scripts for
  it. The switch-criteria fixture scripts the verdicts a correct checker must
  return, so the code-side rules are proven without a model.
- The LENIENT_* answers are what a wrong, permissive checker might say; the
  tests use them to show which rejections do not depend on the checker.
"""

from typing import Any, Callable, Dict, List, Optional

from backend.models.written import (
    BriefSentence,
    ClaimCheckRequest,
    ClaimCheckResponse,
    ClaimVerdict,
)
from backend.ports.claim_checker import ClaimCheckerPort

# A synthetic product: "Acme Relay", also called "Relay".
RELAY_NAME = "Acme Relay"
RELAY_ALIASES = ["Relay"]
RELAY_BRIEF = """
# Acme Relay

Acme Relay delivers webhooks to your services. Relay can retry a failed delivery up to five times. Signed deliveries are available on the Team plan. Relay is faster than polling for changes. The replay tool lets you resend any delivery from the last 30 days. Our dashboard shows delivery latency per endpoint.

Key features:
- Signed deliveries
- Delivery replay
""".strip()


def verdict_from(n: int, spec: Optional[Dict[str, Any]]) -> ClaimVerdict:
    """A ClaimVerdict for sentence n from a fixture/test dict (None = no answer)."""
    spec = spec or {}
    return ClaimVerdict(
        index=n,
        about_product=spec.get("about_product"),
        verdict=spec.get("verdict"),
        brief_sentence_ids=list(spec.get("brief_sentence_ids") or []),
        reason=spec.get("reason", ""),
    )


def not_about_product(request: ClaimCheckRequest) -> List[ClaimVerdict]:
    """A lenient checker: nothing is about the product, no verdict given."""
    return [ClaimVerdict(index=i, about_product=False) for i in range(1, len(request.sentences) + 1)]


def supported_without_citation(request: ClaimCheckRequest) -> List[ClaimVerdict]:
    return [
        ClaimVerdict(index=i, about_product=True, verdict="supported")
        for i in range(1, len(request.sentences) + 1)
    ]


def supported_citing(*ids: str) -> Callable[[ClaimCheckRequest], List[ClaimVerdict]]:
    """A lenient checker: every sentence is supported by the given ids."""
    def answer(request: ClaimCheckRequest) -> List[ClaimVerdict]:
        return [
            ClaimVerdict(index=i, about_product=True, verdict="supported", brief_sentence_ids=list(ids))
            for i in range(1, len(request.sentences) + 1)
        ]
    return answer


def no_answer(request: ClaimCheckRequest) -> List[ClaimVerdict]:
    """A checker whose output could not be parsed at all."""
    return []


class ScriptedChecker(ClaimCheckerPort):
    """A deterministic fake checker; records every request it receives.

    `by_sentence` maps a sentence's text to its verdict dict (about_product,
    verdict, brief_sentence_ids, reason). A sentence that is not scripted gets
    `default` (not about the product, unless given). `answer` replaces the
    whole response with a function of the request.
    """

    def __init__(
        self,
        by_sentence: Optional[Dict[str, Dict[str, Any]]] = None,
        default: Optional[Dict[str, Any]] = None,
        answer: Optional[Callable[[ClaimCheckRequest], List[ClaimVerdict]]] = None,
        error: Optional[BaseException] = None,
    ):
        self.by_sentence = dict(by_sentence or {})
        self.default = default if default is not None else {"about_product": False}
        self.answer = answer
        self.error = error
        self.requests: List[ClaimCheckRequest] = []

    async def check(self, request: ClaimCheckRequest, idempotency_key: str) -> ClaimCheckResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.answer is not None:
            verdicts = self.answer(request)
        else:
            verdicts = [
                verdict_from(i, self.by_sentence.get(sentence, self.default))
                for i, sentence in enumerate(request.sentences, start=1)
            ]
        return ClaimCheckResponse(
            verdicts=verdicts, model_used="scripted-checker", input_tokens=50, output_tokens=10
        )


def brief(*sentences: str) -> List[BriefSentence]:
    return [BriefSentence(id=f"b{i}", text=text) for i, text in enumerate(sentences, start=1)]
