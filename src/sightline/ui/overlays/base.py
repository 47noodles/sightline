"""The base for every window drawn over the desktop, and two-tone painting helpers.

Overlays are frameless, always on top, never focused, left out of Alt+Tab,
and (unless they are meant to be clicked) transparent to input, so a click
lands on whatever is underneath.  They are small windows that move rather
than full-screen windows that repaint, which keeps idle CPU near zero.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from sightline.core.types import Rect

from .. import winapi
from ..coords import ScreenGeometry
from ..services import UiServices

OVERLAY_FLAGS = (
    Qt.WindowType.Window
    | Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.Tool
    | Qt.WindowType.WindowDoesNotAcceptFocus
    | Qt.WindowType.NoDropShadowWindowHint
)


class OverlayWindow(QWidget):
    """A top-level overlay.  Position it with the ``place_*`` helpers, which take physical pixels."""

    def __init__(self, services: UiServices, *, click_through: bool = True, name: str = "") -> None:
        flags = OVERLAY_FLAGS
        if click_through:
            flags |= Qt.WindowType.WindowTransparentForInput
        super().__init__(None, flags)
        self.svc = services
        self.click_through = click_through
        self.setObjectName(name or type(self).__name__)
        self.setWindowTitle(f"Sightline {name}".strip())
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._styled = False
        self.screen_geometry: ScreenGeometry | None = None

    def showEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        super().showEvent(event)
        if not self._styled:
            self._styled = True
            if self.click_through:
                winapi.make_click_through(self)
            else:
                winapi.make_no_activate(self)

    # ------------------------------------------------------------------ placement (physical in, logical out)

    def place_logical(self, rect: Rect) -> None:
        target = QRect(round(rect.x), round(rect.y), max(1, round(rect.w)), max(1, round(rect.h)))
        if self.geometry() != target:
            self.setGeometry(target)

    def place_physical(self, rect: Rect) -> Rect:
        """Cover a physical rectangle.  The window is scaled by the screen holding its centre, as Qt does."""
        smap = self.svc.screens.map
        screen = smap.screen_at(*rect.center)
        logical = smap.rect_to_logical(rect, screen)
        self.screen_geometry = screen
        self.place_logical(logical)
        return logical

    def center_on_physical(self, x: float, y: float, w: float | None = None, h: float | None = None) -> None:
        """Centre a window of logical size ``w`` x ``h`` (default: its current size) on a physical point."""
        w = float(self.width() if w is None else w)
        h = float(self.height() if h is None else h)
        logical, screen = self.svc.screens.map.centered_logical_rect(x, y, w, h)
        self.screen_geometry = screen
        self.place_logical(logical)

    def logical_point(self, x: float, y: float) -> QPointF:
        """A physical point in this window's own coordinates."""
        smap = self.svc.screens.map
        lx, ly = smap.to_logical(x, y, self.screen_geometry)
        return QPointF(lx - self.x(), ly - self.y())


# --------------------------------------------------------------------------- painting helpers


def pen(color: QColor, width: float, *, dash: list[float] | None = None, cap: Qt.PenCapStyle = Qt.PenCapStyle.RoundCap) -> QPen:
    p = QPen(color, width)
    p.setCapStyle(cap)
    p.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    if dash:
        p.setDashPattern(dash)
    return p


def two_tone_path(painter: QPainter, path: QPainterPath, ink: QColor, halo: QColor, width: float, halo_width: float, dash: list[float] | None = None) -> None:
    """Stroke a path twice, a wide halo then the ink, so it reads on any background."""
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(pen(halo, width + 2 * halo_width, dash=_scaled_dash(dash, width, width + 2 * halo_width)))
    painter.drawPath(path)
    painter.setPen(pen(ink, width, dash=dash))
    painter.drawPath(path)


def _scaled_dash(dash: list[float] | None, width: float, halo_total: float) -> list[float] | None:
    # Qt dash lengths are in multiples of the pen width; keep the halo's dashes aligned with the ink's.
    if not dash:
        return None
    k = width / halo_total
    return [d * k for d in dash]


def two_tone_circle(painter: QPainter, center: QPointF, radius: float, ink: QColor, halo: QColor, width: float, halo_width: float, dash: list[float] | None = None) -> None:
    path = QPainterPath()
    path.addEllipse(center, radius, radius)
    two_tone_path(painter, path, ink, halo, width, halo_width, dash)


def two_tone_arc(painter: QPainter, center: QPointF, radius: float, start_deg: float, span_deg: float, ink: QColor, halo: QColor, width: float, halo_width: float) -> None:
    """An arc measured clockwise from 12 o'clock, like a clock hand sweeping."""
    rect = QRectF(center.x() - radius, center.y() - radius, 2 * radius, 2 * radius)
    path = QPainterPath()
    # Qt angles run counter-clockwise from 3 o'clock.
    qt_start = 90.0 - start_deg
    path.arcMoveTo(rect, qt_start)
    path.arcTo(rect, qt_start, -span_deg)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(pen(halo, width + 2 * halo_width, cap=Qt.PenCapStyle.FlatCap))
    painter.drawPath(path)
    painter.setPen(pen(ink, width, cap=Qt.PenCapStyle.FlatCap))
    painter.drawPath(path)


def solid_panel(painter: QPainter, rect: QRectF, fill: QColor, border: QColor, border_width: float = 1.0) -> None:
    """The overlay panel: an opaque fill and a crisp border, square corners."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.fillRect(rect, border)
    inner = rect.adjusted(border_width, border_width, -border_width, -border_width)
    painter.fillRect(inner, fill)
    painter.restore()
