"""The speech service with fake desktop and vision: arbitration, aiming, attention, "did you mean", scratch."""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np
import pytest

from sightline.core.actions import ActionError
from sightline.core.events import ClickPerformed, IntentResult, MicLevel, Notice, SpeechStatus, TranscriptFinal, TranscriptPartial
from sightline.core.types import PRIVATE, Action, AppInfo, Attention, FocusInfo, Rect, SpeechMode, WindowInfo
from sightline.speech.pipeline import Utterance

from speech_helpers import FakeMicrophone

EDIT = FocusInfo(hwnd=7, process="notepad.exe", window_title="Untitled - Notepad", control_type="Edit", name="Text editor", editable=True)
PASSWORD = FocusInfo(hwnd=8, process="chrome.exe", window_title="Sign in", control_type="Edit", name="Password", editable=True, password=True)


@pytest.fixture
def svc(ctx, monkeypatch):
    import sightline.speech.service as service_module

    monkeypatch.setattr(service_module, "Microphone", FakeMicrophone)
    ctx.desktop.apps.items.append(AppInfo("Spotify", r"C:\fake\Spotify.lnk"))
    ctx.desktop.windows.windows = [WindowInfo(101, "Untitled - Notepad", "notepad.exe", Rect(0, 0, 800, 600))]
    from sightline.speech import create

    service = create(ctx)
    yield service
    service.stop()


@pytest.fixture
def events(ctx):
    seen: dict[type, list[Any]] = {}

    def record(event: object) -> None:
        seen.setdefault(type(event), []).append(event)

    ctx.bus.subscribe(object, record)
    return seen


@pytest.fixture
def pressed(ctx):
    """Key presses run synchronously (scratch uses execute_now)."""
    keys: list[Action] = []
    ctx.actions.register("key.press", keys.append)
    return keys


def results(events) -> list[IntentResult]:
    return events.get(IntentResult, [])


def last(events) -> IntentResult:
    return results(events)[-1]


def names(result: IntentResult) -> list[str]:
    return [a.name for a in result.actions]


def utterance(text: str, uid: int = 900, *, onset: float | None = None, audio_ms: float = 900.0, pauses: tuple[float, ...] = ()) -> Utterance:
    start = time.perf_counter() - 1.5 if onset is None else onset
    return Utterance(uid, text, audio_ms, 250.0, 20.0, start + 1.0, time.perf_counter(), False, t_speech_start=start, t_voice_start=start + 0.3, pauses_ms=pauses)


# ---------------------------------------------------------------------- basics


def test_feed_text_runs_a_command(svc, events) -> None:
    svc.feed_text("scroll down three")
    r = last(events)
    assert r.kind == "command"
    assert r.display == "Scroll down 3"
    assert [(a.name, dict(a.args)) for a in r.actions] == [("mouse.scroll", {"direction": "down", "amount": 9})]
    assert r.actions[0].source == "voice"
    assert r.actions[0].utterance == "scroll down three"
    assert r.latency_ms >= 0


def test_not_understood_display(svc, events) -> None:
    svc.feed_text("blah")
    r = last(events)
    assert r.kind == "rejected"
    assert r.display == "Not understood: 'blah'"
    assert r.actions == ()


def test_dictation_display_and_spacing(svc, events, ctx) -> None:
    ctx.desktop.uia.focus = EDIT
    ctx.desktop.uia.caret_text = "Dear Sam"
    svc.feed_text("comma new line thanks for the report full stop")
    r = last(events)
    assert r.kind == "dictation"
    assert r.actions[0].args["text"] == ",\nThanks for the report."
    assert r.display == "Typed: , Thanks for the report."


def test_typed_text_uses_own_record_when_the_field_cannot_be_read(svc, events, ctx) -> None:
    ctx.desktop.uia.focus = EDIT
    ctx.desktop.uia.caret_text = None  # UI Automation cannot read this field
    svc.feed_text("hello there full stop")
    svc.feed_text("how are you")
    assert [r.actions[0].args["text"] for r in results(events)] == ["Hello there.", " How are you"]


def test_scroll_step_follows_desktop_setting(svc, events, ctx) -> None:
    ctx.settings.desktop.scroll_step = 5
    ctx.settings_changed("desktop", save=False)
    svc.feed_text("scroll up two")
    assert last(events).actions[0].args["amount"] == 10


def test_names_come_from_the_desktop(svc, events) -> None:
    svc.feed_text("open spotify")
    assert dict(last(events).actions[0].args) == {"name": "Spotify"}
    svc.feed_text("switch to notepad")
    assert dict(last(events).actions[0].args) == {"name": "Untitled - Notepad", "hwnd": 101}


def test_utterance_ids_share_one_sequence(svc, events) -> None:
    svc.feed_text("copy")
    svc.feed_text("paste")
    ids = [r.utterance_id for r in results(events)]
    assert ids == sorted(ids) and len(set(ids)) == 2


def test_commands_help(svc) -> None:
    rows = svc.commands_help()
    assert any(r.phrase == "click" and r.category == "Mouse" for r in rows)
    assert any(r.phrase == "restart tracking" for r in rows)
    assert all(r.description for r in rows)


# ---------------------------------------------------------------------- aim


def test_every_action_is_aimed_at_the_speech_onset(svc, events, ctx) -> None:
    onset = time.perf_counter() - 2.0
    svc._on_final(utterance("scroll down and click", onset=onset))
    r = last(events)
    assert names(r) == ["mouse.scroll", "mouse.click"]
    assert all(a.aim_t == onset for a in r.actions)
    ctx.desktop.uia.focus = EDIT
    svc._on_final(utterance("hello world", uid=901, onset=onset))
    assert last(events).actions[0].aim_t == onset
    ctx.desktop.uia.focus = FocusInfo()
    svc._on_final(utterance("action menu", uid=902, onset=onset))  # the menu opens where you looked as you spoke
    assert [(a.name, a.aim_t) for a in last(events).actions] == [("ui.menu.show", onset)]


def test_typed_commands_are_not_aimed(svc, events) -> None:
    svc.feed_text("click")
    assert last(events).actions[0].aim_t is None


def test_transcript_final_published_for_spoken_utterances(svc, events) -> None:
    svc._on_final(utterance("copy"))
    final = events[TranscriptFinal][-1]
    assert final.text == "copy" and final.audio_ms == 900.0


# ---------------------------------------------------------------------- attention gate


def test_looking_away_blocks_typing(svc, events, ctx) -> None:
    ctx.desktop.uia.focus = EDIT
    ctx.vision.attention_value = Attention(face=1.0, facing=0.1, frames=30)
    svc._on_final(utterance("I'll call you back later"))
    r = last(events)
    assert r.kind == "ignored"
    assert r.display == "Ignored: you were looking away"
    assert r.actions == ()


def test_looking_away_runs_only_the_safety_commands(svc, events, ctx) -> None:
    ctx.vision.attention_value = Attention(face=1.0, facing=0.0, frames=30)
    svc._on_final(utterance("scroll down"))
    r = last(events)
    assert r.kind == "ignored" and r.display == "Ignored: you were looking away" and r.actions == ()
    svc._on_final(utterance("open command prompt", uid=901))
    assert last(events).kind == "ignored"
    svc._on_final(utterance("stop", uid=902))
    assert last(events).kind == "command"
    svc._on_final(utterance("scrol down", uid=903))
    assert last(events).kind == "ignored"


def test_without_a_camera_everything_still_runs(svc, events, ctx) -> None:
    # Voice-only users have no gate: it fails open without vision.
    ctx.vision = None
    svc._on_final(utterance("scroll down"))
    assert last(events).kind == "command"


def test_no_face_counts_as_away(svc, events, ctx) -> None:
    ctx.desktop.uia.focus = EDIT
    ctx.vision.attention_value = Attention(face=0.2, facing=0.2, frames=30)
    svc._on_final(utterance("hello there"))
    assert last(events).kind == "ignored"


@pytest.mark.parametrize("seen", [None, Attention(face=0.0, facing=0.0, frames=0)])
def test_no_gate_without_evidence(svc, events, ctx, seen) -> None:
    ctx.desktop.uia.focus = EDIT
    ctx.vision.attention_value = seen
    svc._on_final(utterance("hello there"))
    assert last(events).kind == "dictation"


def test_no_gate_without_vision(svc, events, ctx) -> None:
    ctx.desktop.uia.focus = EDIT
    ctx.vision = None
    svc._on_final(utterance("hello there"))
    assert last(events).kind == "dictation"


def test_attention_gate_can_be_turned_off(svc, events, ctx) -> None:
    ctx.desktop.uia.focus = EDIT
    ctx.vision.attention_value = Attention(face=1.0, facing=0.0, frames=30)
    ctx.settings.speech.attention_gate = False
    svc._on_final(utterance("hello there"))
    assert last(events).kind == "dictation"


def test_mouth_gate_is_off_by_default_and_works_when_on(svc, events, ctx) -> None:
    ctx.desktop.uia.focus = EDIT
    ctx.vision.attention_value = Attention(face=1.0, facing=1.0, mouth=0.05, frames=30)
    svc._on_final(utterance("hello there"))
    assert last(events).kind == "dictation"
    ctx.settings.speech.mouth_gate = True
    svc._on_final(utterance("hello again", uid=901))
    r = last(events)
    assert r.kind == "ignored" and r.display == "Ignored: your mouth was not moving"
    ctx.vision.attention_value = Attention(face=1.0, facing=1.0, mouth=None, frames=30)  # mouth hidden: no gate
    svc._on_final(utterance("hello once more", uid=902))
    assert last(events).kind == "dictation"


def test_attention_window_starts_at_the_voice(svc, ctx) -> None:
    calls: list[tuple[float, float]] = []
    ctx.vision.attention = lambda t0, t1: calls.append((t0, t1)) or None
    u = utterance("copy")
    svc._on_final(u)
    assert calls == [(u.t_voice_start, u.t_speech_end)]


# ---------------------------------------------------------------------- did you mean


def test_did_you_mean_then_choose_by_number(svc, events) -> None:
    svc.feed_text("scold down")
    r = last(events)
    assert r.kind == "rejected"
    assert r.alternatives and r.alternatives[0] == "Scroll down"
    svc.feed_text("one")
    r = last(events)
    assert r.kind == "command"
    assert r.display == "Scroll down"
    assert [(a.name, dict(a.args)) for a in r.actions] == [("mouse.scroll", {"direction": "down"})]
    svc.feed_text("one")  # used up
    assert last(events).kind == "rejected"


def test_did_you_mean_keeps_the_original_aim(svc, events) -> None:
    onset = time.perf_counter() - 3.0
    svc._on_final(utterance("scold down", onset=onset))
    svc._on_final(utterance("one", uid=901))
    assert last(events).actions[0].aim_t == onset


def test_suggestions_expire(svc, events) -> None:
    svc.feed_text("scold down")
    svc._choices.t -= 9.0  # hold is 8 s
    svc.feed_text("one")
    assert last(events).kind == "rejected"


def test_suggestions_survive_talk_that_was_not_for_the_computer(svc, events, ctx) -> None:
    svc.feed_text("scold down")
    ctx.vision.attention_value = Attention(face=1.0, facing=0.0, frames=30)
    svc._on_final(utterance("no I was talking to Sam", uid=901))
    assert last(events).kind == "ignored"
    ctx.vision.attention_value = None
    svc.feed_text("one")
    assert last(events).display == "Scroll down"


def test_a_real_command_withdraws_suggestions(svc, events) -> None:
    svc.feed_text("scold down")
    svc.feed_text("copy")
    svc.feed_text("one")
    assert last(events).kind == "rejected"


def test_choose_action_runs_a_suggestion(svc, events, ctx) -> None:
    svc.feed_text("scold down")
    ctx.actions.execute_now(Action("speech.choose", {"index": 1}, source="ui"))
    r = last(events)
    assert r.kind == "command" and names(r) == ["mouse.scroll"]
    with pytest.raises(ActionError):
        ctx.actions.execute_now(Action("speech.choose", {"index": 1}, source="ui"))


def test_choose_action_rejects_bad_index(svc, ctx) -> None:
    svc.feed_text("scold down")
    with pytest.raises(ActionError):
        ctx.actions.execute_now(Action("speech.choose", {"index": 9}, source="ui"))
    with pytest.raises(ActionError):
        ctx.actions.execute_now(Action("speech.choose", {"index": "x"}, source="ui"))


def test_risky_paraphrase_waits_for_confirmation(svc, events) -> None:
    from speech_helpers import bag_of_words_index

    from sightline.speech.interpret import Interpreter

    svc._interpreter = Interpreter(svc._grammar, bag_of_words_index(svc._grammar))
    svc.feed_text("get rid of this app")
    r = last(events)
    assert r.kind == "rejected" and r.actions == ()
    assert r.display == "Say 1 to confirm: Close window"
    assert r.alternatives == ("Close window",)
    svc.feed_text("one")
    assert names(last(events)) == ["window.close"]


# ---------------------------------------------------------------------- privacy


def test_password_text_is_never_shown_or_logged(svc, events, ctx, caplog) -> None:
    ctx.desktop.uia.focus = PASSWORD
    with caplog.at_level(logging.DEBUG):
        svc._on_final(utterance("hunter two"))
        svc._on_partial(901, "hunter")
    r = last(events)
    assert r.kind == "dictation"
    assert r.text == ""
    assert r.display == "Typed " + "•" * 10  # "Hunter two": small numbers stay words in dictation
    assert r.actions[0].utterance is None
    assert r.actions[0].args.get(PRIVATE) is True
    assert "hunter" not in r.actions[0].describe().lower()
    assert events[TranscriptFinal][-1].text == ""
    assert TranscriptPartial not in events
    assert "hunter" not in caplog.text


def test_spelled_keys_in_a_password_field_show_as_bullets(svc, events, ctx, caplog) -> None:
    # Review S-03: "press h u n t e r" in a password box was shown and logged as "Press H, U, N, T, E, R".
    ctx.desktop.uia.focus = PASSWORD
    with caplog.at_level(logging.DEBUG):
        svc._on_final(utterance("press h u n t e r"))
    r = last(events)
    assert r.kind == "command"
    assert r.text == ""
    assert r.display == "Pressed " + "•" * 6
    assert all(a.args.get(PRIVATE) is True and a.utterance is None for a in r.actions)
    assert all("<private>" in a.describe() for a in r.actions)
    assert "hunter" not in caplog.text.replace(" ", "").lower() and "h, u, n" not in caplog.text.lower()


def test_a_snippet_typed_into_a_password_field_shows_as_bullets(ctx, monkeypatch, events, caplog) -> None:
    import sightline.speech.service as service_module
    from sightline.core import paths

    monkeypatch.setattr(service_module, "Microphone", FakeMicrophone)
    (paths.config_dir() / "commands.toml").write_text('[[command]]\nphrase = "my snippet"\ntext = "correct horse"\n', "utf-8")
    from sightline.speech import create

    svc = create(ctx)
    try:
        ctx.desktop.uia.focus = PASSWORD
        with caplog.at_level(logging.DEBUG):
            svc.feed_text("my snippet")
        r = last(events)
        assert r.display == "Typed " + "•" * len("correct horse")
        assert r.text == ""
        assert r.actions[0].args.get(PRIVATE) is True
        assert "correct" not in caplog.text and "horse" not in caplog.text
    finally:
        svc.stop()


def test_shortcuts_in_a_password_field_are_still_named(svc, events, ctx) -> None:
    # Ctrl+A reveals nothing typed, so it keeps its plain caption.
    ctx.desktop.uia.focus = PASSWORD
    svc._on_final(utterance("select all"))
    assert "•" not in last(events).display


SENTINEL = "zebra crossing marmalade"


@pytest.mark.parametrize("kind", ["command", "dictation", "rejected", "ignored"])
def test_what_was_heard_is_never_logged(svc, events, ctx, caplog, kind) -> None:
    # Review S-02: only the kind and length of an utterance reach the log, and only at DEBUG.
    heard = {"command": "type " + SENTINEL, "dictation": SENTINEL, "rejected": SENTINEL, "ignored": SENTINEL}[kind]
    if kind in ("dictation", "ignored"):
        ctx.desktop.uia.focus = EDIT
    if kind == "ignored":
        ctx.vision.attention_value = Attention(face=1.0, facing=0.0, frames=30)
    with caplog.at_level(logging.DEBUG):
        svc._on_final(utterance(heard))
    assert last(events).kind == kind
    assert "zebra" not in caplog.text and "marmalade" not in caplog.text
    info = [r for r in caplog.records if r.levelno >= logging.INFO and r.getMessage().startswith("Speech:")]
    assert not info, [r.getMessage() for r in info]


def test_a_developer_can_log_what_was_heard(svc, events, ctx, caplog) -> None:
    ctx.settings.general.log_speech = True
    with caplog.at_level(logging.DEBUG):
        svc._on_final(utterance("scroll down"))
    assert "scroll down" in caplog.text
    ctx.settings.general.log_speech = False
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        svc._on_final(utterance("scroll up", uid=901))
    assert "scroll up" not in caplog.text


def test_sleep_hides_captions(svc, events, ctx) -> None:
    ctx.state.update(speech_mode=SpeechMode.SLEEP)
    svc._on_partial(5, "talking to someone")
    svc._on_final(utterance("talking to someone"))
    assert TranscriptPartial not in events
    assert TranscriptFinal not in events
    assert last(events).kind == "ignored"


# ---------------------------------------------------------------------- speech actions


def test_mode_action_and_wake(svc, events, ctx) -> None:
    ctx.actions.execute_now(Action("speech.mode", {"mode": "dictation"}))
    assert ctx.state.get("speech_mode") == SpeechMode.DICTATION
    ctx.actions.execute_now(Action("speech.mode", {"mode": "sleep"}))
    svc.feed_text("scroll down")
    assert last(events).kind == "ignored"
    svc.feed_text("wake up")
    assert dict(last(events).actions[0].args) == {"mode": "dictation"}  # back to how it was
    with pytest.raises(ActionError):
        ctx.actions.execute_now(Action("speech.mode", {"mode": "loud"}))


def test_listen_action(svc, ctx) -> None:
    ctx.actions.execute_now(Action("speech.listen", {"on": False}))
    assert ctx.state.get("listening") is False
    ctx.actions.execute_now(Action("speech.listen", {}))
    assert ctx.state.get("listening") is True


def test_speech_actions_can_be_undone() -> None:
    from sightline.speech.service import _undo_listen, _undo_mode, _undo_scratch

    a = Action("x", {}, source="voice")
    assert _undo_mode(a, "command") == Action("speech.mode", {"mode": "command"}, source="voice")
    assert _undo_listen(a, True) == Action("speech.listen", {"on": True}, source="voice")
    assert _undo_scratch(a, ("retype", "hello")) == Action("text.type", {"text": "hello"}, source="voice")
    assert _undo_scratch(a, ("redo", "")) == Action("key.press", {"combo": "ctrl+y"}, source="voice")
    assert _undo_scratch(a, ("retype", "")) is None


def test_scratch_removes_the_last_phrase(svc, events, ctx, pressed) -> None:
    ctx.desktop.uia.focus = EDIT
    ctx.desktop.uia.caret_text = ""
    svc.feed_text("hello world")
    typed = last(events).actions[0].args["text"]
    ctx.desktop.uia.caret_text = typed
    ctx.actions.execute_now(Action("speech.scratch"))
    assert [dict(a.args) for a in pressed] == [{"combo": "backspace", "repeat": len(typed)}]


def test_scratch_uses_undo_when_the_caret_moved(svc, events, ctx, pressed) -> None:
    ctx.desktop.uia.focus = EDIT
    ctx.desktop.uia.caret_text = ""
    svc.feed_text("hello world")
    ctx.bus.publish(ClickPerformed(10, 10, "left"))
    ctx.actions.execute_now(Action("speech.scratch"))
    assert [dict(a.args) for a in pressed] == [{"combo": "ctrl+z"}]


def test_scratch_in_another_field_uses_undo(svc, events, ctx, pressed) -> None:
    ctx.desktop.uia.focus = EDIT
    svc.feed_text("hello world")
    ctx.desktop.uia.focus = FocusInfo(hwnd=99, control_type="Edit", name="Other", editable=True)
    ctx.actions.execute_now(Action("speech.scratch"))
    assert [dict(a.args) for a in pressed] == [{"combo": "ctrl+z"}]


def test_scratch_long_phrase_in_steps(svc, ctx, pressed) -> None:
    ctx.desktop.uia.focus = EDIT
    ctx.desktop.uia.caret_text = None
    svc.feed_text("type " + "word " * 40)
    ctx.actions.execute_now(Action("speech.scratch"))
    assert sum(a.args["repeat"] for a in pressed) > 100
    assert all(a.args["repeat"] <= 100 for a in pressed)


def test_nothing_to_scratch(svc, ctx, pressed) -> None:
    with pytest.raises(ActionError):
        ctx.actions.execute_now(Action("speech.scratch"))


# ---------------------------------------------------------------------- user commands


def test_user_commands_file(ctx, monkeypatch, events) -> None:
    import sightline.speech.service as service_module
    from sightline.core import paths

    monkeypatch.setattr(service_module, "Microphone", FakeMicrophone)
    path = paths.config_dir() / "commands.toml"
    path.write_text('[[command]]\nphrase = "sign off"\ntext = "Kind regards,\\\\nSam"\n\n[[command]]\nphrase = "broken"\n', "utf-8")
    from sightline.speech import create

    svc = create(ctx)
    assert any(n.key == "speech-commands" for n in events.get(Notice, []))
    svc.feed_text("sign off")
    assert last(events).actions[0].args == {"text": "Kind regards,\nSam"}
    assert any(r.category == "My commands" and r.phrase == "sign off" for r in svc.commands_help())
    # Edited while running: picked up without a restart.
    path.write_text('[[command]]\nphrase = "new private window"\nkeys = "ctrl+shift+n"\n', "utf-8")
    svc._commands_mtime = -1.0
    svc._check_commands_file()
    svc.feed_text("new private window")
    assert dict(last(events).actions[0].args) == {"combo": "ctrl+shift+n"}


def test_user_commands_may_not_take_the_safety_words(ctx, monkeypatch, events) -> None:
    # Review S-07: a shared "command pack" mapping "stop" to Enter would remove the way out of everything.
    import sightline.speech.service as service_module
    from sightline.core import paths

    monkeypatch.setattr(service_module, "Microphone", FakeMicrophone)
    path = paths.config_dir() / "commands.toml"
    path.write_text(
        '[[command]]\nphrase = "cancel"\nkeys = "enter"\n'
        '[[command]]\nphrase = "stop"\nkeys = "enter"\n'
        '[[command]]\nphrase = "go to sleep"\ntext = "x"\n'
        '[[command]]\nphrase = "sign off"\ntext = "Regards"\n',
        "utf-8",
    )
    from sightline.speech import create

    svc = create(ctx)
    try:
        notices = [n for n in events.get(Notice, []) if n.key == "speech-commands"]
        assert notices and "safety commands" in notices[0].text
        svc.feed_text("stop")
        assert "key.press" not in names(last(events))
        svc.feed_text("go to sleep")
        assert "text.type" not in names(last(events))
        svc.feed_text("sign off")
        assert last(events).actions[0].args == {"text": "Regards"}
        # The sleep phrase is the user's own: a command may not take a new one either.
        ctx.settings.speech.sleep_phrase = "good night"
        assert "good night" in svc._safety_phrases()
    finally:
        svc.stop()


def test_problems_with_the_users_files_are_shown_again_at_start(ctx, monkeypatch, events) -> None:
    import sightline.speech.service as service_module
    from sightline.core import paths

    monkeypatch.setattr(service_module, "Microphone", FakeMicrophone)
    (paths.config_dir() / "commands.toml").write_text('[[command]]\nphrase = "broken"\n', "utf-8")
    from sightline.speech import create

    svc = create(ctx)
    try:
        before = len([n for n in events.get(Notice, []) if n.key == "speech-commands"])
        svc.start()  # the first notice came before anything showed toasts
        after = [n for n in events.get(Notice, []) if n.key == "speech-commands"]
        assert len(after) == before + 1
    finally:
        svc.stop()


def test_example_commands_file_is_written(svc) -> None:
    from sightline.core import paths

    text = (paths.config_dir() / "commands.toml").read_text("utf-8")
    assert "[[command]]" in text and "phrase" in text


# ---------------------------------------------------------------------- start, status and level


def test_start_without_models_reports_it(svc, events, ctx) -> None:
    svc.start()
    status = events[SpeechStatus][-1]
    assert status.ready is False
    assert status.error == "Speech models are not downloaded yet"
    svc.feed_text("copy")  # typed commands still work
    assert last(events).kind == "command"


def test_mic_level(svc, events) -> None:
    t = np.arange(1600) / 16000.0
    svc._level((0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32))
    level = events[MicLevel][-1]
    assert 0.5 < level.rms < 0.9  # about -23 dBFS
    assert level.speaking is False


def test_microphone_choice_is_saved(svc, ctx, monkeypatch) -> None:
    monkeypatch.setattr(ctx.settings, "save", lambda *a, **k: None)
    svc.set_microphone("WASAPI:Headset")
    assert ctx.settings.speech.microphone == "WASAPI:Headset"


def test_snippets_count_as_dictation(ctx, monkeypatch, events, pressed) -> None:
    import sightline.speech.service as service_module
    from sightline.core import paths

    monkeypatch.setattr(service_module, "Microphone", FakeMicrophone)
    (paths.config_dir() / "commands.toml").write_text('[[command]]\nphrase = "my address"\ntext = "1 High Street"\n', "utf-8")
    from sightline.speech import create

    svc = create(ctx)
    ctx.desktop.uia.focus = EDIT
    ctx.desktop.uia.caret_text = None
    svc.feed_text("my address")
    assert last(events).actions[0].args == {"text": "1 High Street"}
    ctx.actions.execute_now(Action("speech.scratch"))
    assert [dict(a.args) for a in pressed] == [{"combo": "backspace", "repeat": len("1 High Street")}]


@pytest.mark.parametrize("name", ["text.select", "text.select_range", "text.caret", "login.fill", "key.press", "element.click"])
def test_caret_moving_actions_make_scratch_undo(svc, events, ctx, pressed, name) -> None:
    from sightline.core.events import ActionExecuted

    ctx.desktop.uia.focus = EDIT
    ctx.desktop.uia.caret_text = None
    svc.feed_text("hello world")
    ctx.bus.publish(ActionExecuted(Action(name, {}, source="voice"), True))
    ctx.actions.execute_now(Action("speech.scratch"))
    assert [dict(a.args) for a in pressed] == [{"combo": "ctrl+z"}]


# ---------------------------------------------------------------------- personal aliases (concept review item 16a)

OFFER = "Say yes to remember 'scold down' as 'scroll down'"


def _aliases_path():
    from sightline.core import paths

    return paths.config_dir() / "aliases.txt"


def _saved_aliases() -> dict[str, str]:
    from sightline.speech import aliases

    return aliases.load(_aliases_path())[0]


def test_saying_it_again_offers_to_remember_the_miss(svc, events) -> None:
    svc._on_final(utterance("scold down"))
    assert last(events).kind == "rejected"
    svc._on_final(utterance("scroll down", uid=901))
    r = last(events)
    assert r.kind == "command" and names(r) == ["mouse.scroll"]
    assert r.alternatives == (OFFER,)
    svc.feed_text("yes")
    r = last(events)
    assert r.kind == "command" and r.actions == ()
    assert r.display == "Remembered: 'scold down' means 'scroll down'"
    assert _saved_aliases() == {"scold down": "scroll down"}
    svc._on_final(utterance("scold down", uid=903))
    r = last(events)
    assert r.kind == "command" and names(r) == ["mouse.scroll"] and r.alternatives == ()


def test_choosing_a_suggestion_offers_to_remember_the_miss(svc, events) -> None:
    svc._on_final(utterance("scold down"))
    assert last(events).alternatives[0] == "Scroll down"
    svc.feed_text("one")
    r = last(events)
    assert names(r) == ["mouse.scroll"] and r.alternatives == (OFFER,)
    svc.feed_text("one")  # the offer is a numbered choice too, as its chip says
    assert last(events).display.startswith("Remembered")
    assert _saved_aliases() == {"scold down": "scroll down"}


def test_a_bare_number_goes_to_the_chooser_not_the_offer(svc, events, ctx) -> None:
    svc._on_final(utterance("scold down"))
    svc._on_final(utterance("scroll down", uid=901))
    ctx.state.update(active_overlay="chooser")  # say the corrected command was an unsure click
    svc.feed_text("one")
    assert [(a.name, dict(a.args)) for a in last(events).actions] == [("ui.chooser.select", {"number": 1})]
    svc.feed_text("yes")  # the pick was a new command, and a new command withdraws the offer
    assert _saved_aliases() == {}


def test_a_blink_on_a_suggestion_offers_too(svc, events, ctx) -> None:
    svc._on_final(utterance("scold down"))
    ctx.actions.execute_now(Action("speech.choose", {"index": 1}, source="ui"))
    r = last(events)
    assert names(r) == ["mouse.scroll"] and r.alternatives == (OFFER,)
    ctx.actions.execute_now(Action("speech.choose", {"index": 1}, source="ui"))
    assert _saved_aliases() == {"scold down": "scroll down"}


def test_an_offer_is_only_taken_by_yes(svc, events) -> None:
    svc._on_final(utterance("scold down"))
    svc._on_final(utterance("scroll down", uid=901))
    svc.feed_text("copy")  # a new command withdraws the offer
    assert last(events).alternatives == ()
    svc.feed_text("yes")
    assert last(events).kind == "rejected"
    assert _saved_aliases() == {}


@pytest.mark.parametrize(
    ("miss", "then"),
    [
        ("clothes window", "close window"),  # hard to undo: never an alias
        ("scold down", "what can I say"),  # a reaction to the miss, not the command it meant
        ("scold down", "type hello"),  # free text
        ("scroll", "scroll down"),  # the start of a command cut short by a pause, not a mishearing
        ("what time is it over there", "scroll down"),  # a sentence, not a misheard command
    ],
)
def test_no_offer(svc, events, miss: str, then: str) -> None:
    svc._on_final(utterance(miss))
    svc._on_final(utterance(then, uid=901))
    assert last(events).kind == "command"
    assert last(events).alternatives == ()


def test_typed_misses_teach_nothing(svc, events) -> None:
    svc.feed_text("scold down")
    svc.feed_text("scroll down")
    assert last(events).alternatives == ()


def test_a_late_correction_teaches_nothing(svc, events) -> None:
    svc._on_final(utterance("scold down"))
    svc._miss.t -= 9.0  # 8 s is the window
    svc._on_final(utterance("scroll down", uid=901))
    assert last(events).alternatives == ()


def test_alias_offers_can_be_turned_off(svc, events, ctx) -> None:
    ctx.settings.speech.learn_aliases = False
    svc._on_final(utterance("scold down"))
    svc._on_final(utterance("scroll down", uid=901))
    assert last(events).alternatives == ()


def test_yes_from_someone_else_is_ignored(svc, events, ctx) -> None:
    svc._on_final(utterance("scold down"))
    svc._on_final(utterance("scroll down", uid=901))
    ctx.vision.attention_value = Attention(face=1.0, facing=0.0, frames=30)
    svc._on_final(utterance("yes", uid=902))
    assert last(events).kind == "ignored"
    assert _saved_aliases() == {}


def test_a_helper_can_write_aliases(svc, events) -> None:
    path = _aliases_path()
    path.write_text(path.read_text("utf-8") + "skull town = scroll down\nbroken line\n", "utf-8")
    svc._aliases_mtime = -1.0
    svc._check_commands_file()
    svc.feed_text("skull town")
    assert names(last(events)) == ["mouse.scroll"]
    assert any(n.key == "speech-aliases" for n in events.get(Notice, []))


def test_aliases_survive_the_paraphrase_model_loading(svc, events) -> None:
    # Review I-05: the interpreter built when the model arrived had no aliases until the next rebuild.
    from speech_helpers import bag_of_words_index

    path = _aliases_path()
    path.write_text(path.read_text("utf-8") + "skull town = scroll down\n", "utf-8")
    svc._aliases_mtime = -1.0
    svc._check_commands_file()
    svc._install_semantic(bag_of_words_index(svc._grammar))
    assert svc._interpreter.semantic is not None
    svc.feed_text("skull town")
    assert names(last(events)) == ["mouse.scroll"]


# ---------------------------------------------------------------------- the pause inside commands (concept review item 16b)


def test_pauses_inside_commands_set_the_wait_for_unfinished_ones(svc, ctx) -> None:
    from sightline.core import paths
    from sightline.speech import pauses as P

    for i in range(P.MIN_COMMANDS):
        svc._on_final(utterance("scroll down", uid=1000 + i, pauses=(700.0,)))
    assert svc._learned_ms == 840
    assert svc._endpoint_config().incomplete_ms == 840
    svc._sync_learned_setting()
    assert ctx.settings.speech.learned_command_pause == "0.84 seconds"
    svc._save_pauses(force=True)
    saved = P.PauseModel.load(paths.profile_dir("default") / "speech" / "pauses.json")
    assert saved.commands == P.MIN_COMMANDS and saved.samples == [700.0] * P.MIN_COMMANDS


def test_dictation_misses_and_typed_commands_teach_no_pauses(svc, ctx) -> None:
    ctx.desktop.uia.focus = EDIT
    svc._on_final(utterance("hello there my friend", pauses=(900.0,)))
    ctx.desktop.uia.focus = FocusInfo()
    svc._on_final(utterance("blah blah", uid=901, pauses=(900.0,)))
    svc.feed_text("scroll down")
    assert svc._pauses.commands == 0 and svc._pauses.samples == []


def test_a_command_cut_in_two_teaches_a_longer_wait(svc, events) -> None:
    end = time.perf_counter() - 2.0
    svc._on_final(Utterance(900, "scroll", 400.0, 250.0, 20.0, end, time.perf_counter(), False, t_speech_start=end - 0.6, t_voice_start=end - 0.4))
    assert last(events).kind == "rejected"
    svc._on_final(Utterance(901, "down", 300.0, 125.0, 20.0, end + 0.95, time.perf_counter(), False, t_speech_start=end + 0.5, t_voice_start=end + 0.65))
    assert svc._pauses.samples == [pytest.approx(650.0, abs=1.0)]


def test_pause_learning_can_be_turned_off(svc, ctx) -> None:
    from sightline.speech import pauses as P

    ctx.settings.speech.learn_pauses = False
    for i in range(P.MIN_COMMANDS + 5):
        svc._on_final(utterance("scroll down", uid=1000 + i, pauses=(700.0,)))
    assert svc._pauses.commands == 0
    ctx.settings_changed("speech", save=False)
    assert svc._learned_ms is None and svc._endpoint_config().incomplete_ms == 0
    svc._sync_learned_setting()
    assert ctx.settings.speech.learned_command_pause == "Not learning"


def test_learned_pauses_belong_to_the_profile(svc, ctx) -> None:
    from sightline.core import paths
    from sightline.speech import pauses as P

    P.PauseModel([800.0] * 10, commands=50).save(paths.profile_dir("Sam") / "speech" / "pauses.json")
    ctx.settings.general.profile = "Sam"
    ctx.settings_changed("general", save=False)
    assert svc._learned_ms == 960


def test_the_learned_pause_is_read_only_in_the_settings() -> None:
    import dataclasses

    from sightline.speech.config import SpeechConfig

    meta = {f.name: f.metadata for f in dataclasses.fields(SpeechConfig)}["learned_command_pause"]
    assert meta["readonly"] and not meta["hidden"] and meta["label"] and meta["help"]


# ---------------------------------------------------------------------- vocabulary (concept review item 9)


def _vocabulary() -> list[str]:
    from sightline.core import paths
    from sightline.speech import custom

    return custom.load_vocabulary(paths.config_dir() / "vocabulary.txt")[0]


class FakeBiasingEngine:
    biasing = True
    streaming = False

    def __init__(self) -> None:
        self.hotwords: list[str] | None = None

    def set_hotwords(self, words: list[str]) -> None:
        self.hotwords = list(words)

    def close(self) -> None:
        pass


def test_add_that_to_vocabulary(svc, events, ctx) -> None:
    ctx.desktop.uia.focus = EDIT
    ctx.desktop.uia.caret_text = None
    svc.feed_text("spell capital sierra india oscar bravo hotel alpha november")
    assert last(events).actions[0].args["text"] == "Siobhan"
    svc.feed_text("add that to vocabulary")
    r = last(events)
    assert r.kind == "command" and r.actions == ()
    assert r.display == "Added 'Siobhan' to your vocabulary"
    assert _vocabulary() == ["Siobhan"]
    svc.feed_text("add that to vocabulary")
    assert last(events).display == "'Siobhan' is already in your vocabulary"


def test_add_a_word_to_vocabulary_keeps_its_capitals(svc, events) -> None:
    svc.feed_text("Add Kubernetes to my vocabulary.")
    assert last(events).display == "Added 'Kubernetes' to your vocabulary"
    assert _vocabulary() == ["Kubernetes"]


def test_nothing_to_add_to_vocabulary(svc, events) -> None:
    svc.feed_text("add that to vocabulary")
    assert last(events).display.startswith("Dictate or spell the word first")
    assert _vocabulary() == []


def test_vocabulary_reaches_the_recogniser(svc) -> None:
    from sightline.core import paths

    engine = FakeBiasingEngine()
    svc._engine, svc._engine_id = engine, "parakeet-0.6b"
    try:
        svc.feed_text("add Siobhan to vocabulary")
        assert engine.hotwords == ["Siobhan"]
        path = paths.config_dir() / "vocabulary.txt"  # a helper's edit is picked up too
        path.write_text(path.read_text("utf-8") + "Nguyen\n", "utf-8")
        svc._vocabulary_mtime = -1.0
        svc._check_commands_file()
        assert engine.hotwords == ["Siobhan", "Nguyen"]
    finally:
        svc._engine = None


def test_a_recogniser_built_without_biasing_is_rebuilt_for_the_vocabulary(svc, ctx) -> None:
    class Greedy(FakeBiasingEngine):
        biasing = False

    svc._engine, svc._engine_id, svc._running = Greedy(), "parakeet-0.6b", True
    try:
        svc.feed_text("add Siobhan to vocabulary")
        assert svc._pending_engine == ctx.settings.speech.engine and svc._rebuild_engine
    finally:
        svc._engine, svc._running, svc._pending_engine = None, False, None
