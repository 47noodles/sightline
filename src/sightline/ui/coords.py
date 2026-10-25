"""Physical virtual-desktop pixels to Qt logical pixels and back, per screen.

Every coordinate in Sightline's events and actions is a physical pixel in
the Windows virtual desktop.  Qt widgets are positioned in logical pixels.
On Windows with per-monitor DPI awareness Qt maps each screen separately:
a screen keeps its native top-left corner as its logical top-left corner,
and distances inside it are divided by that screen's device pixel ratio::

    logical = origin + (physical - origin) / dpr
    physical = origin + (logical - origin) * dpr

So with mixed DPI the logical screens shrink toward their top-left corners
and leave gaps between them; they never overlap, which keeps the reverse
mapping unambiguous.  The functions below are pure so they can be tested
without Qt; ``ScreenMap.from_qt`` builds the map from the live screens.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sightline.core.types import Rect

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScreenGeometry:
    name: str
    native: Rect  # physical pixels
    dpr: float
    work: Rect | None = None  # physical work area (screen minus taskbar and app bars)
    primary: bool = False
    key: object = field(default=None, compare=False, repr=False)  # the QScreen, when built from Qt

    @property
    def logical(self) -> Rect:
        """This screen's rectangle in Qt logical coordinates."""
        return Rect(self.native.x, self.native.y, self.native.w / self.dpr, self.native.h / self.dpr)

    @property
    def work_rect(self) -> Rect:
        return self.work if self.work is not None else self.native


# --------------------------------------------------------------------------- pure mapping


def phys_to_logical(x: float, y: float, screen: ScreenGeometry) -> tuple[float, float]:
    ox, oy = screen.native.x, screen.native.y
    return (ox + (x - ox) / screen.dpr, oy + (y - oy) / screen.dpr)


def logical_to_phys(x: float, y: float, screen: ScreenGeometry) -> tuple[float, float]:
    ox, oy = screen.native.x, screen.native.y
    return (ox + (x - ox) * screen.dpr, oy + (y - oy) * screen.dpr)


def _nearest(screens: Sequence[ScreenGeometry], x: float, y: float, logical: bool) -> ScreenGeometry:
    best: tuple[float, ScreenGeometry] | None = None
    for s in screens:
        rect = s.logical if logical else s.native
        if rect.contains(x, y):
            return s
        d = rect.distance_to(x, y)
        if best is None or d < best[0]:
            best = (d, s)
    assert best is not None
    return best[1]


class ScreenMap:
    """An immutable description of the screens with conversion helpers."""

    def __init__(self, screens: Iterable[ScreenGeometry]) -> None:
        self.screens: tuple[ScreenGeometry, ...] = tuple(screens)
        if not self.screens:
            self.screens = (ScreenGeometry("default", Rect(0, 0, 1920, 1080), 1.0, primary=True),)

    # ------------------------------------------------------------------ lookup

    @property
    def primary(self) -> ScreenGeometry:
        for s in self.screens:
            if s.primary:
                return s
        return self.screens[0]

    def screen_at(self, x: float, y: float) -> ScreenGeometry:
        """The screen containing a physical point, or the nearest one when it falls in a gap."""
        return _nearest(self.screens, x, y, logical=False)

    def screen_at_logical(self, x: float, y: float) -> ScreenGeometry:
        return _nearest(self.screens, x, y, logical=True)

    def screen_for_rect(self, rect: Rect) -> ScreenGeometry:
        """The screen holding most of a physical rectangle (its centre when it holds none)."""
        best: tuple[float, ScreenGeometry] | None = None
        for s in self.screens:
            area = s.native.intersection(rect).area if s.native.intersects(rect) else 0.0
            if area > 0 and (best is None or area > best[0]):
                best = (area, s)
        if best is not None:
            return best[1]
        cx, cy = rect.center
        return self.screen_at(cx, cy)

    def virtual_rect(self) -> Rect:
        left = min(s.native.left for s in self.screens)
        top = min(s.native.top for s in self.screens)
        right = max(s.native.right for s in self.screens)
        bottom = max(s.native.bottom for s in self.screens)
        return Rect.from_ltrb(left, top, right, bottom)

    # ------------------------------------------------------------------ conversion

    def to_logical(self, x: float, y: float, screen: ScreenGeometry | None = None) -> tuple[float, float]:
        s = screen or self.screen_at(x, y)
        return phys_to_logical(x, y, s)

    def to_physical(self, x: float, y: float, screen: ScreenGeometry | None = None) -> tuple[float, float]:
        s = screen or self.screen_at_logical(x, y)
        return logical_to_phys(x, y, s)

    def rect_to_logical(self, rect: Rect, screen: ScreenGeometry | None = None) -> Rect:
        """A physical rectangle in the logical coordinates of one screen (the one holding most of it)."""
        s = screen or self.screen_for_rect(rect)
        lx, ly = phys_to_logical(rect.x, rect.y, s)
        return Rect(lx, ly, rect.w / s.dpr, rect.h / s.dpr)

    def rect_to_physical(self, rect: Rect, screen: ScreenGeometry | None = None) -> Rect:
        s = screen or self.screen_at_logical(*rect.center)
        px, py = logical_to_phys(rect.x, rect.y, s)
        return Rect(px, py, rect.w * s.dpr, rect.h * s.dpr)

    def centered_logical_rect(self, x: float, y: float, w: float, h: float) -> tuple[Rect, ScreenGeometry]:
        """A logical rectangle of size ``w`` x ``h`` centred on a physical point.

        Qt assigns a top-level window to the screen holding its centre, so a
        window centred on the point is always scaled by that point's screen.
        """
        s = self.screen_at(x, y)
        lx, ly = phys_to_logical(x, y, s)
        return Rect(lx - w / 2.0, ly - h / 2.0, w, h), s

    # ------------------------------------------------------------------ construction

    @classmethod
    def from_qt(cls) -> ScreenMap:
        from PySide6.QtGui import QGuiApplication

        screens: list[ScreenGeometry] = []
        primary = QGuiApplication.primaryScreen()
        for qs in QGuiApplication.screens():
            dpr = float(qs.devicePixelRatio()) or 1.0
            native, work = _native_rects(qs, dpr)
            screens.append(ScreenGeometry(qs.name(), native, dpr, work, qs is primary, qs))
        return cls(screens)


def _native_rects(qs: object, dpr: float) -> tuple[Rect, Rect]:
    """Exact physical rectangles of a QScreen: from the HMONITOR on Windows, else derived from Qt."""
    from PySide6.QtGui import QGuiApplication

    if sys.platform == "win32" and QGuiApplication.platformName() == "windows":
        try:
            hmonitor = qs.nativeInterface().handle()  # type: ignore[attr-defined]
            rects = _monitor_rects(int(hmonitor))
            if rects is not None:
                return rects
        except Exception:  # older Qt or no native interface: fall back to arithmetic
            log.debug("No HMONITOR for screen %s", getattr(qs, "name", lambda: "?")(), exc_info=True)
    geo = qs.geometry()  # type: ignore[attr-defined]
    avail = qs.availableGeometry()  # type: ignore[attr-defined]
    native = Rect(geo.x(), geo.y(), round(geo.width() * dpr), round(geo.height() * dpr))
    ox, oy = geo.x(), geo.y()
    work = Rect(
        ox + round((avail.x() - ox) * dpr),
        oy + round((avail.y() - oy) * dpr),
        round(avail.width() * dpr),
        round(avail.height() * dpr),
    )
    return native, work


def _monitor_rects(hmonitor: int) -> tuple[Rect, Rect] | None:
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    user32 = ctypes.windll.user32
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
    if not user32.GetMonitorInfoW(wintypes.HMONITOR(hmonitor), ctypes.byref(info)):
        return None
    m, w = info.rcMonitor, info.rcWork
    return (
        Rect.from_ltrb(m.left, m.top, m.right, m.bottom),
        Rect.from_ltrb(w.left, w.top, w.right, w.bottom),
    )
