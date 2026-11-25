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


def test_nested_headings_keep_their_parents():
    """An item under "## Integrations" inside "# Not supported yet" must keep the "not"."""
    brief = (
        "# Acme Relay\n\nRelay delivers webhooks.\n\n"
        "# Not supported yet\n## Integrations\n- Slack\n\n"
        "## Regions\nOnly one region today.\n\n"
        "# Pricing\n- Free tier\n"
    )

    assert _texts(brief) == [
        ("b1", "Acme Relay", "Relay delivers webhooks."),
        ("b2", "Not supported yet > Integrations", "Slack"),
        ("b3", "Not supported yet > Regions", "Only one region today."),
        ("b4", "Pricing", "Free tier"),  # a new top-level heading closes the old path
    ]


def test_nested_list_items_keep_the_items_they_stand_under():
    brief = (
        "- Chat tools we do not plan:\n"
        "  - Teams\n"
        "    1. Channel mentions\n"
        "  - Rooms\n"
        "- Email\n"
    )

    assert [(c, t) for _, c, t in _texts(brief)] == [
        ("", "Chat tools we do not plan:"),
        ("Chat tools we do not plan", "Teams"),
        ("Chat tools we do not plan > Teams", "Channel mentions"),
        ("Chat tools we do not plan", "Rooms"),
        ("", "Email"),
    ]


def test_setext_headings_are_headings_and_rules_are_not_sentences():
    brief = (
        "Acme Relay\n==========\n\nRelay delivers webhooks.\n\n"
        "Roadmap\n-------\nSingle sign-on.\n\n"
        "---\n\n- - -\n\n***\n"
    )

    assert _texts(brief) == [
        ("b1", "Acme Relay", "Relay delivers webhooks."),
        ("b2", "Acme Relay > Roadmap", "Single sign-on."),
    ]


def test_a_colon_line_is_the_context_of_the_paragraph_that_follows_it():
    """ "Not offered yet:" must stay attached to the sentences it introduces."""
    brief = "Not offered yet:\n\nSingle sign-on for teams. Audit logs for admins.\n\nBilling is monthly."

    assert _texts(brief) == [
        ("b1", "Not offered yet", "Single sign-on for teams."),
        ("b2", "Not offered yet", "Audit logs for admins."),
        ("b3", "", "Billing is monthly."),  # the lead-in covers one paragraph
    ]


def test_a_colon_line_keeps_its_scope_without_a_blank_line_after_it():
    """The lines of one paragraph are joined, but a lead-in line is not
    joined with the first sentence it introduces."""
    brief = "Relay delivers webhooks.\nNot supported yet:\nSlack for alerts. Teams for alerts.\n"

    assert _texts(brief) == [
        ("b1", "", "Relay delivers webhooks."),
        ("b2", "Not supported yet", "Slack for alerts."),
        ("b3", "Not supported yet", "Teams for alerts."),
    ]


def test_a_lead_in_under_a_lead_in_keeps_it():
    brief = "Not supported yet:\nIntegrations:\n- Our Slack integration\n"

    assert _texts(brief) == [("b1", "Not supported yet > Integrations", "Our Slack integration")]
    # With blank lines between, and when nothing follows the second lead-in
    assert _texts("Not supported yet:\n\nIntegrations:\n\n- Slack\n") == [
        ("b1", "Not supported yet > Integrations", "Slack"),
    ]
    assert _texts("Not supported yet:\nIntegrations:\n") == [("b1", "Not supported yet", "Integrations:")]


def test_the_sentence_before_a_list_is_its_context_even_without_a_colon():
    """A bold line used as a heading, or a plain sentence that introduces the list."""
    bold = "**Not supported yet**\n- Slack\n- Teams\n"
    plain = "Relay delivers webhooks. We do not integrate with these tools.\n\n- Slack\n"

    assert _texts(bold) == [
        ("b1", "", "**Not supported yet**"),
        ("b2", "Not supported yet", "Slack"),
        ("b3", "Not supported yet", "Teams"),
    ]
    assert _texts(plain) == [
        ("b1", "", "Relay delivers webhooks."),
        ("b2", "", "We do not integrate with these tools."),
        ("b3", "We do not integrate with these tools.", "Slack"),
    ]


def test_a_sentence_is_the_context_of_the_list_right_after_it_only():
    brief = "We do not integrate with these tools.\n- Slack\n\nBilling is monthly for everyone.\n\n# Plans\n- Free tier\n"

    assert _texts(brief) == [
        ("b1", "", "We do not integrate with these tools."),
        ("b2", "We do not integrate with these tools.", "Slack"),
        ("b3", "", "Billing is monthly for everyone."),
        ("b4", "Plans", "Free tier"),  # a heading in between: the sentence does not introduce this list
    ]


def test_a_one_or_two_word_sentence_stays_with_the_sentence_before_it():
    """ "Slack." alone would lose what the sentence before says about it."""
    assert [t for _, _, t in _texts("We do not offer these yet. Slack. Teams. Billing is monthly.")] == [
        "We do not offer these yet. Slack. Teams.",
        "Billing is monthly.",
    ]
    assert _texts("Not supported yet:\nSlack. Teams.") == [("b1", "Not supported yet", "Slack. Teams.")]
    # The first sentence of a paragraph has nothing before it to join
    assert [t for _, _, t in _texts("Fast.\n\nReliable too.")] == ["Fast.", "Reliable too."]


def test_a_colon_line_that_introduces_nothing_stays_a_sentence():
    assert _texts("Billing is monthly. Pricing follows usage:") == [
        ("b1", "", "Billing is monthly."),
        ("b2", "", "Pricing follows usage:"),
    ]
    assert _texts("Pricing follows usage:\n\n# Plans\n- Free tier") == [
        ("b1", "", "Pricing follows usage:"),
        ("b2", "Plans", "Free tier"),
    ]


def test_a_list_items_continuation_lines_stay_one_sentence_with_it():
    """A wrapped "- Not supported:" must not lose hold of the names below it."""
    brief = (
        "## Integrations\n"
        "- Not supported:\n"
        "  Slack and Microsoft Teams.\n"
        "- Supported\n"
        "\n"
        "  Email, in a second paragraph of the item.\n"
        "- Webhooks\n"
        "with a lazy continuation line.\n"
        "\n"
        "A paragraph after the list.\n"
    )

    assert _texts(brief) == [
        ("b1", "Integrations", "Not supported: Slack and Microsoft Teams."),
        ("b2", "Integrations", "Supported Email, in a second paragraph of the item."),
        ("b3", "Integrations", "Webhooks with a lazy continuation line."),
        ("b4", "Integrations", "A paragraph after the list."),
    ]


def test_a_nested_item_sees_its_parents_continuation():
    brief = "- We do not plan\n  these chat tools:\n  - Teams\n"

    assert _texts(brief) == [
        ("b1", "", "We do not plan these chat tools:"),
        ("b2", "We do not plan these chat tools", "Teams"),
    ]


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


def test_a_wrapped_feature_item_gives_the_whole_term():
    """Read after its continuation line, or the term would be "Signed" and
    fire on "I signed up yesterday"."""
    brief = "Key features:\n- Signed\n  deliveries\n- Replay\n"

    assert brief_feature_terms(brief) == ["Signed deliveries", "Replay"]
    assert [t for _, _, t in _texts(brief)] == ["Signed deliveries", "Replay"]


def test_inline_feature_list_is_read():
    brief = "Key features: retries with backoff, a dead-letter queue, and replay."

    assert brief_feature_terms(brief) == ["retries with backoff", "a dead-letter queue", "replay"]


def test_feature_terms_need_the_nearest_heading_lead_in_or_parent_item_to_say_features():
    under_heading = "# Features\nPlans:\n- Free tier\n"  # the heading says it
    under_parent = "- Main features:\n  - Replay\n- Limits:\n  - Ten endpoints\n"
    far_above = "# Feature overview\n## Not supported yet\n- Single sign-on\n"

    assert brief_feature_terms(under_heading) == ["Free tier"]
    assert brief_feature_terms(under_parent) == ["Replay"]
    assert brief_feature_terms(far_above) == []  # only the nearest heading counts


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
