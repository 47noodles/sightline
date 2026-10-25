"""Controls for gaze and voice: steppers, choice rows, live previews and a level meter.

No sliders, spin boxes or text fields: a slider is a drag target and a spin
box is two tiny arrows, both hard to hit with eye-tracking error.  Every
control here is a row of ``GazeButton``s at least 64 px tall.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QSizePolicy, QWidget

from .overlays.base import pen
from .theme import Theme, fill_hairline_h, frame_rect
from .widgets import GazeButton, text


class Stepper(QWidget):
    """Large minus and plus buttons around the value in words.  ``stepped`` carries -1 or +1."""

    stepped = Signal(int)

    def __init__(self, theme: Theme, parent: QWidget | None = None, *, less: str = "Less", more: str = "More") -> None:
        super().__init__(parent)
        self.minus = GazeButton(theme, less, "minus", self, min_height=64)
        self.plus = GazeButton(theme, more, "plus", self, min_height=64)
        self.value = text("", "heading", self, wrap=False)
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for b in (self.minus, self.plus):
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            b.setMinimumWidth(theme.px(150))
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.px(12))
        row.addWidget(self.minus)
        row.addWidget(self.value, 1)
        row.addWidget(self.plus)
        self.minus.clicked.connect(lambda: self.stepped.emit(-1))
        self.plus.clicked.connect(lambda: self.stepped.emit(1))

    def set_value(self, words: str, *, can_less: bool = True, can_more: bool = True) -> None:
        self.value.setText(words)
        self.minus.setEnabled(can_less)
        self.plus.setEnabled(can_more)


class ChoiceRow(QWidget):
    """One large button per choice; the current one is shown inverted.  ``chosen`` carries the value."""

    chosen = Signal(object)

    def __init__(self, theme: Theme, choices: Sequence[tuple[Any, str]], parent: QWidget | None = None, *, columns: int | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self.buttons: list[tuple[Any, GazeButton]] = []
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(theme.px(10))
        grid.setVerticalSpacing(theme.px(10))
        longest = max((len(label) for _v, label in choices), default=0)
        cols = columns or (min(len(choices), 5) if longest <= 14 else min(len(choices), 3) if longest <= 26 else 2)
        for i, (value, label) in enumerate(choices):
            button = GazeButton(theme, label, None, self, min_height=64)
            button.clicked.connect(lambda _c=False, v=value: self.chosen.emit(v))
            grid.addWidget(button, i // max(1, cols), i % max(1, cols))
            self.buttons.append((value, button))

    def set_current(self, value: Any) -> None:
        for v, button in self.buttons:
            button.set_on(v == value)

    def set_enabled_values(self, enabled: Sequence[Any]) -> None:
        for v, button in self.buttons:
            button.setEnabled(v in enabled)


class Preview(QWidget):
    """A small live picture of what a setting does.

    ``speed``: a dot crossing at the pointer speed.  ``duration``: a bar that
    fills over the time.  ``ring``: a dwell ring that fills.  ``hearing``: a
    word that lights while the microphone hears speech.  It animates only
    while visible, so an open settings window costs nothing at rest.
    """

    def __init__(self, theme: Theme, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self.kind = kind
        self.value = 1.0
        self.caption = ""
        self.hearing = False
        self._t0 = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self.update)
        self.setFixedHeight(theme.px(56))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, value: float, caption: str = "") -> None:
        self.value = float(value)
        self.caption = caption
        self._t0 = time.monotonic()
        self.update()

    def set_hearing(self, hearing: bool) -> None:
        if hearing != self.hearing:
            self.hearing = hearing
            self.update()

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        if self.kind != "hearing":
            self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: ANN001
        super().hideEvent(event)
        self._timer.stop()

    def phase(self, now: float | None = None) -> float:
        """0..1 through the current cycle (exposed for tests)."""
        elapsed = (now if now is not None else time.monotonic()) - self._t0
        if self.kind == "speed":
            period = 2.4 / max(0.1, self.value)
        else:
            period = max(0.2, self.value / 1000.0) + 0.6  # the fill, then a short rest
        return (elapsed % period) / period

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        t = self._theme
        pal = t.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())
        p.fillRect(rect, pal.ground)
        frame_rect(p, rect, pal.rule)
        mid = rect.height() / 2.0
        pad = t.pxf(16)
        if self.kind == "speed":
            span = rect.width() - 2 * pad
            ph = self.phase()
            x = pad + span * (0.5 - 0.5 * math.cos(2 * math.pi * ph))
            fill_hairline_h(p, pad, rect.width() - pad, mid, pal.rule)
            r = t.pxf(9)
            p.setPen(pen(pal.ground, 2))
            p.setBrush(pal.ink)
            p.drawEllipse(QPointF(x, mid), r, r)
        elif self.kind == "duration":
            total = max(0.2, self.value / 1000.0)
            elapsed = self.phase() * (total + 0.6)
            frac = min(1.0, elapsed / total)
            bar = QRectF(pad, mid - t.pxf(6), rect.width() - 2 * pad, t.pxf(12))
            frame_rect(p, bar, pal.ink)
            p.fillRect(QRectF(bar.x(), bar.y(), bar.width() * frac, bar.height()), pal.accent if frac >= 1.0 else pal.ink)
        elif self.kind == "ring":
            total = max(0.2, self.value / 1000.0)
            frac = min(1.0, self.phase() * (total + 0.6) / total)
            r = t.pxf(16)
            centre = QPointF(rect.width() / 2.0, mid)
            p.setPen(pen(pal.rule_strong, t.pxf(3)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(centre, r, r)
            p.setPen(pen(pal.accent if frac >= 1.0 else pal.ink, t.pxf(4), cap=Qt.PenCapStyle.FlatCap))
            p.drawArc(QRectF(centre.x() - r, centre.y() - r, 2 * r, 2 * r), 90 * 16, -int(360 * 16 * frac))
        elif self.kind == "hearing":
            p.setFont(t.font("label"))
            word = "Hearing you" if self.hearing else "Say something to test"
            box = QRectF(pad, t.pxf(10), rect.width() - 2 * pad, rect.height() - t.pxf(20))
            if self.hearing:
                p.fillRect(box, pal.accent)
                p.setPen(pal.on_accent)
            else:
                p.setPen(pal.ink_2)
            p.drawText(box, int(Qt.AlignmentFlag.AlignCenter), word)
        if self.caption and self.kind != "hearing":
            p.setFont(t.font("small"))
            p.setPen(pal.ink_2)
            p.drawText(QRectF(pad, 0, rect.width() - 2 * pad, t.pxf(16)), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop), self.caption)
        p.end()


class LevelMeter(QWidget):
    """The microphone level, solid ink, amber while speech is heard."""

    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self.level = 0.0
        self.speaking = False
        self.setFixedHeight(theme.px(24))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_level(self, rms: float, speaking: bool) -> None:
        # The same fast-rise curve as the status strip, so quiet speech still shows.
        level = min(1.0, math.sqrt(max(0.0, rms)) * 1.6)
        if abs(level - self.level) > 0.01 or speaking != self.speaking:
            self.level, self.speaking = level, speaking
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        pal = self._theme.palette
        p = QPainter(self)
        rect = QRectF(0, 0, self.width(), self.height())
        p.fillRect(rect, pal.ground)
        frame_rect(p, rect, pal.ink)
        inner = rect.adjusted(3, 3, -3, -3)
        p.fillRect(QRectF(inner.x(), inner.y(), inner.width() * self.level, inner.height()), pal.accent if self.speaking else pal.ink)
        p.end()
