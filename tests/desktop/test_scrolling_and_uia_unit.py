from __future__ import annotations

import threading
import time

import pytest

from sightline.core.fakes import FakeInput
from sightline.core.types import ElementInfo, FocusInfo, Rect
from sightline.desktop.scrolling import ContinuousScroller, ramp_rate
from sightline.desktop.spatial import ClickableSnapshot, Target
from sightline.desktop.uia import ElementRef, UIAProvider, _hover_key, _is_editable


class RecordingScroll:
    def __init__(self) -> None:
        self.calls: list[tuple[float, float, bool]] = []
        self.lock = threading.Lock()

    def scroll(self, dy=0.0, dx=0.0, *, smooth=False):
        with self.lock:
            self.calls.append((dy, dx, smooth))


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_ramp_is_gentle_then_faster():
    rates = [ramp_rate(t / 10, 1.0) for t in range(0, 40)]
    assert rates[0] == pytest.approx(2.0)
    assert rates[-1] == pytest.approx(8.0)
    assert all(b >= a for a, b in zip(rates, rates[1:], strict=False))
    assert ramp_rate(0.0, 2.0) == pytest.approx(4.0)
    assert ramp_rate(5.0, 0.0) == 0.0


def test_scroller_runs_until_stopped_and_reports_it():
    rec = RecordingScroll()
    stopped: list[bool] = []
    scroller = ContinuousScroller(rec, on_stop=lambda: stopped.append(True), tick_s=0.01)
    scroller.start("down")
    assert scroller.running and scroller.direction == "down"
    assert wait_for(lambda: len(rec.calls) >= 5)
    assert scroller.stop() is True
    assert stopped == [True]
    count = len(rec.calls)
    time.sleep(0.05)
    assert len(rec.calls) == count
    assert all(dy > 0 and dx == 0 and smooth for dy, dx, smooth in rec.calls)
    assert scroller.stop() is False


def test_scroller_changes_direction_and_rejects_nonsense():
    rec = RecordingScroll()
    scroller = ContinuousScroller(rec, tick_s=0.01, smooth=lambda: False)
    scroller.start("left", speed=2)
    assert wait_for(lambda: len(rec.calls) >= 3)
    scroller.start("up")
    with rec.lock:
        mark = len(rec.calls)
    assert wait_for(lambda: len(rec.calls) >= mark + 3)
    scroller.stop()
    with rec.lock:
        latest = rec.calls[-1]
    assert latest[0] < 0 and latest[1] == 0 and latest[2] is False
    with pytest.raises(ValueError):
        scroller.start("diagonal")


def test_scroller_works_with_the_protocol_injector():
    fake = FakeInput()
    scroller = ContinuousScroller(fake, tick_s=0.01)
    scroller.start("right")
    assert wait_for(lambda: "scroll" in fake.names())
    scroller.stop()
    assert all(args[0] == 0 and args[1] > 0 for name, args in fake.calls if name == "scroll")


@pytest.mark.parametrize(
    ("kind", "value", "read_only", "expected"),
    [
        ("Edit", True, False, True),
        ("Edit", False, False, True),
        ("Edit", True, True, False),
        ("Document", True, False, True),
        ("Document", True, True, False),  # a web page: the URL is its read-only value
        ("Document", False, False, False),  # decided later from the caret
        ("ComboBox", True, False, True),
        ("Button", True, False, False),
    ],
)
def test_editable_rule(kind, value, read_only, expected):
    assert _is_editable(kind, value, read_only) is expected


def element(name, x=0, y=0, w=10, h=10, root=5):
    return ElementInfo(Rect(x, y, w, h), name, "Button", clickable=True, handle=ElementRef("scan", (1, 0), root))


def test_provider_answers_from_its_caches_before_start():
    focus_events: list[FocusInfo] = []
    hovers: list[Target] = []
    provider = UIAProvider(on_focus=focus_events.append, on_hover=hovers.append, own_pid=1)
    assert provider.focus_info() == FocusInfo()
    assert provider.element_at(5, 5) is None
    assert provider.text_before_caret() is None
    assert provider.nearest_clickable(5, 5, 10) is None
    assert provider.invoke(ElementInfo(Rect(0, 0, 1, 1))) is False

    snap = ClickableSnapshot(elements=(element("File", 0, 0), element("Edit", 40, 0)), roots=frozenset({5}), generation=3)
    provider._set_snapshot(snap)
    assert provider.visible_names() == ["File", "Edit"]
    assert provider.nearest_clickable(45, 25, 20).name == "Edit"  # no window there: any root

    a = FocusInfo(hwnd=1, process="notepad.exe", control_type="Document", editable=True, t=1.0)
    provider._set_focus(a)
    provider._set_focus(FocusInfo(hwnd=1, process="notepad.exe", control_type="Document", editable=True, t=2.0))
    provider._set_focus(FocusInfo(hwnd=1, process="notepad.exe", control_type="Edit", editable=True, t=3.0))
    assert [f.control_type for f in focus_events] == ["Document", "Edit"]  # time alone is not a change
    assert provider.focus_info().t == 3.0

    provider._set_hover(Target(1, 1, snap.elements[0], False))
    provider._set_hover(Target(2, 2, snap.elements[0], False))  # same element: no new event
    provider._set_hover(Target(50, 5, snap.elements[1], True))
    provider._set_hover(Target(500, 500, None, False))
    assert [_hover_key(t)[1] if t.element else None for t in hovers] == ["File", "Edit", None]


def test_clickables_near_reads_the_snapshot_nearest_first(monkeypatch):
    from sightline.desktop import uia as uia_module

    provider = UIAProvider(own_pid=1)
    snap = ClickableSnapshot(
        elements=(element("Far", 200, 0, root=5), element("Near", 30, 0, root=5), element("Start", 0, 1000, root=9)),
        roots=frozenset({5, 9}),
        foreground=5,
        generation=1,
    )
    provider._set_snapshot(snap)
    monkeypatch.setattr(uia_module.w, "GetForegroundWindow", lambda: 5)
    monkeypatch.setattr(uia_module, "_is_taskbar", lambda hwnd: hwnd == 9)
    assert [e.name for e in provider.clickables_near(20, 5, 300)] == ["Near", "Far"]
    assert provider.clickables_near(20, 5, 5) == []
    # Another window came to the front and the rescan has not landed: only the taskbar is offered.
    monkeypatch.setattr(uia_module.w, "GetForegroundWindow", lambda: 77)
    assert provider.clickables_near(20, 5, 300) == []
    assert [e.name for e in provider.clickables_near(5, 990, 30)] == ["Start"]