"""The "what can I say" sheet: eye gestures first, then every voice command grouped by category.

It never takes keyboard focus (the user's text field keeps it), its buttons
are big enough to hit by eye, and while it is open "scroll up" and "scroll
down" (or page up and page down) scroll the sheet rather than the window
behind it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from sightline.core.types import Action, CommandHelp

from ..services import UiServices
from ..widgets import GazeButton, Rule, RuledTable, TableRow, text
from .base import OverlayWindow, solid_panel

log = logging.getLogger(__name__)

_GESTURE_WORDS = {
    "click": "Click",
    "double_click": "Double click",
    "right_click": "Right click",
    "middle_click": "Middle click",
    "drag_toggle": "Start or end a drag",
    "none": "Nothing",
}

_PAGE_KEYS = {"pagedown": 1, "page_down": 1, "page down": 1, "pgdn": 1, "pageup": -1, "page_up": -1, "page up": -1, "pgup": -1}


def _seconds(ms: object, default: float) -> str:
    try:
        value = float(ms) / 1000.0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = default
    return f"{value:.2f}".rstrip("0").rstrip(".")


def eye_rows(vision_settings: object) -> list[TableRow]:
    """Plain-English rows for the eye gestures, from the user's current settings."""
    actions = dict(getattr(vision_settings, "gesture_actions", {}) or {})
    click_s = _seconds(getattr(vision_settings, "blink_click_ms", 350), 0.35)
    double_s = _seconds(getattr(vision_settings, "blink_double_ms", 1000), 1.0)
    cancel_s = _seconds(getattr(vision_settings, "blink_cancel_ms", 2500), 2.5)

    def does(gesture: str, default: str) -> str:
        return _GESTURE_WORDS.get(str(actions.get(gesture, default)), str(actions.get(gesture, default)).replace("_", " ").capitalize())

    return [
        TableRow(("With your eyes",), heading=True),
        TableRow((f"Close both eyes for {click_s} s", f"{does('long_blink', 'click')} where you were looking"), ("", "A tick tells you it is ready. It happens when you open your eyes.")),
        TableRow((f"Keep them closed for {double_s} s", does("hold_blink", "double_click")), ("", "A second, higher tick.")),
        TableRow(("Wink your right eye", does("wink_right", "right_click"))),
        TableRow(("Wink your left eye", does("wink_left", "middle_click"))),
        TableRow((f"Close them longer than {cancel_s} s", "Nothing"), ("", "Resting your eyes never clicks.")),
        TableRow(("Look at a dock button and blink", "Use that button"), ("", "Pause, scroll, keyboard, numbers, zoom and more.")),
    ]


def command_rows(commands: Iterable[CommandHelp]) -> list[TableRow]:
    groups: dict[str, list[CommandHelp]] = {}
    for c in commands:
        groups.setdefault(c.category or "Other", []).append(c)
    rows: list[TableRow] = []
    for category, items in groups.items():
        rows.append(TableRow((category,), heading=True))
        for c in items:
            example = f"For example: {c.example}" if c.example else ""
            rows.append(TableRow((c.phrase, c.description), (example, "")))
    return rows


class HelpSheet(OverlayWindow):
    def __init__(self, services: UiServices) -> None:
        super().__init__(services, click_through=False, name="help")
        t = services.theme
        self.setWindowTitle("What you can say")
        root = QVBoxLayout(self)
        m = t.px(24)
        root.setContentsMargins(m, t.px(18), m, m)
        root.setSpacing(t.px(14))

        header = QHBoxLayout()
        title = text("What you can say", "title", wrap=False)
        header.addWidget(title, 1)
        self.close_button = GazeButton(t, "Close", "x", min_height=56)
        self.close_button.setFixedWidth(t.px(140))
        self.close_button.clicked.connect(self._close_clicked)
        header.addWidget(self.close_button)
        root.addLayout(header)
        root.addWidget(Rule(t, strong=True))

        self.table = RuledTable(t, ("Say or do", "What happens"), (1.0, 1.25))
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.addWidget(self.table)
        holder_layout.addStretch(1)
        self.scroll.setWidget(holder)
        root.addWidget(self.scroll, 1)

        footer = QHBoxLayout()
        footer.setSpacing(t.px(12))
        self.up_button = GazeButton(t, "Scroll up", "chevrons-up", min_height=64)
        self.down_button = GazeButton(t, "Scroll down", "chevrons-down", min_height=64)
        self.up_button.clicked.connect(lambda: self.scroll_by(-1))
        self.down_button.clicked.connect(lambda: self.scroll_by(1))
        footer.addWidget(self.up_button)
        footer.addWidget(self.down_button)
        root.addWidget(Rule(t))
        root.addLayout(footer)

        self._remove_interceptor = None
        services.overlays.register("help", self._close)
        services.theme.changed.connect(self.update)

    # ------------------------------------------------------------------ actions (GUI thread)

    def show_sheet(self, action: Action | None = None) -> None:
        self.refresh()
        smap = self.svc.screens.map
        screen = smap.screen_at(*self.svc.here(action))  # the monitor the user was looking at
        logical = screen.logical
        t = self.svc.theme
        w = min(t.pxf(880), logical.w * 0.74)
        h = logical.h * 0.82
        self.screen_geometry = screen
        self.setGeometry(round(logical.x + (logical.w - w) / 2), round(logical.y + (logical.h - h) / 2), round(w), round(h))
        self.scroll.verticalScrollBar().setValue(0)
        self.svc.overlays.opened("help")
        if self._remove_interceptor is None:
            self._remove_interceptor = self.svc.ctx.actions.add_interceptor(self._intercept, priority=90, owner="ui.help")
        self.show()
        self.raise_()

    def hide_sheet(self, _action: Action | None = None) -> None:
        self.svc.overlays.close("help")

    def refresh(self) -> None:
        rows = eye_rows(getattr(self.svc.ctx.settings, "vision", None))
        speech = self.svc.ctx.speech
        commands: list[CommandHelp] = []
        if speech is not None:
            try:
                commands = list(speech.commands_help())
            except Exception:
                log.exception("commands_help failed")
        if commands:
            rows += command_rows(commands)
        else:
            rows += [TableRow(("Voice commands",), heading=True), TableRow(("Voice is not running", "Start Sightline with a microphone to use voice commands."))]
        self.table.set_rows(rows)

    def scroll_by(self, pages: float) -> None:
        bar = self.scroll.verticalScrollBar()
        step = max(1, int(self.scroll.viewport().height() * 0.8))
        bar.setValue(bar.value() + round(step * pages))

    # ------------------------------------------------------------------ internals

    def _close_clicked(self) -> None:
        self.svc.overlays.close("help")

    def _close(self) -> None:
        if self._remove_interceptor is not None:
            self._remove_interceptor()
            self._remove_interceptor = None
        self.hide()
        self.svc.overlays.closed("help")

    def _intercept(self, action: Action) -> Action | None:
        """Action thread: scrolling and paging go to the sheet while it is open."""
        if action.name == "mouse.scroll" and action.arg("x") is None:
            direction = str(action.arg("direction", "down")).lower()
            sign = -1 if direction == "up" else 1 if direction == "down" else 0
            if sign == 0:
                return action
            amount = float(action.arg("amount", 3) or 3)
            self.svc.bridge.post(lambda: self.scroll_by(sign * max(0.25, amount / 6.0)))
            return None
        if action.name == "key.press":
            combo = str(action.arg("combo", "")).lower().strip()
            if combo in _PAGE_KEYS:
                sign = _PAGE_KEYS[combo]
                self.svc.bridge.post(lambda: self.scroll_by(sign))
                return None
        return action

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        pal = self.svc.theme.palette
        p = QPainter(self)
        solid_panel(p, QRectF(0, 0, self.width(), self.height()), pal.ground, pal.ink)
        p.end()
