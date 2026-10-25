"""Things every UI component shares: the context, theme, bridge, screens, cursor, sounds, overlays."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from .coords import ScreenMap

if TYPE_CHECKING:
    from sightline.core.context import AppContext
    from sightline.core.types import Action

    from .bridge import Bridge
    from .overlay_state import OverlayState
    from .overlays.toasts import ToastStack
    from .sounds import Sounds
    from .theme import Theme

log = logging.getLogger(__name__)


class Screens(QObject):
    """The current ScreenMap, rebuilt whenever a monitor is added, removed or rescaled."""

    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.map = ScreenMap.from_qt()
        app = QGuiApplication.instance()
        if app is not None:
            app.screenAdded.connect(self._watch_and_rebuild)
            app.screenRemoved.connect(lambda _s: self._rebuild())
            app.primaryScreenChanged.connect(lambda _s: self._rebuild())
            for screen in QGuiApplication.screens():
                self._watch(screen)

    def _watch(self, screen: object) -> None:
        screen.geometryChanged.connect(lambda _r: self._rebuild())  # type: ignore[attr-defined]
        screen.availableGeometryChanged.connect(lambda _r: self._rebuild())  # type: ignore[attr-defined]
        screen.logicalDotsPerInchChanged.connect(lambda _d: self._rebuild())  # type: ignore[attr-defined]

    def _watch_and_rebuild(self, screen: object) -> None:
        self._watch(screen)
        self._rebuild()

    def _rebuild(self) -> None:
        self.map = ScreenMap.from_qt()
        self.changed.emit()


class CursorTracker(QObject):
    """Where the real cursor is, in physical pixels, polled through the desktop's InputInjector.

    Polls at about 60 Hz while the pointer moves and drops to 10 Hz after a
    second of stillness, so an idle desktop costs next to nothing.  Vision's
    pointer samples call ``kick`` to switch back to the fast rate at once.
    """

    moved = Signal(float, float)

    FAST_MS = 16
    SLOW_MS = 100
    IDLE_S = 1.0

    def __init__(self, source: Callable[[], tuple[float, float]] | None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._source = source
        self._pos: tuple[float, float] = (0.0, 0.0)
        self._last_move = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(self.SLOW_MS)
        self._timer.timeout.connect(self.poll)

    @property
    def pos(self) -> tuple[float, float]:
        return self._pos

    def start(self) -> None:
        if self._source is not None:
            self.poll()
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def kick(self) -> None:
        self._last_move = time.monotonic()
        if self._timer.isActive() and self._timer.interval() != self.FAST_MS:
            self._timer.setInterval(self.FAST_MS)

    def set_pos(self, x: float, y: float) -> None:
        """Feed a position directly (no desktop service, tests)."""
        if (x, y) != self._pos:
            self._pos = (x, y)
            self.moved.emit(x, y)

    def poll(self) -> None:
        if self._source is None:
            return
        try:
            x, y = self._source()
        except Exception:
            log.debug("cursor_pos failed", exc_info=True)
            return
        now = time.monotonic()
        if (x, y) != self._pos:
            self._pos = (float(x), float(y))
            self._last_move = now
            self.moved.emit(float(x), float(y))
        fast = now - self._last_move < self.IDLE_S
        interval = self.FAST_MS if fast else self.SLOW_MS
        if self._timer.interval() != interval:
            self._timer.setInterval(interval)


@dataclass
class UiServices:
    ctx: AppContext
    theme: Theme
    bridge: Bridge
    screens: Screens
    cursor: CursorTracker
    sounds: Sounds
    overlays: OverlayState
    toasts: ToastStack | None = None  # set once the toast stack exists

    def aim(self, action: Action | None) -> tuple[float, float] | None:
        """Where an action means "here", in physical pixels, or None to use the cursor.

        The action's own ``x`` and ``y`` win.  Otherwise a spoken command carries ``aim_t``,
        the moment the user began to speak, and vision says where the pointer rested then:
        people look at the target before they say "zoom", and the pointer moves on.
        Safe on any thread (interceptors call it): ``aim_point`` never blocks.
        """
        if action is None:
            return None
        x, y = action.arg("x"), action.arg("y")
        if x is not None and y is not None:
            return (float(x), float(y))
        vision = self.ctx.vision
        if action.aim_t is None or vision is None:
            return None
        try:
            point = vision.aim_point(action.aim_t)
        except Exception:  # vision's trouble must not cost the command; the cursor is a fair answer
            log.debug("aim_point failed", exc_info=True)
            return None
        return (float(point[0]), float(point[1])) if point is not None else None

    def here(self, action: Action | None) -> tuple[float, float]:
        """``aim`` with the cursor as the fallback (GUI thread)."""
        return self.aim(action) or self.cursor.pos

    def notify(self, text: str, level: str = "info", key: str | None = None) -> None:
        """Show a toast from inside the UI (other subsystems publish a Notice instead)."""
        if self.toasts is not None:
            self.toasts.show(text, level, key)
        else:
            log.info("Notice (%s, %d characters)", level, len(text))  # its words can name what was said or clicked
