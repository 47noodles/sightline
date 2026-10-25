"""The status strip: what the voice is doing, the microphone level, and whether the eyes are tracked.

``StatusModel`` collects the state; ``StatusView`` draws it (inside the dock,
or inside ``HudWindow`` when the dock is hidden).
"""

from __future__ import annotations

import math

from PySide6.QtCore import QObject, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFontMetricsF, QPainter
from PySide6.QtWidgets import QWidget

from sightline.core.events import FaceStatus, MicLevel, SpeechStatus, StateChanged
from sightline.core.types import SpeechMode

from .. import icons
from ..services import UiServices
from ..theme import fill_hairline_h
from .base import OverlayWindow, solid_panel


class StatusModel(QObject):
    changed = Signal()
    level_changed = Signal()

    def __init__(self, services: UiServices) -> None:
        super().__init__()
        ctx = services.ctx
        st = ctx.state.snapshot()
        self.speech_available = ctx.speech is not None
        self.vision_available = ctx.vision is not None
        self.speech_mode = SpeechMode(st.speech_mode)
        self.listening = st.listening
        self.speaking = st.speaking
        self.pointer_paused = st.pointer_paused
        self.face_present: bool | None = None
        self.speech_ready: bool | None = None
        self.speech_error: str | None = None
        self.level = 0.0

    # ------------------------------------------------------------------ inputs

    def on_state(self, e: StateChanged) -> None:
        if e.field in ("speech_mode", "listening", "speaking", "pointer_paused", "face_present"):
            value = SpeechMode(e.new) if e.field == "speech_mode" else e.new
            setattr(self, e.field, value)
            self.changed.emit()

    def on_mic(self, e: MicLevel) -> None:
        db = 20.0 * math.log10(max(e.rms, 1e-6))
        target = max(0.0, min(1.0, (db + 60.0) / 54.0))
        # Fast rise, slow fall, like a level meter on a mixing desk.
        self.level = target if target > self.level else self.level * 0.75 + target * 0.25
        speaking_changed = e.speaking != self.speaking
        self.speaking = e.speaking
        self.level_changed.emit()
        if speaking_changed:
            self.changed.emit()

    def on_speech_status(self, e: SpeechStatus) -> None:
        self.speech_ready = e.ready
        self.speech_error = e.error
        self.changed.emit()

    def on_face(self, e: FaceStatus) -> None:
        if e.present != self.face_present:
            self.face_present = e.present
            self.changed.emit()

    # ------------------------------------------------------------------ derived text

    def voice(self) -> tuple[str, str]:
        """(icon, short text) for the voice row.  The icon says which row it is, so the text stays short."""
        if not self.speech_available:
            return "mic-off", "Voice off"
        if self.speech_error:
            return "mic-off", "Problem"
        if self.speech_ready is False:
            return "mic", "Starting"
        if not self.listening:
            return "mic-off", "Mic off"
        return {
            SpeechMode.SLEEP: ("moon", "Asleep"),
            SpeechMode.COMMAND: ("mic", "Commands"),
            SpeechMode.DICTATION: ("mic", "Dictation"),
        }.get(self.speech_mode, ("mic", "Listening"))

    def voice_on(self) -> bool:
        return self.speech_available and self.listening and self.speech_mode != SpeechMode.SLEEP and not self.speech_error

    def hearing(self) -> bool:
        """The microphone is hearing speech right now: the one place the status uses the accent."""
        return self.voice_on() and self.speaking

    def eyes(self) -> tuple[str, str]:
        if not self.vision_available:
            return "eye-off", "Eyes off"
        if self.pointer_paused:
            return "eye-off", "Paused"
        if self.face_present is None:
            return "eye", "Starting"
        return ("eye", "Tracking") if self.face_present else ("scan-eye", "No face")

    def eyes_on(self) -> bool:
        return self.vision_available and not self.pointer_paused and bool(self.face_present)


class StatusView(QWidget):
    """Two ruled rows: voice (with a level meter) and eyes.  A filled square means on."""

    def __init__(self, services: UiServices, model: StatusModel, parent: QWidget | None = None, *, framed: bool = False) -> None:
        super().__init__(parent)
        self.svc = services
        self.model = model
        self.framed = framed
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        model.changed.connect(self.update)
        model.level_changed.connect(self._update_meter)
        services.theme.changed.connect(self.updateGeometry)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt
        t = self.svc.theme
        return QSize(t.px(176), t.px(76))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def _meter_rect(self) -> QRectF:
        t = self.svc.theme
        pad = t.pxf(10)
        return QRectF(pad, t.pxf(34), self.width() - 2 * pad, t.pxf(5))

    def _update_meter(self) -> None:
        self.update(self._meter_rect().adjusted(-2, -2, 2, 2).toAlignedRect())

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        t = self.svc.theme
        pal = t.palette
        m = self.model
        p = QPainter(self)
        full = QRectF(0, 0, self.width(), self.height())
        if self.framed:
            solid_panel(p, full, pal.surface, pal.ink)
        pad = t.pxf(10)
        icon = t.pxf(18)
        font = t.font("status")
        p.setFont(font)
        fm = QFontMetricsF(font)
        row_h = t.pxf(28)

        def row(y: float, glyph: str, text: str) -> None:
            icons.draw_icon(p, glyph, QRectF(pad, y + (row_h - icon) / 2.0, icon, icon), pal.ink)
            x = pad + icon + t.pxf(7)
            text_rect = QRectF(x, y, self.width() - x - t.pxf(6), row_h)
            p.setPen(pal.ink)
            p.drawText(text_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), fm.elidedText(text, Qt.TextElideMode.ElideRight, text_rect.width()))

        row(t.pxf(4), *m.voice())
        meter = self._meter_rect()
        p.fillRect(meter, pal.rule)
        if m.voice_on() and m.level > 0.01:
            fill = QRectF(meter.x(), meter.y(), meter.width() * m.level, meter.height())
            if m.hearing():
                p.fillRect(fill, pal.accent)
            else:
                p.fillRect(fill, pal.ink_2)
        fill_hairline_h(p, pad, self.width() - pad, t.pxf(44), pal.rule)
        row(t.pxf(46), *m.eyes())
        p.end()


class HudWindow(OverlayWindow):
    """The status strip on its own, top right, when the dock is hidden.  Fades when the pointer is over it."""

    def __init__(self, services: UiServices, model: StatusModel) -> None:
        super().__init__(services, name="hud")
        self.view = StatusView(services, model, self, framed=True)
        self._faded = False
        services.theme.changed.connect(self.reposition)
        services.screens.changed.connect(self.reposition)
        services.cursor.moved.connect(self._on_cursor)
        self.reposition()

    def reposition(self) -> None:
        t = self.svc.theme
        size = self.view.sizeHint()
        self.view.resize(size)
        screen = self.svc.screens.map.primary
        work = self.svc.screens.map.rect_to_logical(screen.work_rect, screen)
        margin = t.px(8)
        self.setGeometry(
            round(work.right - size.width() - margin), round(work.top + margin), size.width(), size.height()
        )

    def _on_cursor(self, x: float, y: float) -> None:
        if not self.isVisible():
            return
        lx, ly = self.svc.screens.map.to_logical(x, y)
        near = self.geometry().adjusted(-24, -24, 24, 24).contains(round(lx), round(ly))
        if near != self._faded:
            self._faded = near
            self.setWindowOpacity(0.15 if near else 1.0)
