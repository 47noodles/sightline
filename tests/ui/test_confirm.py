"""The countdown before hard-to-reverse actions: held, shown, cancellable, then run on the window that was in front."""

from __future__ import annotations

import dataclasses
import time

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from sightline.core.actions import GUARD_FAILED_MESSAGE
from sightline.core.events import ActionExecuted, IntentResult
from sightline.core.types import Action, ElementInfo, FocusInfo, Rect, WindowInfo
from sightline.ui.bridge import process_events_until
from sightline.ui.config import UIConfig
from sightline.ui.overlays import confirm as confirm_mod
from sightline.ui.overlays.confirm import CONFIRMED, delay_ms, describe, normalize_combo, risk_of

pytestmark = pytest.mark.gui

EXTRA = ("window.close", "app.quit", "key.release_all", "text.type")
WORD = WindowInfo(4242, "Report.docx - Word", "WINWORD.EXE", Rect(0, 0, 1920, 1040))


@pytest.fixture(autouse=True)
def _shell_without_its_overlays(shell):
    """These tests install their own overlay, so take out the ones the shell installs at start."""
    for uninstall in reversed(shell._overlay_uninstalls):
        uninstall()
    shell._overlay_uninstalls = []


@pytest.fixture
def countdown(recorder, ui_ctx, shell):
    for name in EXTRA:
        if not ui_ctx.actions.has(name):
            ui_ctx.actions.register(name, recorder.append)
    ui_ctx.settings.ui.confirm_delay_ms = 600
    ui_ctx.desktop.windows.windows = [WORD]
    remove = confirm_mod.install(ui_ctx, shell.services)
    ui_ctx.actions.start()
    try:
        yield remove.overlay
    finally:
        ui_ctx.actions.stop()
        remove()


def _do(ctx, name: str, source: str = "voice", utterance: str | None = None, aim_t: float | None = None, **args) -> None:
    ctx.actions.dispatch(Action(name, args, source=source, utterance=utterance, aim_t=aim_t))


def _calls(recorder, name: str) -> list[Action]:
    return [a for a in recorder if a.name == name]


def _settle(seconds: float = 0.15) -> None:
    process_events_until(lambda: False, timeout=seconds)


def _held(ctx, countdown, name: str = "window.close", **kw) -> None:
    _do(ctx, name, **kw)
    assert process_events_until(lambda: countdown.is_counting, timeout=3.0), f"{name} is held"


def _click_widget(widget) -> None:
    """A left click delivered straight to the widget, without the platform's cursor."""
    pos = QPointF(widget.width() / 2.0, widget.height() / 2.0)
    glob = QPointF(widget.mapToGlobal(pos.toPoint()))
    for kind, buttons in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton), (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)):
        QApplication.sendEvent(widget, QMouseEvent(kind, pos, glob, Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier))


# =========================================================================== what counts as risky (pure)


@pytest.mark.parametrize(
    "spoken, combo",
    [
        ("Alt + F4", "alt+f4"),
        ("f4+alt", "alt+f4"),
        ("alt f4", "alt+f4"),
        ("Control+W", "ctrl+w"),
        ("left control+w", "ctrl+w"),
        ("shift+ctrl+w", "ctrl+shift+w"),
        ("ctrl shift w", "ctrl+shift+w"),
        ("CTRL+F4", "ctrl+f4"),
        ("Del", "delete"),
        ("shift+del", "shift+delete"),
        ("ctrl+Return", "ctrl+enter"),
        ("alt+S", "alt+s"),
        ("page down", "pagedown"),
        ("ctrl+a", "ctrl+a"),
        ("", ""),
    ],
)
def test_combos_are_recognised_however_they_are_spelled(spoken, combo):
    assert normalize_combo(spoken) == combo


def test_what_counts_as_risky():
    def risk(name, **args):
        return risk_of(Action(name, args))

    assert risk("window.close") == "Closing this window" and risk("app.quit") == "Quitting Sightline"
    assert risk("key.press", combo="Control + W") == "Closing this tab"
    assert risk("key.press", key="Delete") == "Pressing Delete"
    for safe in ("ctrl+a", "ctrl+c", "alt+tab", "win+d", "enter", "shift+tab", "backspace", "ctrl+delete"):
        assert risk("key.press", combo=safe) is None, safe
    assert risk("mouse.click") is None and risk("window.minimize") is None


CONSOLE = FocusInfo(hwnd=5, process="cmd.exe", control_type="Edit", editable=True)
NOTEPAD = FocusInfo(hwnd=6, process="notepad.exe", control_type="Document", editable=True)


@pytest.mark.parametrize(
    "name, args, focus, doing",
    [
        # Review S-04: what the old list let through.
        ("key.press", {"combo": "ctrl+q"}, None, "Pressing Control+Q"),
        ("key.press", {"combo": "ctrl+shift+q"}, None, "Pressing Control+Shift+Q"),
        ("key.press", {"combo": "ctrl+shift+delete"}, None, "Pressing Control+Shift+Delete"),
        ("key.press", {"combo": "win+r"}, None, "Opening the Run box"),
        ("key.press", {"combo": "alt+f4+a"}, None, "Closing this window"),  # an extra key does not slip past
        ("key.press", {"combo": "ctrl+alt+w"}, None, "Closing this tab"),  # nor an extra modifier
        ("key.press", {"combo": "enter"}, CONSOLE, "Pressing Enter in a command window"),
        ("text.type", {"text": "del *.*\n"}, CONSOLE, "Typing a line into a command window"),
        ("app.open", {"name": "Command Prompt"}, None, "Opening Command Prompt"),
        ("app.open", {"name": "Windows PowerShell"}, None, "Opening Windows PowerShell"),
        ("app.open", {"name": "Terminal"}, None, "Opening Terminal"),
        ("app.open", {"name": "Registry Editor"}, None, "Opening Registry Editor"),
        ("app.open", {"name": "Run"}, None, "Opening Run"),
        ("element.click", {"name": "Delete"}, None, "Clicking “Delete”"),
        ("element.click", {"name": "Send"}, None, "Clicking “Send”"),
        ("element.click", {"name": "Pay now"}, None, "Clicking “Pay now”"),
        ("element.click", {"name": "Empty Recycle Bin"}, None, "Clicking “Empty Recycle Bin”"),
    ],
)
def test_the_shared_policy_holds_what_is_hard_to_undo(name, args, focus, doing):
    assert describe(Action(name, args), "", focus)[0] == doing


@pytest.mark.parametrize(
    "name, args, focus",
    [
        ("key.press", {"combo": "enter"}, NOTEPAD),
        ("text.type", {"text": "Dear Sam,\n"}, NOTEPAD),
        ("text.type", {"text": "dir"}, CONSOLE),
        ("app.open", {"name": "Notepad"}, None),
        ("app.open", {"name": "Temple Run"}, None),
        ("element.click", {"name": "Sent Items"}, None),
        ("element.click", {"name": "Recycle Bin"}, None),
    ],
)
def test_the_shared_policy_lets_the_rest_through(name, args, focus):
    assert risk_of(Action(name, args), focus) is None


def test_the_chip_says_what_is_about_to_happen_in_plain_words():
    assert describe(Action("window.close"), "Report.docx - Word") == ("Closing “Report.docx - Word”", "Cancelled. The window stays open.")
    assert describe(Action("window.close")) == ("Closing this window", "Cancelled. The window stays open.")
    assert describe(Action("key.press", {"combo": "alt+f4"}))[0] == "Closing this window"
    assert describe(Action("key.press", {"combo": "ctrl+w"})) == ("Closing this tab", "Cancelled. The tab stays open.")
    assert describe(Action("key.press", {"combo": "delete", "repeat": 3})) == ("Pressing Delete 3 times", "Cancelled. Nothing was deleted.")
    assert describe(Action("app.quit")) == ("Quitting Sightline", "Cancelled. Sightline keeps running.")
    long_title = describe(Action("window.close"), "A very long document title that goes on and on - Word")[0]
    assert long_title.endswith("…”") and len(long_title) < 55


def test_the_delay_is_kept_in_range():
    assert delay_ms(100) == 600 and delay_ms(5000) == 3000 and delay_ms(1500) == 1500 and delay_ms("soon") == 1200


def test_the_settings_are_added_at_the_end_with_plain_labels():
    fields = dataclasses.fields(UIConfig)
    risky, delay = fields[-2], fields[-1]
    assert (risky.name, delay.name) == ("confirm_risky", "confirm_delay_ms")
    cfg = UIConfig()
    assert cfg.confirm_risky is True and cfg.confirm_delay_ms == 1200
    assert risky.metadata["label"] and risky.metadata["help"] and risky.metadata["group"] == "Feedback"
    assert (delay.metadata["minimum"], delay.metadata["maximum"], delay.metadata["unit"], delay.metadata["group"]) == (600, 3000, "ms", "Feedback")


# =========================================================================== holding and releasing


def test_a_spoken_close_waits_then_closes_the_window_that_was_in_front(countdown, recorder, ui_ctx, shell):
    started = time.monotonic()
    _held(ui_ctx, countdown, utterance="close window")
    assert ui_ctx.state.active_overlay == "confirm"
    chip = countdown.chip
    assert chip.isVisible() and chip.title.startswith("Closing “Report.docx - Word” in ")
    assert chip.hint == "Say cancel or blink here to stop."
    flags = chip.windowFlags()
    assert not flags & Qt.WindowType.WindowTransparentForInput, "a blink on the chip is a real click"
    assert flags & Qt.WindowType.WindowDoesNotAcceptFocus, "the window about to close keeps focus"
    assert not _calls(recorder, "window.close")

    ui_ctx.desktop.windows.windows = [WindowInfo(77, "Something else", "notepad.exe", Rect(0, 0, 800, 600)), WORD]
    assert process_events_until(lambda: bool(_calls(recorder, "window.close")), timeout=3.0)
    assert time.monotonic() - started >= 0.55
    done = _calls(recorder, "window.close")[-1]
    assert done.arg("hwnd") == 4242, "it closes the window that was in front when the user asked"
    assert done.arg(CONFIRMED) is True and done.source == "voice" and done.utterance == "close window"
    assert process_events_until(lambda: not chip.isVisible(), timeout=1.0)
    assert not countdown.is_counting
    assert ui_ctx.state.active_overlay is None


def test_the_bar_drains_as_the_seconds_count_down(countdown, ui_ctx):
    _held(ui_ctx, countdown)
    first = countdown.chip.fraction
    _settle(0.25)
    assert countdown.chip.fraction < first
    assert 0.0 < countdown.remaining < 0.6
    assert " in 0." in countdown.chip.title


@pytest.mark.parametrize("source", ["voice", "eye", "dwell"])
def test_every_user_source_waits(countdown, recorder, ui_ctx, source):
    _held(ui_ctx, countdown, source=source)
    assert not _calls(recorder, "window.close")
    _do(ui_ctx, "ui.cancel")
    assert process_events_until(lambda: not countdown.is_counting, timeout=3.0)


@pytest.mark.parametrize(
    "combo, title",
    [("Control + W", "Closing this tab"), ("alt+F4", "Closing “Report.docx - Word”"), ("Del", "Pressing Delete"), ("ctrl+enter", "Pressing Control+Enter")],
)
def test_risky_keys_wait_and_keep_their_own_spelling(countdown, recorder, ui_ctx, combo, title):
    _held(ui_ctx, countdown, "key.press", combo=combo)
    assert countdown.chip.title.startswith(title + " in ")
    assert process_events_until(lambda: bool(_calls(recorder, "key.press")), timeout=3.0)
    pressed = _calls(recorder, "key.press")[-1]
    assert pressed.arg("combo") == combo and pressed.arg(CONFIRMED) is True
    assert pressed.arg("hwnd") == 4242, "the keys go to the window that was in front"


def _register(ctx, recorder, *names: str) -> None:
    for name in names:
        if not ctx.actions.has(name):
            ctx.actions.register(name, recorder.append)


def _held_by_name(ctx, countdown, action_name: str, element: str) -> None:
    """``_held`` for actions whose own argument is called ``name``."""
    ctx.actions.dispatch(Action(action_name, {"name": element}, source="voice"))
    assert process_events_until(lambda: countdown.is_counting, timeout=3.0), f"{action_name} is held"


def test_clicking_delete_by_name_waits(countdown, recorder, ui_ctx):
    _register(ui_ctx, recorder, "element.click")
    _held_by_name(ui_ctx, countdown, "element.click", "Delete")
    assert countdown.chip.title.startswith("Clicking “Delete” in ")
    assert process_events_until(lambda: bool(_calls(recorder, "element.click")), timeout=3.0)
    assert _calls(recorder, "element.click")[-1].arg(CONFIRMED) is True


def test_an_eye_click_on_send_waits_and_lands_where_it_was_aimed(countdown, recorder, ui_ctx):
    ui_ctx.desktop.uia.elements = [ElementInfo(Rect(100, 100, 80, 30), "Send", "Button", clickable=True)]
    _held(ui_ctx, countdown, "mouse.click", source="eye", button="left", count=1, x=120, y=110)
    assert countdown.chip.title.startswith("Clicking “Send” in ")
    assert process_events_until(lambda: bool(_calls(recorder, "mouse.click")), timeout=3.0)
    done = _calls(recorder, "mouse.click")[-1]
    assert (done.arg("x"), done.arg("y")) == (120, 110) and done.arg(CONFIRMED) is True


def test_a_click_that_snaps_to_send_waits_too(countdown, recorder, ui_ctx):
    ui_ctx.desktop.uia.elements = [ElementInfo(Rect(100, 100, 80, 30), "Send", "Button", clickable=True)]
    _held(ui_ctx, countdown, "mouse.click", source="dwell", button="left", count=1, x=95, y=110)  # just outside: snapping takes it
    _do(ui_ctx, "ui.cancel")
    assert process_events_until(lambda: not countdown.is_counting, timeout=3.0)
    assert countdown.notice.text == "Cancelled. Nothing was clicked."


def test_an_eye_click_on_anything_else_runs_at_once(countdown, recorder, ui_ctx):
    ui_ctx.desktop.uia.elements = [ElementInfo(Rect(100, 100, 80, 30), "Sent Items", "TreeItem", clickable=True)]
    _do(ui_ctx, "mouse.click", source="eye", button="left", count=1, x=120, y=110)
    assert process_events_until(lambda: bool(_calls(recorder, "mouse.click")), timeout=0.5)
    assert not countdown.is_counting


def test_a_line_typed_into_a_command_window_waits(countdown, recorder, ui_ctx):
    ui_ctx.desktop.uia.focus = CONSOLE
    _held(ui_ctx, countdown, "text.type", text="format c:\n")
    assert countdown.chip.title.startswith("Typing a line into a command window in ")
    _do(ui_ctx, "ui.cancel")
    assert process_events_until(lambda: not countdown.is_counting, timeout=3.0)
    _settle(0.8)
    assert not _calls(recorder, "text.type")
    assert countdown.notice.text == "Cancelled. Nothing was typed."


def test_opening_a_command_prompt_waits(countdown, recorder, ui_ctx):
    _register(ui_ctx, recorder, "app.open")
    _held_by_name(ui_ctx, countdown, "app.open", "Command Prompt")
    assert countdown.chip.title.startswith("Opening Command Prompt in ")
    assert process_events_until(lambda: bool(_calls(recorder, "app.open")), timeout=3.0)


def test_a_broken_countdown_drops_the_action_instead_of_running_it(countdown, recorder, ui_ctx, monkeypatch):
    # Review S-05: the countdown is fail-closed.
    def broken(_action):
        raise RuntimeError("the countdown itself failed")

    monkeypatch.setattr(countdown, "_risk", broken)
    seen: list[ActionExecuted] = []
    ui_ctx.bus.subscribe(ActionExecuted, seen.append)
    _do(ui_ctx, "window.close")
    assert process_events_until(lambda: any(e.action.name == "window.close" for e in seen), timeout=3.0)
    result = next(e for e in seen if e.action.name == "window.close")
    assert result.ok is False and result.error == GUARD_FAILED_MESSAGE
    _settle(0.3)
    assert not _calls(recorder, "window.close")


def test_the_log_names_the_kind_not_the_window(countdown, recorder, ui_ctx, caplog):
    import logging

    with caplog.at_level(logging.DEBUG, logger="sightline"):
        _held(ui_ctx, countdown)
        _do(ui_ctx, "ui.cancel")
        assert process_events_until(lambda: not countdown.is_counting, timeout=3.0)
    assert "close-window" in caplog.text
    assert "Report.docx" not in caplog.text


def test_quitting_waits_too(countdown, recorder, ui_ctx):
    _held(ui_ctx, countdown, "app.quit")
    assert countdown.chip.title.startswith("Quitting Sightline in ")
    assert process_events_until(lambda: bool(_calls(recorder, "app.quit")), timeout=3.0)
    assert _calls(recorder, "app.quit")[-1].arg("hwnd") is None


@pytest.mark.parametrize(
    "name, args, source",
    [
        ("key.press", {"combo": "ctrl+c"}, "voice"),
        ("window.minimize", {}, "voice"),
        ("window.close", {}, "ui"),
        ("window.close", {}, "keyboard"),
        ("key.press", {"combo": "delete"}, "keyboard"),
        ("window.close", {CONFIRMED: True}, "voice"),
    ],
    ids=["safe-keys", "safe-action", "from-sightline", "from-the-keyboard", "delete-on-the-keyboard", "already-confirmed"],
)
def test_other_actions_pass_at_once(countdown, recorder, ui_ctx, name, args, source):
    if not ui_ctx.actions.has(name):
        ui_ctx.actions.register(name, recorder.append)
    _do(ui_ctx, name, source=source, **args)
    assert process_events_until(lambda: bool(_calls(recorder, name)), timeout=0.5)
    assert not countdown.is_counting


def test_turning_it_off_lets_everything_through(countdown, recorder, ui_ctx):
    ui_ctx.settings.ui.confirm_risky = False
    _do(ui_ctx, "window.close")
    assert process_events_until(lambda: bool(_calls(recorder, "window.close")), timeout=0.5)
    assert not countdown.is_counting


@pytest.mark.parametrize("setting, seconds", [(100, 0.6), (9000, 3.0)])
def test_the_delay_setting_is_kept_in_range_when_counting(countdown, ui_ctx, setting, seconds):
    ui_ctx.settings.ui.confirm_delay_ms = setting
    _held(ui_ctx, countdown)
    assert seconds - 0.2 < countdown.remaining <= seconds
    _do(ui_ctx, "ui.cancel")
    assert process_events_until(lambda: not countdown.is_counting, timeout=3.0)


# =========================================================================== cancelling


@pytest.mark.parametrize("closer", ["ui.cancel", "key.release_all", "action.undo"])
def test_cancel_stop_or_undo_stops_it_and_says_so(countdown, recorder, ui_ctx, closer):
    _held(ui_ctx, countdown)
    _do(ui_ctx, closer)
    assert process_events_until(lambda: not countdown.is_counting, timeout=3.0)
    assert countdown.notice.isVisible() and countdown.notice.text == "Cancelled. The window stays open."
    assert countdown.notice.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert not countdown.chip.isVisible()
    assert ui_ctx.state.active_overlay is None
    _settle(0.8)
    assert not _calls(recorder, "window.close"), "the close never happens"
    if closer == "key.release_all":
        assert _calls(recorder, "key.release_all"), "stop still lets go of every key"


def test_saying_cancel_cancels_once(countdown, recorder, ui_ctx):
    # Speech turns "cancel" into ui.cancel then key.release_all.
    _held(ui_ctx, countdown)
    for name in ("ui.cancel", "key.release_all"):
        _do(ui_ctx, name, utterance="cancel", aim_t=5.0)
    assert process_events_until(lambda: not countdown.is_counting, timeout=3.0)
    _settle(0.8)
    assert not _calls(recorder, "window.close")
    assert len(_calls(recorder, "key.release_all")) == 1


def test_a_blink_on_the_chip_cancels_it(countdown, recorder, ui_ctx, shell):
    _held(ui_ctx, countdown)
    rect = shell.screens.map.rect_to_physical(Rect(*countdown.chip.geometry().getRect()), countdown.chip.screen_geometry)
    _do(ui_ctx, "mouse.click", source="eye", button="left", count=1, x=rect.center[0], y=rect.center[1])
    assert process_events_until(lambda: not countdown.is_counting, timeout=3.0)
    _settle(0.8)
    assert not _calls(recorder, "mouse.click"), "the blink on the chip was not a click on the window"
    assert not _calls(recorder, "window.close")


def test_a_click_on_the_chip_cancels_it(countdown, recorder, ui_ctx):
    _held(ui_ctx, countdown)
    _click_widget(countdown.chip)
    assert not countdown.is_counting
    assert countdown.notice.isVisible()
    _settle(0.8)
    assert not _calls(recorder, "window.close")


def test_a_click_elsewhere_does_not_cancel(countdown, recorder, ui_ctx):
    _held(ui_ctx, countdown)
    _do(ui_ctx, "mouse.click", source="eye", button="left", count=1, x=300, y=300)
    assert process_events_until(lambda: bool(_calls(recorder, "mouse.click")), timeout=3.0)
    assert process_events_until(lambda: bool(_calls(recorder, "window.close")), timeout=3.0)


def test_another_overlay_opening_cancels_it(countdown, recorder, ui_ctx):
    _held(ui_ctx, countdown)
    _do(ui_ctx, "ui.grid.show")
    assert process_events_until(lambda: ui_ctx.state.active_overlay == "grid", timeout=3.0)
    assert not countdown.is_counting
    _settle(0.8)
    assert not _calls(recorder, "window.close")
    ui_ctx.actions.execute_now(Action("ui.grid.hide", source="test"))
    assert ui_ctx.state.active_overlay is None


# =========================================================================== several at once


def test_actions_said_in_the_same_breath_wait_their_turn(countdown, recorder, ui_ctx):
    said = {"utterance": "select all delete and type hello", "aim_t": 12.5}
    _do(ui_ctx, "key.press", combo="ctrl+a", **said)
    _do(ui_ctx, "key.press", combo="delete", **said)
    _do(ui_ctx, "text.type", text="hello", **said)
    assert process_events_until(lambda: countdown.is_counting and len(countdown.held) == 2, timeout=3.0)
    assert [a.arg("combo") for a in _calls(recorder, "key.press")] == ["ctrl+a"], "the safe one before the risky one ran at once"
    assert not _calls(recorder, "text.type"), "typing waits behind the delete it was said with"
    _do(ui_ctx, "mouse.scroll", direction="down")  # something else entirely does not wait
    assert process_events_until(lambda: bool(_calls(recorder, "mouse.scroll")), timeout=0.4)
    assert process_events_until(lambda: bool(_calls(recorder, "text.type")), timeout=3.0)
    order = [(a.name, a.arg("combo") or a.arg("text")) for a in recorder if a.name in ("key.press", "text.type")]
    assert order == [("key.press", "ctrl+a"), ("key.press", "delete"), ("text.type", "hello")]


def test_a_second_risky_request_restarts_the_countdown_and_both_run(countdown, recorder, ui_ctx):
    ui_ctx.settings.ui.confirm_delay_ms = 1500
    _held(ui_ctx, countdown, "key.press", combo="ctrl+w", utterance="close tab")
    _settle(0.4)
    _do(ui_ctx, "key.press", combo="ctrl+w", utterance="close tab", aim_t=2.0)
    assert process_events_until(lambda: len(countdown.held) == 2, timeout=3.0)
    assert countdown.chip.title.startswith("Closing this tab and 1 more in ")
    assert countdown.remaining > 1.2, "the whole time again"
    assert process_events_until(lambda: len(_calls(recorder, "key.press")) == 2, timeout=4.0)


# =========================================================================== placement and uninstall


def test_the_chip_sits_above_the_captions(countdown, ui_ctx, shell):
    _held(ui_ctx, countdown)
    smap = shell.screens.map
    screen = smap.screen_at(*shell.cursor.pos)
    work = smap.rect_to_logical(screen.work_rect, screen)
    g = countdown.chip.geometry()
    assert g.bottom() <= work.bottom - shell.captions.reserved_height()
    assert abs(g.center().x() - (work.x + work.w / 2.0)) <= 2
    assert g.width() >= shell.theme.px(380) and g.height() >= shell.theme.px(80)
    _do(ui_ctx, "ui.cancel")
    assert process_events_until(lambda: not countdown.is_counting, timeout=3.0)


def test_the_chip_stands_clear_of_did_you_mean(countdown, ui_ctx, shell):
    shell.did_you_mean.on_intent(IntentResult(1, "close windo", "rejected", "Not understood", alternatives=("Close window", "Close tab")))
    bar = shell.did_you_mean.bar
    assert bar.isVisible()
    _held(ui_ctx, countdown)
    assert countdown.chip.geometry().bottom() < bar.geometry().top()
    _do(ui_ctx, "ui.cancel")
    assert process_events_until(lambda: not countdown.is_counting, timeout=3.0)


def test_uninstall_drops_what_is_held_and_lets_everything_through(recorder, ui_ctx, shell):
    for name in EXTRA:
        if not ui_ctx.actions.has(name):
            ui_ctx.actions.register(name, recorder.append)
    remove = confirm_mod.install(ui_ctx, shell.services)
    countdown = remove.overlay
    ui_ctx.actions.execute_now(Action("window.close", source="voice"))
    process_events_until(lambda: countdown.is_counting, timeout=3.0)
    assert countdown.is_counting and ui_ctx.state.active_overlay == "confirm"
    remove()
    remove()
    assert not countdown.is_counting and ui_ctx.state.active_overlay is None
    ui_ctx.actions.execute_now(Action("window.close", source="voice"))
    assert len(_calls(recorder, "window.close")) == 1, "only the one after uninstalling ran"
