"""Numbered choices that a blink can pick, such as "did you mean" after an utterance Sightline could not place.

The window takes clicks but never focus, like the dock.  An eye click is a
real mouse click at the pointer, so the choices cannot be click-through, and
taking focus would pull the keyboard away from the window the user is in.
Once shown the choices stay where they are: a target that moves while the
user aims at it is worse than one slightly out of the way.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from PySide6.QtCore import QObject, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QFontMetricsF, QPainter

from sightline.core.events import IntentResult
from sightline.core.types import Action

from ..coords import ScreenGeometry
from ..services import UiServices
from ..widgets import GazeButton
from .base import OverlayWindow, solid_panel
from .captions import Captions

log = logging.getLogger(__name__)

MAX_CHOICES = 3
CHOICE_SECONDS = 8.0
MAX_SECONDS = 30.0  # the pointer resting on the choices keeps them up, but not for ever
RECHECK_MS = 1500
CHIP_HEIGHT = 72  # logical pixels before ui.scale: bigger than the 64 px floor, these are picked with the eyes
# (title, hint) over the choices: guesses at a rejected utterance, or the offer to remember how a command was said.
DID_YOU_MEAN = ("Did you mean", "Say the number, or look at one and blink")
REMEMBER = ("Remember this?", "Say yes, or look at it and blink")


class ChoiceBar(OverlayWindow):
    """A short title over large numbered buttons, in a row when they fit.  ``chosen`` carries the 1-based number."""

    chosen = Signal(int)

    def __init__(self, services: UiServices, *, name: str = "choices", title: str = "", hint: str = "", seconds: float = CHOICE_SECONDS) -> None:
        super().__init__(services, click_through=False, name=name)
        self.title = title
        self.hint = hint
        self.seconds = seconds
        self.buttons: list[GazeButton] = []
        self._labels: list[str] = []
        self._screen: ScreenGeometry | None = None
        self._anchor = (0.0, 0.0)
        self._above = True
        self._show_hint = True
        self._shown_at = 0.0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._expire)
        services.theme.changed.connect(self._relayout)

    @property
    def labels(self) -> list[str]:
        """The choices as given (the buttons may show them shortened)."""
        return list(self._labels)

    # ------------------------------------------------------------------ showing

    def show_choices(self, labels: Sequence[str], screen: ScreenGeometry, anchor: tuple[float, float], *, above: bool = True) -> None:
        """Show ``labels`` numbered from 1.

        ``anchor`` is a logical point on ``screen``: the bar is centred on its x, with its
        bottom edge on its y when ``above`` (else its top edge), kept inside the work area.
        """
        self._clear_buttons()
        self._labels = [str(label) for label in labels]
        if not self._labels:
            self.dismiss()
            return
        for number, label in enumerate(self._labels, start=1):
            button = GazeButton(self.svc.theme, label, None, self, min_height=CHIP_HEIGHT, badge=str(number))
            button.setAccessibleName(label)
            button.clicked.connect(lambda _checked=False, n=number: self._choose(n))
            self.buttons.append(button)
        self._screen, self._anchor, self._above = screen, anchor, above
        self._relayout()
        for button in self.buttons:
            button.show()
        self.show()
        self.raise_()
        self.update()
        self._shown_at = time.monotonic()
        self._timer.start(int(self.seconds * 1000))

    def dismiss(self) -> None:
        self._timer.stop()
        if self.isVisible():
            self.hide()
        self._clear_buttons()
        self._labels = []

    def _clear_buttons(self) -> None:
        for button in self.buttons:
            button.hide()
            button.deleteLater()  # safe from inside the button's own clicked signal
        self.buttons = []

    def _choose(self, number: int) -> None:
        if not self.isVisible():
            return
        self.dismiss()
        self.chosen.emit(number)

    def _expire(self) -> None:
        if self._pointer_on_bar() and time.monotonic() - self._shown_at < MAX_SECONDS:
            self._timer.start(RECHECK_MS)  # the user may be aiming at one: do not pull it away
            return
        self.dismiss()

    def _pointer_on_bar(self) -> bool:
        if not self.isVisible():
            return False
        lx, ly = self.svc.screens.map.to_logical(*self.svc.cursor.pos)
        margin = self.svc.theme.px(24)
        return self.geometry().adjusted(-margin, -margin, margin, margin).contains(round(lx), round(ly))

    # ------------------------------------------------------------------ layout and paint

    def _relayout(self) -> None:
        if not self.buttons or self._screen is None:
            return
        t = self.svc.theme
        smap = self.svc.screens.map
        work = smap.rect_to_logical(self._screen.work_rect, self._screen)
        pad, gap = t.pxf(14), t.pxf(10)
        avail = work.w - 2 * t.pxf(16)
        title_fm = QFontMetricsF(t.font("label"))
        hint_fm = QFontMetricsF(t.font("small"))
        head_h = title_fm.height()
        chip_h = float(t.px(CHIP_HEIGHT))
        min_w, max_w = t.pxf(190), min(t.pxf(400), avail - 2 * pad)
        # From the full labels, not the buttons' current (perhaps shortened) text, so a relayout is stable.
        label_fm = QFontMetricsF(t.font("label"))
        widths = [min(max_w, max(min_w, label_fm.horizontalAdvance(label) + t.pxf(88))) for label in self._labels]
        for button, width, label in zip(self.buttons, widths, self._labels, strict=True):
            button.set_label(label_fm.elidedText(label, Qt.TextElideMode.ElideRight, width - t.pxf(72)))
            button.setAccessibleName(label)
        title_w = title_fm.horizontalAdvance(self.title)
        header_w = title_w + (3 * gap + hint_fm.horizontalAdvance(self.hint) if self.hint else 0)
        row_w = sum(widths) + gap * (len(widths) - 1)
        in_row = row_w + 2 * pad <= avail
        content_w = row_w if in_row else max(widths)
        width = min(avail, max(content_w, header_w) + 2 * pad)
        self._show_hint = bool(self.hint) and header_w + 2 * pad <= width
        chips_h = chip_h if in_row else len(widths) * chip_h + (len(widths) - 1) * gap
        height = pad + head_h + gap + chips_h + pad

        x = self._anchor[0] - width / 2.0
        y = self._anchor[1] - height if self._above else self._anchor[1]
        x = max(work.x, min(x, work.right - width))
        y = max(work.y, min(y, work.bottom - height))
        self.setGeometry(round(x), round(y), round(width), round(height))

        top = pad + head_h + gap
        if in_row:
            left = (width - row_w) / 2.0
            for button, w in zip(self.buttons, widths, strict=True):
                button.setGeometry(QRect(round(left), round(top), round(w), round(chip_h)))
                left += w + gap
        else:
            for i, button in enumerate(self.buttons):
                button.setGeometry(QRect(round(pad), round(top + i * (chip_h + gap)), round(width - 2 * pad), round(chip_h)))

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        t = self.svc.theme
        pal = t.palette
        p = QPainter(self)
        solid_panel(p, QRectF(0, 0, self.width(), self.height()), pal.surface, pal.ink)
        pad = t.pxf(14)
        head_h = QFontMetricsF(t.font("label")).height()
        head = QRectF(pad, pad, self.width() - 2 * pad, head_h)
        p.setFont(t.font("label"))
        p.setPen(pal.ink)
        p.drawText(head, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.title)
        if self._show_hint:
            p.setFont(t.font("small"))
            p.setPen(pal.ink_2)
            p.drawText(head, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), self.hint)
        p.end()


def is_alias_offer(e: IntentResult) -> bool:
    """A command that ran and carries one choice: speech offering to remember how the user said it.

    ("Say yes to remember 'scold down' as 'scroll down'".)  It gets its own title, because it
    is not a guess at what a missed utterance meant, which is what "Did you mean" says.
    """
    return e.kind == "command" and len(e.alternatives) == 1


class DidYouMean(QObject):
    """Shows the alternatives of an utterance Sightline could not place; picking one runs ``speech.choose``.

    The same bar carries speech's offer to remember a mishearing as an alias, under its own title.
    """

    def __init__(self, services: UiServices, captions: Captions) -> None:
        super().__init__()
        self.svc = services
        self.captions = captions
        title, hint = DID_YOU_MEAN
        self.bar = ChoiceBar(services, name="did-you-mean", title=title, hint=hint)
        self.bar.chosen.connect(self._choose)

    def on_intent(self, e: IntentResult) -> None:
        if e.kind == "ignored":
            return  # talk that was not meant for the computer leaves the choices where they are
        if not e.alternatives:
            self.bar.dismiss()
            return
        self.bar.title, self.bar.hint = REMEMBER if is_alias_offer(e) else DID_YOU_MEAN
        t = self.svc.theme
        smap = self.svc.screens.map
        screen = smap.screen_at(*self.svc.cursor.pos)
        work = smap.rect_to_logical(screen.work_rect, screen)
        # Above the tallest caption, so new captions never cover a choice the user is aiming at.
        below = self.captions.reserved_height() if self.captions.enabled else t.pxf(28)
        anchor = (work.x + work.w / 2.0, work.bottom - below - t.pxf(10))
        self.bar.show_choices(e.alternatives[:MAX_CHOICES], screen, anchor)

    def dismiss(self) -> None:
        self.bar.dismiss()

    def _choose(self, number: int) -> None:
        log.info("Did you mean: choice %d", number)
        self.svc.ctx.actions.dispatch(Action("speech.choose", {"index": number}, source="ui"))
