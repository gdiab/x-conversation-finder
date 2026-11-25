"""
Tests for PlainReplyAgent's handling of OpenAI responses.

The fake client replays the shape the real API returned during a live run
(2026-11-23, gpt-4o-mini, full product brief): without JSON mode the model
wrapped its answer in a ```json fence, which json.loads rejects. With
response_format={"type": "json_object"} the API returns a bare object.
"""

import json
from types import SimpleNamespace

import pytest

from backend.adapters.plain_reply_agent import PlainReplyAgent
from backend.models import FounderMaterial


RESEARCH_JSON = {
    "matched_topic": "multi-agent-orchestration",
    "why_it_fits": "The post asks how to keep track of several coding agents.",
    "founder_materials": [
        {
            "source": "product_brief",
            "passage": "Work is recorded in one durable ledger. Status is computed from the record.",
            "relevance": "Answers how to track agent work",
        }
    ],
    "post_kind": "question",
    "brief_problem_span": None,
    "in_others_thread": False,
    "is_vendor_post": False,
}

DRAFT_JSON = {
    "segments": [
        {"kind": "non_claim", "phrase_id": "opener.thanks"},
        {"kind": "claim", "source_passage_id": "passage_0", "sentence_index": 0},
    ]
}


class FakeCompletions:
    """Mimics chat.completions: fenced text unless JSON mode is requested."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        body = json.dumps(self._payloads.pop(0), indent=2)
        if kwargs.get("response_format") != {"type": "json_object"}:
            body = f"```json\n{body}\n```"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=body))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        )


def make_agent(payloads):
    completions = FakeCompletions(payloads)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return PlainReplyAgent(openai_client=client, require_budget_meter=False), completions


@pytest.mark.asyncio
async def test_research_uses_json_mode_and_parses(sample_post):
    agent, completions = make_agent([RESEARCH_JSON])

    evidence = await agent.research(sample_post, "multi-agent-orchestration", "brief", "key")

    assert completions.calls[0]["response_format"] == {"type": "json_object"}
    assert evidence.matched_topic == "multi-agent-orchestration"
    assert len(evidence.founder_materials) == 1
    assert evidence.reply_fit.post_kind == "question"
    # The adapter proposes features only; the research_post activity scores them.
    assert evidence.confidence_inputs == {}


@pytest.mark.asyncio
async def test_draft_uses_json_mode_and_reads_segments(sample_post):
    agent, completions = make_agent([RESEARCH_JSON, DRAFT_JSON])
    evidence = await agent.research(sample_post, "multi-agent-orchestration", "brief", "key")

    evidence = await agent.draft(evidence)

    assert completions.calls[1]["response_format"] == {"type": "json_object"}
    assert [s.kind for s in evidence.draft.segments] == ["non_claim", "claim"]
    assert "Work is recorded in one durable ledger" in evidence.draft.text


def test_materials_are_numbered_with_gate_indices():
    from backend.adapters.plain_reply_agent import format_materials_for_prompt
    from backend.activities.claim_gate_activity import split_into_sentences

    passage = "Tightbeam records agent work. It never estimates. Status is computed from the record."
    text = format_materials_for_prompt([
        FounderMaterial(source="brief", passage=passage, relevance="r", passage_id="passage_0")
    ])

    sentences = split_into_sentences(passage)
    assert f"  0: {sentences[0]}" in text
    assert f"  2: {sentences[2]}" in text
    # Starts with an unresolved referent, so the gate would reject it: not offered.
    assert "It never estimates" not in text


def test_phrase_list_matches_current_phrase_set():
    from backend.adapters.plain_reply_agent import format_phrases_for_prompt
    from backend.gate.non_claim_set import NON_CLAIM_PHRASES

    text = format_phrases_for_prompt()
    for p in NON_CLAIM_PHRASES:
        assert f"{p.id}: {p.text}" in text
    # IDs removed in phrase set v2.1.0 must not be offered.
    for removed in ("opener.great_question", "thanks.for_asking", "offer.glad_to_help", "q.tried"):
        assert removed not in text


@pytest.mark.asyncio
async def test_draft_prompt_shows_numbered_sentences(sample_post):
    agent, completions = make_agent([RESEARCH_JSON, DRAFT_JSON])
    evidence = await agent.research(sample_post, "multi-agent-orchestration", "brief", "key")

    await agent.draft(evidence)

    prompt = completions.calls[1]["messages"][1]["content"]
    assert "[passage_0] Source: product_brief" in prompt
    assert "  0: Work is recorded in one durable ledger." in prompt
    assert "  1: Status is computed from the record." in prompt


@pytest.mark.asyncio
async def test_draft_prompt_caps_segments_at_three(sample_post):
    """The gate rejects drafts over 3 segments; the prompt must not invite four."""
    agent, completions = make_agent([RESEARCH_JSON, DRAFT_JSON])
    evidence = await agent.research(sample_post, "multi-agent-orchestration", "brief", "key")

    await agent.draft(evidence)

    prompt = completions.calls[1]["messages"][1]["content"]
    assert "At most 3 segments in total" in prompt
    assert "never both" in prompt
