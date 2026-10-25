"""The shell around the overlays: dock, status, captions, toasts, sounds, windows, setup and models."""

from __future__ import annotations

import time
import wave

import pytest

from sightline.core.events import (
    ActionExecuted,
    ClickPerformed,
    EyeHold,
    FaceStatus,
    IntentResult,
    ModelProgress,
    Notice,
    PointerSample,
    StateChanged,
    TranscriptFinal,
    TranscriptPartial,
)
from sightline.core.models import ModelFile, ModelSpec
from sightline.core.types import Action, ClickType, SpeechMode
from sightline.ui.bridge import process_events_until

pytestmark = pytest.mark.gui


def _publish(shell, *events) -> None:
    for e in events:
        shell.ctx.bus.publish(e)
    process_events_until(lambda: False, timeout=0.05)


# =========================================================================== windows and flags


def test_overlays_are_click_through_topmost_and_never_focused(shell):
    from PySide6.QtCore import Qt

    for window in (shell.ring, shell.gaze_dot, shell.ripple, shell.captions, shell.hud, shell.resume, *shell.highlight.edges):
        flags = window.windowFlags()
        assert flags & Qt.WindowType.WindowTransparentForInput, window
        assert flags & Qt.WindowType.WindowDoesNotAcceptFocus, window
        assert flags & Qt.WindowType.WindowStaysOnTopHint, window
        assert flags & Qt.WindowType.Tool, window
        assert window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    for window in (shell.dock, shell.help, shell.dock.flyout, shell.did_you_mean.bar):
        flags = window.windowFlags()
        assert not flags & Qt.WindowType.WindowTransparentForInput, "these are clicked"
        assert flags & Qt.WindowType.WindowDoesNotAcceptFocus


def test_pointer_ring_follows_vision_state(shell):
    ring = shell.ring
    shell.ctx.desktop.input.pos = (600, 400)
    shell.cursor.poll()
    _publish(shell, FaceStatus(True), PointerSample(time.perf_counter(), 600, 400, phase="fine"))
    assert ring.isVisible()
    lx, ly = shell.screens.map.to_logical(600, 400)
    assert ring.geometry().center().x() == pytest.approx(lx, abs=1)
    assert ring.geometry().center().y() == pytest.approx(ly, abs=1)
    _publish(shell, FaceStatus(False))
    assert ring.phase == "lost"
    shell.ctx.state.update(pointer_paused=True)
    _publish(shell)
    assert ring.phase == "paused"
    _publish(shell, EyeHold(0.0, 2))
    assert ring._hold == 2  # noqa: SLF001
    _publish(shell, EyeHold(0.0, 0, cancelled=True))
    assert ring._hold == 0  # noqa: SLF001


def test_highlight_outlines_the_hover_target_and_hides(shell):
    from sightline.core.events import HoverTarget
    from sightline.core.types import Rect

    _publish(shell, HoverTarget(Rect(300, 300, 200, 50), "Save", "Button"))
    edges = shell.highlight.edges
    assert all(e.isVisible() for e in edges)
    top = edges[0].geometry()
    lx, ly = shell.screens.map.to_logical(300, 300)
    assert top.left() < lx and top.bottom() <= ly
    _publish(shell, HoverTarget(None))
    assert not any(e.isVisible() for e in edges)


# =========================================================================== dock


def test_dock_buttons_dispatch_actions(recorder, ui_ctx, shell):
    ui_ctx.actions.start()
    try:
        dock = shell.dock
        dock.refresh_enabled()
        dock.buttons["dwell"].click()
        dock.buttons["keyboard"].click()
        dock.buttons["eyes"].click()
        dock.buttons["voice"].click()
        assert process_events_until(lambda: len(recorder) >= 4, timeout=3.0)
        names = [a.name for a in recorder]
        assert names[:4] == ["dwell.toggle", "keyboard.toggle", "pointer.pause", "speech.mode"]
        assert recorder[3].arg("mode") == "sleep"
        assert all(a.source == "ui" for a in recorder)
    finally:
        ui_ctx.actions.stop()


def test_dock_scroll_and_zoom_aim_where_the_user_was_looking(recorder, ui_ctx, shell):
    ui_ctx.actions.start()
    try:
        ui_ctx.desktop.input.pos = (700, 450)
        shell.cursor.poll()
        dock_rect = shell.dock._granted  # noqa: SLF001
        ui_ctx.desktop.input.pos = dock_rect.center  # now on the dock
        shell.cursor.poll()
        shell.dock.buttons["scroll_down"].click()
        assert process_events_until(lambda: any(a.name == "mouse.scroll" for a in recorder), timeout=3.0)
        scroll = next(a for a in recorder if a.name == "mouse.scroll")
        assert (scroll.arg("x"), scroll.arg("y"), scroll.arg("direction")) == (700, 450, "down")
    finally:
        ui_ctx.actions.stop()


def test_dock_menu_opens_the_action_menu_where_the_user_was_looking(recorder, ui_ctx, shell):
    from sightline.ui.overlays.action_menu import ActionMenu

    menu = next(u.overlay for u in shell._overlay_uninstalls if isinstance(u.overlay, ActionMenu))  # noqa: SLF001
    ui_ctx.actions.start()
    try:
        ui_ctx.desktop.input.pos = (700, 450)
        shell.cursor.poll()
        ui_ctx.desktop.input.pos = shell.dock._granted.center  # noqa: SLF001 - now on the dock
        shell.cursor.poll()
        shell.dock.refresh_enabled()
        button = shell.dock.buttons["menu"]
        assert button.isEnabled() and button.text() == "Menu"
        button.click()
        assert process_events_until(lambda: menu.is_open, timeout=3.0)
        assert menu.anchor == (700, 450), "where the user looked before looking at the dock"
        assert ui_ctx.state.active_overlay == "menu"
    finally:
        ui_ctx.actions.stop()


def test_dock_reflects_state(recorder, shell, ui_ctx):
    dock = shell.dock
    ui_ctx.state.update(pointer_paused=True, dwell_enabled=True, dwell_click=ClickType.RIGHT, speech_mode=SpeechMode.SLEEP)
    _publish(shell)
    assert dock.buttons["eyes"].text() == "Resume eyes" and dock.buttons["eyes"].is_on
    assert dock.buttons["dwell"].text() == "Dwell on" and dock.buttons["dwell"].is_on
    assert dock.buttons["click_type"].text() == "Right click"
    assert dock.buttons["voice"].text() == "Wake voice" and dock.buttons["voice"].is_on


def test_dock_click_type_flyout(recorder, ui_ctx, shell):
    ui_ctx.actions.start()
    try:
        shell.dock.buttons["click_type"].click()
        assert shell.dock.flyout.isVisible()
        shell.dock.flyout.buttons[ClickType.DOUBLE].click()
        assert not shell.dock.flyout.isVisible()
        assert process_events_until(lambda: any(a.name == "dwell.click_type" for a in recorder), timeout=3.0)
        assert next(a for a in recorder if a.name == "dwell.click_type").arg("type") == "double"
    finally:
        ui_ctx.actions.stop()


def test_dock_buttons_are_big_and_page_when_the_screen_is_short(shell):
    dock = shell.dock
    assert dock.isVisible()
    for button in dock.buttons.values():
        if button.isVisible():
            assert button.height() >= 64 and button.width() >= 64
    dock.resize(dock.width(), 520)
    assert dock.more_button.isVisible()
    shown = [b for b in dock.buttons.values() if b.isVisible()]
    assert 0 < len(shown) < len(dock.buttons)
    dock.more_button.click()
    assert any(b.isVisible() for b in dock.buttons.values() if b not in shown)


def test_dock_toggle_swaps_for_the_hud_and_settings_hide_it(shell, ui_ctx):
    ui_ctx.actions.execute_now(Action("ui.dock.toggle"))
    assert not shell.dock.isVisible() and shell.hud.isVisible()
    ui_ctx.actions.execute_now(Action("ui.dock.toggle"))
    assert shell.dock.isVisible() and not shell.hud.isVisible()
    ui_ctx.settings.ui.show_dock = False
    ui_ctx.settings_changed("ui")
    _publish(shell)
    assert not shell.dock.isVisible()


# =========================================================================== captions and toasts


def test_captions_partial_final_and_outcome(shell):
    cap = shell.captions
    _publish(shell, TranscriptPartial(1, "scroll"))
    assert cap.isVisible() and cap.text == "scroll" and not cap.is_final
    _publish(shell, TranscriptFinal(1, "scroll down three"))
    assert cap.text == "scroll down three" and cap.is_final
    _publish(shell, TranscriptPartial(1, "scroll down"))  # late partial is ignored
    assert cap.text == "scroll down three"
    _publish(shell, IntentResult(1, "scroll down three", "command", "Scroll down 3"))
    assert cap.outcome == "Scroll down 3"
    _publish(shell, TranscriptPartial(2, "open"))
    assert cap.text == "open" and cap.outcome == ""


def test_captions_stay_quiet_while_asleep_except_for_waking(shell, ui_ctx):
    cap = shell.captions
    cap.hide_now()
    ui_ctx.state.update(speech_mode=SpeechMode.SLEEP)
    _publish(shell, TranscriptPartial(5, "private conversation"), IntentResult(5, "private conversation", "ignored", ""))
    assert not cap.isVisible()
    _publish(shell, IntentResult(6, "wake up", "command", "Awake"))
    assert cap.isVisible() and cap.outcome == "Awake"


def test_captions_say_why_talk_was_ignored_without_keeping_the_words(shell, ui_ctx):
    cap = shell.captions
    cap.hide_now()
    _publish(shell, TranscriptFinal(7, "tell her I said hi"), IntentResult(7, "tell her I said hi", "ignored", "Ignored: you were looking away"))
    assert cap.isVisible() and cap.outcome == "Ignored: you were looking away"
    assert cap.text == ""


def test_captions_setting_turns_them_off(shell, ui_ctx):
    ui_ctx.settings.ui.show_captions = False
    ui_ctx.settings_changed("ui")
    _publish(shell, TranscriptPartial(9, "hello"))
    assert not shell.captions.isVisible()


def _rejected(uid: int, *alternatives: str) -> IntentResult:
    return IntentResult(uid, "scrol dawn", "rejected", "Not understood", alternatives=alternatives)


def test_did_you_mean_shows_numbered_choices_and_a_click_runs_one(recorder, ui_ctx, shell):
    from PySide6.QtCore import Qt

    chosen: list[Action] = []
    ui_ctx.actions.register("speech.choose", chosen.append)
    ui_ctx.actions.start()
    try:
        _publish(shell, TranscriptFinal(4, "scrol dawn"), _rejected(4, "Scroll down", "Scroll up"))
        bar = shell.did_you_mean.bar
        assert bar.isVisible()
        assert bar.labels == ["Scroll down", "Scroll up"]
        assert [b.badge for b in bar.buttons] == ["1", "2"]
        flags = bar.windowFlags()
        assert not flags & Qt.WindowType.WindowTransparentForInput, "a blink is a real click, so the chips take clicks"
        assert flags & Qt.WindowType.WindowDoesNotAcceptFocus and flags & Qt.WindowType.WindowStaysOnTopHint
        assert all(b.height() >= shell.theme.px(64) and b.width() >= shell.theme.px(150) for b in bar.buttons)
        # Above the tallest caption, inside the work area of the screen the pointer is on.
        screen = shell.screens.map.screen_at(*shell.cursor.pos)
        work = shell.screens.map.rect_to_logical(screen.work_rect, screen)
        geo = bar.geometry()
        assert geo.bottom() <= work.bottom - shell.captions.reserved_height()
        assert work.x <= geo.left() and geo.right() <= work.right and geo.top() >= work.y
        assert shell.captions.geometry().top() > geo.bottom()

        bar.buttons[1].click()
        assert process_events_until(lambda: bool(chosen), timeout=3.0)
        assert chosen[0].name == "speech.choose" and dict(chosen[0].args) == {"index": 2} and chosen[0].source == "ui"
        assert not bar.isVisible() and bar.buttons == []
    finally:
        ui_ctx.actions.stop()


def test_did_you_mean_goes_away_on_the_next_result_or_after_a_while(shell, ui_ctx, monkeypatch):
    from sightline.ui.overlays import choices

    bar = shell.did_you_mean.bar
    _publish(shell, _rejected(1, "Close window", "Close tab", "Close menu", "Close all"))
    assert bar.labels == ["Close window", "Close tab", "Close menu"], "three at most"
    _publish(shell, IntentResult(2, "what a day", "ignored", ""))
    assert bar.isVisible(), "talk not meant for the computer leaves the choices up"
    _publish(shell, IntentResult(3, "scroll down", "command", "Scroll down"))
    assert not bar.isVisible()
    _publish(shell, _rejected(4, "Scroll down"))
    _publish(shell, _rejected(5))
    assert not bar.isVisible(), "a rejected result with nothing to offer clears the old choices"

    monkeypatch.setattr(choices, "RECHECK_MS", 40)
    bar.seconds = 0.05
    _publish(shell, _rejected(6, "Scroll down", "Scroll up"))
    centre = bar.geometry().center()
    ui_ctx.desktop.input.pos = shell.screens.map.to_physical(centre.x(), centre.y())
    shell.cursor.poll()
    process_events_until(lambda: False, timeout=0.2)
    assert bar.isVisible(), "the pointer resting on the choices keeps them up"
    ui_ctx.desktop.input.pos = (5, 5)
    shell.cursor.poll()
    assert process_events_until(lambda: not bar.isVisible(), timeout=2.0)


def test_an_offer_to_remember_a_mishearing_has_its_own_title(shell):
    bar = shell.did_you_mean.bar
    offer = "Say yes to remember 'scold down' as 'scroll down'"
    # Speech ran the command the user gave next, and offers to remember how it heard it the first time.
    _publish(shell, IntentResult(5, "scroll down", "command", "Scroll down", alternatives=(offer,)))
    assert bar.isVisible() and bar.labels == [offer]
    assert bar.title == "Remember this?" and bar.hint.startswith("Say yes")
    assert [b.badge for b in bar.buttons] == ["1"]
    _publish(shell, _rejected(6, "Scroll down", "Scroll up"))
    assert bar.title == "Did you mean" and bar.labels == ["Scroll down", "Scroll up"]
    _publish(shell, IntentResult(7, "shut it", "rejected", "Say 1 to confirm: Close window", alternatives=("Close window",)))
    assert bar.title == "Did you mean", "a check before a hard-to-undo command answers a miss too"


def test_failed_actions_and_notices_become_toasts(shell):
    _publish(
        shell,
        ActionExecuted(Action("window.close"), False, "No window to close"),
        ActionExecuted(Action("magic.thing"), False, "unknown action 'magic.thing'"),
        ActionExecuted(Action("mouse.click"), False, "TypeError: bad things"),
        ActionExecuted(Action("mouse.click"), True),
    )
    texts = [t.entry.text for t in shell.toasts.toasts]
    assert texts == ["No window to close", "That command is not available yet.", "Something went wrong. The log has the details."]
    for _ in range(3):
        _publish(shell, Notice("Camera lost", "warn", key="camera"))
    assert sum(t.entry.text == "Camera lost" for t in shell.toasts.toasts) == 1
    assert len(shell.toasts.toasts) <= 3


# =========================================================================== sounds


def test_sound_files_exist_and_are_short_soft_wavs():
    from sightline.ui.sounds import SOUND_NAMES, sounds_dir

    for name in SOUND_NAMES:
        path = sounds_dir() / f"{name}.wav"
        assert path.exists(), path
        with wave.open(str(path)) as w:
            assert w.getnchannels() == 1 and w.getsampwidth() == 2
            assert w.getnframes() / w.getframerate() < 1.2


def test_sounds_load_and_map_events(qapp):
    from sightline.ui.sounds import SOUND_NAMES, Sounds

    sounds = Sounds(enabled=False, volume=0.0)  # silent: tests never play audio
    sounds.load()
    loaded = sounds.loaded()
    assert set(loaded) == set(SOUND_NAMES)
    assert all(path.is_file() for path in loaded.values())
    assert not sounds.thread_running  # sounds off: no reason to open the audio device
    sounds.on_eye_hold(EyeHold(0, 1))
    sounds.on_eye_hold(EyeHold(0, 2))
    sounds.on_eye_hold(EyeHold(0, 0, cancelled=True))
    sounds.on_click(ClickPerformed(0, 0, "right"))
    time.sleep(0.06)
    sounds.on_click(ClickPerformed(0, 0, "left", 2))
    sounds.on_intent(IntentResult(1, "x", "rejected", "Not understood"))
    sounds.on_intent(IntentResult(2, "click", "command", "Click", (Action("mouse.click"),)))  # the click sound covers it
    sounds.on_intent(IntentResult(3, "copy", "command", "Copy", (Action("key.press", {"combo": "ctrl+c"}),)))
    sounds.on_state(StateChanged("speech_mode", SpeechMode.AUTO, SpeechMode.SLEEP))
    sounds.on_state(StateChanged("speech_mode", SpeechMode.SLEEP, SpeechMode.AUTO))
    sounds.on_state(StateChanged("pointer_paused", False, True))
    sounds.on_action(ActionExecuted(Action("x"), False, "nope"))
    sounds.on_notice(Notice("again", "error"))  # within the error gap: one error sound only
    assert sounds.played == [
        "hold_1", "hold_2", "right_click", "double_click", "not_understood", "recognised",
        "sleep", "wake", "eyes_paused", "error",
    ]  # fmt: skip


def test_sound_thread_starts_when_sounds_are_on_and_stop_never_hangs(qapp):
    from sightline.ui.sounds import Sounds

    sounds = Sounds(enabled=True, volume=0.5)
    sounds.load()  # starts the thread; nothing is played
    assert sounds.thread_running
    t = time.monotonic()
    sounds.stop()
    assert time.monotonic() - t < 1.6  # a stuck audio stack is left behind, not waited for
    assert not sounds.thread_running


class _FakeEffect:
    class Status:
        Ready = "ready"
        Loading = "loading"

    def __init__(self, status: str = "ready") -> None:
        self._status = status
        self._volume = 0.5
        self.plays = 0

    def status(self) -> str:
        return self._status

    def volume(self) -> float:
        return self._volume

    def setVolume(self, v: float) -> None:  # noqa: N802 - Qt's name
        self._volume = v

    def isPlaying(self) -> bool:  # noqa: N802 - Qt's name
        return False

    def stop(self) -> None:
        pass

    def play(self) -> None:
        self.plays += 1


def test_sound_player_drops_late_and_unloaded_requests(qapp):
    from sightline.ui.sounds import _STALE_S, _Player

    player = _Player({}, 0.5, None)
    ready, loading = _FakeEffect(), _FakeEffect("loading")
    player.effects = {"click": ready, "wake": loading}
    player.play("click", 0.8, time.monotonic())
    assert ready.plays == 1 and ready.volume() == 0.8
    player.play("click", 0.8, time.monotonic() - _STALE_S - 0.1)  # waited too long behind a stuck audio stack
    assert ready.plays == 1
    player.play("wake", 0.8, time.monotonic())  # Qt would queue it until loaded, then play it late
    assert loading.plays == 0
    player.play("missing", 0.8, time.monotonic())


# =========================================================================== models and setup


def _spec(model_id: str = "fake_model") -> ModelSpec:
    return ModelSpec(model_id, "Fake model", (ModelFile("https://example.invalid/m", "m.bin", "0" * 64, 1000),), "MIT")


def test_ensure_models_with_nothing_missing_calls_back_at_once(shell):
    called: list[int] = []
    shell.ensure_models(lambda: called.append(1))
    assert called == [1]


def test_ensure_models_downloads_on_a_worker_then_calls_back(shell, ui_ctx, monkeypatch):
    spec = _spec()
    ui_ctx.models.register(spec)
    ready = {"done": False}

    def fake_ensure(ids=None, *, on_progress=None, cancel=None):  # noqa: ANN001, ARG001
        ui_ctx.bus.publish(ModelProgress(spec.id, "downloading", 500, 1000))
        ui_ctx.bus.publish(ModelProgress(spec.id, "ready", 1000, 1000))
        ready["done"] = True

    monkeypatch.setattr(ui_ctx.models, "ensure", fake_ensure)
    monkeypatch.setattr(ui_ctx.models, "missing", lambda ids=None: [] if ready["done"] else [spec])
    called: list[int] = []
    shell.ensure_models(lambda: called.append(1))
    assert called == []
    window = shell._models_window  # noqa: SLF001
    assert window is not None and window.isVisible()
    assert process_events_until(lambda: called == [1], timeout=5.0)
    assert window.view.bar.value == pytest.approx(1.0)


def test_failed_download_offers_a_retry(shell, ui_ctx, monkeypatch):
    from sightline.core.models import ModelError

    spec = _spec("broken")
    ui_ctx.models.register(spec)

    def failing(ids=None, *, on_progress=None, cancel=None):  # noqa: ANN001, ARG001
        raise ModelError("could not download m.bin: offline")

    monkeypatch.setattr(ui_ctx.models, "ensure", failing)
    shell.ensure_models(lambda: None)
    view = shell._models_window.view  # noqa: SLF001
    assert process_events_until(lambda: view.retry_button.isVisible(), timeout=5.0)
    assert "offline" in view.error.text()
    assert "Trying again" in view.summary.text()
    view.cancel()


def test_theme_follows_the_setting(shell, ui_ctx):
    ui_ctx.settings.ui.theme = "light"
    ui_ctx.settings_changed("ui")
    _publish(shell)
    assert shell.theme.palette.name == "light"
    ui_ctx.settings.ui.theme = "dark"
    ui_ctx.settings.ui.scale = 1.5
    ui_ctx.settings_changed("ui")
    _publish(shell)
    assert shell.theme.palette.name == "dark"
    assert shell.theme.px(16) == 24


def test_stop_uninstalls_the_invoker_and_leaves_nothing_on_screen(qapp, ui_ctx):
    from sightline.ui.shell import UIShell

    s = UIShell(ui_ctx, qapp)
    s.start()
    _publish(s, FaceStatus(True), PointerSample(time.perf_counter(), 100, 100))
    s.stop()
    assert ui_ctx.actions._ui_invoker is None  # noqa: SLF001
    assert not [w for w in qapp.topLevelWidgets() if w.isVisible()]
    s.ring._stale_timer.timeout.emit()  # noqa: SLF001 - a late tick must not bring the ring back
    assert not s.ring.isVisible()
