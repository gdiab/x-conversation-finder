"""What text-free activities return to a workflow (issue #23).

A Review started with only a post id keeps the post, the evidence, drafts
and check results in SQLite (backend/review_docs.py). Its activities return
these summaries instead: the counts, flags, digests and code-written reasons
the workflow branches on, and nothing a model or the post wrote.

Every field has a default, so a later field addition still decodes recorded
results.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class ResearchSummary:
    """research_post: the research is stored in the Review's evidence slot."""

    confidence_score: float = 0.0
    # A should-we-reply rule's reason (backend/gate/reply_fit.py): code-written,
    # it names the rule, never the post
    skip_reason: Optional[str] = None
    has_reply_fit: bool = False


@dataclass
class DraftSummary:
    """draft_reply: the evidence with the new draft is stored in its out slot."""

    has_draft: bool = False
    has_segments: bool = False
    draft_mode: Optional[str] = None
    # Digest of the draft text (backend.review_docs.text_digest)
    text_sha256: str = ""
    plan_failure_count: int = 0


@dataclass
class GateSummary:
    """claim_gate, check_written_draft or verify_edited_text on a stored text.

    The per-sentence results and reasons are stored; the workflow sees the
    verdict and counts. failure_count counts the (sentence, reason) rows of
    the stored failures, plan failures included.
    """

    passed: bool = False
    failure_count: int = 0
    plan_failure_count: int = 0
    # Results recorded (sentences checked); for an edit, how many failed
    result_count: int = 0
    failing_count: int = 0
    # Digest of the text the result is about (the gate can rewrite a draft)
    text_sha256: str = ""


@dataclass
class PostLookup:
    """lookup_post without the post's content: is it there, and in which conversation."""

    post_id: str = ""
    conversation_id: str = ""
