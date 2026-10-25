"""The bridge: GUI-thread delivery, coalescing of high-rate events, and the UI invoker."""

from __future__ import annotations

import threading
import time

import pytest

from sightline.core.actions import ActionRegistry
from sightline.core.bus import EventBus
from sightline.core.events import EyeHold, MicLevel, PointerSample
from sightline.core.types import Action
from sightline.ui.bridge import Bridge, process_events_until

pytestmark = pytest.mark.gui


def test_high_rate_events_are_coalesced_to_the_latest(qapp):
    bus = EventBus()
    bridge = Bridge(bus)
    seen: list[PointerSample] = []
    bridge.on(PointerSample, seen.append)

    def flood() -> None:
        for i in range(2000):
            bus.publish(PointerSample(float(i), float(i), 0.0))

    t = threading.Thread(target=flood)
    t.start()
    t.join()
    # Nothing ran yet: the GUI thread has not been back to its event loop.
    assert seen == []
    assert process_events_until(lambda: bool(seen))
    qapp.processEvents()
    assert len(seen) == 1
    assert seen[0].t == 1999.0
    assert bridge.wakeups == 1  # one queued wake-up for 2000 events, never a flood
    bridge.close()


def test_ordered_events_all_arrive_in_order_alongside_coalesced_ones(qapp):
    bus = EventBus()
    bridge = Bridge(bus)
    holds: list[int] = []
    levels: list[float] = []
    bridge.on(EyeHold, lambda e: holds.append(e.stage))
    bridge.on(MicLevel, lambda e: levels.append(e.rms))

    def publish() -> None:
        for i in range(50):
            bus.publish(EyeHold(0.0, i % 3))
            bus.publish(MicLevel(i / 100.0, False))

    t = threading.Thread(target=publish)
    t.start()
    t.join()
    assert process_events_until(lambda: len(holds) == 50)
    assert holds == [i % 3 for i in range(50)]
    assert levels[-1] == pytest.approx(0.49)
    assert len(levels) < 50
    bridge.close()


def test_handlers_run_on_the_gui_thread(qapp):
    bus = EventBus()
    bridge = Bridge(bus)
    threads: list[threading.Thread] = []
    bridge.on(EyeHold, lambda _e: threads.append(threading.current_thread()))
    worker = threading.Thread(target=lambda: bus.publish(EyeHold(0.0, 1)))
    worker.start()
    worker.join()
    assert process_events_until(lambda: bool(threads))
    assert threads == [threading.main_thread()]
    bridge.close()


def test_remove_handler_and_close(qapp):
    bus = EventBus()
    bridge = Bridge(bus)
    seen: list[object] = []
    remove = bridge.on(EyeHold, seen.append)
    remove()
    bus.publish(EyeHold(0.0, 1))
    qapp.processEvents()
    assert seen == []
    bridge.close()
    assert bus.subscriber_count(EyeHold) == 0


def test_invoker_runs_on_the_gui_thread_and_inline_from_it(qapp):
    bus = EventBus()
    bridge = Bridge(bus)
    where: list[str] = []
    bridge.invoke(lambda: where.append("inline:" + threading.current_thread().name))
    assert where == ["inline:" + threading.main_thread().name]

    t = threading.Thread(target=lambda: bridge.invoke(lambda: where.append(threading.current_thread().name)))
    t.start()
    t.join()
    assert process_events_until(lambda: len(where) == 2)
    assert where[1] == threading.main_thread().name
    bridge.close()


def test_registry_ui_actions_run_through_the_bridge(qapp):
    bus = EventBus()
    registry = ActionRegistry(bus, ui_timeout=3.0)
    bridge = Bridge(bus)
    registry.set_ui_invoker(bridge.invoke)
    ran_on: list[str] = []
    registry.register("ui.test", lambda _a: ran_on.append(threading.current_thread().name), thread="ui")
    registry.start()
    try:
        registry.dispatch(Action("ui.test"))
        assert process_events_until(lambda: bool(ran_on), timeout=3.0)
        assert ran_on == [threading.main_thread().name]
        # A UI handler chaining another UI action with execute_now must not deadlock.
        registry.register("ui.outer", lambda _a: registry.execute_now(Action("ui.test")), thread="ui")
        registry.dispatch(Action("ui.outer"))
        assert process_events_until(lambda: len(ran_on) == 2, timeout=3.0)
    finally:
        registry.stop()
        bridge.close()


def test_post_never_runs_inline(qapp):
    bus = EventBus()
    bridge = Bridge(bus)
    ran: list[int] = []
    bridge.post(lambda: ran.append(1))
    assert ran == []
    deadline = time.monotonic() + 1
    while not ran and time.monotonic() < deadline:
        qapp.processEvents()
    assert ran == [1]
    bridge.close()
