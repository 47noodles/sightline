"""Parts shared by the number chooser, the action menu and the confirm countdown.

* ``OverlaySlot`` opens and closes one of them through ``OverlayState``, so only one
  transient overlay is up at a time and ``state.active_overlay`` names it.
* ``NumberedTile`` and ``TileWindow``: a large numbered target in a window of its own,
  which takes clicks (a real click or an eye click) but never keyboard focus.
* ``point_of`` says where a click action points; ``OwnWindows`` says whether a point is
  over one of Sightline's own clickable windows.  Both are safe on the action thread.
* ``register_ui_actions`` and ``Teardown`` let each ``install`` function undo itself.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from collections.abc import Callable, Iterable
from ctypes import wintypes

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QFontMetricsF, QPainter
from PySide6.QtWidgets import QAbstractButton, QWidget

from sightline.core.types import Action, Rect

from .. import icons, winapi
from ..services import UiServices
from ..theme import Theme, frame_rect
from .base import OverlayWindow, solid_panel
from .choices import CHIP_HEIGHT

log = logging.getLogger(__name__)

TILE_HEIGHT = CHIP_HEIGHT  # logical pixels before ui.scale, the same as the "did you mean" choices
GAZE_TOLERANCE = 10.0  # logical pixels round a tile that still count as on it, as for the numbered labels
USER_SOURCES = frozenset({"eye", "dwell", "voice"})  # clicks the user aimed with their eyes or voice


# =========================================================================== overlay bookkeeping


class OverlaySlot:
    """One overlay's place in ``OverlayState``: opening closes any other, closing restores the old value.

    ``close`` hides the overlay and must end by calling ``closed()``.  OverlayState calls it
    when another overlay opens, on ``ui.cancel`` and when the shell stops.
    """

    def __init__(self, services: UiServices, name: str, close: Callable[[], None]) -> None:
        self.svc = services
        self.name = name
        self._close = close
        services.overlays.register(name, self._close_requested)

    @property
    def is_current(self) -> bool:
        return self.svc.overlays.current == self.name

    def opened(self) -> None:
        self.svc.overlays.opened(self.name)

    def closed(self) -> None:
        overlays = self.svc.overlays
        overlays.closed(self.name)
        # OverlayState only clears the names in its TRANSIENT list (labels, grid, zoom, help).  Until it
        # lists ours too it would leave "chooser" in active_overlay, and speech would keep sending bare
        # numbers to a chooser that has gone.  Once it does, this finds nothing left to clear.
        state = self.svc.ctx.state
        if overlays.current is None and state.get("active_overlay") == self.name:
            state.update(active_overlay=overlays.wizard)

    def release(self) -> None:
        """For uninstalling: close if open, and leave a harmless closer (OverlayState cannot unregister)."""
        if self.is_current:
            self.svc.overlays.close(self.name)
        self.svc.overlays.register(self.name, lambda: None)

    def _close_requested(self) -> None:
        self._close()


# =========================================================================== numbered tiles


class NumberedTile(QAbstractButton):
    """A large numbered target: the number in a solid square, then an optional icon and label.

    It sits on the desktop on its own, so it is drawn as an overlay panel (opaque fill, ink
    border) rather than as a ``GazeButton``, whose resting border is grey.  The badge, icon and
    label follow ``GazeButton``'s row layout.  Hover thickens the border.  ``armed`` means the
    pointer is on it, so a blink would pick it: the number turns amber, like a numbered label
    under the pointer.  ``on`` shows a running choice (the scroll direction) by inverting.
    """

    BADGE = 34.0

    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._number = ""
        self._icon: str | None = None
        self._armed = False
        self._on = False
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        theme.changed.connect(self.update)

    # ------------------------------------------------------------------ content and state

    def set_content(self, number: str, label: str = "", icon: str | None = None) -> None:
        self._number = number
        self._icon = icon
        self.setText(label)
        self.setAccessibleName(f"{number} {label}".strip())
        self.update()

    @property
    def number(self) -> str:
        return self._number

    @property
    def icon_name(self) -> str | None:
        return self._icon

    @property
    def armed(self) -> bool:
        return self._armed

    def set_armed(self, armed: bool) -> None:
        if armed != self._armed:
            self._armed = armed
            self.update()

    @property
    def is_on(self) -> bool:
        return self._on

    def set_on(self, on: bool) -> None:
        if on != self._on:
            self._on = on
            self.update()

    def natural_width(self, max_label: float | None = None) -> float:
        """Logical width that shows the whole label (or ``max_label`` of it); a bare number is a square-ish chip."""
        t = self._theme
        width = t.pxf(12) + t.pxf(self.BADGE)
        if self._icon:
            width += t.pxf(14) + t.pxf(24)
        if self.text():
            text_w = QFontMetricsF(t.font("label")).horizontalAdvance(self.text())
            if max_label is not None:
                text_w = min(text_w, max_label)
            width += t.pxf(12 if self._icon else 14) + text_w
        return width + (t.pxf(18) if (self._icon or self.text()) else t.pxf(12))

    # ------------------------------------------------------------------ painting

    def paintEvent(self, _event) -> None:  # noqa: ANN001 - Qt signature
        t = self._theme
        pal = t.palette
        p = QPainter(self)
        rect = QRectF(0, 0, self.width(), self.height())
        enabled = self.isEnabled()
        inverted = enabled and (self._on != self.isDown())  # pressing flips it, so the press is visible
        fill = pal.ink if inverted else (pal.surface if enabled else pal.ground)
        fg = pal.ground if inverted else (pal.ink if enabled else pal.ink_2)
        # The edge must show on any desktop: ink round a dark fill, the halo colour round an ink fill.
        edge = pal.halo if inverted else (pal.ink if enabled else pal.rule_strong)
        solid_panel(p, rect, fill, edge)
        if enabled and self.underMouse():
            frame_rect(p, rect.adjusted(1, 1, -1, -1), fg, t.pxf(2.5))

        side = t.pxf(self.BADGE)
        square = QRectF(t.pxf(12), (rect.height() - side) / 2.0, side, side)
        if self._armed and enabled:
            p.fillRect(square.adjusted(-1, -1, 1, 1), pal.on_accent)
            p.fillRect(square, pal.accent)
            number_color = pal.on_accent
        else:
            p.fillRect(square, fg)
            number_color = fill
        p.setFont(t.font("badge", size=18))
        p.setPen(number_color)
        p.drawText(square, int(Qt.AlignmentFlag.AlignCenter), self._number)

        x = square.right() + t.pxf(14)
        if self._icon:
            icon = t.pxf(24)
            icons.draw_icon(p, self._icon, QRectF(x, (rect.height() - icon) / 2.0, icon, icon), fg)
            x += icon + t.pxf(12)
        if self.text():
            font = t.font("label")
            p.setFont(font)
            p.setPen(fg)
            room = rect.width() - x - t.pxf(12)
            shown = QFontMetricsF(font).elidedText(self.text(), Qt.TextElideMode.ElideRight, room)
            p.drawText(QRectF(x, 0, room, rect.height()), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), shown)
        p.end()


class TileWindow(OverlayWindow):
    """One ``NumberedTile`` in a small window of its own: it takes clicks but never focus.

    Separate windows, not one big panel, so the gaps between tiles (and the target the
    action menu leaves open) stay clickable for whatever is underneath.
    """

    def __init__(self, services: UiServices, name: str) -> None:
        super().__init__(services, click_through=False, name=name)
        self.tile = NumberedTile(services.theme, self)
        self.physical = Rect(0, 0, 0, 0)  # where it was placed, in physical pixels

    def place(self, rect: Rect) -> None:
        self.physical = rect
        self.place_physical(rect)
        self.tile.setGeometry(0, 0, self.width(), self.height())

    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        super().resizeEvent(event)
        self.tile.setGeometry(0, 0, self.width(), self.height())


def tile_at(tiles: Iterable[tuple[int, Rect]], x: float, y: float, tolerance: float) -> int | None:
    """The number of the tile at a physical point (within ``tolerance``), nearest centre first; None when off them all."""
    best: tuple[float, int] | None = None
    for number, rect in tiles:
        if rect.distance_to(x, y) > tolerance:
            continue
        cx, cy = rect.center
        d = (cx - x) ** 2 + (cy - y) ** 2
        if best is None or d < best[0]:
            best = (d, number)
    return best[1] if best is not None else None


def gaze_tolerance(services: UiServices, x: float, y: float) -> float:
    """``GAZE_TOLERANCE`` in physical pixels at a point."""
    return GAZE_TOLERANCE * services.theme.scale * services.screens.map.screen_at(x, y).dpr


# =========================================================================== where a click points


def point_of(services: UiServices, action: Action) -> tuple[float, float] | None:
    """Where a click action lands before snapping: its x and y, else the aim point, else the cursor.

    Safe on the action thread (interceptors call it): ``aim_point`` never blocks and the
    cursor comes from the desktop service, not from a widget.
    """
    point = services.aim(action)
    if point is not None:
        return point
    desktop = services.ctx.desktop
    if desktop is None:
        return None
    try:
        x, y = desktop.input.cursor_pos()
    except Exception:  # the cursor is a convenience here; without it the click is left alone
        log.debug("cursor_pos failed", exc_info=True)
        return None
    return (float(x), float(y))


class OwnWindows:
    """Whether a physical point is over one of Sightline's own windows that take clicks (the dock, a chip).

    Read through Win32 so it works on the action thread, where Qt geometry must not be
    touched.  Click-through overlays never count: ``WindowFromPoint`` skips them, just as
    Windows does when it delivers the click.  Off the real Windows platform (tests, the
    playground) nothing is ours.
    """

    GA_ROOT = 2

    def __init__(self) -> None:
        self._native = sys.platform == "win32" and winapi.native_windows()  # read once, on the GUI thread
        self._pid = os.getpid()

    def at(self, x: float, y: float) -> bool:
        if not self._native:
            return False
        try:
            user32 = ctypes.windll.user32
            user32.WindowFromPoint.argtypes = [wintypes.POINT]
            user32.WindowFromPoint.restype = wintypes.HWND
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
            if not hwnd:
                return False
            root = user32.GetAncestor(hwnd, self.GA_ROOT) or hwnd
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
            return int(pid.value) == self._pid
        except Exception:
            log.debug("Could not read the window under %s, %s", x, y, exc_info=True)
            return False


# =========================================================================== install and uninstall


def register_ui_actions(services: UiServices, table: Iterable[tuple[str, Callable[[Action], object], str]]) -> list[str]:
    """Register ``ui.*`` handlers on the GUI thread, kept out of "undo that" history.

    They only open, pick from or close an overlay; the action they lead to (a click, a scroll)
    is what "undo that" should see.  Returns the names registered, for uninstalling.
    """
    names: list[str] = []
    for name, handler, description in table:
        try:
            services.ctx.actions.register(name, handler, thread="ui", description=description, history=False)
        except ValueError:
            log.warning("Action %s was already registered; keeping the existing one", name)
            continue
        names.append(name)
    return names


class Teardown:
    """Collects undo steps as ``install`` goes; calling it runs them newest first, once."""

    def __init__(self) -> None:
        self._steps: list[Callable[[], object]] = []
        self._done = False
        self.overlay: object | None = None  # the overlay object, for tests and tools

    def add(self, step: Callable[[], object]) -> None:
        self._steps.append(step)

    def __call__(self) -> None:
        if self._done:
            return
        self._done = True
        for step in reversed(self._steps):
            try:
                step()
            except Exception:
                log.exception("Uninstall step %r failed", step)
        self._steps.clear()
