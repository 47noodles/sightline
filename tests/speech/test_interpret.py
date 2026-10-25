"""Arbitration between commands, dictation and nothing (ARCHITECTURE.md section 8)."""

from __future__ import annotations

import pytest

from sightline.speech.interpret import Situation, is_risky, normalize_combo


# ---------------------------------------------------------------------- modes


def test_auto_mode_command_without_text_field(decide) -> None:
    d = decide("scroll down")
    assert d.kind == "command" and d.method == "exact"


def test_auto_mode_types_sentences_in_a_text_field(decide) -> None:
    assert decide("I will be there in ten minutes", editable=True).kind == "dictation"
    assert decide("I will be there in ten minutes").kind == "rejected"


def test_auto_mode_whole_commands_still_run_in_a_text_field(decide) -> None:
    assert decide("select all", editable=True).kind == "command"
    assert decide("press enter", editable=True).kind == "command"
    assert decide("new line", editable=True).kind == "command"


def test_free_text_commands_give_way_to_typing_in_a_text_field(decide) -> None:
    # "find ..." and "search for ..." read like sentences; in a text field they are typed.
    assert decide("search for the best pizza in town", editable=True).kind == "dictation"
    assert decide("search for the best pizza in town").kind == "command"
    assert decide("type search for cats", editable=True).kind == "command"  # "type" always types


def test_command_mode_never_types(decide) -> None:
    assert decide("I will be there in ten minutes", mode="command", editable=True).kind == "rejected"
    assert decide("type hello", mode="command", editable=True).kind == "command"


def test_dictation_mode_types_everything_but_escapes(decide) -> None:
    assert decide("scroll down", mode="dictation", editable=True).kind == "dictation"
    assert decide("open notepad", mode="dictation", editable=True).kind == "dictation"
    for escape in ("stop dictation", "command mode", "scratch that", "go to sleep"):
        assert decide(escape, mode="dictation", editable=True).kind == "command", escape


def test_sleep_mode_only_wakes(decide, bctx) -> None:
    bctx.current_mode = "sleep"
    assert decide("scroll down", mode="sleep").kind == "ignored"
    assert decide("type hello", mode="sleep").kind == "ignored"
    wake = decide("wake up", mode="sleep")
    assert wake.kind == "command"
    assert wake.outcomes[0].actions[0].args == {"mode": "auto"}
    assert decide("wake upp", mode="sleep").kind == "command"  # near enough to wake


def test_noise_is_ignored_silently(decide) -> None:
    d = decide("you", audio_ms=200)
    assert d.kind == "ignored" and d.reason == ""
    assert decide("[MUSIC]").kind == "ignored"


def test_password_fields_still_take_dictation(decide) -> None:
    assert decide("hunter two", editable=True, password=True).kind == "dictation"


# ---------------------------------------------------------------------- recogniser slips


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("Scrolled down.", "mouse.scroll"),
        ("write click", "mouse.right_click"),
        ("I'll tab.", "key.combo"),
        ("Undue.", "edit.undo"),
        ("Hold ship.", "key.hold"),
        ("Re-centered.", "pointer.recenter"),
        ("Word write three.", "edit.word_right"),
        ("Let's scroll down.", "mouse.scroll"),
        ("It's scroll up five.", "mouse.scroll"),
        ("Save please.", "edit.save"),
        ("Just zoom in.", "browser.zoom_in"),
        ("scrol down", "mouse.scroll"),  # fuzzy
        ("selec all", "edit.select_all"),
    ],
)
def test_common_slips_still_work(decide, heard: str, expected: str) -> None:
    d = decide(heard, mode="command")
    assert d.kind == "command", d
    assert d.parse.matches[0].spec.id == expected


def test_lead_in_words_are_kept_in_text_fields(decide) -> None:
    # "Let's go back" is a sentence when typing, not the browser's Back button.
    assert decide("Let's go back.", editable=True).kind == "dictation"
    assert decide("Let's go back.").kind == "command"


def test_opposites_are_not_guessed(decide) -> None:
    # "hot keyboard" is about as close to "hide keyboard" as to "show keyboard": ask, don't guess.
    d = decide("hot keyboard", mode="command")
    assert d.kind == "rejected"
    assert {m.outcome.display for m in d.alternatives} >= {"Keyboard shown", "Keyboard hidden"}


def test_modifier_without_its_key_is_not_pressed(decide) -> None:
    # "press control shift t" heard as "press control shift": pressing ctrl+shift alone would do nothing useful.
    assert decide("Press control shift.", mode="command").kind == "rejected"
    assert decide("press windows", mode="command").kind == "command"


# ---------------------------------------------------------------------- attention gate


AWAY = "Ignored: you were looking away"


def test_away_allows_only_the_safety_commands(decide) -> None:
    # Review S-01: a voice on a call or in a video may say a command exactly, so while the user looks
    # away only what stops, pauses, cancels or undoes runs.
    for heard in ("stop", "cancel", "undo that", "pause eyes", "resume eyes", "help", "go to sleep"):
        d = decide(heard, away=AWAY)
        assert d.kind == "command", heard
    for heard in (
        "scroll down",
        "select all and copy",
        "open command prompt",
        "press enter",
        "close window",
        "paste",
        "go to evil dot com",
        "open settings",
        "type calc dot exe",
        "scrol down",
        "Let's scroll down.",
    ):
        d = decide(heard, away=AWAY)
        assert d.kind == "ignored" and d.reason == AWAY, heard
    # Not even mixed into a chain with a safety command.
    assert decide("stop and close window", away=AWAY).kind == "ignored"


def test_away_in_dictation_lets_only_safety_commands_through(decide) -> None:
    assert decide("scratch that", mode="dictation", editable=True, away=AWAY).kind == "ignored"
    assert decide("go to sleep", mode="dictation", editable=True, away=AWAY).kind == "command"


def test_facing_the_screen_runs_exact_commands_as_before(decide) -> None:
    assert decide("scroll down").kind == "command"
    assert decide("select all and copy").kind == "command"


def test_away_never_types(decide) -> None:
    d = decide("I'll call you back later", editable=True, away=AWAY)
    assert d.kind == "ignored" and d.reason == AWAY
    d = decide("I'll call you back later", mode="dictation", editable=True, away=AWAY)
    assert d.kind == "ignored"
    # Even a free-text command that would have been typed in the field.
    assert decide("search for cheap flights to rome", editable=True, away=AWAY).kind == "ignored"


def test_away_wake_needs_the_exact_phrase(decide, bctx) -> None:
    bctx.current_mode = "sleep"
    assert decide("wake upp", mode="sleep", away=AWAY).kind == "ignored"
    assert decide("wake up", mode="sleep", away=AWAY).kind == "command"


def test_away_blocks_paraphrases(semantic_interp, bctx) -> None:
    here = semantic_interp.decide("further down please", Situation(mode="command"), bctx)
    assert here.kind == "command" and here.method == "semantic"
    away = semantic_interp.decide("further down please", Situation(mode="command", away=AWAY), bctx)
    assert away.kind == "ignored"


# ---------------------------------------------------------------------- paraphrases and risky actions


def test_paraphrase_runs_a_safe_command(semantic_interp, bctx) -> None:
    d = semantic_interp.decide("raise the volume", Situation(mode="command"), bctx)
    assert d.kind == "command" and d.method == "semantic"
    assert d.outcomes[0].actions[0].name == "system.volume"


@pytest.mark.parametrize("heard", ["get rid of this app", "make this window go away", "shut this tab"])
def test_paraphrase_never_runs_a_risky_command(semantic_interp, bctx, heard: str) -> None:
    d = semantic_interp.decide(heard, Situation(mode="command"), bctx)
    assert d.kind == "rejected", d
    assert d.reason.startswith("Say 1 to confirm")
    assert len(d.alternatives) == 1 and is_risky(d.alternatives[0])


def test_exact_risky_commands_run(decide) -> None:
    assert decide("close window").kind == "command"
    assert decide("close tab").kind == "command"
    assert decide("press alt f4").kind == "command"


@pytest.mark.parametrize(
    ("combo", "risky"),
    [("alt+f4", True), ("ctrl+w", True), ("shift+ctrl+w", True), ("ctrl+f4", True), ("delete", True), ("shift+delete", True), ("ctrl+enter", True), ("alt+s", True), ("ctrl+c", False), ("enter", False)],
)
def test_risky_key_combos(combo: str, risky: bool) -> None:
    from sightline.core.types import Action
    from sightline.speech.grammar import CommandSpec, Outcome
    from sightline.speech.matcher import Match

    dummy = CommandSpec("custom.test", "Keys", ("x",), lambda s, c: Outcome())
    m = Match(dummy, {}, Outcome((Action("key.press", {"combo": combo}),)), "semantic", 0.7, "x")
    assert is_risky(m) is risky
    assert normalize_combo("Shift+Ctrl+W") == "ctrl+shift+w"


# ---------------------------------------------------------------------- did you mean


def test_near_misses_are_offered(decide) -> None:
    d = decide("scold down", mode="command")  # 86% like "scroll down": too far to run, close enough to offer
    assert d.kind == "rejected"
    assert d.alternatives
    first = d.alternatives[0].outcome.actions[0]
    assert (first.name, first.args) == ("mouse.scroll", {"direction": "down"})


def test_suggestions_are_runnable_and_distinct(decide) -> None:
    d = decide("show the grip", mode="command")
    labels = [m.outcome.display for m in d.alternatives]
    assert len(labels) == len(set(labels)) <= 3
    for m in d.alternatives:
        assert m.outcome.actions or m.outcome.type_request


def test_no_suggestions_for_long_sentences(decide) -> None:
    d = decide("the weather was really lovely down by the river yesterday afternoon", mode="command")
    assert d.kind == "rejected" and d.alternatives == ()


def test_choices_context_takes_bare_numbers(decide) -> None:
    d = decide("two", mode="command", choices=3)
    assert d.kind == "command"
    assert d.parse.matches[0].spec.id == "speech.choose"
    assert d.outcomes[0].actions[0].args == {"index": 2}
    for heard, index in (("1", 1), ("the first one", 1), ("second", 2), ("number three", 3), ("option 2", 2)):
        d = decide(heard, mode="command", choices=3)
        assert d.outcomes[0].actions[0].args == {"index": index}, heard


def test_choice_number_beyond_the_list_means_something_else(decide) -> None:
    d = decide("three", mode="command", overlay="labels", choices=2)
    assert d.outcomes[0].actions[0].name == "ui.labels.select"
    assert decide("two", mode="command").kind == "rejected"  # nothing offered


# ---------------------------------------------------------------------- personal aliases


def _actions(d) -> list:
    return [(a.name, dict(a.args)) for o in d.outcomes for a in o.actions]


def test_aliases_are_tried_first(grammar, bctx) -> None:
    from sightline.speech.interpret import Interpreter

    interp = Interpreter(grammar, aliases={"scold down": "scroll down", "copy": "paste"})
    d = interp.decide("Scold down.", Situation(mode="command"), bctx, semantic=False)
    assert d.kind == "command" and d.method == "alias"
    assert _actions(d) == [("mouse.scroll", {"direction": "down"})]
    # A helper may even point a built-in phrase somewhere else...
    assert _actions(interp.decide("copy", Situation(mode="command"), bctx, semantic=False)) == [("key.press", {"combo": "ctrl+v"})]
    # ...but not what an open overlay takes: "next" in the settings window stays the next setting.
    interp = Interpreter(grammar, aliases={"next": "next track"})
    assert _actions(interp.decide("next", Situation(mode="command", overlay="settings"), bctx, semantic=False)) == [("ui.settings.next", {})]
    assert _actions(interp.decide("next", Situation(mode="command"), bctx, semantic=False)) == [("system.media", {"key": "next"})]


def test_aliases_stay_out_of_text_fields_and_dictation(grammar, bctx) -> None:
    from sightline.speech.interpret import Interpreter

    interp = Interpreter(grammar, aliases={"scold down": "scroll down"})
    assert interp.decide("scold down", Situation(mode="auto", editable=True), bctx, semantic=False).kind == "dictation"
    assert interp.decide("scold down", Situation(mode="dictation", editable=True), bctx, semantic=False).kind == "dictation"
    assert interp.decide("scold down", Situation(mode="command", editable=True), bctx, semantic=False).kind == "command"
    # Learned, not exact: like a near miss, it does not run from talk that was not for the computer.
    assert interp.decide("scold down", Situation(mode="auto", away=AWAY), bctx, semantic=False).kind == "ignored"


def test_an_alias_for_something_hard_to_undo_asks_first(grammar, bctx) -> None:
    from sightline.speech.interpret import Interpreter

    interp = Interpreter(grammar, aliases={"clothes window": "close window"})
    d = interp.decide("clothes window", Situation(mode="command"), bctx, semantic=False)
    assert d.kind == "rejected" and d.reason == "Say 1 to confirm: Close window"
    assert d.alternatives[0].outcome.actions[0].name == "window.close"


def test_an_alias_works_where_its_command_does(grammar, bctx) -> None:
    from sightline.speech.interpret import Interpreter

    interp = Interpreter(grammar, aliases={"twelf": "12"})
    assert _actions(interp.decide("twelf", Situation(mode="command", overlay="labels"), bctx, semantic=False)) == [("ui.labels.select", {"number": 12})]
    assert interp.decide("twelf", Situation(mode="command"), bctx, semantic=False).kind == "rejected"


def test_yes_takes_an_offer_and_nothing_else(decide) -> None:
    d = decide("yes", mode="command", choices=1, offer=True)
    assert d.kind == "command" and _actions(d) == [("speech.choose", {"index": 1})]
    assert _actions(decide("yes please", mode="command", choices=1, offer=True)) == [("speech.choose", {"index": 1})]
    assert _actions(decide("one", mode="command", choices=1, offer=True)) == [("speech.choose", {"index": 1})]
    # An overlay's numbers come before the offer's: the corrected click may have opened the chooser.
    assert _actions(decide("1", mode="command", overlay="chooser", choices=1, offer=True)) == [("ui.chooser.select", {"number": 1})]
    assert decide("yes", mode="command").kind == "rejected"
    assert decide("yes", mode="command", choices=3).kind == "rejected"  # "did you mean" needs a number
    # Talk to someone else is full of "yes".
    d = decide("yes", mode="command", choices=1, offer=True, away=AWAY)
    assert d.kind == "ignored" and d.reason == AWAY
