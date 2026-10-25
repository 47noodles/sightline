from __future__ import annotations

import json
import threading
import time

import pytest

from sightline.core.actions import GUARD_FAILED_MESSAGE, ActionError, ActionRegistry
from sightline.core.bus import EventBus
from sightline.core.events import ActionExecuted, StateChanged
from sightline.core.state import AppState
from sightline.core.types import PRIVATE, Action, PointerMode, Rect, SpeechMode
from sightline.settings import Settings


def test_bus_delivers_to_base_class_subscribers():
    bus = EventBus()
    seen: list[object] = []
    bus.subscribe(object, seen.append)
    bus.subscribe(StateChanged, seen.append)
    bus.publish(StateChanged("x", 1, 2))
    assert len(seen) == 2


def test_bus_survives_failing_subscriber():
    bus = EventBus()
    seen: list[object] = []

    def bad(_e):
        raise RuntimeError("boom")

    bus.subscribe(StateChanged, bad)
    bus.subscribe(StateChanged, seen.append)
    bus.publish(StateChanged("x", 1, 2))
    assert seen


def test_subscription_cancel():
    bus = EventBus()
    seen: list[object] = []
    sub = bus.subscribe(StateChanged, seen.append)
    sub.cancel()
    bus.publish(StateChanged("x", 1, 2))
    assert not seen


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_registry_runs_actions_in_order_and_reports():
    bus = EventBus()
    reg = ActionRegistry(bus)
    order: list[int] = []
    results: list[ActionExecuted] = []
    bus.subscribe(ActionExecuted, results.append)
    reg.register("t.add", lambda a: order.append(a.arg("n")))
    reg.start()
    try:
        for n in range(20):
            reg.dispatch(Action("t.add", {"n": n}))
        assert _wait_for(lambda: len(results) == 20)
        assert order == list(range(20))
        assert all(r.ok for r in results)
    finally:
        reg.stop()


def test_registry_interceptor_can_swallow_and_rewrite():
    bus = EventBus()
    reg = ActionRegistry(bus)
    got: list[Action] = []
    reg.register("mouse.click", got.append)
    remove = reg.add_interceptor(lambda a: a.with_args(x=5) if a.name == "mouse.click" else a, owner="test")
    reg.execute_now(Action("mouse.click", {"button": "left"}))
    assert got[-1].arg("x") == 5
    remove()
    reg.add_interceptor(lambda a: None, owner="swallow")
    reg.execute_now(Action("mouse.click"))
    assert len(got) == 1


def test_a_guard_that_breaks_drops_what_it_guards():
    # Review S-05: an interceptor that declares itself fail-closed never lets an action through by failing.
    bus = EventBus()
    reg = ActionRegistry(bus)
    ran: list[Action] = []
    results: list[ActionExecuted] = []
    bus.subscribe(ActionExecuted, results.append)
    reg.register("window.close", ran.append)

    def broken(_a):
        raise RuntimeError("the guard itself failed")

    remove = reg.add_interceptor(broken, priority=120, owner="ui.confirm", fail_closed=True)
    with pytest.raises(ActionError, match="safety check failed"):
        reg.execute_now(Action("window.close", source="voice"))
    assert ran == []
    reg.start()
    try:
        reg.dispatch(Action("window.close", source="voice"))
        assert _wait_for(lambda: len(results) == 1)
    finally:
        reg.stop()
    assert ran == [] and results[-1].ok is False and results[-1].error == GUARD_FAILED_MESSAGE
    remove()
    # An ordinary interceptor that fails is still skipped, as before.
    reg.add_interceptor(broken, owner="cosmetic")
    reg.execute_now(Action("window.close", source="voice"))
    assert len(ran) == 1


def test_registry_reports_errors_and_unknown_actions():
    bus = EventBus()
    reg = ActionRegistry(bus)
    results: list[ActionExecuted] = []
    bus.subscribe(ActionExecuted, results.append)

    def fails(_a):
        raise ActionError("No window to close")

    reg.register("window.close", fails)
    reg.start()
    try:
        reg.dispatch(Action("window.close"))
        reg.dispatch(Action("does.not.exist"))
        assert _wait_for(lambda: len(results) == 2)
        assert results[0].error == "No window to close"
        assert not results[1].ok
    finally:
        reg.stop()


def test_registry_undo_reverses_the_last_user_action():
    bus = EventBus()
    reg = ActionRegistry(bus)
    done: list[str] = []
    reg.register("t.min", lambda a: done.append("min") or 42, undo=lambda a, hwnd: Action("t.restore", {"hwnd": hwnd}, "system"))
    reg.register("t.restore", lambda a: done.append(f"restore {a.arg('hwnd')}"))
    reg.register("t.close", lambda a: done.append("close"), description="Close a window")
    reg.register("t.overlay", lambda a: done.append("overlay"), history=False)
    results: list[ActionExecuted] = []
    bus.subscribe(ActionExecuted, results.append)
    reg.start()
    try:
        reg.dispatch(Action("t.min"))
        reg.dispatch(Action("t.close", utterance="close window"))
        reg.dispatch(Action("t.overlay"))
        reg.dispatch(Action("action.undo"))  # the close can't be undone; the overlay doesn't count
        reg.dispatch(Action("action.undo"))  # reverses the minimise
        reg.dispatch(Action("action.undo"))  # nothing left
        reg.dispatch(Action("t.close", source="system"))  # not the user's doing, so not remembered
        reg.dispatch(Action("action.undo"))
        assert _wait_for(lambda: len(results) == 8)
    finally:
        reg.stop()
    undos = [r for r in results if r.action.name == "action.undo"]
    assert undos[0].error == "Sightline can't undo “close window”."
    assert undos[1].ok
    assert undos[2].error == undos[3].error == "There is nothing to undo."
    assert done == ["min", "close", "overlay", "restore 42", "close"]


def test_registry_refuses_ui_handlers_off_the_main_thread_without_an_invoker():
    bus = EventBus()
    reg = ActionRegistry(bus)
    calls: list[str] = []
    reg.register("ui.thing", lambda a: calls.append(threading.current_thread().name), thread="ui")
    reg.execute_now(Action("ui.thing"))  # the main thread may run it inline (tests, start-up)
    assert calls == [threading.main_thread().name]
    results: list[ActionExecuted] = []
    bus.subscribe(ActionExecuted, results.append)
    reg.start()
    try:
        reg.dispatch(Action("ui.thing"))
        assert _wait_for(lambda: len(results) == 1)
    finally:
        reg.stop()
    assert results[0].error == "The Sightline window isn't ready yet."
    assert len(calls) == 1


def test_fakes_satisfy_the_contracts():
    from sightline.core.fakes import FakeDesktop, FakeVision
    from sightline.core.interfaces import Desktop, UIAProvider, VisionAPI, WindowManager

    desktop = FakeDesktop()
    assert isinstance(desktop, Desktop) and isinstance(desktop.uia, UIAProvider)
    assert isinstance(desktop.windows, WindowManager)
    assert isinstance(FakeVision(), VisionAPI)
    assert desktop.system_checks()[0].id == "camera_privacy"


def test_fake_windows_answer_the_window_under_a_point():
    from sightline.core.fakes import FakeWindows
    from sightline.core.types import WindowInfo

    front = WindowInfo(1, "Front", "a.exe", Rect(0, 0, 400, 300))
    minimised = WindowInfo(2, "Minimised", "b.exe", Rect(0, 0, 1920, 1080), minimized=True)
    back = WindowInfo(3, "Back", "c.exe", Rect(0, 0, 1920, 1080))
    windows = FakeWindows([front, minimised, back])
    assert windows.window_at(100, 100) is front, "front to back, like the real list"
    assert windows.window_at(1000, 500) is back, "a minimised window is under nothing"
    assert windows.window_at(3000, 500) is None


def test_setting_metadata_marks_read_only_values():
    import dataclasses

    from sightline.core.settings_base import setting

    @dataclasses.dataclass
    class Config:
        learned: str = setting("Not learned yet", label="What Sightline learned", readonly=True)
        chosen: int = setting(3, label="What you chose", minimum=1, maximum=5)

    meta = {f.name: f.metadata for f in dataclasses.fields(Config)}
    assert meta["learned"]["readonly"] is True
    assert meta["chosen"]["readonly"] is False


def test_setting_metadata_marks_protected_values():
    import dataclasses

    from sightline.core.settings_base import setting

    @dataclasses.dataclass
    class Config:
        guard: bool = setting(True, label="A safety check", protected=True)
        plain: bool = setting(True, label="Something else")

    meta = {f.name: f.metadata for f in dataclasses.fields(Config)}
    assert meta["guard"]["protected"] is True and meta["plain"]["protected"] is False


def test_action_descriptions_leave_out_what_was_said_typed_or_named():
    # Review S-02: names, addresses, queries and text become their length in the log.
    for key, value in (("text", "my diagnosis"), ("query", "my diagnosis"), ("url", "https://my-diagnosis.example"),
                       ("phrase", "my diagnosis"), ("start", "my diagnosis"), ("end", "my diagnosis"), ("name", "My diagnosis.docx - Word")):
        described = Action("x.y", {key: value, "count": 2}).describe()
        assert "diagnosis" not in described.lower(), key
        assert f"{key}=<{len(value)} chars>" in described and "count=2" in described
    assert "ctrl+shift+n" in Action("key.press", {"combo": "ctrl+shift+n"}).describe()
    assert Action("key.press", {"combo": "h", PRIVATE: True}).describe() == "key.press(<private>)"


def test_pointer_samples_are_not_steered_unless_vision_says_so():
    from sightline.core.events import PointerSample

    assert PointerSample(0.0, 1.0, 2.0).steered is False
    assert PointerSample(0.0, 1.0, 2.0, phase="fine", steered=True).steered is True


def test_registry_ui_handlers_use_invoker():
    bus = EventBus()
    reg = ActionRegistry(bus)
    calls: list[str] = []
    reg.register("ui.thing", lambda a: calls.append(threading.current_thread().name), thread="ui")

    def invoker(fn):
        t = threading.Thread(target=fn, name="fake-gui")
        t.start()

    reg.set_ui_invoker(invoker)
    reg.execute_now(Action("ui.thing"))
    assert calls == ["fake-gui"]


def test_state_publishes_changes_only():
    bus = EventBus()
    state = AppState(bus)
    changes: list[StateChanged] = []
    bus.subscribe(StateChanged, changes.append)
    state.update(speech_mode=SpeechMode.SLEEP)
    state.update(speech_mode=SpeechMode.SLEEP)
    assert state.speech_mode is SpeechMode.SLEEP
    assert len(changes) == 1
    with pytest.raises(AttributeError):
        state.update(nonsense=1)


def test_settings_round_trip_and_tolerance(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings()
    s.vision.pointer_mode = PointerMode.HEAD
    s.vision.head_speed = 2.5
    s.speech.wake_phrase = "hello computer"
    s.save(path)
    loaded = Settings.load(path)
    assert loaded.vision.pointer_mode is PointerMode.HEAD
    assert loaded.vision.head_speed == 2.5
    assert loaded.speech.wake_phrase == "hello computer"

    data = json.loads(path.read_text())
    data["vision"]["head_speed"] = 99  # out of range, clamped
    data["vision"]["pointer_mode"] = "telepathy"  # invalid, default kept
    data["vision"]["unknown_field"] = True  # ignored
    data["speech"]["command_endpoint_ms"] = "fast"  # wrong type, default kept
    path.write_text(json.dumps(data))
    loaded = Settings.load(path)
    assert loaded.vision.head_speed == 4.0
    assert loaded.vision.pointer_mode is PointerMode.HYBRID
    assert loaded.speech.command_endpoint_ms == 250


def test_settings_corrupt_file_falls_back(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    loaded = Settings.load(path)
    assert loaded.general.first_run_complete is False
    assert (tmp_path / "settings.broken.json").exists()
    assert loaded.load_problem and "settings.broken.json" in loaded.load_problem


@pytest.mark.parametrize(
    "text",
    [
        '{"vision": {"head_speed": Infinity}}',
        '{"vision": {"head_speed": NaN}}',
        '{"vision": {"head_speed": -Infinity}}',
        '{"vision": {"head_speed": 1e999}}',
        '{"speech": {"command_endpoint_ms": 1e999}}',
        "[" * 100_000 + "]" * 100_000,
        '{"vision": ' + "[" * 40 + "]" * 40 + "}",
    ],
    ids=["infinity", "nan", "minus-infinity", "1e999-float", "1e999-int", "deep", "deeper-than-32"],
)
def test_settings_that_would_stop_sightline_starting_fall_back(tmp_path, text):
    # Review S-06: these raised OverflowError or RecursionError out of Settings.load, before logging existed.
    path = tmp_path / "settings.json"
    path.write_text(text, "utf-8")
    loaded = Settings.load(path)
    assert loaded.vision.head_speed == Settings().vision.head_speed
    assert (tmp_path / "settings.broken.json").exists() and not path.exists()
    assert loaded.load_problem


def test_a_huge_settings_file_is_not_read(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"general": {}, "pad": "' + "x" * 1_100_000 + '"}', "utf-8")
    assert Settings.load(path).load_problem


def test_settings_reject_non_finite_numbers_field_by_field():
    from sightline.core.settings_base import from_jsonable

    loaded = from_jsonable(Settings, {"vision": {"head_speed": float("inf"), "smoothing": 0.75}, "speech": {"command_endpoint_ms": float("nan")}})
    assert loaded.vision.head_speed == Settings().vision.head_speed and loaded.vision.smoothing == 0.75
    assert loaded.speech.command_endpoint_ms == 250


def test_strict_json_refuses_what_python_json_lets_through():
    from sightline.core.settings_base import loads_strict

    assert loads_strict('{"a": [1, 2.5, "x"]}') == {"a": [1, 2.5, "x"]}
    assert loads_strict('{"a": "[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[["}')["a"].startswith("[")  # brackets in text do not count
    for bad in ("NaN", "[Infinity]", "[-Infinity]", "[1e999]", "[" * 33 + "]" * 33):
        with pytest.raises(ValueError):
            loads_strict(bad)


def test_log_level_is_only_a_real_level_name():
    from sightline.core.log import level_name

    assert level_name("debug") == "DEBUG" and level_name(" WARNING ") == "WARNING"
    for bad in ("BASIC_FORMAT", "Logger", "", None, 10, "getLogger"):
        assert level_name(bad) == "INFO", bad


# =========================================================================== model downloads (review S-11)


class _Response:
    """A stand-in for a urllib response: a declared length (or none) and a body read in chunks."""

    def __init__(self, body: bytes, declared: int | None = None) -> None:
        self.body = body
        self.headers = {"Content-Length": str(declared)} if declared is not None else {}
        self.read_bytes = 0

    def read(self, n: int) -> bytes:
        chunk = self.body[self.read_bytes : self.read_bytes + n]
        self.read_bytes += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _one_file_spec(body: bytes, size: int | None = None):
    import hashlib

    from sightline.core.models import ModelFile, ModelSpec

    f = ModelFile("https://models.example/m.bin", "m.bin", hashlib.sha256(body).hexdigest(), len(body) if size is None else size)
    return ModelSpec("test_model", "Test model", (f,), "MIT")


def _manager(tmp_path, spec):
    from sightline.core.models import ModelManager

    manager = ModelManager(root=tmp_path / "models")
    manager.register(spec)
    return manager


def test_a_download_larger_than_promised_is_refused(tmp_path, monkeypatch):
    from sightline.core import models

    body = b"x" * 10_000
    spec = _one_file_spec(body)
    sent = _Response(body + b"y" * (2 * models.SIZE_MARGIN_BYTES), declared=None)  # says nothing, sends far more
    monkeypatch.setattr(models, "_open_url", lambda request, timeout: sent)
    with pytest.raises(models.ModelError, match="more than"):
        _manager(tmp_path, spec).ensure()
    assert sent.read_bytes <= models.size_limit(len(body)) + models._CHUNK, "it stopped reading, not after the whole body"
    assert not list((tmp_path / "models").rglob("*.part")) and not (tmp_path / "models" / "test_model" / "m.bin").exists()


def test_a_server_that_says_it_will_send_too_much_is_refused_at_once(tmp_path, monkeypatch):
    from sightline.core import models

    body = b"x" * 10_000
    sent = _Response(body, declared=50_000_000)
    monkeypatch.setattr(models, "_open_url", lambda request, timeout: sent)
    with pytest.raises(models.ModelError, match="offered"):
        _manager(tmp_path, _one_file_spec(body)).ensure()
    assert sent.read_bytes == 0


def test_a_download_within_the_size_is_verified_and_kept(tmp_path, monkeypatch):
    from sightline.core import models

    body = b"model weights" * 100
    monkeypatch.setattr(models, "_open_url", lambda request, timeout: _Response(body, declared=len(body)))
    manager = _manager(tmp_path, _one_file_spec(body))
    manager.ensure()
    assert (tmp_path / "models" / "test_model" / "m.bin").read_bytes() == body


def test_redirects_are_followed_only_to_https():
    import urllib.request

    from sightline.core.models import HttpsOnlyRedirects, ModelError, _open_url, size_limit

    handler = HttpsOnlyRedirects()
    request = urllib.request.Request("https://models.example/m.bin")
    for target in ("http://models.example/m.bin", "ftp://models.example/m.bin", "file:///c:/windows/win.ini"):
        with pytest.raises(ModelError, match="not https"):
            handler.redirect_request(request, None, 302, "Found", {}, target)
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://cdn.example/m.bin").full_url == "https://cdn.example/m.bin"
    with pytest.raises(ModelError, match="only downloaded over https"):
        _open_url(urllib.request.Request("http://models.example/m.bin"), timeout=1)
    assert size_limit(1000) == 1000 + 64 * 1024 and size_limit(100_000_000) == 101_000_000


def test_rect_helpers():
    r = Rect(10, 10, 100, 50)
    assert r.contains(10, 10) and not r.contains(110, 10)
    assert r.distance_to(50, 30) == 0
    assert r.distance_to(120, 30) == pytest.approx(10)
    assert r.center == (60, 35)
    assert Rect.from_ltrb(0, 0, 10, 10).intersects(Rect(5, 5, 10, 10))
