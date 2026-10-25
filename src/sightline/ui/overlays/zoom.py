"""The zoom lens: a 3x magnified still of the area around the pointer for precise clicks.

Opening it grabs the screen around the point (with our own overlays hidden
for a moment), shows it magnified, and installs an action interceptor.  The
next click inside the lens is moved to the real screen point under that
spot, with snapping off; a click outside the lens goes through unchanged.
Either way the lens closes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QImage, QPainter, QPainterPath

from sightline.core.types import Action, Rect

from ..coords import ScreenGeometry
from ..geometry import LensMapping, lens_geometry
from ..services import UiServices
from .base import OverlayWindow, solid_panel, two_tone_path

log = logging.getLogger(__name__)

Grabber = Callable[[Rect, ScreenGeometry], "tuple[QImage, Rect] | None"]
GRAB_DELAY_MS = 60  # long enough for the compositor to drop our hidden overlays from the frame


def qt_grabber(source: Rect, screen: ScreenGeometry) -> tuple[QImage, Rect] | None:
    """Grab about ``source`` from a screen; returns the image and the exact physical rectangle it covers.

    ``QScreen.grabWindow`` takes whole logical pixels relative to the screen
    and rounds them to physical ones, so the area it really copies can be a
    pixel off what was asked.  Reporting that area keeps the lens mapping exact.
    """
    qscreen = screen.key
    if qscreen is None:
        return None
    dpr = screen.dpr
    lx = round((source.x - screen.native.x) / dpr)
    ly = round((source.y - screen.native.y) / dpr)
    lw = max(1, round(source.w / dpr))
    lh = max(1, round(source.h / dpr))
    pixmap = qscreen.grabWindow(0, lx, ly, lw, lh)
    if pixmap.isNull():
        return None
    image = pixmap.toImage()
    actual = Rect(screen.native.x + round(lx * dpr), screen.native.y + round(ly * dpr), image.width(), image.height())
    return image, actual


class _LensWindow(OverlayWindow):
    def __init__(self, services: UiServices) -> None:
        super().__init__(services, name="zoom-lens")
        self.image: QImage | None = None
        self.mapping: LensMapping | None = None
        self.focus: tuple[float, float] | None = None  # the point the lens was opened on

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        t = self.svc.theme
        pal = t.palette
        p = QPainter(self)
        full = QRectF(0, 0, self.width(), self.height())
        p.fillRect(full, pal.surface)
        if self.image is not None and not self.image.isNull():
            # Nearest-neighbour: every source pixel becomes a crisp block, which is what you aim at.
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            p.drawImage(full, self.image)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = QPainterPath()
        inset = t.pxf(2)
        frame.addRect(full.adjusted(inset, inset, -inset, -inset))
        two_tone_path(p, frame, pal.ink, pal.halo, t.pxf(2.5), t.pxf(1.25))
        if self.mapping is not None and self.focus is not None:
            fx, fy = self.mapping.to_lens(*self.focus)
            c = self.logical_point(fx, fy)
            gap, arm = t.pxf(6), t.pxf(16)
            cross = QPainterPath()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                cross.moveTo(c.x() + dx * gap, c.y() + dy * gap)
                cross.lineTo(c.x() + dx * (gap + arm), c.y() + dy * (gap + arm))
            two_tone_path(p, cross, pal.ink, pal.halo, t.pxf(1.5), t.pxf(1.0))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        label = f"Zoom {self.mapping.zoom:g}x. Look at the spot, then click." if self.mapping else ""
        if label:
            h = t.pxf(30)
            strip = QRectF(inset, self.height() - inset - h, self.width() - 2 * inset, h)
            solid_panel(p, strip, pal.surface, pal.ink)
            p.setFont(t.font("small", weight=500))
            p.setPen(pal.ink)
            p.drawText(strip.adjusted(t.pxf(10), 0, -t.pxf(10), 0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), label)
        p.end()


class ZoomLens(QObject):
    def __init__(
        self,
        services: UiServices,
        *,
        grabber: Grabber | None = None,
        hide_overlays: Callable[[bool], None] | None = None,
        target_point: Callable[[], tuple[float, float] | None] | None = None,
    ) -> None:
        super().__init__()
        self.svc = services
        self.factor = 3.0
        self.lens_size = 360.0  # logical pixels, before ui.scale
        self.window = _LensWindow(services)
        self._grabber = grabber or qt_grabber
        self._hide_overlays = hide_overlays or (lambda _hidden: None)
        self._target_point = target_point
        self._mapping: LensMapping | None = None
        self._remove_interceptor = None
        self._pending = 0
        services.overlays.register("zoom", self._close)

    @property
    def mapping(self) -> LensMapping | None:
        return self._mapping

    @property
    def interceptor_installed(self) -> bool:
        return self._remove_interceptor is not None

    # ------------------------------------------------------------------ actions (GUI thread)

    def show(self, action: Action | None = None) -> None:
        # The action's x and y, else where the user looked as they began to say "zoom".
        point = self.svc.aim(action)
        if point is None:
            # From the dock the pointer sits on the dock button, so aim where the user was looking before.
            point = (self._target_point() if self._target_point else None) or self.svc.cursor.pos
        x, y = point
        self.svc.overlays.opened("zoom")
        if self._remove_interceptor is None:
            self._remove_interceptor = self.svc.ctx.actions.add_interceptor(self._intercept, priority=110, owner="ui.zoom")
        self._pending += 1
        token = self._pending
        self.window.hide()
        self._hide_overlays(True)
        QTimer.singleShot(GRAB_DELAY_MS, self, lambda: self._capture(token, float(x), float(y)))

    def hide(self, _action: Action | None = None) -> None:
        self.svc.overlays.close("zoom")

    # ------------------------------------------------------------------ internals

    def _capture(self, token: int, x: float, y: float) -> None:
        if token != self._pending or self._remove_interceptor is None:
            return
        image: QImage | None = None
        try:
            screen = self.svc.screens.map.screen_at(x, y)
            side = self.svc.theme.pxf(self.lens_size) * screen.dpr
            mapping = lens_geometry(x, y, side, side, self.factor, screen.native)
            grabbed = self._grabber(mapping.source, screen)
            if grabbed is not None:
                image, actual = grabbed
                mapping = LensMapping(mapping.lens, actual)
        finally:
            self._hide_overlays(False)
        if image is None:
            log.warning("Screen grab failed; the lens shows nothing")
        self.window.image = image
        self.window.mapping = mapping
        self.window.focus = (x, y)
        self.window.place_physical(mapping.lens)
        self._mapping = mapping
        self.window.show()
        self.window.update()

    def _close(self) -> None:
        self._pending += 1
        if self._remove_interceptor is not None:
            self._remove_interceptor()
            self._remove_interceptor = None
        self._mapping = None
        self.window.hide()
        self.window.image = None
        self.svc.overlays.closed("zoom")

    def _close_if_open(self) -> None:
        if self._remove_interceptor is not None:
            self.svc.overlays.close("zoom")

    def _intercept(self, action: Action) -> Action | None:
        """Action thread: the next click inside the lens lands on the real point it magnifies."""
        if action.name != "mouse.click":
            return action
        mapping = self._mapping
        if mapping is None:
            return action  # still grabbing; let the click through rather than eat it
        point = self.svc.aim(action)  # a spoken "click" aims where the user looked as they began to speak
        if point is None:
            desktop = self.svc.ctx.desktop
            if desktop is None:
                return action
            point = desktop.input.cursor_pos()
        x, y = point
        self.svc.bridge.post(self._close_if_open)
        if not mapping.contains(x, y):
            return action
        sx, sy = mapping.to_source(x, y)
        return action.with_args(x=round(sx), y=round(sy), snap=False)
