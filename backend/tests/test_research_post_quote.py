"""Issue #44: the research model quotes the post, not the brief.

The research prompt used to ask for `brief_problem_span`, listed right after
the brief, and the model often copied the brief's own problem statement
(70 of 71 `not_in_post` cards on 2026-11-26). The model-facing key is now
`post_problem_quote`, the post sits between markers right before the keys,
and the request says to quote the post and never the brief. The stored field
keeps its name (`ReplyFit.brief_problem_span`), `match_span` stays strict,
and no workflow code changed: histories recorded on main fd3f38e replay.

Everything runs with fake adapters and synthetic text.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from backend.adapters.plain_reply_agent import (
    BRIEF_END,
    BRIEF_START,
    POST_END,
    POST_QUOTE_KEY,
    POST_START,
    PlainReplyAgent,
    parse_reply_fit,
)
from backend.gate.reply_fit import assess_reply_fit
from backend.tests.test_reply_fit import BRIEF, NOW, TOPIC, JsonModeCompletions, _post
from backend.workflows.review import ReviewWorkflow
from backend.workflows.topic_watch import TopicWatchWorkflow

FIXTURES = Path(__file__).parent / "fixtures"
POST_TEXT = "How do you keep a record of what each coding agent did?"
MATERIALS = [{"source": "product_brief", "passage": BRIEF.split("\n")[0].strip(), "relevance": "r"}]


def _agent(payload: dict):
    completions = JsonModeCompletions({
        "post_kind": "question", "in_others_thread": False, "is_vendor_post": False,
        "founder_materials": MATERIALS, **payload,
    })
    agent = PlainReplyAgent(
        openai_client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        require_budget_meter=False,
    )
    return agent, completions


async def _research(payload: dict, text: str = POST_TEXT):
    agent, completions = _agent(payload)
    post = _post(text=text)
    evidence = await agent.research(post, TOPIC, BRIEF, "k")
    return assess_reply_fit(evidence, post, BRIEF, now=NOW), completions.calls[0]["messages"][1]["content"]


# ---------- the request ----------


@pytest.mark.asyncio
async def test_the_prompt_asks_for_a_quote_of_the_post_next_to_the_post():
    _, prompt = await _research({POST_QUOTE_KEY: None})

    # The old key named the brief; it is gone from what the model sees
    assert "brief_problem_span" not in prompt
    assert f"- {POST_QUOTE_KEY}:" in prompt
    # The post is between its markers, after the brief and right before the keys
    assert f"{POST_START}\n{POST_TEXT}\n{POST_END}" in prompt
    assert f"{BRIEF_START}\n{BRIEF}\n{BRIEF_END}" in prompt
    assert prompt.index(BRIEF_END) < prompt.index(POST_START)
    keys = prompt.index("Return a JSON object")
    assert prompt.index(POST_END) < keys
    # The quote is the first key, before the others
    assert prompt.index(f"- {POST_QUOTE_KEY}:") < prompt.index("- post_kind:") < prompt.index("- founder_materials:")
    # The rule names the post as the source and forbids the brief
    rule = prompt[prompt.index(f"- {POST_QUOTE_KEY}:"):prompt.index("- post_kind:")]
    assert f"between\n  {POST_START} and {POST_END}" in rule
    assert "Never copy text from the product brief" in rule
    assert "character for character" in rule
    assert "shows a problem that the product brief solves" in rule
    assert "Use null if no part of the post shows such a problem" in rule


@pytest.mark.asyncio
async def test_the_founder_materials_still_come_from_the_brief():
    _, prompt = await _research({POST_QUOTE_KEY: None})
    materials = prompt[prompt.index("- founder_materials:"):]
    assert f"Copy each\n  passage from the product brief (between {BRIEF_START} and {BRIEF_END})" in materials


# ---------- the parse ----------


def test_the_quote_is_read_from_the_new_key():
    assert parse_reply_fit({POST_QUOTE_KEY: POST_TEXT}).brief_problem_span == POST_TEXT
    for value in (None, "", "   ", 17, ["a"], {"a": 1}):
        assert parse_reply_fit({POST_QUOTE_KEY: value}).brief_problem_span is None


def test_a_quote_under_the_old_key_is_ignored():
    """Only the key the prompt asks for is read: an answer under another key
    is not this request's answer, and the post then has no quote."""
    assert parse_reply_fit({"brief_problem_span": POST_TEXT}).brief_problem_span is None


@pytest.mark.asyncio
async def test_a_quote_of_the_post_passes_and_is_stored_under_the_old_field():
    evidence, _ = await _research({POST_QUOTE_KEY: POST_TEXT})
    assert evidence.reply_fit.span_check == "ok"
    assert evidence.reply_fit.brief_problem_span == POST_TEXT
    assert evidence.reply_fit.rejected_span is None


@pytest.mark.asyncio
async def test_a_quote_of_the_brief_is_still_rejected():
    """match_span stays strict: text from the brief is not in the post."""
    brief_sentence = BRIEF.split("\n")[0].strip()
    assert brief_sentence not in POST_TEXT
    evidence, _ = await _research({POST_QUOTE_KEY: brief_sentence})
    assert evidence.reply_fit.span_check == "not_in_post"
    assert evidence.reply_fit.brief_problem_span is None
    assert evidence.reply_fit.rejected_span == brief_sentence
    assert evidence.reply_fit.skip_rule == "no_problem_span"


@pytest.mark.asyncio
async def test_a_near_quote_of_the_post_is_still_rejected():
    """No fuzzy matching: a changed word fails."""
    evidence, _ = await _research({POST_QUOTE_KEY: POST_TEXT.replace("record", "log")})
    assert evidence.reply_fit.span_check == "not_in_post"


# ---------- replay ----------

# Recorded on main fd3f38e with fake adapters, synthetic data, identities
# scrubbed: a TopicWatch with a product URL in flight after its first search
# (the live watches' shape), its text-free Review waiting at the card, and a
# text-free Review skipped at research as no_problem_span because the
# research proposed a brief sentence.
MAIN_FD3F38E_HISTORIES = [
    ("topic_watch_history_main_fd3f38e_in_flight.json", "watch:test:reliability_main_fd3f38e_in_flight"),
    ("review_history_main_fd3f38e_text_free_card_ready.json", "review:test:1001"),
    ("review_history_main_fd3f38e_text_free_no_problem_span.json", "review:test:1001"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture,workflow_id", MAIN_FD3F38E_HISTORIES)
async def test_histories_recorded_before_the_change_replay(fixture, workflow_id):
    history = WorkflowHistory.from_json(workflow_id, (FIXTURES / fixture).read_text())
    await Replayer(workflows=[TopicWatchWorkflow, ReviewWorkflow]).replay_workflow(history)


def test_the_recorded_histories_are_the_live_shape():
    """Control: the watch has a product URL, its Reviews are text-free (the
    input carries no post, only its id), and one Review ended at the
    research skip for a quote that is not in the post."""
    watch = _decoded((FIXTURES / MAIN_FD3F38E_HISTORIES[0][0]).read_text())
    assert "https://github.com/example-org/acme" in watch
    for fixture, _ in MAIN_FD3F38E_HISTORIES[1:]:
        review_input = json.loads(_first_payload((FIXTURES / fixture).read_text()))
        assert review_input["post"] is None and review_input["post_id"] == "1001"
        assert review_input["product_url"] == "https://github.com/example-org/acme"
    skipped = _decoded((FIXTURES / MAIN_FD3F38E_HISTORIES[2][0]).read_text())
    assert "the quoted span is not in the post" in skipped
    ready = _decoded((FIXTURES / MAIN_FD3F38E_HISTORIES[1][0]).read_text())
    assert '"skip_reason":null' in ready


def _first_payload(history_json: str) -> str:
    """The workflow's start input, decoded."""
    import base64

    started = json.loads(history_json)["events"][0]["workflowExecutionStartedEventAttributes"]
    return base64.b64decode(started["input"]["payloads"][0]["data"]).decode()


def _decoded(history_json: str) -> str:
    """Every payload of a history, decoded, joined."""
    import base64

    out = []

    def walk(node):
        if isinstance(node, dict):
            if "data" in node and "metadata" in node and isinstance(node["data"], str):
                try:
                    out.append(base64.b64decode(node["data"]).decode())
                except Exception:
                    pass
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(history_json))
    return "\n".join(out)
