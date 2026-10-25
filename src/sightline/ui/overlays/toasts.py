"""Toasts: short notices and the reason an action failed, top centre of the pointer's screen.

A notice with a ``key`` replaces the toast with the same key instead of
stacking; identical text within a couple of seconds refreshes the existing
toast.  At most three are shown.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QFontMetricsF, QPainter, QTextOption

from sightline.core.events import ActionExecuted, Notice

from .. import icons
from ..services import UiServices
from .base import OverlayWindow, solid_panel

MAX_TOASTS = 3
SECONDS = {"info": 4.0, "warn": 6.0, "error": 8.0}
_EXCEPTION_TEXT = re.compile(r"^[A-Z][A-Za-z0-9_]*(Error|Exception|Warning|Interrupt)\b")


def failure_text(e: ActionExecuted) -> str:
    """The user-facing reason for a failed action.

    ActionError messages are written for people and shown as they are.
    Anything else is a bug or a missing piece, which the log explains.
    """
    error = (e.error or "").strip()
    if error.startswith("unknown action"):
        return "That command is not available yet."
    if not error or _EXCEPTION_TEXT.match(error):
        return "Something went wrong. The log has the details."
    return error


@dataclass
class _Entry:
    text: str
    level: str
    key: str | None
    created: float


class Toast(OverlayWindow):
    def __init__(self, services: UiServices, entry: _Entry) -> None:
        super().__init__(services, name="toast")
        self.entry = entry
        self._fade = 1.0
        self._dimmed = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(300)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.valueChanged.connect(self._on_fade)

    def restart(self) -> None:
        self._anim.stop()
        self._fade = 1.0
        self._timer.start(int(1000 * SECONDS.get(self.entry.level, 4.0)))
        self.update()

    def _on_fade(self, value: object) -> None:
        self._fade = float(value)  # type: ignore[arg-type]
        self.update()

    def set_dimmed(self, dimmed: bool) -> None:
        if dimmed != self._dimmed:
            self._dimmed = dimmed
            self.update()

    # ------------------------------------------------------------------ layout

    def layout_size(self, max_width: float) -> tuple[float, float]:
        t = self.svc.theme
        font = t.font("body", weight=600 if self.entry.level == "error" else 400)
        fm = QFontMetricsF(font)
        pad = t.pxf(14)
        icon = t.pxf(22)
        text_w = max_width - 2 * pad - icon - t.pxf(12)
        box = fm.boundingRect(QRectF(0, 0, text_w, 10_000), int(Qt.TextFlag.TextWordWrap), self.entry.text)
        height = min(box.height(), fm.lineSpacing() * 3 + 1)
        width = box.width() + 2 * pad + icon + t.pxf(12) + 1
        return max(width, t.pxf(220)), max(height, icon) + 2 * pad

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        t = self.svc.theme
        pal = t.palette
        p = QPainter(self)
        p.setOpacity(self._fade * (0.18 if self._dimmed else 1.0))
        full = QRectF(0, 0, self.width(), self.height())
        error = self.entry.level == "error"
        solid_panel(p, full, pal.surface, pal.ink, t.pxf(2) if error else 1.0)
        pad = t.pxf(14)
        icon = t.pxf(22)
        name = {"error": "circle-alert", "warn": "triangle-alert"}.get(self.entry.level, "info")
        icons.draw_icon(p, name, QRectF(pad, pad, icon, icon), pal.ink, stroke=2.25 if error else 2.0)
        p.setFont(t.font("body", weight=600 if error else 400))
        p.setPen(pal.ink)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WordWrap)
        text_x = pad + icon + t.pxf(12)
        p.drawText(QRectF(text_x, pad - t.pxf(1), self.width() - text_x - pad, self.height() - 2 * pad + t.pxf(2)), self.entry.text, option)
        p.end()


class ToastStack(QObject):
    def __init__(self, services: UiServices) -> None:
        super().__init__()
        self.svc = services
        self._toasts: list[Toast] = []
        services.cursor.moved.connect(self._on_cursor)

    @property
    def toasts(self) -> list[Toast]:
        return list(self._toasts)

    def on_notice(self, e: Notice) -> None:
        self.show(e.text, e.level, e.key)

    def on_action(self, e: ActionExecuted) -> None:
        if not e.ok:
            self.show(failure_text(e), "error", key=f"action:{e.action.name}")

    def show(self, text: str, level: str = "info", key: str | None = None) -> Toast:
        now = time.monotonic()
        for toast in self._toasts:
            same_key = key is not None and toast.entry.key == key
            same_text = toast.entry.text == text and now - toast.entry.created < 2.5
            if same_key or same_text:
                toast.entry = _Entry(text, level, key, now)
                toast.restart()
                self._layout()
                return toast
        toast = Toast(self.svc, _Entry(text, level, key, now))
        toast._timer.timeout.connect(lambda t=toast: self._expire(t))
        toast._anim.finished.connect(lambda t=toast: self._remove(t))
        self._toasts.append(toast)
        while len(self._toasts) > MAX_TOASTS:
            self._remove(self._toasts[0])
        toast.restart()
        self._layout()
        toast.show()
        return toast

    def clear(self) -> None:
        for toast in list(self._toasts):
            self._remove(toast)

    def _expire(self, toast: Toast) -> None:
        toast._anim.start()

    def _remove(self, toast: Toast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
        toast._timer.stop()
        toast._anim.stop()
        toast.hide()
        toast.deleteLater()
        self._layout()

    def _layout(self) -> None:
        t = self.svc.theme
        smap = self.svc.screens.map
        screen = smap.screen_at(*self.svc.cursor.pos)
        work = smap.rect_to_logical(screen.work_rect, screen)
        max_w = min(t.pxf(560), work.w * 0.8)
        y = work.top + t.pxf(16)
        for toast in self._toasts:
            w, h = toast.layout_size(max_w)
            toast.screen_geometry = screen
            toast.setGeometry(round(work.x + (work.w - w) / 2.0), round(y), round(w), round(h))
            y += h + t.pxf(8)

    def _on_cursor(self, x: float, y: float) -> None:
        if not self._toasts:
            return
        lx, ly = self.svc.screens.map.to_logical(x, y)
        for toast in self._toasts:
            toast.set_dimmed(toast.geometry().adjusted(-24, -24, 24, 24).contains(round(lx), round(ly)))
