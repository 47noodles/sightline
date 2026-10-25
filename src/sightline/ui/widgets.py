"""Reusable widgets of the design system.

* ``Rule``: a one-device-pixel hairline.
* ``text``: a QLabel in one of the type roles (display, title, heading, body, label, small).
* ``GazeButton``: a large, flat button with an optional Lucide icon, for eyes and voice.
  Hover thickens the border, press and "on" invert the colours.  No accent:
  the accent is reserved for armed and listening.
* ``RuledTable``: rows separated by hairlines, a header row in the label style.
* ``ProgressLine``: a hairline-framed progress bar with a solid ink fill.
* ``SightlineWindow``: base for ordinary windows (setup, settings, model download).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFontMetricsF, QPainter, QTextOption
from PySide6.QtWidgets import QAbstractButton, QLabel, QSizePolicy, QWidget

from . import icons, winapi
from .theme import Theme, fill_hairline_h, fill_hairline_v, frame_rect


class Rule(QWidget):
    def __init__(self, theme: Theme, parent: QWidget | None = None, *, strong: bool = False, vertical: bool = False) -> None:
        super().__init__(parent)
        self._theme = theme
        self._strong = strong
        self._vertical = vertical
        if vertical:
            self.setFixedWidth(1)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        else:
            self.setFixedHeight(1)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        theme.changed.connect(self.update)

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        p = QPainter(self)
        pal = self._theme.palette
        color = pal.ink if self._strong else pal.rule
        if self._vertical:
            fill_hairline_v(p, 0, 0, self.height(), color)
        else:
            fill_hairline_h(p, 0, self.width(), 0, color)
        p.end()


def text(content: str, role: str = "body", parent: QWidget | None = None, *, wrap: bool = True, selectable: bool = False) -> QLabel:
    label = QLabel(content, parent)
    label.setProperty("role", role)
    label.setWordWrap(wrap)
    label.setTextFormat(Qt.TextFormat.PlainText)
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class GazeButton(QAbstractButton):
    """A big target: icon left of the label (``layout="row"``) or above it (``layout="stack"``).

    ``badge`` puts a short number in a solid square before the label (row layout),
    for numbered choices the user can also pick by saying the number.
    """

    def __init__(
        self,
        theme: Theme,
        label: str,
        icon: str | None = None,
        parent: QWidget | None = None,
        *,
        layout: str = "row",
        primary: bool = False,
        min_height: int = 64,
        focusable: bool = False,
        flat: bool = False,
        badge: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        self._icon = icon
        self._badge = badge
        self._layout = layout
        self._primary = primary
        self._min_height = min_height
        self._flat = flat  # a cell in a ruled column (the dock): no box, a hairline below
        self._on = False
        self.setText(label)
        self.setAccessibleName(label)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus if focusable else Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        theme.changed.connect(self._restyle)

    def _restyle(self) -> None:
        self.updateGeometry()
        self.update()

    # ------------------------------------------------------------------ state

    def set_label(self, label: str, icon: str | None = None) -> None:
        changed = label != self.text() or (icon is not None and icon != self._icon)
        self.setText(label)
        self.setAccessibleName(label)
        if icon is not None:
            self._icon = icon
        if changed:
            self.updateGeometry()
            self.update()

    def set_on(self, on: bool) -> None:
        """Show a non-default state (dwell on, eyes paused, voice asleep) by inverting the button."""
        if on != self._on:
            self._on = on
            self.update()

    @property
    def is_on(self) -> bool:
        return self._on

    @property
    def icon_name(self) -> str | None:
        return self._icon

    @property
    def badge(self) -> str | None:
        return self._badge

    def set_badge(self, badge: str | None) -> None:
        if badge != self._badge:
            self._badge = badge
            self.updateGeometry()
            self.update()

    def _badge_side(self) -> float:
        return self._theme.pxf(34)

    # ------------------------------------------------------------------ geometry

    def sizeHint(self) -> QSize:  # noqa: N802
        t = self._theme
        fm = QFontMetricsF(t.font("label"))
        if self._layout == "stack":
            w = max(fm.horizontalAdvance(self.text()) + t.pxf(16), t.pxf(88))
            return QSize(round(w), t.px(self._min_height))
        icon = t.pxf(22) + t.pxf(10) if self._icon else 0
        badge = self._badge_side() + t.pxf(14) if self._badge else 0
        w = fm.horizontalAdvance(self.text()) + icon + badge + t.pxf(40)
        return QSize(round(w), t.px(self._min_height))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        t = self._theme
        return QSize(t.px(64), t.px(self._min_height))

    # ------------------------------------------------------------------ painting

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        t = self._theme
        pal = t.palette
        p = QPainter(self)
        rect = QRectF(0, 0, self.width(), self.height())
        enabled = self.isEnabled()
        pressed = self.isDown()
        hover = self.underMouse() and enabled
        inverted = enabled and (self._on or pressed or self._primary)
        if pressed and (self._on or self._primary):
            inverted = False  # pressing an inverted button flips it back, so the press is visible
        if self._flat:
            fill = pal.ink if inverted else pal.surface
        else:
            fill = pal.ink if inverted else (pal.raised if enabled else pal.ground)
        fg = pal.ground if inverted else (pal.ink if enabled else pal.ink_2)
        p.fillRect(rect, fill)
        if self._flat:
            fill_hairline_h(p, 0, rect.width(), rect.height() - 1, pal.rule)
            if hover:
                frame_rect(p, rect.adjusted(1, 1, -1, -1), pal.ground if inverted else pal.ink, t.pxf(2.5))
        else:
            border_w = t.pxf(2.5) if hover else 1.0
            frame_rect(p, rect, pal.ink if (hover or inverted) else pal.rule_strong, border_w)

        font = t.font("label")
        p.setFont(font)
        p.setPen(fg)
        icon_size = t.pxf(24)
        if self._layout == "stack":
            gap = t.pxf(6)
            fm = QFontMetricsF(font)
            option = QTextOption(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            option.setWrapMode(QTextOption.WrapMode.WordWrap)
            text_w = rect.width() - t.pxf(12)
            text_h = fm.boundingRect(QRectF(0, 0, text_w, 1000), int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignHCenter), self.text()).height()
            block = (icon_size + gap if self._icon else 0) + text_h
            y = (rect.height() - block) / 2.0
            if self._icon:
                icons.draw_icon(p, self._icon, QRectF((rect.width() - icon_size) / 2.0, y, icon_size, icon_size), fg)
                y += icon_size + gap
            p.drawText(QRectF(t.pxf(6), y, text_w, text_h + 2), self.text(), option)
        else:
            x = t.pxf(18)
            if self._badge:
                # The number reads as a solid square in the text colour, so it stands apart from the label.
                side = self._badge_side()
                square = QRectF(t.pxf(12), (rect.height() - side) / 2.0, side, side)
                p.fillRect(square, fg)
                p.setFont(t.font("badge", size=18))
                p.setPen(fill)
                p.drawText(square, int(Qt.AlignmentFlag.AlignCenter), self._badge)
                p.setFont(font)
                p.setPen(fg)
                x = square.right() + t.pxf(14)
            if self._icon:
                icons.draw_icon(p, self._icon, QRectF(x, (rect.height() - icon_size) / 2.0, icon_size, icon_size), fg)
                x += icon_size + t.pxf(12)
            p.drawText(QRectF(x, 0, rect.width() - x - t.pxf(12), rect.height()), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self.text())
        p.end()


@dataclass(frozen=True, slots=True)
class TableRow:
    cells: tuple[str, ...]
    secondary: tuple[str, ...] = ()  # a second, smaller line under each cell (may be empty strings)
    heading: bool = False  # a section heading spanning every column


class RuledTable(QWidget):
    """A table drawn in one widget: fast with hundreds of rows, hairlines between rows, no boxes."""

    def __init__(self, theme: Theme, columns: Sequence[str], weights: Sequence[float], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._columns = tuple(columns)
        self._weights = tuple(weights)
        self._rows: list[TableRow] = []
        self._layout: list[tuple[float, float]] = []  # (y, height) per row
        self._line_heights: dict[int, float] = {}
        self._height = 0.0
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        theme.changed.connect(self._relayout)

    def set_rows(self, rows: Sequence[TableRow]) -> None:
        rows = list(rows)
        if rows == self._rows and self._layout:
            return  # unchanged (the help sheet refreshes on every open); skip the measuring
        self._rows = rows
        self._relayout()

    @property
    def rows(self) -> list[TableRow]:
        return list(self._rows)

    def row_top(self, index: int) -> float:
        return self._layout[index][0] if 0 <= index < len(self._layout) else 0.0

    def _col_rects(self, width: float) -> list[tuple[float, float]]:
        t = self._theme
        pad = t.pxf(16)
        usable = width - pad * (len(self._columns) + 1)
        total = sum(self._weights) or 1.0
        out, x = [], pad
        for wgt in self._weights:
            w = usable * wgt / total
            out.append((x, w))
            x += w + pad
        return out

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self._relayout()

    def _fonts(self) -> tuple[QFontMetricsF, QFontMetricsF, QFontMetricsF]:
        """Metrics for the first column (medium), the other columns and the secondary lines."""
        t = self._theme
        return (
            QFontMetricsF(t.font("body", weight=500)),
            QFontMetricsF(t.font("body")),
            QFontMetricsF(t.font("small")),
        )

    def _text_height(self, fm: QFontMetricsF, content: str, width: float) -> float:
        # Most cells fit on one line; measuring wrapped text is the slow path, so skip it when we can.
        if fm.horizontalAdvance(content or " ") <= width:
            key = id(fm)
            line = self._line_heights.get(key)
            if line is None:
                line = self._line_heights[key] = fm.boundingRect(QRectF(0, 0, 10_000, 10_000), int(Qt.TextFlag.TextWordWrap), "Ag").height()
            return line
        return fm.boundingRect(QRectF(0, 0, width, 10_000), int(Qt.TextFlag.TextWordWrap), content).height()

    def _relayout(self) -> None:
        t = self._theme
        width = max(self.width(), t.px(320))
        cols = self._col_rects(width)
        first, body, small = self._fonts()
        self._line_heights = {}
        head = QFontMetricsF(t.font("heading"))
        pad_y = t.pxf(10)
        y = QFontMetricsF(t.font("label")).height() + 2 * pad_y + 1 if any(self._columns) else 0.0
        self._layout = []
        for row in self._rows:
            if row.heading:
                h = head.height() + t.pxf(28)
            else:
                h = 0.0
                for i, (_x, w) in enumerate(cols):
                    cell = row.cells[i] if i < len(row.cells) else ""
                    ch = self._text_height(first if i == 0 else body, cell, w)
                    sec = row.secondary[i] if i < len(row.secondary) else ""
                    if sec:
                        ch += self._text_height(small, sec, w) + t.pxf(2)
                    h = max(h, ch)
                h += 2 * pad_y
            self._layout.append((y, h))
            y += h + 1
        self._height = y
        self.setMinimumHeight(round(self._height))
        self.setMaximumHeight(round(self._height))
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._theme.px(640), round(self._height))

    def paintEvent(self, event) -> None:  # noqa: ANN001
        t = self._theme
        pal = t.palette
        p = QPainter(self)
        cols = self._col_rects(self.width())
        pad_y = t.pxf(10)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WordWrap)
        visible = QRectF(event.rect())
        metrics = self._fonts()
        self._line_heights = {}
        if any(self._columns):
            p.setFont(t.font("label"))
            p.setPen(pal.ink_2)
            hh = QFontMetricsF(t.font("label")).height() + 2 * pad_y
            for (x, w), title in zip(cols, self._columns):
                p.drawText(QRectF(x, pad_y, w, hh - 2 * pad_y), title, option)
            fill_hairline_h(p, 0, self.width(), hh, pal.ink)
        for row, (y, h) in zip(self._rows, self._layout):
            if y > visible.bottom() or y + h < visible.top():
                continue
            if row.heading:
                p.setFont(t.font("heading"))
                p.setPen(pal.ink)
                p.drawText(QRectF(cols[0][0], y + t.pxf(18), self.width(), h - t.pxf(18)), row.cells[0], option)
                fill_hairline_h(p, 0, self.width(), y + h, pal.ink)
                continue
            for i, (x, w) in enumerate(cols):
                cell = row.cells[i] if i < len(row.cells) else ""
                p.setFont(t.font("body", weight=500 if i == 0 else 400))
                p.setPen(pal.ink)
                body_h = self._text_height(metrics[0] if i == 0 else metrics[1], cell, w)
                p.drawText(QRectF(x, y + pad_y, w, body_h + 2), cell, option)
                sec = row.secondary[i] if i < len(row.secondary) else ""
                if sec:
                    p.setFont(t.font("small"))
                    p.setPen(pal.ink_2)
                    p.drawText(QRectF(x, y + pad_y + body_h + t.pxf(2), w, h), sec, option)
            fill_hairline_h(p, 0, self.width(), y + h, pal.rule)
        p.end()


class ProgressLine(QWidget):
    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._value = 0.0
        self._busy = False
        self.setFixedHeight(theme.px(10))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        theme.changed.connect(lambda: (self.setFixedHeight(theme.px(10)), self.update()))

    def set_value(self, value: float) -> None:
        value = max(0.0, min(1.0, value))
        if abs(value - self._value) > 1e-4:
            self._value = value
            self.update()

    @property
    def value(self) -> float:
        return self._value

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        pal = self._theme.palette
        p = QPainter(self)
        rect = QRectF(0, 0, self.width(), self.height())
        p.fillRect(rect, pal.ground)
        frame_rect(p, rect, pal.ink)
        inner = rect.adjusted(2, 2, -2, -2)
        p.fillRect(QRectF(inner.x(), inner.y(), inner.width() * self._value, inner.height()), pal.ink)
        p.end()


class SightlineWindow(QWidget):
    """An ordinary window in the Sightline style, with a native title bar matched to the theme."""

    closed = Signal()

    def __init__(self, theme: Theme, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.theme = theme
        self.setObjectName("Window")
        self.setWindowTitle(title)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setWindowIcon(icons.app_icon(theme.palette.ink))
        theme.changed.connect(self._on_theme)

    def _on_theme(self) -> None:
        winapi.set_dark_title_bar(self, self.theme.palette.dark)
        self.setWindowIcon(icons.app_icon(self.theme.palette.ink))
        self.update()

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        winapi.set_dark_title_bar(self, self.theme.palette.dark)

    def closeEvent(self, event) -> None:  # noqa: ANN001
        super().closeEvent(event)
        self.closed.emit()
