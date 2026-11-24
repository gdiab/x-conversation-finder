"""Reply plan model (issue #5): how to reply, decided before drafting.

The plan is drafting-mode agnostic. It says what kind of reply to write, which
part of the post it responds to, and whether the product is mentioned. It never
refers to segments, phrase ids or passage sentences, so the assembled drafter
(ADR-0006, 2026-11-21 amendment) and a future written drafter (proposed
2026-11-24 amendment) consume the same plan unchanged.

Activity payloads use plain str values, not str-Enums (a str-Enum decodes as a
char list at the Temporal activity boundary). `reply_type` is typed `str`, not a
Literal, so that a plan with an unknown type still decodes and reaches
validation, which rejects it with a reason.
"""

from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional, Tuple

ReplyTypeStr = Literal["answer_question", "add_observation", "share_experience"]

REPLY_TYPES: Tuple[str, ...] = ("answer_question", "add_observation", "share_experience")

REPLY_TYPE_DESCRIPTIONS: Dict[str, str] = {
    "answer_question": "the post asks something we can answer",
    "add_observation": "react to the poster's specific point",
    "share_experience": "relate the post to how we run our own product",
}


@dataclass
class ReplyPlan:
    """A plan for one reply, proposed by the reply agent and validated by code.

    Fields:
    - reply_type: one of REPLY_TYPES
    - target_point: the exact span of the post text the reply responds to
    - mention_product: True or False; None only when the model returned a
      non-boolean, which validation rejects
    - mention_product_reason: one line explaining the mention_product decision
    - model_used, input_tokens, output_tokens: provenance and budget usage
    """

    reply_type: str
    target_point: str
    mention_product: Optional[bool]
    mention_product_reason: str
    model_used: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


def reply_plan_card_fields(evidence: Any) -> Dict[str, Any]:
    """Card/DB fields for the reply plan, merged into the evidence dict.

    One helper for both the get_card query (ReviewCard.to_dict) and the stored
    card (store_activities), so the two views cannot drift.
    """
    plan = getattr(evidence, "reply_plan", None)
    return {
        "reply_plan": None if plan is None else {
            "reply_type": plan.reply_type,
            "target_point": plan.target_point,
            "mention_product": plan.mention_product,
            "mention_product_reason": plan.mention_product_reason,
            "model_used": plan.model_used,
        },
        "reply_plan_errors": list(getattr(evidence, "reply_plan_errors", None) or []),
        "reply_plan_attempts": getattr(evidence, "reply_plan_attempts", 0) or 0,
    }
