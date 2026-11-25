"""Deterministic triggers of the written-draft check (issue #12; ADR-0006
amendment 2026-11-24, step 2). Synthetic sentences only."""

import pytest

from backend.gate.written_check import (
    TRIGGER_COMPARISON,
    TRIGGER_FEATURE_TERM,
    TRIGGER_FIRST_PERSON_PLURAL,
    TRIGGER_NUMBER,
    TRIGGER_PRODUCT_NAME,
    clean_feature_terms,
    clean_names,
    find_triggers,
)

NAME = "Acme Relay"
ALIASES = ["Relay"]
TERMS = ["Signed deliveries", "Delivery replay"]


def _kinds(sentence, name=NAME, aliases=ALIASES, terms=TERMS):
    return {h.trigger for h in find_triggers(sentence, name, aliases, terms)}


def _matches(sentence, trigger, name=NAME, aliases=ALIASES, terms=TERMS):
    return [h.match for h in find_triggers(sentence, name, aliases, terms) if h.trigger == trigger]


# ---------- sentences that respond to the post trigger nothing ----------


@pytest.mark.parametrize("sentence", [
    "The policy is the interesting part.",
    "Does Ship let the same agent that wrote the change approve it?",
    "That is a hard thing to get right.",
    "That works as well as anything.",
    "I ran into the same wall last spring.",
    "What are you using today?",
    "Replaying events by hand gets old fast.",
])
def test_reply_sentences_without_a_trigger(sentence):
    assert _kinds(sentence) == set()


# ---------- product name and aliases ----------


def test_name_and_alias_fire_as_whole_words_in_any_case():
    assert _matches("acme relay handles that.", TRIGGER_PRODUCT_NAME) == ["Acme Relay", "Relay"]
    assert _matches("That is what RELAY does.", TRIGGER_PRODUCT_NAME) == ["Relay"]
    assert _matches("Relay's queue handles that.", TRIGGER_PRODUCT_NAME) == ["Relay"]


def test_name_does_not_fire_inside_another_word():
    assert TRIGGER_PRODUCT_NAME not in _kinds("Replaying events by hand gets old.")
    assert TRIGGER_PRODUCT_NAME not in _kinds("Relays are cheap.")


def test_name_trigger_reads_only_the_watch_fields_never_the_brief():
    """George's decision (2026-11-24): the brief may name the product any way
    it likes; the trigger knows the name only from the two form fields."""
    sentence = "Tightrope handles that."

    assert TRIGGER_PRODUCT_NAME not in _kinds(sentence, name="Acme Relay", aliases=[], terms=TERMS)
    assert TRIGGER_PRODUCT_NAME in _kinds(sentence, name="Acme Relay", aliases=["Tightrope"], terms=TERMS)
    # find_triggers has no brief parameter at all
    assert "brief" not in find_triggers.__code__.co_varnames


def test_without_a_product_name_the_name_trigger_cannot_fire():
    assert TRIGGER_PRODUCT_NAME not in _kinds("Relay handles that.", name="", aliases=[])


def test_a_digit_in_the_product_name_is_not_a_number():
    hits = find_triggers("Relay2 handles that.", "Relay2", [], [])

    assert [(h.trigger, h.match) for h in hits] == [(TRIGGER_PRODUCT_NAME, "Relay2")]
    assert TRIGGER_NUMBER in {h.trigger for h in find_triggers("Relay2 retries 3 times.", "Relay2", [], [])}


@pytest.mark.parametrize("name,sentence", [
    ("++", "I think ++ handles that."),
    (chr(0x1F680), "Try " + chr(0x1F680) + " for that."),  # a name that is one emoji
    ("@acme", "Ask @acme about it."),
    (".NET", "Use .NET for that."),
])
def test_a_name_without_letters_or_with_symbols_still_fires(name, sentence):
    """A name made of symbols has no words to match on; it is matched as written."""
    assert _matches(sentence, TRIGGER_PRODUCT_NAME, name=name, aliases=[]) == [name]


def test_a_name_that_is_itself_a_number_does_not_hide_a_quantity():
    """A product called "One": "one delivery" could be the name or a number,
    so nothing is exempt. A name with a digit in it ("Relay2") still is."""
    hits = find_triggers("One retries one delivery.", "One", [], [])

    assert [(h.trigger, h.match) for h in hits] == [(TRIGGER_PRODUCT_NAME, "One"), (TRIGGER_NUMBER, "one")]
    assert TRIGGER_NUMBER in {h.trigger for h in find_triggers("37 handles that.", "37", [], [])}
    assert TRIGGER_NUMBER not in {h.trigger for h in find_triggers("Relay2 handles that.", "Relay2", [], [])}


def test_a_numeric_alias_does_not_take_the_exemption_from_another_name():
    """Each matched name decides for itself: "Relay2" keeps its digit although
    the alias "One" exempts nothing."""
    plain = find_triggers("Relay2 retries failed deliveries.", "Relay2", ["One"], [])
    both = find_triggers("Relay2, or One, retries one delivery.", "Relay2", ["One"], [])

    assert [(h.trigger, h.match) for h in plain] == [(TRIGGER_PRODUCT_NAME, "Relay2")]
    assert [(h.trigger, h.match) for h in both] == [
        (TRIGGER_PRODUCT_NAME, "Relay2"), (TRIGGER_PRODUCT_NAME, "One"), (TRIGGER_NUMBER, "one"),
    ]


def test_clean_names_trims_and_drops_blanks_and_repeats():
    assert clean_names("  Acme   Relay ", [" Relay", "relay", "", "acme relay", "AR"]) == (
        "Acme Relay", ["Relay", "AR"]
    )
    assert clean_names("", None) == ("", [])


# ---------- first-person plural ----------


@pytest.mark.parametrize("sentence,word", [
    ("We ran into that too.", "we"),
    ("Our default went the other way.", "our"),
    ("Many teams came to us for this.", "us"),
    ("Ours does it differently.", "ours"),
    ("We're still tuning it.", "we"),
    ("We built it ourselves.", "we"),
])
def test_first_person_plural_fires(sentence, word):
    assert word in _matches(sentence, TRIGGER_FIRST_PERSON_PLURAL)


@pytest.mark.parametrize("sentence", [
    "Your tour of the options was useful.",  # "our" inside "your", "tour"
    "Focus on the queue first.",  # "us" inside "focus"
    "The weather was fine.",  # "we" inside "weather"
    "I like the idea.",
])
def test_first_person_plural_needs_a_whole_word(sentence):
    assert TRIGGER_FIRST_PERSON_PLURAL not in _kinds(sentence)


def test_look_alike_letters_and_invisible_characters_do_not_hide_a_trigger():
    # Written with chr() so the odd characters are visible in the source
    cyrillic_o = chr(0x043E) + "urs is quicker."  # Cyrillic small o, not Latin o
    zero_width = "o" + chr(0x200B) + "ur default is different."  # zero-width space inside "our"
    fullwidth = chr(0xFF37) + chr(0xFF45) + " handle that."  # fullwidth "We"

    assert TRIGGER_FIRST_PERSON_PLURAL in _kinds(cyrillic_o)
    assert TRIGGER_FIRST_PERSON_PLURAL in _kinds(zero_width)
    assert TRIGGER_FIRST_PERSON_PLURAL in _kinds(fullwidth)


# ---------- numbers ----------


@pytest.mark.parametrize("sentence,match", [
    ("It is 10x quicker.", "10x"),
    ("That took 3 retries.", "3"),
    ("The product has twenty integrations.", "twenty"),
    ("Uptime was 99.9% last month.", "99"),
    ("It costs $5.", "5"),
    ("Dozens of teams do this.", "dozens"),
    ("The p99 was fine.", "p99"),
    ("Zero downtime is the goal.", "zero"),
    ("It is twice as fast.", "twice"),
    ("That cut the queue in half.", "half"),
    ("It has half the latency.", "half"),
    ("Throughput doubled.", "doubled"),
    # Ordinals, fractions and "single"/"couple" are numbers too ("any number")
    ("It was the first to ship that.", "first"),
    ("A quarter of the calls fail.", "quarter"),
    ("A single command rolls it back.", "single"),
    ("It takes a couple of minutes.", "couple"),
])
def test_numbers_fire(sentence, match):
    assert match in _matches(sentence, TRIGGER_NUMBER)


@pytest.mark.parametrize("sentence", [
    "It takes one minute.",
    "One command rolls it back.",
    # The amendment says "any number", and code cannot tell the pronoun from
    # the quantity: these are checked too. The drafter is told to avoid them.
    "Which one are you on?",
    "No one warns you about that.",
])
def test_one_counts_as_a_number(sentence):
    assert _matches(sentence, TRIGGER_NUMBER) == ["one"]


@pytest.mark.parametrize("sentence", ["Someone should fix that.", "Anyone can hit this.", "That alone is hard."])
def test_one_inside_another_word_is_not_a_number(sentence):
    assert TRIGGER_NUMBER not in _kinds(sentence)


# ---------- comparisons ----------


@pytest.mark.parametrize("sentence", [
    "I think it's way cheaper.",
    "It is faster than polling.",
    "Polling vs webhooks is an old fight.",
    "Compared to polling, that is quiet.",
    "Unlike polling, that scales.",
    "It is as fast as anything.",
    "That is the same as polling.",
    "This is the best way.",
    "It outperforms polling.",
])
def test_comparisons_fire(sentence):
    assert TRIGGER_COMPARISON in _kinds(sentence)


@pytest.mark.parametrize("sentence", [
    "I ran into the same wall last spring.",
    "Same here.",
    "That works as well as anything.",
    "As soon as it broke, I knew.",
    "Tell me more.",
    "Most of that is familiar.",
])
def test_common_reply_phrases_are_not_comparisons(sentence):
    assert TRIGGER_COMPARISON not in _kinds(sentence)


# ---------- feature terms (the watch's "Key features") ----------


def test_feature_terms_fire_as_whole_phrases():
    assert _matches("Signed deliveries would catch that.", TRIGGER_FEATURE_TERM) == ["Signed deliveries"]
    assert _matches("A signed delivery would catch that.", TRIGGER_FEATURE_TERM) == ["Signed deliveries"]
    assert _matches("DELIVERY REPLAY helps there.", TRIGGER_FEATURE_TERM) == ["Delivery replay"]


def test_feature_terms_do_not_fire_on_parts_or_other_words():
    assert TRIGGER_FEATURE_TERM not in _kinds("Deliveries were late all week.")
    assert TRIGGER_FEATURE_TERM not in _kinds("I signed up for the delivery plan.")
    assert TRIGGER_FEATURE_TERM not in _kinds("Signed deliveries would catch that.", terms=[])
    assert TRIGGER_FEATURE_TERM not in _kinds("Signed deliveries would catch that.", terms=None)


def test_key_features_are_cleaned_like_the_names():
    """The watch's "Key features" as entered: trimmed, in order, without
    blanks, repeats or entries that have no word to match on."""
    assert clean_feature_terms(None) == []
    assert clean_feature_terms([]) == []
    assert clean_feature_terms(
        ["  Signed   deliveries ", "", "signed deliveries", "Delivery replay", "--", None, "S3 export"]
    ) == ["Signed deliveries", "Delivery replay", "S3 export"]


def test_a_key_feature_is_matched_as_normalized_whole_words():
    terms = ["Dead-letter queue", "S3 export"]

    assert _matches("A dead letter queue would hold those.", TRIGGER_FEATURE_TERM, terms=terms) == ["Dead-letter queue"]
    assert _matches("Dead-letter queues hold those.", TRIGGER_FEATURE_TERM, terms=terms) == ["Dead-letter queue"]
    # Fullwidth letters fold to the same words (NFKC), as in the claim gate
    assert _matches("The ｓ３ export covers that.", TRIGGER_FEATURE_TERM, terms=terms) == ["S3 export"]
    assert TRIGGER_FEATURE_TERM not in _kinds("The queue filled up with dead jobs.", terms=terms)


@pytest.mark.parametrize("term,sentence", [
    ("Delivery status", "Delivery statuses stay visible."),
    ("Delivery statuses", "The delivery status stays visible."),
    ("Approval process", "Approval processes differ per team."),
    ("Approval processes", "The approval process is the hard part."),
    ("Audit log", "Audit logs help there."),
    ("Audit logs", "An audit log helps there."),
    ("Dead-letter box", "Dead letter boxes fill up."),
    ("Retry policy", "Retry policies matter here."),
    ("Retry policies", "A retry policy matters here."),
    ("Safe house", "Safe houses are rare."),
    ("Bus topology", "Buses topology is odd wording."),
    ("Team address", "Team addresses change."),
    # No word is too short to have a plural
    ("Web UI", "Web UIs support approvals."),
    ("Web UIs", "The web UI supports approvals."),
    ("User ID", "User IDs are enough."),
    ("User IDs", "A user ID is enough."),
    ("PR check", "PRs checks is odd wording."),
    ("Sign-up", "Sign-ups doubled."),
    ("OS image", "OSes image is odd wording."),
    ("UI", "The UI's layout helps."),  # a possessive: "ui", then "s"
])
def test_a_key_feature_matches_its_plain_plural_and_singular(term, sentence):
    """Whichever form the founder entered: -s, -es (also after s, ss and us) and -ies."""
    assert _matches(sentence, TRIGGER_FEATURE_TERM, terms=[term]) == [term]


def test_a_plural_of_a_key_feature_forces_the_check():
    """The trigger is what makes code require a verdict from the checker."""
    from backend.gate.brief import split_brief
    from backend.gate.written_check import prepare_check

    brief = split_brief("The web UI supports approvals.")
    prepared = prepare_check("Web UIs support approvals.", brief, "Acme", [], ["Web UI"])

    assert prepared.needs_checker
    assert prepared.request().must_verify == [True]
    assert prepare_check("Web UIs support approvals.", brief, "Acme", [], []).request().must_verify == [False]


@pytest.mark.parametrize("term,sentence", [
    ("Delivery status", "The delivery state stays visible."),
    ("Audit log", "We audit login attempts by hand."),
    ("Replay", "Replaying events by hand gets old fast."),
    ("UI", "The build is quick."),  # "ui" inside "build" and "quick" is not the word
    ("ID", "I did the same thing."),
])
def test_plural_folding_does_not_match_other_words(term, sentence):
    assert TRIGGER_FEATURE_TERM not in _kinds(sentence, terms=[term])


def test_another_spelling_of_a_key_feature_is_another_entry():
    """Whole words in the order entered: a closed compound or "and" for "&"
    is a different term, which the founder lists as its own key feature."""
    assert TRIGGER_FEATURE_TERM not in _kinds("Realtime monitoring helps.", terms=["Real-time monitoring"])
    assert TRIGGER_FEATURE_TERM not in _kinds("Drag and drop helps.", terms=["Drag & drop"])
    assert _matches(
        "Realtime monitoring helps.", TRIGGER_FEATURE_TERM, terms=["Real-time monitoring", "Realtime monitoring"]
    ) == ["Realtime monitoring"]
    # Punctuation between the words is not part of the term
    assert _matches("Real time monitoring helps.", TRIGGER_FEATURE_TERM, terms=["Real-time monitoring"]) == [
        "Real-time monitoring"
    ]
    assert _matches("Drag & drop helps.", TRIGGER_FEATURE_TERM, terms=["Drag and drop"]) == []


def test_plural_folding_may_match_an_unrelated_word_and_that_only_adds_a_check():
    """The price of having no word list: "it" and "its" share a form. The
    feature-term trigger exempts nothing, so the sentence is only checked."""
    hits = find_triggers("Its shape is odd.", NAME, ALIASES, ["It"])

    assert [(h.trigger, h.match) for h in hits] == [(TRIGGER_FEATURE_TERM, "It")]


def test_the_product_name_is_not_plural_folded():
    """Only key features are: "Relays" is not the product "Relay"."""
    assert TRIGGER_PRODUCT_NAME not in _kinds("Relays are old hardware.", name="Relay", aliases=[], terms=[])


def test_a_long_key_feature_is_kept_and_only_adds_a_check():
    """The API refuses an entry of more than six words. One that reaches the
    gate another way is not cut: it can only add a sentence to the checked set."""
    long_term = "retries with backoff and a dead-letter queue"

    assert clean_feature_terms([long_term]) == [long_term]
    assert _matches(
        "Retries with backoff and a dead letter queue cover that.", TRIGGER_FEATURE_TERM, terms=[long_term]
    ) == [long_term]


# ---------- several triggers at once ----------


def test_every_trigger_that_fires_is_reported_once():
    hits = find_triggers("Hi! Ours is 10x quicker, and ours is 10x quicker.", NAME, ALIASES, TERMS)

    assert [(h.trigger, h.match) for h in hits] == [
        (TRIGGER_FIRST_PERSON_PLURAL, "ours"),
        (TRIGGER_NUMBER, "10x"),
        (TRIGGER_COMPARISON, "quicker"),
    ]
