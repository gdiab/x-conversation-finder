"""One JSON-mode chat completion, metered against the daily inference budget.

Shared by every adapter that makes a paid inference call (the reply agent's
research, plan and draft calls, and the written-draft claim checker, issue
#12), so each call is reserved and settled the same way (issue #8).
"""

from typing import Any, List

from temporalio.exceptions import ApplicationError

from backend.budget import current_inference_meter, prompt_token_bound, request_never_sent


async def metered_json_completion(
    client: Any,
    *,
    model: str,
    call: str,
    messages: List[dict],
    temperature: float,
    max_output_tokens: int,
    require_budget_meter: bool,
):
    """Send one JSON-mode chat completion, metered against the daily budget.

    Issue #8: inside a metered activity attempt (backend.budget.meter_inference)
    the call is reserved right before the request, sized from an upper bound
    on the prompt's tokens and the max output tokens sent with it, then
    reconciled to the reported usage. A failed request (any error status,
    read timeout, cancellation) keeps its reservation counted:
    openai_base_url may be a gateway, so a status does not prove that no
    upstream work was billed. Only a connection that was never established
    (backend.budget.request_never_sent) releases it.

    Outside a metered attempt the call is refused when require_budget_meter is
    set, so a paid request can never skip the daily cap and kill switch.
    """
    meter = current_inference_meter()
    if meter is None and require_budget_meter:
        raise ApplicationError(
            f"Unmetered {call} inference call refused: paid calls must run "
            "inside backend.budget.meter_inference()",
            non_retryable=True,
            type="UnmeteredInferenceCall",
        )
    reservation = (
        meter.reserve(prompt_token_bound(messages), max_output_tokens) if meter else None
    )
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_completion_tokens=max_output_tokens,
            # Without JSON mode, models wrap long answers in ```json fences,
            # which json.loads rejects.
            response_format={"type": "json_object"},
        )
    except Exception as e:
        if reservation and request_never_sent(e):
            reservation.release()
        raise
    if reservation:
        usage = getattr(response, "usage", None)
        reservation.settle(
            getattr(usage, "prompt_tokens", None) if usage else None,
            getattr(usage, "completion_tokens", None) if usage else None,
        )
    return response


def raise_if_truncated(call: str, response: Any, max_output_tokens: int) -> None:
    """Fail without retry when the output hit max_completion_tokens.

    The JSON is cut off, and a retry sends the same prompt with the same
    limit, paying again for the same failure. The call is already counted.
    Raise the call's limit (settings: <call>_max_output_tokens) instead.
    """
    choices = getattr(response, "choices", None) or []
    if choices and getattr(choices[0], "finish_reason", None) == "length":
        raise ApplicationError(
            f"{call} output truncated at {max_output_tokens} tokens; "
            f"raise {call.upper()}_MAX_OUTPUT_TOKENS",
            non_retryable=True,
            type="InferenceOutputTruncated",
        )
