"""Marks drawn on the desktop: the click-target outline, the click ripple and the resume zone."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QObject, QPointF, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QFontMetricsF, QPainter

from sightline.core.events import ClickPerformed, GazeZoneProgress, HoverTarget, StateChanged
from sightline.core.types import Rect

from ..services import UiServices
from .base import OverlayWindow, solid_panel, two_tone_circle

# =========================================================================== target highlight


class _Edge(OverlayWindow):
    """One side of the outline: a thin bar of ink with a halo on both sides."""

    def __init__(self, services: UiServices, horizontal: bool) -> None:
        super().__init__(services, name="highlight-edge")
        self.horizontal = horizontal

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        pal = self.svc.theme.palette
        p = QPainter(self)
        full = QRectF(0, 0, self.width(), self.height())
        p.fillRect(full, pal.halo)
        inset = self.svc.theme.pxf(1)
        p.fillRect(full.adjusted(inset, inset, -inset, -inset), pal.ink)
        p.end()


class TargetHighlight(QObject):
    """Outlines the element a click would hit.  Four thin windows, so a large element costs nothing to draw."""

    def __init__(self, services: UiServices) -> None:
        super().__init__()
        self.svc = services
        self.enabled = True
        self._suppressed = False
        self._rect: Rect | None = None
        self._edges = [_Edge(services, True), _Edge(services, True), _Edge(services, False), _Edge(services, False)]

    @property
    def rect(self) -> Rect | None:
        return self._rect

    @property
    def edges(self) -> list[OverlayWindow]:
        return list(self._edges)

    def on_hover(self, e: HoverTarget) -> None:
        self._rect = e.rect
        self._sync()

    def suppress(self, hidden: bool) -> None:
        self._suppressed = hidden
        self._sync()

    def hide(self) -> None:
        for edge in self._edges:
            edge.hide()

    def _sync(self) -> None:
        rect = self._rect
        if rect is None or rect.is_empty() or not self.enabled or self._suppressed:
            self.hide()
            return
        smap = self.svc.screens.map
        dpr = smap.screen_at(*rect.center).dpr
        t = self.svc.theme.pxf(4) * dpr  # thickness in physical pixels
        g = 2.0 * dpr  # gap between the element and the outline
        x0, y0 = rect.x - g - t, rect.y - g - t
        span_w = rect.w + 2 * (g + t)
        top, bottom, left, right = self._edges
        top.place_physical(Rect(x0, y0, span_w, t))
        bottom.place_physical(Rect(x0, rect.bottom + g, span_w, t))
        left.place_physical(Rect(x0, rect.y - g, t, rect.h + 2 * g))
        right.place_physical(Rect(rect.right + g, rect.y - g, t, rect.h + 2 * g))
        for edge in self._edges:
            if not edge.isVisible():
                edge.show()
            edge.update()


# =========================================================================== click ripple


class ClickRipple(OverlayWindow):
    """A ring that widens and fades where a click landed; right, middle and double clicks are named."""

    def __init__(self, services: UiServices) -> None:
        super().__init__(services, name="click-ripple")
        self.enabled = True
        self._t = 1.0
        self._label = ""
        self._count = 1
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(380)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_tick)
        self._anim.finished.connect(self.hide)
        self._resize()
        services.theme.changed.connect(self._resize)

    def _resize(self) -> None:
        self.resize(self.svc.theme.px(220), self.svc.theme.px(110))

    def on_click(self, e: ClickPerformed) -> None:
        if not self.enabled:
            return
        self._count = e.count
        if e.button == "right":
            self._label = "Right click"
        elif e.button == "middle":
            self._label = "Middle click"
        elif e.count >= 2:
            self._label = "Double click"
        else:
            self._label = ""
        self.center_on_physical(e.x, e.y)
        self._anim.stop()
        self._t = 0.0
        self.show()
        self._anim.start()

    def _on_tick(self, value: object) -> None:
        self._t = float(value)  # type: ignore[arg-type]
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        theme = self.svc.theme
        pal = theme.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.width() / 2.0, self.height() / 2.0)
        r0, r1 = theme.pxf(6), theme.pxf(34)
        p.setOpacity(max(0.0, 1.0 - self._t))
        two_tone_circle(p, c, r0 + (r1 - r0) * self._t, pal.ink, pal.halo, theme.pxf(2.5), theme.pxf(1.25))
        if self._count >= 2:
            t2 = max(0.0, (self._t - 0.3) / 0.7)
            two_tone_circle(p, c, r0 + (r1 - r0) * t2, pal.ink, pal.halo, theme.pxf(2.5), theme.pxf(1.25))
        if self._label:
            font = theme.font("status")
            p.setFont(font)
            pad = theme.pxf(6)
            w = QFontMetricsF(font).horizontalAdvance(self._label) + 2 * pad
            h = theme.pxf(24)
            rect = QRectF(c.x() - r1 - theme.pxf(8) - w, c.y() - h / 2, w, h)
            solid_panel(p, rect, pal.surface, pal.ink)
            p.setPen(pal.ink)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._label)
        p.end()


# =========================================================================== resume zone


class ResumeZone(OverlayWindow):
    """While eye control is paused: where to look to resume it, and how close it is to resuming."""

    def __init__(self, services: UiServices) -> None:
        super().__init__(services, name="resume-zone")
        self._zone: Rect | None = None
        self._progress = 0.0
        self._paused = bool(services.ctx.state.get("pointer_paused"))

    @property
    def progress(self) -> float:
        return self._progress

    def on_zone(self, e: GazeZoneProgress) -> None:
        if e.zone != "resume":
            return
        if e.rect is not None:
            self._zone = e.rect
        self._progress = max(0.0, min(1.0, e.progress))
        if self._progress >= 1.0:
            self._progress = 0.0
        self._sync()

    def on_state(self, e: StateChanged) -> None:
        if e.field == "pointer_paused":
            self._paused = bool(e.new)
            if not self._paused:
                self._progress = 0.0
            self._sync()

    def _sync(self) -> None:
        if self._zone is None or not (self._paused or self._progress > 0.0):
            self.hide()
            return
        self.place_physical(self._zone)
        self.show()
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        theme = self.svc.theme
        pal = theme.palette
        p = QPainter(self)
        full = QRectF(0, 0, self.width(), self.height())
        # Outline the zone: halo, ink, halo, like the target outline.
        t = theme.pxf(4)
        for rect, color in ((full, pal.halo), (full.adjusted(1, 1, -1, -1), pal.ink)):
            p.fillRect(QRectF(rect.left(), rect.top(), rect.width(), t - 2), color)
            p.fillRect(QRectF(rect.left(), rect.bottom() - t + 2, rect.width(), t - 2), color)
            p.fillRect(QRectF(rect.left(), rect.top(), t - 2, rect.height()), color)
            p.fillRect(QRectF(rect.right() - t + 2, rect.top(), t - 2, rect.height()), color)
        font = theme.font("label")
        p.setFont(font)
        text = "Resume"
        pad = theme.pxf(8)
        tw = QFontMetricsF(font).horizontalAdvance(text) + 2 * pad
        th = theme.pxf(30)
        bar_h = theme.pxf(6)
        panel = QRectF(full.center().x() - tw / 2, full.center().y() - (th + bar_h) / 2, tw, th + bar_h)
        solid_panel(p, panel, pal.surface, pal.ink)
        p.setPen(pal.ink)
        p.drawText(QRectF(panel.x(), panel.y(), panel.width(), th), Qt.AlignmentFlag.AlignCenter, text)
        if self._progress > 0.0:
            bar = QRectF(panel.x() + 1, panel.bottom() - bar_h - 1, (panel.width() - 2) * self._progress, bar_h)
            p.fillRect(bar, pal.accent)
        p.end()
