"""Bookkeeping for ``state.active_overlay``.

Speech reads ``active_overlay`` to decide what bare words mean ("12" while
the numbers are up, "next" while a wizard is open), so it must never be
stale.  At most one transient overlay (labels, grid, zoom, help) is open;
opening one closes the others.  A wizard ("setup", "calibration") sits
underneath and shows through when no transient overlay is open.  A value
set by another subsystem is left alone and comes back when our overlay
closes (the desktop does not set one for its on-screen keyboard today).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal

from sightline.core.state import AppState

log = logging.getLogger(__name__)

TRANSIENT = ("labels", "grid", "zoom", "help", "chooser", "menu", "confirm")
WIZARDS = ("setup", "calibration", "settings")  # windows that stay open underneath


class OverlayState(QObject):
    changed = Signal(object)  # the overlay name now in effect, or None

    def __init__(self, state: AppState, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._closers: dict[str, Callable[[], None]] = {}
        self._current: str | None = None
        self._wizard: str | None = None
        self._displaced: str | None = None  # someone else's value we covered up
        self._closing = False
        self._switching = False

    @property
    def current(self) -> str | None:
        return self._current

    @property
    def wizard(self) -> str | None:
        return self._wizard

    def register(self, name: str, close: Callable[[], None]) -> None:
        """``close`` hides the overlay and must end by calling ``closed(name)``."""
        self._closers[name] = close

    # ------------------------------------------------------------------ transient overlays

    def opening(self, name: str) -> None:
        """Call before showing an overlay: closes whichever other one is open."""
        if self._current is not None and self._current != name:
            self.close(self._current)

    def opened(self, name: str) -> None:
        # Switching straight from one overlay to another publishes once, not "labels", None, "grid".
        self._switching = True
        try:
            self.opening(name)
        finally:
            self._switching = False
        self._current = name
        self._publish()

    def closed(self, name: str) -> None:
        if self._current == name:
            self._current = None
            if not self._switching:
                self._publish()

    def close(self, name: str) -> None:
        closer = self._closers.get(name)
        if closer is None or self._closing:
            self.closed(name)
            return
        self._closing = True
        try:
            closer()
        except Exception:
            log.exception("Closing %s failed", name)
        finally:
            self._closing = False
        self.closed(name)

    def close_current(self) -> bool:
        if self._current is None:
            return False
        self.close(self._current)
        return True

    # ------------------------------------------------------------------ wizards

    def wizard_opened(self, name: str) -> None:
        self._wizard = name
        self._publish()

    def wizard_closed(self, name: str) -> None:
        if self._wizard == name:
            self._wizard = None
            self._publish()

    # ------------------------------------------------------------------ state

    def _ours(self, value: object) -> bool:
        return value in TRANSIENT or value in WIZARDS

    def _publish(self) -> None:
        wanted = self._current or self._wizard
        existing = self._state.get("active_overlay")
        if wanted is not None:
            if existing is not None and not self._ours(existing):
                self._displaced = existing
            self._state.update(active_overlay=wanted)
        elif self._ours(existing):
            restore, self._displaced = self._displaced, None
            self._state.update(active_overlay=restore)
        self.changed.emit(wanted)
