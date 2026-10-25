"""EventBus to Qt: delivers events on the GUI thread without flooding its queue.

The bus calls subscribers synchronously on the publisher's thread (camera,
audio, actions).  Here those callbacks only store the event and, if no
delivery is pending yet, post one wake-up to the GUI thread.  High-rate
events are coalesced: only the newest of each type survives until the GUI
thread gets round to it, so a busy GUI thread drops stale pointer samples
instead of falling behind.  Everything else is queued in order.

The bridge is also the ``ActionRegistry`` UI invoker, so ``thread="ui"``
handlers run here.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication

from sightline.core.bus import EventBus, Subscription
from sightline.core.events import (
    CameraFrame,
    DwellProgress,
    EyeState,
    FaceStatus,
    GazeZoneProgress,
    HoverTarget,
    MicLevel,
    PointerSample,
)

log = logging.getLogger(__name__)

# Only the latest of each of these matters to anything that draws.
COALESCED: frozenset[type] = frozenset(
    {PointerSample, EyeState, MicLevel, CameraFrame, DwellProgress, GazeZoneProgress, HoverTarget, FaceStatus}
)

_SLOW_DRAIN_MS = 8.0


class Bridge(QObject):
    """Subscribe with ``on(EventType, handler)``; handlers run on the GUI thread."""

    _wake = Signal()
    _call = Signal(object)

    def __init__(self, bus: EventBus, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._bus = bus
        self._lock = threading.Lock()
        self._queue: deque[object] = deque()
        self._latest: dict[type, object] = {}
        self._pending = False
        self._handlers: dict[type, list[Callable[[Any], None]]] = {}
        self._subs: dict[type, Subscription] = {}
        self._closed = False
        self._gui_thread = QThread.currentThread()
        self.delivered = 0  # events handed to handlers, for tests and diagnostics
        self.wakeups = 0
        # Queued connections: emitting from any thread posts one event to the GUI thread.
        from PySide6.QtCore import Qt

        self._wake.connect(self._drain, Qt.ConnectionType.QueuedConnection)
        self._call.connect(self._run_call, Qt.ConnectionType.QueuedConnection)

    # ------------------------------------------------------------------ subscriptions

    def on(self, event_type: type, handler: Callable[[Any], None]) -> Callable[[], None]:
        """Run ``handler(event)`` on the GUI thread for every ``event_type`` (coalesced if high-rate).

        Returns a function that removes the handler again (for windows that come and go).
        """
        self._handlers.setdefault(event_type, []).append(handler)
        if event_type not in self._subs:
            self._subs[event_type] = self._bus.subscribe(event_type, self._receive)

        def remove() -> None:
            handlers = self._handlers.get(event_type)
            if handlers and handler in handlers:
                handlers.remove(handler)

        return remove

    def close(self) -> None:
        self._closed = True
        for sub in self._subs.values():
            sub.cancel()
        self._subs.clear()
        self._handlers.clear()
        with self._lock:
            self._queue.clear()
            self._latest.clear()

    # ------------------------------------------------------------------ any thread

    def _receive(self, event: object) -> None:
        if self._closed:
            return
        kind = type(event)
        with self._lock:
            if kind in COALESCED:
                self._latest[kind] = event
            else:
                self._queue.append(event)
            if self._pending:
                return
            self._pending = True
        self._wake.emit()

    # ------------------------------------------------------------------ GUI thread

    @Slot()
    def _drain(self) -> None:
        with self._lock:
            queued = list(self._queue)
            self._queue.clear()
            latest = list(self._latest.values())
            self._latest.clear()
            self._pending = False
        if self._closed:
            return
        self.wakeups += 1
        started = time.perf_counter()
        for event in queued:
            self._deliver(event)
        for event in latest:
            self._deliver(event)
        spent = (time.perf_counter() - started) * 1000.0
        if spent > _SLOW_DRAIN_MS:
            log.debug("Event delivery took %.1f ms for %d events", spent, len(queued) + len(latest))

    def _deliver(self, event: object) -> None:
        for klass in type(event).__mro__:
            for handler in self._handlers.get(klass, ()):
                try:
                    handler(event)
                except Exception:
                    log.exception("UI handler %r failed on %s", handler, type(event).__name__)
                self.delivered += 1

    def flush(self) -> None:
        """Deliver anything pending right now (GUI thread only; tests and shutdown)."""
        self._drain()

    # ------------------------------------------------------------------ calls onto the GUI thread

    def on_gui_thread(self) -> bool:
        return QThread.currentThread() is self._gui_thread

    def invoke(self, fn: Callable[[], None]) -> None:
        """The ActionRegistry UI invoker: run ``fn`` on the GUI thread.

        Called on the GUI thread itself (an action chained with
        ``execute_now`` from a UI handler) it runs at once, because queueing
        it would deadlock the registry, which waits for it to finish.
        """
        if self.on_gui_thread():
            fn()
        else:
            self._call.emit(fn)

    def post(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the GUI thread later, never inline.  Safe from interceptors on any thread."""
        if self.on_gui_thread():
            QTimer.singleShot(0, fn)
        else:
            self._call.emit(fn)

    @Slot(object)
    def _run_call(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            log.exception("Call on the GUI thread failed")


def process_events_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Spin the Qt event loop until ``predicate()`` is true (tests and tools)."""
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app is not None:
            app.processEvents()
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()
