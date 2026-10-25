"""The dock: a column of large buttons on a screen edge, usable by blink or dwell alone.

It never takes focus (WS_EX_NOACTIVATE), so the text field the user was
typing into keeps the caret.  On Windows it registers as an app bar, which
keeps maximised windows (and their scroll bars and close buttons) out from
under it.

Buttons that act on "where I was looking" (scroll, zoom, the action menu)
use the last pointer position outside the dock, because by the time a dock
button is clicked the pointer is on the dock.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from PySide6.QtCore import QRectF, QTimer
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QBoxLayout, QWidget

from sightline.core.events import StateChanged
from sightline.core.types import Action, ClickType, Rect, SpeechMode

from . import winapi
from .overlays.base import OverlayWindow, solid_panel
from .overlays.status import StatusModel, StatusView
from .services import UiServices
from .widgets import GazeButton

log = logging.getLogger(__name__)

TARGET_MEMORY_S = 60.0

CLICK_TYPES: tuple[tuple[ClickType, str, str], ...] = (
    (ClickType.LEFT, "Left click", "mouse-left"),
    (ClickType.DOUBLE, "Double click", "mouse-pointer-click"),
    (ClickType.RIGHT, "Right click", "mouse-right"),
    (ClickType.MIDDLE, "Middle click", "mouse-pointer-2"),
    (ClickType.DRAG, "Drag", "move"),
)


@dataclass(frozen=True, slots=True)
class DockItem:
    key: str
    label: str
    icon: str
    action: str  # the action the button needs, used to disable it when nobody registered it


DOCK_ITEMS: tuple[DockItem, ...] = (
    DockItem("eyes", "Pause eyes", "pause", "pointer.pause"),
    DockItem("click_type", "Left click", "mouse-left", "dwell.click_type"),
    DockItem("dwell", "Dwell off", "timer", "dwell.toggle"),
    DockItem("scroll_up", "Scroll up", "chevrons-up", "mouse.scroll"),
    DockItem("scroll_down", "Scroll down", "chevrons-down", "mouse.scroll"),
    DockItem("keyboard", "Keyboard", "keyboard", "keyboard.toggle"),
    DockItem("numbers", "Numbers", "hash", "ui.labels.show"),
    DockItem("zoom", "Zoom", "zoom-in", "ui.zoom.show"),
    DockItem("menu", "Menu", "target", "ui.menu.show"),
    DockItem("voice", "Sleep voice", "moon", "speech.mode"),
    DockItem("help", "Help", "circle-question-mark", "ui.help.show"),
    DockItem("settings", "Settings", "settings", "ui.settings.show"),
    DockItem("calibrate", "Calibrate", "crosshair", "ui.calibrate"),
    DockItem("restart", "Restart tracking", "refresh-cw", "pointer.restart"),
)


class ClickTypeFlyout(OverlayWindow):
    """Pick what the next dwell click does.  Opens beside the dock, closes after a choice or 10 s."""

    def __init__(self, services: UiServices, dock: Dock) -> None:
        super().__init__(services, click_through=False, name="click-type")
        self._dock = dock
        t = services.theme
        self.buttons: dict[ClickType, GazeButton] = {}
        layout = QBoxLayout(QBoxLayout.Direction.TopToBottom, self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        for kind, label, icon in CLICK_TYPES:
            button = GazeButton(t, label, icon, layout="row", min_height=68, flat=True)
            button.clicked.connect(lambda _c=False, k=kind: self._choose(k))
            layout.addWidget(button)
            self.buttons[kind] = button
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def open_beside(self, anchor: QWidget) -> None:
        current = ClickType(self.svc.ctx.state.get("dwell_click"))
        for kind, button in self.buttons.items():
            button.set_on(kind == current)
        t = self.svc.theme
        width, height = t.px(200), len(self.buttons) * t.px(68) + 2
        top_left = anchor.mapToGlobal(anchor.rect().topLeft())
        dock_geo = self._dock.geometry()
        edge = self._dock.edge
        if edge == "left":
            x = dock_geo.right() + 1
        elif edge == "top":
            x = top_left.x()
        else:
            x = dock_geo.left() - width
        y = dock_geo.bottom() + 1 if edge == "top" else top_left.y()
        screen = self.svc.screens.map.screen_at_logical(x, y)
        bounds = screen.logical
        x = min(max(x, bounds.x), bounds.right - width)
        y = min(max(y, bounds.y), bounds.bottom - height)
        self.screen_geometry = screen
        self.setGeometry(round(x), round(y), width, height)
        self.show()
        self.raise_()
        self._timer.start(10_000)

    def _choose(self, kind: ClickType) -> None:
        self.hide()
        self._dock.dispatch("dwell.click_type", type=kind.value)

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        pal = self.svc.theme.palette
        p = QPainter(self)
        solid_panel(p, QRectF(0, 0, self.width(), self.height()), pal.surface, pal.ink)
        p.end()


class Dock(OverlayWindow):
    def __init__(self, services: UiServices, status: StatusModel) -> None:
        super().__init__(services, click_through=False, name="dock")
        self.setWindowTitle("Sightline dock")
        self.edge = "right"
        self.reserve_space = True
        self._appbar = winapi.AppBar(self)
        self._granted: Rect | None = None
        self._page = 0
        self._last_target: tuple[float, float, float] | None = None  # x, y, time
        t = services.theme
        self.status_view = StatusView(services, status, self)
        self.buttons: dict[str, GazeButton] = {}
        for item in DOCK_ITEMS:
            button = GazeButton(t, item.label, item.icon, self, layout="stack", min_height=64, flat=True)
            button.clicked.connect(lambda _c=False, key=item.key: self._pressed(key))
            self.buttons[item.key] = button
        self.more_button = GazeButton(t, "More", "ellipsis", self, layout="stack", min_height=64, flat=True)
        self.more_button.clicked.connect(self._next_page)
        self.flyout = ClickTypeFlyout(services, self)
        services.cursor.moved.connect(self._on_cursor)
        services.theme.changed.connect(self.reposition)
        services.screens.changed.connect(self.reposition)
        self.sync_state()

    # ------------------------------------------------------------------ public

    def dispatch(self, name: str, **args: object) -> None:
        self.svc.ctx.actions.dispatch(Action(name, args, source="ui"))

    def last_target(self) -> tuple[float, float] | None:
        """Where the pointer last rested outside the dock (within the last minute)."""
        if self._last_target is None:
            return None
        x, y, when = self._last_target
        return (x, y) if time.monotonic() - when < TARGET_MEMORY_S else None

    def open(self) -> None:
        self.refresh_enabled()
        self.reposition()
        self.show()

    def close_dock(self) -> None:
        self.flyout.hide()
        self.hide()
        self._appbar.release()
        self._granted = None

    def refresh_enabled(self) -> None:
        registry = self.svc.ctx.actions
        for item in DOCK_ITEMS:
            self.buttons[item.key].setEnabled(registry.has(item.action))

    def on_state(self, e: StateChanged) -> None:
        if e.field in ("pointer_paused", "dwell_enabled", "dwell_click", "speech_mode"):
            self.sync_state()

    def sync_state(self) -> None:
        st = self.svc.ctx.state.snapshot()
        eyes = self.buttons["eyes"]
        if st.pointer_paused:
            eyes.set_label("Resume eyes", "play")
        else:
            eyes.set_label("Pause eyes", "pause")
        eyes.set_on(bool(st.pointer_paused))
        kind = ClickType(st.dwell_click)
        label, icon = next(((lb, ic) for k, lb, ic in CLICK_TYPES if k == kind), ("Left click", "mouse-left"))
        self.buttons["click_type"].set_label(label, icon)
        dwell = self.buttons["dwell"]
        dwell.set_label("Dwell on" if st.dwell_enabled else "Dwell off", "timer")
        dwell.set_on(bool(st.dwell_enabled))
        voice = self.buttons["voice"]
        asleep = SpeechMode(st.speech_mode) == SpeechMode.SLEEP
        voice.set_label("Wake voice" if asleep else "Sleep voice", "mic" if asleep else "moon")
        voice.set_on(asleep)

    # ------------------------------------------------------------------ buttons

    def _pressed(self, key: str) -> None:
        st = self.svc.ctx.state.snapshot()
        if key == "eyes":
            self.dispatch("pointer.resume" if st.pointer_paused else "pointer.pause")
        elif key == "click_type":
            if self.flyout.isVisible():
                self.flyout.hide()
            else:
                self.flyout.open_beside(self.buttons["click_type"])
        elif key == "dwell":
            self.dispatch("dwell.toggle")
        elif key in ("scroll_up", "scroll_down"):
            args: dict[str, object] = {"direction": "up" if key == "scroll_up" else "down"}
            target = self.last_target()
            if target is not None:
                args["x"], args["y"] = target
            self.dispatch("mouse.scroll", **args)
        elif key == "keyboard":
            self.dispatch("keyboard.toggle")
        elif key == "numbers":
            self.dispatch("ui.labels.show")
        elif key in ("zoom", "menu"):
            # The lens or the action menu opens where the user was looking before they looked at the dock.
            target = self.last_target()
            self.dispatch("ui.zoom.show" if key == "zoom" else "ui.menu.show", **({"x": target[0], "y": target[1]} if target else {}))
        elif key == "voice":
            asleep = SpeechMode(st.speech_mode) == SpeechMode.SLEEP
            self.dispatch("speech.mode", mode=SpeechMode.AUTO.value if asleep else SpeechMode.SLEEP.value)
        elif key == "help":
            self.dispatch("ui.help.show")
        elif key == "settings":
            self.dispatch("ui.settings.show")
        elif key == "calibrate":
            self.dispatch("ui.calibrate", kind="gaze")
        elif key == "restart":
            self.dispatch("pointer.restart")

    def _next_page(self) -> None:
        self._page += 1
        self._layout_buttons()

    # ------------------------------------------------------------------ geometry

    def thickness(self) -> int:
        """Logical width of a side dock, or height of a top dock."""
        t = self.svc.theme
        return t.px(76) if self.edge == "top" else t.px(118)

    def reposition(self) -> None:
        smap = self.svc.screens.map
        screen = smap.primary
        thickness_phys = round(self.thickness() * screen.dpr)
        rect: Rect | None = None
        if self.reserve_space and self.isVisible():
            rect = self._appbar.reserve(self.edge, screen.native, thickness_phys)
        elif self._appbar.registered:
            self._appbar.release()
        if rect is None:
            work = screen.work_rect
            if self.edge == "left":
                rect = Rect(work.x, work.y, thickness_phys, work.h)
            elif self.edge == "top":
                rect = Rect(work.x, work.y, work.w, thickness_phys)
            else:
                rect = Rect(work.right - thickness_phys, work.y, thickness_phys, work.h)
        self._granted = rect
        self.place_physical(rect)
        self._layout_buttons()

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        winapi.square_corners(self)
        if self.reserve_space and not self._appbar.registered:
            QTimer.singleShot(0, self, self.reposition)

    def _content(self) -> tuple[int, int, int, int]:
        """The area inside the strong rule that faces the desktop: (x, y, w, h)."""
        line = self._rule_width()
        if self.edge == "right":
            return line, 0, self.width() - line, self.height()
        if self.edge == "left":
            return 0, 0, self.width() - line, self.height()
        return 0, 0, self.width(), self.height() - line

    def _rule_width(self) -> int:
        return max(1, self.svc.theme.px(1))

    def _layout_buttons(self) -> None:
        t = self.svc.theme
        vertical = self.edge != "top"
        header = self.status_view.sizeHint()
        min_cell = t.px(64)
        cx, cy, cw, ch = self._content()
        if vertical:
            self.status_view.setGeometry(cx, cy, cw, header.height())
            start, length = cy + header.height() + 1, ch - header.height() - 1
        else:
            self.status_view.setGeometry(cx, cy, header.width(), ch)
            start, length = cx + header.width() + 1, cw - header.width() - 1
        keys = [item.key for item in DOCK_ITEMS]
        fits = max(1, length // min_cell)
        if fits >= len(keys):
            pages = [keys]
        else:
            per_page = max(1, fits - 1)
            pages = [keys[i : i + per_page] for i in range(0, len(keys), per_page)]
        self._page %= len(pages)
        shown = pages[self._page]
        paged = len(pages) > 1
        slots = len(shown) + (1 if paged else 0)
        cell = min(t.px(96 if vertical else 120), max(min_cell, length // max(1, slots))) if slots else min_cell
        for button in self.buttons.values():
            button.hide()
        pos = start
        for key in shown:
            button = self.buttons[key]
            if vertical:
                button.setGeometry(cx, pos, cw, cell)
            else:
                button.setGeometry(pos, cy, cell, ch)
            button.show()
            pos += cell
        if paged:
            last_page = self._page == len(pages) - 1
            self.more_button.set_label("Back" if last_page else "More", "arrow-left" if last_page else "ellipsis")
            if vertical:
                self.more_button.setGeometry(cx, pos, cw, cell)
            else:
                self.more_button.setGeometry(pos, cy, cell, ch)
            self.more_button.show()
        else:
            self.more_button.hide()

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self._layout_buttons()

    def _on_cursor(self, x: float, y: float) -> None:
        if self._granted is not None and self._granted.contains(x, y):
            return
        if self.flyout.isVisible():
            lx, ly = self.svc.screens.map.to_logical(x, y)
            if self.flyout.geometry().contains(round(lx), round(ly)):
                return
        self._last_target = (x, y, time.monotonic())

    def nativeEvent(self, event_type, message):  # noqa: ANN001, ANN201 - Qt signature
        if winapi.native_windows() and bytes(event_type) == b"windows_generic_MSG":
            parsed = winapi.parse_native_message(message)
            if parsed and parsed[0] == winapi.APPBAR_CALLBACK and parsed[1] == winapi.ABN_POSCHANGED:
                QTimer.singleShot(0, self, self.reposition)
        return super().nativeEvent(event_type, message)

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        t = self.svc.theme
        pal = t.palette
        p = QPainter(self)
        p.fillRect(QRectF(0, 0, self.width(), self.height()), pal.surface)
        # A strong rule on the side that faces the desktop separates the dock from content.
        line = self._rule_width()
        if self.edge == "right":
            p.fillRect(QRectF(0, 0, line, self.height()), pal.ink)
        elif self.edge == "left":
            p.fillRect(QRectF(self.width() - line, 0, line, self.height()), pal.ink)
        else:
            p.fillRect(QRectF(0, self.height() - line, self.width(), line), pal.ink)
        header = self.status_view.geometry()
        if self.edge == "top":
            p.fillRect(QRectF(header.right() + 1, 0, 1, self.height()), pal.ink)
        else:
            p.fillRect(QRectF(header.left(), header.bottom() + 1, header.width(), 1), pal.ink)
        p.end()
