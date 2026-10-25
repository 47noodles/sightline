"""The ring around the cursor and the optional gaze dot.

The ring is the user's main feedback while their eyes do the work:

* plain ring: tracking normally (fine, gaze or head control);
* a ring that snaps in from wide: the pointer just jumped to where you looked;
* four ticks: the pointer is held still (frozen);
* dashed ring with a pause mark: eye control is paused;
* dashed ring with "No face": the camera has lost your face;
* amber ring: a click is armed (eyes held shut); a second amber ring: double click armed;
* amber arc sweeping clockwise: dwell progress.

The ring follows the real cursor (polled), not the pointer engine's target,
so it never runs ahead of the arrow while the cursor pump eases it along.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QFontMetricsF, QPainter, QPainterPath

from sightline.core.events import DwellProgress, EyeHold, FaceStatus, PointerSample, StateChanged

from ..services import UiServices
from .base import OverlayWindow, solid_panel, two_tone_arc, two_tone_circle, two_tone_path

VISION_STALE_S = 3.0


class PointerRing(OverlayWindow):
    def __init__(self, services: UiServices) -> None:
        super().__init__(services, name="pointer-ring")
        self.enabled = True
        self._suppressed = False
        self._phase = "fine"
        self._sample_phase = "fine"
        self._hold = 0
        self._dwell = 0.0
        self._face_present: bool | None = None
        self._paused = bool(services.ctx.state.get("pointer_paused"))
        self._last_vision = 0.0
        self._warp = 0.0  # 1 at the moment of a jump, eases to 0
        self._warp_anim = QVariantAnimation(self)
        self._warp_anim.setDuration(180)
        self._warp_anim.setStartValue(1.0)
        self._warp_anim.setEndValue(0.0)
        self._warp_anim.valueChanged.connect(self._on_warp)
        self._stale_timer = QTimer(self)
        self._stale_timer.setInterval(1000)
        self._stale_timer.timeout.connect(self._refresh_visibility)
        self._resize()
        services.theme.changed.connect(self._resize)
        services.cursor.moved.connect(self._on_cursor)

    # ------------------------------------------------------------------ inputs

    def _resize(self) -> None:
        side = self.svc.theme.px(200)
        self.resize(side, side)
        self.update()

    def _on_cursor(self, x: float, y: float) -> None:
        if self.isVisible():
            self.center_on_physical(x, y)

    def on_sample(self, e: PointerSample) -> None:
        self._last_vision = time.monotonic()
        self.svc.cursor.kick()
        if e.phase == "warp" and self._sample_phase != "warp":
            self._warp_anim.stop()
            self._warp_anim.start()
        self._sample_phase = e.phase
        self._update_phase()
        self._refresh_visibility()

    def on_face(self, e: FaceStatus) -> None:
        self._last_vision = time.monotonic()
        if self._face_present != e.present:
            self._face_present = e.present
            self._update_phase()
        self._refresh_visibility()

    def on_hold(self, e: EyeHold) -> None:
        stage = 0 if e.cancelled else max(0, e.stage)
        if stage != self._hold:
            self._hold = stage
            self.update()

    def on_dwell(self, e: DwellProgress) -> None:
        progress = max(0.0, min(1.0, e.progress))
        if progress >= 1.0:
            progress = 0.0  # fired; the click ripple takes over
        if abs(progress - self._dwell) > 0.004:
            self._dwell = progress
            self.update()

    def on_state(self, e: StateChanged) -> None:
        if e.field == "pointer_paused":
            self._paused = bool(e.new)
            self._update_phase()
            self._refresh_visibility()
        elif e.field == "face_present":
            self._face_present = bool(e.new)
            self._update_phase()

    def _on_warp(self, value: object) -> None:
        self._warp = float(value)  # type: ignore[arg-type]
        self.update()

    def _update_phase(self) -> None:
        if self._paused:
            phase = "paused"
        elif self._face_present is False:
            phase = "lost"
        else:
            phase = self._sample_phase
        if phase != self._phase:
            self._phase = phase
            self.update()

    # ------------------------------------------------------------------ visibility

    @property
    def phase(self) -> str:
        return self._phase

    def vision_active(self) -> bool:
        return time.monotonic() - self._last_vision < VISION_STALE_S or self._paused

    def suppress(self, hidden: bool) -> None:
        """Hide for a moment (while the zoom lens grabs the screen)."""
        self._suppressed = hidden
        self._refresh_visibility()

    def shutdown(self) -> None:
        """Stop for good: no timer may show the ring again after the shell stops."""
        self.enabled = False
        self._stale_timer.stop()
        self._warp_anim.stop()
        self.hide()

    def _refresh_visibility(self) -> None:
        want = self.enabled and not self._suppressed and self.vision_active()
        if want and not self.isVisible():
            x, y = self.svc.cursor.pos
            self.center_on_physical(x, y)
            self.show()
            self._stale_timer.start()
        elif not want and self.isVisible():
            self.hide()
            self._stale_timer.stop()

    # ------------------------------------------------------------------ painting

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        theme = self.svc.theme
        pal = theme.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.width() / 2.0, self.height() / 2.0)
        r = theme.pxf(17)
        w = theme.pxf(2.5)
        hw = theme.pxf(1.5)
        dashed = self._phase in ("paused", "lost")

        if self._hold >= 1:
            # Armed: the ring itself turns amber with dark edges, unmissable on any background.
            two_tone_circle(p, c, r, pal.accent, pal.on_accent, theme.pxf(4.5), theme.pxf(1.25))
            if self._hold >= 2:
                two_tone_circle(p, c, r + theme.pxf(8), pal.accent, pal.on_accent, theme.pxf(4.5), theme.pxf(1.25))
        else:
            two_tone_circle(p, c, r, pal.ink, pal.halo, w, hw, dash=[3.0, 2.2] if dashed else None)

        if self._dwell > 0.0 and self._hold < 2:
            two_tone_arc(p, c, r + theme.pxf(8), 0.0, 360.0 * self._dwell, pal.accent, pal.on_accent, theme.pxf(4.5), theme.pxf(1.25))

        if self._warp > 0.0:
            wr = r * (1.0 + 1.1 * self._warp)
            two_tone_circle(p, c, wr, pal.ink, pal.halo, theme.pxf(1.5), theme.pxf(1.0))

        if self._phase == "frozen" and self._hold == 0:
            tick = QPainterPath()
            inner, outer = r + theme.pxf(4), r + theme.pxf(10)
            for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
                tick.moveTo(c.x() + dx * inner, c.y() + dy * inner)
                tick.lineTo(c.x() + dx * outer, c.y() + dy * outer)
            two_tone_path(p, tick, pal.ink, pal.halo, w, hw)

        if self._phase == "paused":
            self._badge(p, c, r, None)
        elif self._phase == "lost":
            self._badge(p, c, r, "No face")
        p.end()

    def _badge(self, p: QPainter, c: QPointF, r: float, text: str | None) -> None:
        """A small solid tag left of the ring (the arrow cursor extends down and right)."""
        theme = self.svc.theme
        pal = theme.palette
        pad = theme.pxf(6)
        h = theme.pxf(24)
        if text is None:
            w = h
        else:
            font = theme.font("status")
            p.setFont(font)
            w = QFontMetricsF(font).horizontalAdvance(text) + 2 * pad
        rect = QRectF(c.x() - r - theme.pxf(10) - w, c.y() - h / 2.0, w, h)
        solid_panel(p, rect, pal.surface, pal.ink)
        if text is None:
            bar_w, bar_h, gap = theme.pxf(3), theme.pxf(10), theme.pxf(2.5)
            p.fillRect(QRectF(rect.center().x() - gap - bar_w, rect.center().y() - bar_h / 2, bar_w, bar_h), pal.ink)
            p.fillRect(QRectF(rect.center().x() + gap, rect.center().y() - bar_h / 2, bar_w, bar_h), pal.ink)
        else:
            p.setPen(pal.ink)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


class GazeDot(OverlayWindow):
    """Where the eyes are looking (after filtering), for users and helpers tuning the tracker."""

    def __init__(self, services: UiServices) -> None:
        super().__init__(services, name="gaze-dot")
        self.enabled = False
        self._resize()
        services.theme.changed.connect(self._resize)

    def _resize(self) -> None:
        side = self.svc.theme.px(28)
        self.resize(side, side)

    def on_sample(self, e: PointerSample) -> None:
        if not self.enabled or e.gaze_x is None or e.gaze_y is None:
            if self.isVisible():
                self.hide()
            return
        self.center_on_physical(e.gaze_x, e.gaze_y)
        if not self.isVisible():
            self.show()

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        theme = self.svc.theme
        pal = theme.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.width() / 2.0, self.height() / 2.0)
        r = theme.pxf(5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(pal.halo)
        p.drawEllipse(c, r + theme.pxf(2), r + theme.pxf(2))
        p.setBrush(pal.ink)
        p.drawEllipse(c, r, r)
        p.end()
