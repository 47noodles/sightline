"""Shared runtime state (modes and toggles), readable from any thread.

Each change publishes ``StateChanged``.  Persistent preferences live in
``Settings``; this holds what is true right now.
"""

from __future__ import annotations

import dataclasses
import threading
from dataclasses import dataclass
from typing import Any

from .bus import EventBus
from .events import StateChanged
from .types import ClickType, PointerMode, SpeechMode


@dataclass
class StateSnapshot:
    speech_mode: SpeechMode = SpeechMode.AUTO
    pointer_mode: PointerMode = PointerMode.HYBRID
    pointer_paused: bool = False  # eye control paused (by voice, hot corner or the dock)
    face_present: bool = False
    gaze_calibrated: bool = False  # a usable gaze model is loaded
    eyes_calibrated: bool = False  # per-user blink and wink thresholds are loaded
    dwell_enabled: bool = False
    dwell_click: ClickType = ClickType.LEFT
    drag_active: bool = False  # a mouse button is held down for a drag
    # "labels", "grid", "zoom", "keyboard", "help", "setup", "calibration", "settings", "chooser", "menu", "confirm" or None
    active_overlay: str | None = None
    listening: bool = True  # microphone open (False while muted from the tray)
    speaking: bool = False  # user is mid-utterance (voice activity)
    reading_scroll: bool = False  # looking low or high on the page scrolls it (pointer.reading_scroll)


class AppState:
    def __init__(self, bus: EventBus, initial: StateSnapshot | None = None) -> None:
        self._bus = bus
        self._lock = threading.Lock()
        self._values = dataclasses.asdict(initial or StateSnapshot())
        self._fields = {f.name for f in dataclasses.fields(StateSnapshot)}

    def __getattr__(self, name: str) -> Any:
        # Only called for names that are not normal attributes.
        values = self.__dict__.get("_values")
        if values is not None and name in values:
            return values[name]
        raise AttributeError(name)

    def get(self, name: str) -> Any:
        with self._lock:
            return self._values[name]

    def snapshot(self) -> StateSnapshot:
        with self._lock:
            return StateSnapshot(**self._values)

    def update(self, **changes: Any) -> None:
        """Set one or more fields.  Publishes StateChanged for each field whose value changed."""
        unknown = set(changes) - self._fields
        if unknown:
            raise AttributeError(f"unknown state field(s): {', '.join(sorted(unknown))}")
        changed: list[StateChanged] = []
        with self._lock:
            for key, value in changes.items():
                old = self._values[key]
                if old != value:
                    self._values[key] = value
                    changed.append(StateChanged(key, old, value))
        for event in changed:
            self._bus.publish(event)
