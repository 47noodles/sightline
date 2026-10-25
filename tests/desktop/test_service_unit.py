"""WindowsDesktop construction and wiring, without starting anything that touches the desktop."""

from __future__ import annotations

import dataclasses
import threading
import time

import pytest

from sightline.core.events import FocusChanged, HoverTarget
from sightline.core.types import ElementInfo, FocusInfo, Rect
from sightline.desktop import create
from sightline.desktop.service import _ListsNotifier
from sightline.desktop.spatial import Target


def test_construction_is_cheap_and_starts_no_threads(ctx):
    before = {t.name for t in threading.enumerate()}
    started = time.perf_counter()
    desktop = create(ctx)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0
    assert {t.name for t in threading.enumerate()} == before
    # Usable before start(): vision and speech call these from their constructors.
    assert desktop.display.monitors()
    assert desktop.uia.focus_info() == FocusInfo()
    assert desktop.uia.nearest_clickable(10, 10, 40) is None
    assert desktop.uia.visible_names() == []
    assert isinstance(desktop.input.cursor_pos(), tuple)
    desktop.stop()  # never started: nothing to do


def test_registers_desktop_actions_once(ctx):
    create(ctx)
    names = set(ctx.actions.names())
    assert {"mouse.click", "key.press", "app.open", "keyboard.toggle", "magnifier.zoom"} <= names


def test_focus_and_hover_become_bus_events(ctx):
    desktop = create(ctx)
    seen: list[object] = []
    ctx.bus.subscribe(FocusChanged, seen.append)
    ctx.bus.subscribe(HoverTarget, seen.append)
    focus = FocusInfo(hwnd=5, process="notepad.exe", control_type="Document", editable=True, t=1.0)
    desktop.uia._set_focus(focus)
    element = ElementInfo(Rect(1, 2, 30, 20), "Save", "Button", clickable=True)
    desktop.uia._set_hover(Target(5, 5, element, True))
    desktop.uia._set_hover(Target(500, 500, None, False))
    assert seen == [
        FocusChanged(focus),
        HoverTarget(Rect(1, 2, 30, 20), "Save", "Button", True),
        HoverTarget(None),
    ]


def test_speech_hears_about_new_names_coalesced(ctx):
    calls: list[float] = []

    class Speech:
        def lists_changed(self):
            calls.append(time.monotonic())

    ctx.speech = Speech()
    notifier = _ListsNotifier(ctx, min_interval=0.0)
    for _ in range(10):
        notifier.poke()
    deadline = time.monotonic() + 2.0
    while not calls and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.4)
    assert len(calls) == 1


def test_system_checks_are_read_only_and_quick(ctx):
    desktop = create(ctx)
    started = time.perf_counter()
    checks = desktop.system_checks()
    assert time.perf_counter() - started < 1.0
    assert [c.id for c in checks] == [
        "camera_privacy", "microphone_privacy", "camera_sharing", "uac_prompts", "voice_access_signin", "keep_awake",
    ]  # fmt: skip
    ctx.settings.desktop.keep_awake = False
    assert next(c for c in desktop.system_checks() if c.id == "keep_awake").ok is False


# ------------------------------------------------------------------ start and stop (review S-08, I-01)


def test_start_lets_go_of_held_keys_and_buttons_before_anything_else(ctx, monkeypatch):
    desktop = create(ctx)
    order: list[str] = []
    monkeypatch.setattr(desktop.input, "release_all", lambda: order.append("release"))
    monkeypatch.setattr(desktop.display, "refresh", lambda: order.append("display"))
    monkeypatch.setattr(desktop.awake, "_apply", lambda on: order.append(f"awake {on}"))
    monkeypatch.setattr(desktop._events, "start", lambda: order.append("events"))
    monkeypatch.setattr(desktop.uia, "start", lambda: order.append("uia"))
    monkeypatch.setattr(desktop.apps, "refresh_async", lambda force=False: order.append("apps"))
    desktop.start()
    assert order == ["release", "display", "awake True", "events", "uia", "apps"]
    monkeypatch.setattr(desktop.uia, "stop", lambda: order.append("uia stop"))
    monkeypatch.setattr(desktop._events, "stop", lambda: order.append("events stop"))
    monkeypatch.setattr(desktop.keyboard, "close_and_put_back", lambda: order.append("keyboard settings"))
    desktop.stop()
    assert order[-4:] == ["release", "keyboard settings", "uia stop", "events stop"]


# ------------------------------------------------------------------ keeping the screen on (review S-13)


class _Timer:
    def __init__(self, delay: float, fn) -> None:
        self.delay, self.fn, self.daemon, self.started, self.cancelled = delay, fn, False, False, False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


def _policy(cfg, clock, applied, timers):
    from sightline.desktop.service import AwakePolicy

    def timer(delay, fn):
        timers.append(_Timer(delay, fn))
        return timers[-1]

    return AwakePolicy(applied.append, lambda: cfg, clock=lambda: clock[0], timer=timer)


def test_the_screen_stays_on_only_while_someone_is_there():
    from types import SimpleNamespace

    cfg = SimpleNamespace(keep_awake=True, keep_awake_minutes=5)
    clock, applied, timers = [1000.0], [], []
    policy = _policy(cfg, clock, applied, timers)
    policy.start()
    assert applied == [True], "no face seen yet (no camera, voice only): the setting alone decides"
    policy.face(True)
    assert applied == [True] and len(timers) == 1 and timers[0].started and timers[0].delay == pytest.approx(301.0)
    clock[0] += 200
    policy.face(False)
    assert applied == [True] and len(timers) == 1
    clock[0] += 101.5
    timers[0].fn()  # five minutes without a face: Windows may lock as usual
    assert applied == [True, False]
    policy.face(False)
    assert applied == [True, False]
    policy.face(True)  # back at the computer
    assert applied == [True, False, True] and len(timers) == 2
    cfg.keep_awake = False
    policy.update()
    assert applied[-1] is False
    policy.stop()
    assert timers[-1].cancelled
    policy.face(True)
    assert applied[-1] is False, "nothing after stop"


def test_the_wait_follows_the_setting():
    from types import SimpleNamespace

    from sightline.desktop.config import DesktopConfig

    cfg = SimpleNamespace(keep_awake=True, keep_awake_minutes=1)
    clock, applied, timers = [0.0], [], []
    policy = _policy(cfg, clock, applied, timers)
    policy.start()
    policy.face(True)
    assert timers[0].delay == pytest.approx(61.0)
    clock[0] += 61
    policy.face(False)
    assert applied == [True, False]
    meta = next(f for f in dataclasses.fields(DesktopConfig) if f.name == "keep_awake_minutes").metadata
    assert DesktopConfig().keep_awake_minutes == 5 and (meta["minimum"], meta["maximum"], meta["unit"]) == (1, 60, "minutes")


def test_face_status_reaches_the_policy_once_started(ctx, monkeypatch):
    from sightline.core.events import FaceStatus

    desktop = create(ctx)
    seen: list[bool] = []
    monkeypatch.setattr(desktop.awake, "face", seen.append)
    ctx.bus.publish(FaceStatus(True))
    assert seen == [], "not before start"
    desktop._started = True
    ctx.bus.publish(FaceStatus(True))
    ctx.bus.publish(FaceStatus(False))
    desktop._started = False
    assert seen == [True, False]

