"""The product brief as numbered sentences, and its feature terms (issue #12).

Synthetic briefs only.
"""

from backend.gate.brief import brief_feature_terms, render_brief_sentence, split_brief
from backend.gate.sentence_boundary import merge_at_unclear_boundaries


def _texts(brief):
    return [(s.id, s.context, s.text) for s in split_brief(brief)]


def test_prose_is_split_into_sentences_with_ids_in_order(product_brief):
    sentences = split_brief(product_brief)

    assert [s.id for s in sentences] == [f"b{i}" for i in range(1, len(sentences) + 1)]
    assert sentences[0].text == "Our product helps developers build reliable distributed systems."
    assert sentences[1].text == "Our product provides automatic failover."
    assert sentences[2].text == "Our product guarantees strong consistency."
    assert sentences[-1].text == "We focus on reliability and ease of use."


def test_a_heading_is_context_not_a_sentence(product_brief):
    sentences = split_brief(product_brief)

    assert all(s.text != "Product Brief" for s in sentences)
    assert sentences[0].context == "Product Brief"


def test_a_list_item_is_one_sentence_under_its_heading_and_lead_in(product_brief):
    items = [s for s in split_brief(product_brief) if "Key features" in s.context]

    assert [s.text for s in items] == [
        "Automatic failover",
        "Strong consistency guarantees",
        "Real-time monitoring",
    ]
    assert all(s.context == "Product Brief > Key features" for s in items)
    # The lead-in line is carried by its items, not listed on its own
    assert all(s.text != "Key features:" for s in split_brief(product_brief))


def test_an_item_under_a_negative_heading_keeps_the_heading():
    brief = "## Not supported yet\nIntegrations:\n- Single sign-on\n"

    [item] = split_brief(brief)

    assert item.context == "Not supported yet > Integrations"
    assert render_brief_sentence(item) == "[b1] (Not supported yet > Integrations) Single sign-on"


def test_a_colon_line_that_introduces_no_list_stays_a_sentence():
    brief = "Pricing follows usage:\n\nYou pay per delivery."

    assert [t for _, _, t in _texts(brief)] == ["Pricing follows usage:", "You pay per delivery."]


def test_a_brief_sentence_is_never_a_fragment_at_an_unclear_boundary():
    """split_into_sentences cuts after "U.S." before a sentence starter; as
    evidence the two pieces stay together (merge_at_unclear_boundaries)."""
    brief = "We sell in the U.S. The Netherlands is next. Billing is monthly."

    assert [t for _, _, t in _texts(brief)] == [
        "We sell in the U.S. The Netherlands is next.",
        "Billing is monthly.",
    ]
    assert merge_at_unclear_boundaries(["Plain end.", "Next one."]) == ["Plain end.", "Next one."]


def test_lines_of_one_paragraph_are_joined_and_blank_lines_separate():
    brief = "Relay delivers\nwebhooks.\n\nIt retries failures."

    assert [t for _, _, t in _texts(brief)] == ["Relay delivers webhooks.", "It retries failures."]


def test_empty_brief_has_no_sentences():
    assert split_brief("") == []
    assert split_brief("   \n\n") == []
    assert brief_feature_terms("") == []


def test_split_is_deterministic(product_brief):
    assert split_brief(product_brief) == split_brief(product_brief)


# ---------- feature terms ----------


def test_feature_terms_are_the_items_of_a_features_list(product_brief):
    assert brief_feature_terms(product_brief) == [
        "Automatic failover",
        "Strong consistency guarantees",
        "Real-time monitoring",
    ]


def test_feature_terms_come_from_a_features_heading_too():
    brief = "## Features\n- **Signed deliveries** - every request carries a signature\n- `Replay`: resend a delivery\n"

    assert brief_feature_terms(brief) == ["Signed deliveries", "Replay"]


def test_inline_feature_list_is_read():
    brief = "Key features: retries with backoff, a dead-letter queue, and replay."

    assert brief_feature_terms(brief) == ["retries with backoff", "a dead-letter queue", "replay"]


def test_lists_that_are_not_feature_lists_give_no_terms():
    brief = "## Not supported yet\n- Single sign-on\n\nWho it is for:\n- Small teams\n"

    assert brief_feature_terms(brief) == []


def test_long_items_and_repeats_are_not_terms():
    brief = (
        "## Features\n"
        "- A very long item that describes something in many many words overall\n"
        "- Replay\n"
        "- replay\n"
    )

    assert brief_feature_terms(brief) == ["Replay"]


def test_prose_is_never_a_source_of_feature_terms():
    """The trigger must be predictable from what the founder listed."""
    brief = "Our product has automatic failover and real-time monitoring as features."

    assert brief_feature_terms(brief) == []
