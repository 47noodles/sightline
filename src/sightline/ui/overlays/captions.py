"""Live captions: what was heard (dim while partial, solid once final) and what Sightline did with it.

Shown bottom centre on the screen the pointer is on, above the taskbar, and
faded out a few seconds after the last update.  While voice is asleep
nothing is captioned except the command that wakes it.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QFontMetricsF, QPainter, QTextOption

from sightline.core.events import IntentResult, StateChanged, TranscriptFinal, TranscriptPartial
from sightline.core.types import SpeechMode

from ..services import UiServices
from ..theme import fill_hairline_h
from .base import OverlayWindow, solid_panel

MAX_CHARS = 160


def _tail(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_CHARS else "…" + text[-(MAX_CHARS - 1) :]


class Captions(OverlayWindow):
    def __init__(self, services: UiServices) -> None:
        super().__init__(services, name="captions")
        self.enabled = True
        self.hold_seconds = 3.5
        self._utterance = -1
        self._text = ""
        self._final = False
        self._outcome = ""
        self._kind = ""
        self._asleep = SpeechMode(services.ctx.state.get("speech_mode")) == SpeechMode.SLEEP
        self._fade = 1.0
        self._dimmed = False
        self._updated = 0.0
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)
        self._fade_anim = QVariantAnimation(self)
        self._fade_anim.setDuration(450)
        self._fade_anim.setStartValue(1.0)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.valueChanged.connect(self._on_fade)
        self._fade_anim.finished.connect(self._clear)
        services.cursor.moved.connect(self._on_cursor)
        services.theme.changed.connect(self._relayout)

    # ------------------------------------------------------------------ state for tests

    @property
    def text(self) -> str:
        return self._text

    @property
    def outcome(self) -> str:
        return self._outcome

    @property
    def is_final(self) -> bool:
        return self._final

    # ------------------------------------------------------------------ events

    def on_partial(self, e: TranscriptPartial) -> None:
        if not self.enabled or self._asleep or e.utterance_id < self._utterance:
            return
        if e.utterance_id > self._utterance:
            self._outcome, self._kind, self._final = "", "", False
        elif self._final:
            return  # a late partial must not overwrite the final text of the same utterance
        self._utterance = e.utterance_id
        self._text = _tail(e.text)
        self._show_now()

    def on_final(self, e: TranscriptFinal) -> None:
        if e.utterance_id < self._utterance:
            return
        if e.utterance_id > self._utterance:
            self._outcome, self._kind = "", ""
        self._utterance = e.utterance_id
        self._text, self._final = _tail(e.text), True
        if self.enabled and not self._asleep:
            self._show_now()

    def on_intent(self, e: IntentResult) -> None:
        if not self.enabled or e.utterance_id < self._utterance:
            return
        if e.kind == "ignored":
            if not e.display or self._asleep:
                return  # noise, or asleep: stay quiet
            # Talk that wasn't meant for the computer: say why nothing happened, but don't keep the words on screen.
            self._utterance = e.utterance_id
            self._text, self._final = "", True
            self._outcome, self._kind = e.display, e.kind
            self._show_now()
            return
        if self._asleep and e.kind != "command":
            return
        if e.utterance_id > self._utterance or not self._final or not self._text:
            # No final transcript arrived for this utterance: the intent carries the full text.
            self._text = _tail(e.text)
        self._utterance = e.utterance_id
        self._final = True
        self._outcome, self._kind = e.display, e.kind
        self._show_now()

    def on_state(self, e: StateChanged) -> None:
        if e.field == "speech_mode":
            self._asleep = e.new == SpeechMode.SLEEP

    # ------------------------------------------------------------------ showing

    def _show_now(self) -> None:
        self._fade_anim.stop()
        self._fade = 1.0
        self._updated = time.monotonic()
        self._relayout()
        if not self.isVisible():
            self.show()
        self.update()
        chars = len(self._text) + len(self._outcome)
        self._hide_timer.start(int(1000 * min(9.0, self.hold_seconds + 0.03 * chars)))

    def _fade_out(self) -> None:
        self._fade_anim.start()

    def _on_fade(self, value: object) -> None:
        self._fade = float(value)  # type: ignore[arg-type]
        self.update()

    def _clear(self) -> None:
        self.hide()
        self._text, self._outcome, self._kind = "", "", ""

    def hide_now(self) -> None:
        self._hide_timer.stop()
        self._fade_anim.stop()
        self._clear()

    def _on_cursor(self, x: float, y: float) -> None:
        if not self.isVisible():
            return
        lx, ly = self.svc.screens.map.to_logical(x, y)
        near = self.geometry().adjusted(-24, -24, 24, 24).contains(round(lx), round(ly))
        if near != self._dimmed:
            self._dimmed = near
            self.update()

    # ------------------------------------------------------------------ layout and paint

    def _metrics(self) -> tuple[float, float, float, float]:
        """(width, transcript height, outcome height, total height) in logical pixels."""
        t = self.svc.theme
        screen = self.svc.screens.map.screen_at(*self.svc.cursor.pos)
        max_w = min(t.pxf(760), screen.logical.w * 0.7)
        pad_x = t.pxf(20)
        text_font = t.font("caption")
        out_font = t.font("label", size=17)
        fm = QFontMetricsF(text_font)
        inner_max = max_w - 2 * pad_x
        flags = int(Qt.TextFlag.TextWordWrap)
        text_box = fm.boundingRect(QRectF(0, 0, inner_max, 10_000), flags, self._text or " ")
        out_w = 0.0
        out_h = 0.0
        if self._outcome:
            ofm = QFontMetricsF(out_font)
            out_box = ofm.boundingRect(QRectF(0, 0, inner_max - t.pxf(22), 10_000), flags, self._outcome)
            out_w, out_h = out_box.width() + t.pxf(22), out_box.height()
        width = max(t.pxf(280), min(max_w, max(text_box.width(), out_w) + 2 * pad_x + 1))
        text_h = min(text_box.height(), fm.lineSpacing() * 2 + 1)
        pad_y = t.pxf(12)
        total = pad_y + text_h + pad_y
        if self._outcome:
            total += out_h + pad_y + 1
        return width, text_h, out_h, total

    def reserved_height(self) -> float:
        """Logical pixels from the bottom of the work area to the top of the tallest caption.

        That is two lines heard plus one line of outcome, which is as tall as the panel
        gets while anything else (the "did you mean" choices) sits above it.
        """
        t = self.svc.theme
        heard = QFontMetricsF(t.font("caption")).lineSpacing() * 2 + 1
        outcome = QFontMetricsF(t.font("label", size=17)).lineSpacing()
        pad_y = t.pxf(12)
        return t.pxf(28) + pad_y + heard + pad_y + outcome + pad_y + 1

    def _relayout(self) -> None:
        if not self._text and not self._outcome:
            return
        t = self.svc.theme
        w, _th, _oh, h = self._metrics()
        smap = self.svc.screens.map
        screen = smap.screen_at(*self.svc.cursor.pos)
        work = smap.rect_to_logical(screen.work_rect, screen)
        x = work.x + (work.w - w) / 2.0
        y = work.bottom - h - t.pxf(28)
        self.screen_geometry = screen
        self.setGeometry(round(x), round(y), round(w), round(h))

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        t = self.svc.theme
        pal = t.palette
        p = QPainter(self)
        p.setOpacity(self._fade * (0.18 if self._dimmed else 1.0))
        full = QRectF(0, 0, self.width(), self.height())
        solid_panel(p, full, pal.surface, pal.ink)
        _w, text_h, out_h, _h = self._metrics()
        pad_x, pad_y = t.pxf(20), t.pxf(12)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WordWrap)
        p.setFont(t.font("caption"))
        p.setPen(pal.ink if self._final else pal.ink_2)
        p.drawText(QRectF(pad_x, pad_y, self.width() - 2 * pad_x, text_h), self._text, option)
        if self._outcome:
            y = pad_y + text_h + pad_y
            fill_hairline_h(p, 0, self.width(), y, pal.rule)
            y += 1 + pad_y / 2.0
            sq = t.pxf(11)
            marker = QRectF(pad_x, y + (t.pxf(22) - sq) / 2.0, sq, sq)
            if self._kind == "command":
                p.fillRect(marker.adjusted(-1, -1, 1, 1), pal.on_accent)
                p.fillRect(marker, pal.accent)
            elif self._kind == "rejected":
                p.fillRect(marker, pal.ink)
                inset = t.pxf(2)
                p.fillRect(marker.adjusted(inset, inset, -inset, -inset), pal.surface)
            else:
                p.fillRect(marker, pal.ink)
            p.setFont(t.font("label", size=17))
            p.setPen(pal.ink)
            p.drawText(QRectF(pad_x + t.pxf(22), y, self.width() - 2 * pad_x - t.pxf(22), out_h + 2), self._outcome, option)
        p.end()
