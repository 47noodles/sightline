"""The action menu at the aim point: double click, right click, drag, scroll, zoom, numbers or the keyboard, right there.

Concept review item 19.  The dock sits on a screen edge, where webcam gaze is least
accurate, and works as "choose the action, then the target": a trip to the edge and back
for every action that is not a left click.  The menu turns that round.  Look at the
target, say "menu" (or open it from the dock), then pick what to do there.

Eight large numbered tiles sit in a 3 by 3 block with the target showing through the
middle cell, marked with a crosshair.  Every tile is a solid thing to look at, with
nothing to cross through empty space: border-crossing in gaze pie menus was error prone
in the studies the review cites, and visual anchors fixed it.  A blink or dwell on a
tile, or ``ui.menu.select`` with ``number`` or ``item``, runs that action at the point
and closes the menu.  "Scroll here" starts scrolling and the menu becomes Stop, Up and
Down.  ``ui.menu.hide``, ``ui.cancel`` or 10 s without a choice closes it.  Near a screen
edge the block moves on screen and the open cell moves to wherever the target is, so a
tile never covers it.
"""

from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QPointF, QTimer
from PySide6.QtGui import QPainter, QPainterPath

from sightline.core.actions import ActionError
from sightline.core.events import ActionExecuted
from sightline.core.types import Action, Rect

from ..services import UiServices
from .base import OverlayWindow, two_tone_circle, two_tone_path
from .shared import (
    TILE_HEIGHT,
    USER_SOURCES,
    OverlaySlot,
    Teardown,
    TileWindow,
    gaze_tolerance,
    point_of,
    register_ui_actions,
    tile_at,
)

if TYPE_CHECKING:
    from sightline.core.context import AppContext

log = logging.getLogger(__name__)

NAME = "menu"
PRIORITY = 60  # after the zoom lens, numbers, grid and help; before the number chooser (50)
MENU_SECONDS = 10.0
MAX_SECONDS = 30.0  # the pointer resting on a tile keeps the menu up, but not for ever
RECHECK_MS = 1500
TILE_MIN_WIDTH = 176.0  # logical pixels before ui.scale
TILE_GAP = 12.0
# Anything that stops the desktop's continuous scrolling also retires the Stop tile.
SCROLL_STOPPERS = frozenset({"mouse.scroll_stop", "key.release_all", "mouse.click", "mouse.scroll", "mouse.drag_toggle", "mouse.down"})


@dataclass(frozen=True, slots=True)
class MenuItem:
    key: str
    label: str
    icon: str
    action: str  # the action it needs: the tile is disabled while nobody has registered it
    words: tuple[str, ...] = ()  # what ``ui.menu.select item=`` accepts besides the key


MAIN_ITEMS: tuple[MenuItem, ...] = (
    MenuItem("double", "Double click", "mouse-pointer-click", "mouse.click", ("double click",)),
    MenuItem("right", "Right click", "mouse-right", "mouse.click", ("right click", "context menu")),
    MenuItem("middle", "Middle click", "mouse-pointer-2", "mouse.click", ("middle click",)),
    MenuItem("drag", "Drag from here", "move", "mouse.drag_toggle", ("drag from here", "drag here", "drop", "drop here", "pick up")),
    MenuItem("scroll", "Scroll here", "chevrons-down", "mouse.scroll_start", ("scroll here", "scroll down", "keep scrolling")),
    MenuItem("zoom", "Zoom here", "zoom-in", "ui.zoom.show", ("zoom here", "zoom in", "magnify")),
    MenuItem("numbers", "Show numbers", "hash", "ui.labels.show", ("numbers here", "show numbers")),
    MenuItem("keyboard", "Keyboard", "keyboard", "keyboard.show", ("show keyboard", "on screen keyboard")),
)

SCROLL_ITEMS: tuple[MenuItem, ...] = (
    MenuItem("stop", "Stop scrolling", "pause", "mouse.scroll_stop", ("stop scrolling",)),
    MenuItem("up", "Scroll up", "chevrons-up", "mouse.scroll_start", ("scroll up",)),
    MenuItem("down", "Scroll down", "chevrons-down", "mouse.scroll_start", ("scroll down",)),
)


def find_item(items: tuple[MenuItem, ...], name: str) -> MenuItem | None:
    """The item a spoken or written name means ("right click", "Drag from here", "zoom")."""
    words = " ".join(str(name).lower().replace("-", " ").replace("_", " ").split())
    words = words.removeprefix("the ").removeprefix("a ")
    for item in items:
        if words == item.key or words == item.label.lower() or words in item.words:
            return item
    return None


# =========================================================================== layout (pure, physical pixels)


@dataclass(frozen=True, slots=True)
class Lattice:
    tiles: tuple[Rect, ...]  # in item order
    hole: Rect  # the cell left open over the point
    rows: tuple[tuple[Rect, ...], ...]  # all nine cells by row, for the scroll controls
    hole_row: int


def lattice(x: float, y: float, tile_w: float, tile_h: float, gap: float, work: Rect, count: int = 8) -> Lattice:
    """Up to eight tiles in a 3 by 3 block round a point, with the point's cell left open.

    The block stays inside ``work``.  Near an edge it cannot be centred on the point, so the
    open cell is whichever cell the point falls in, and the tiles fill the other eight in
    reading order: the target is never under a tile, and the numbers never change.
    """
    block_w = 3 * tile_w + 2 * gap
    block_h = 3 * tile_h + 2 * gap
    left = min(max(x - block_w / 2.0, work.left), work.right - block_w) if block_w <= work.w else work.left
    top = min(max(y - block_h / 2.0, work.top), work.bottom - block_h) if block_h <= work.h else work.top
    cells = [[Rect(left + c * (tile_w + gap), top + r * (tile_h + gap), tile_w, tile_h) for c in range(3)] for r in range(3)]
    col = min(range(3), key=lambda c: abs(x - cells[0][c].center[0]))
    row = min(range(3), key=lambda r: abs(y - cells[r][0].center[1]))
    tiles = [cells[r][c] for r in range(3) for c in range(3) if (r, c) != (row, col)]
    return Lattice(tuple(tiles[:count]), cells[row][col], tuple(tuple(cells_row) for cells_row in cells), row)


def scroll_row(grid: Lattice) -> tuple[Rect, ...]:
    """Where Stop, Up and Down go: the row below the target, or above it when the target is in the bottom row."""
    return grid.rows[2] if grid.hole_row != 2 else grid.rows[0]


# =========================================================================== windows


class _AnchorMark(OverlayWindow):
    """Click-through crosshair on the point the menu acts on, so "here" is never in doubt."""

    SIZE = 44.0

    def __init__(self, services: UiServices) -> None:
        super().__init__(services, name="action-menu-mark")

    def show_at(self, x: float, y: float) -> None:
        side = self.svc.theme.pxf(self.SIZE)
        self.center_on_physical(x, y, side, side)
        self.show()
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: ANN001 - Qt signature
        t = self.svc.theme
        pal = t.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.width() / 2.0, self.height() / 2.0)
        two_tone_circle(p, c, t.pxf(9), pal.ink, pal.halo, t.pxf(2.5), t.pxf(1.5))
        gap, arm = t.pxf(12), t.pxf(7)
        cross = QPainterPath()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            cross.moveTo(c.x() + dx * gap, c.y() + dy * gap)
            cross.lineTo(c.x() + dx * (gap + arm), c.y() + dy * (gap + arm))
        two_tone_path(p, cross, pal.ink, pal.halo, t.pxf(2.5), t.pxf(1.5))
        p.end()


@dataclass(frozen=True, slots=True)
class _Live:
    """What the action thread may read while the menu is up.  Replaced whole, never changed."""

    token: int
    tiles: tuple[tuple[int, Rect], ...]  # (number, physical rectangle)


# =========================================================================== the menu


class ActionMenu(QObject):
    def __init__(self, services: UiServices) -> None:
        super().__init__()
        self.svc = services
        self.seconds = MENU_SECONDS
        self.slot = OverlaySlot(services, NAME, self._close)
        self.windows = [TileWindow(services, "action-menu-tile") for _ in MAIN_ITEMS]
        for i, window in enumerate(self.windows):
            window.tile.clicked.connect(lambda _checked=False, i=i: self._tile_clicked(i))
        self.mark = _AnchorMark(services)
        self._tokens = itertools.count(1)
        self._token = 0
        self._mode: str | None = None  # "main", "scroll" or None when closed
        self._items: tuple[MenuItem, ...] = ()
        self._enabled: tuple[bool, ...] = ()
        self._anchor = (0.0, 0.0)
        self._aim_t: float | None = None
        self._grid: Lattice | None = None
        self._live: _Live | None = None
        self._armed: int | None = None
        self._scrolling: str | None = None  # the direction while the menu's scrolling runs
        self._opened_at = 0.0
        self._alive = True
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._expire)
        services.cursor.moved.connect(self._on_cursor)
        services.theme.changed.connect(self._relayout)
        services.screens.changed.connect(self.close)
        self._unwatch = services.bridge.on(ActionExecuted, self._on_executed)

    # ------------------------------------------------------------------ for tests and tools

    @property
    def is_open(self) -> bool:
        return self._mode is not None

    @property
    def mode(self) -> str | None:
        return self._mode

    @property
    def anchor(self) -> tuple[float, float]:
        return self._anchor

    @property
    def items(self) -> tuple[MenuItem, ...]:
        return self._items

    @property
    def grid(self) -> Lattice | None:
        return self._grid

    @property
    def scrolling(self) -> str | None:
        return self._scrolling

    def visible_tiles(self) -> list[TileWindow]:
        return [w for w in self.windows if w.isVisible()]

    # ------------------------------------------------------------------ actions (GUI thread)

    def show(self, action: Action | None = None) -> None:
        x, y = self.svc.here(action)
        self._aim_t = action.aim_t if action is not None and action.aim_t is not None else time.perf_counter()
        if self._scrolling is not None:
            self._scrolling = None
            self._dispatch("mouse.scroll_stop")
        self._open("main", float(x), float(y))
        log.info("Menu at (%d, %d)", x, y)

    def hide(self, _action: Action | None = None) -> None:
        self.close()

    def select(self, action: Action) -> None:
        number, name = action.arg("number"), action.arg("item")
        if number is None and name and self._mode != "scroll" and self._scroll_word(str(name)):
            # "Stop scrolling", "up" or "down" once the scroll controls have gone (scrolling stopped some
            # other way), or before "Scroll here": the scrolling they would steer is not running, so
            # there is nothing to do and nothing worth a message.
            log.info("Menu: %r ignored outside scroll mode", name)
            return
        if self._mode is None:
            raise ActionError("The menu is not open.")
        token = action.arg("token")
        if token is not None and int(token) != self._token:
            raise ActionError("That menu has gone. Please open it again.")
        if number is not None:
            try:
                index = int(number) - 1
            except (TypeError, ValueError):
                raise ActionError("Say the number you want, like two.") from None
            if not 0 <= index < len(self._items):
                raise ActionError(f"There is no number {index + 1} in the menu.")
        elif name:
            item = find_item(self._items, str(name))
            if item is None:
                raise ActionError(f"The menu has nothing called {name}.")
            index = self._items.index(item)
        else:
            raise ActionError("Say a number or the name of a menu item.")
        if not self._enabled[index]:
            raise ActionError(f"{self._items[index].label} is not available right now.")
        self._run(self._items[index])

    def close(self, _action: Action | None = None) -> None:
        if self._mode is not None or self.slot.is_current:
            self._close()

    @staticmethod
    def _scroll_word(name: str) -> bool:
        """A name only the scroll controls answer to ("stop scrolling", "scroll up", "up", "down")."""
        return find_item(SCROLL_ITEMS, name) is not None and find_item(MAIN_ITEMS, name) is None

    # ------------------------------------------------------------------ the interceptor (action thread)

    def intercept(self, action: Action) -> Action | None:
        """A blink or dwell on a tile picks it; any other click goes through and closes the menu."""
        live = self._live
        if live is None or action.name != "mouse.click" or action.source not in USER_SOURCES:
            return action
        point = point_of(self.svc, action)
        if point is None:
            return action
        x, y = point
        number = tile_at(live.tiles, x, y, gaze_tolerance(self.svc, x, y))
        if number is not None:
            return Action("ui.menu.select", {"number": number, "token": live.token}, source=action.source, utterance=action.utterance)
        self.svc.bridge.post(lambda token=live.token: self._close_token(token))
        return action

    # ------------------------------------------------------------------ showing (GUI thread)

    def _open(self, mode: str, x: float, y: float) -> None:
        if not self._alive:
            return
        self._mode = mode
        self._anchor = (x, y)
        self._items = MAIN_ITEMS if mode == "main" else SCROLL_ITEMS
        self._enabled = tuple(self.svc.ctx.actions.has(item.action) for item in self._items)
        self._token = next(self._tokens)
        self.slot.opened()  # closes the numbers, grid, zoom, help or chooser if one is up
        self._present()
        self._opened_at = time.monotonic()
        if mode == "main":
            self._timer.start(int(self.seconds * 1000))
        else:
            self._timer.stop()  # the Stop tile stays while the page scrolls

    def _present(self) -> None:
        if self._mode is None:
            return
        t = self.svc.theme
        x, y = self._anchor
        screen = self.svc.screens.map.screen_at(x, y)
        dpr = screen.dpr
        dragging = bool(self.svc.ctx.state.get("drag_active"))
        labels = ["Drop here" if item.key == "drag" and dragging else item.label for item in self._items]
        width = TILE_MIN_WIDTH * t.scale
        for item, label, window in zip(self._items, labels, self.windows):
            window.tile.set_content("", label, item.icon)
            width = max(width, window.tile.natural_width())
        grid = lattice(x, y, width * dpr, t.pxf(TILE_HEIGHT) * dpr, t.pxf(TILE_GAP) * dpr, screen.work_rect, len(MAIN_ITEMS))
        self._grid = grid
        rects = grid.tiles if self._mode == "main" else scroll_row(grid)
        for number, (item, label, window, rect, enabled) in enumerate(zip(self._items, labels, self.windows, rects, self._enabled), start=1):
            window.tile.set_content(str(number), label, item.icon)
            window.tile.setEnabled(enabled)
            window.tile.set_armed(False)
            window.tile.set_on(self._mode == "scroll" and item.key == self._scrolling)
            window.place(rect)
        for window in self.windows[len(self._items) :]:
            window.hide()
        self._armed = None
        self._live = _Live(self._token, tuple((number, rect) for number, rect in enumerate(rects[: len(self._items)], start=1)))
        for window in self.windows[: len(self._items)]:
            window.show()
            window.raise_()
            window.update()
        if self._mode == "main":
            self.mark.show_at(x, y)
        else:
            self.mark.hide()  # the page moves under it while scrolling
        self._on_cursor(*self.svc.cursor.pos)

    def _relayout(self) -> None:
        if self._mode is not None:
            self._present()

    def _close(self) -> None:
        """Hide everything.  Also OverlayState's closer, so it ends by calling ``slot.closed``."""
        self._timer.stop()
        stop = self._scrolling is not None
        self._scrolling = None
        self._mode = None
        self._live = None
        self._items = ()
        self._armed = None
        for window in self.windows:
            window.tile.set_armed(False)
            window.hide()
        self.mark.hide()
        self.slot.closed()
        if stop:
            self._dispatch("mouse.scroll_stop")  # closing the scroll controls stops the scrolling they started

    def _close_token(self, token: int) -> None:
        if self._mode is not None and token == self._token:
            self.close()

    def _expire(self) -> None:
        if self._mode != "main":
            return
        if self._pointer_on_tiles() and time.monotonic() - self._opened_at < MAX_SECONDS:
            self._timer.start(RECHECK_MS)
            return
        log.info("Menu: closed after %.0f s without a choice", self.seconds)
        self.close()

    def _pointer_on_tiles(self) -> bool:
        live = self._live
        if live is None:
            return False
        x, y = self.svc.cursor.pos
        return tile_at(live.tiles, x, y, 2.4 * gaze_tolerance(self.svc, x, y)) is not None

    # ------------------------------------------------------------------ running an item

    def _tile_clicked(self, index: int) -> None:
        if self._mode is not None and index < len(self._items) and self._enabled[index]:
            self._run(self._items[index])

    def _dispatch(self, name: str, **args: object) -> None:
        # Source "ui": the user picked a tile; the click it makes is not a gaze guess for the chooser to doubt.
        self.svc.ctx.actions.dispatch(Action(name, args, source="ui", aim_t=self._aim_t))

    def _run(self, item: MenuItem) -> None:
        x, y = self._anchor
        key = item.key
        log.info("Menu: %s", item.label)
        if key == "scroll":
            self._dispatch("mouse.move", x=x, y=y)  # the wheel scrolls what is under the pointer
            self._dispatch("mouse.scroll_start", direction="down", x=x, y=y)
            self._scrolling = "down"
            self._open("scroll", x, y)
            return
        if key in ("up", "down"):
            self._dispatch("mouse.scroll_start", direction=key, x=x, y=y)
            self._scrolling = key
            for window, menu_item in zip(self.windows, self._items):
                window.tile.set_on(menu_item.key == key)
            return
        if key == "stop":
            self._scrolling = None
            self.close()
            self._dispatch("mouse.scroll_stop")
            return
        self.close()
        if key == "double":
            self._dispatch("mouse.click", button="left", count=2, x=x, y=y)
        elif key == "right":
            self._dispatch("mouse.click", button="right", count=1, x=x, y=y)
        elif key == "middle":
            self._dispatch("mouse.click", button="middle", count=1, x=x, y=y)
        elif key == "drag":
            self._dispatch("mouse.drag_toggle", x=x, y=y)
        elif key == "zoom":
            self._dispatch("ui.zoom.show", x=x, y=y)
        elif key == "numbers":
            self._dispatch("ui.labels.show")
        elif key == "keyboard":
            self._dispatch("keyboard.show")

    # ------------------------------------------------------------------ pointer and events

    def _on_cursor(self, x: float, y: float) -> None:
        live = self._live
        if live is None:
            return
        number = tile_at(live.tiles, x, y, gaze_tolerance(self.svc, x, y))
        if number == self._armed:
            return
        self._armed = number
        for i, window in enumerate(self.windows[: len(self._items)]):
            window.tile.set_armed(number == i + 1 and window.tile.isEnabled())
        if number is not None and self._mode == "main":
            self._timer.start(int(self.seconds * 1000))  # looking over the tiles is not idle

    def _on_executed(self, e: ActionExecuted) -> None:
        if self._mode != "scroll":
            return
        name = e.action.name
        if (e.ok and name in SCROLL_STOPPERS) or (name == "mouse.scroll_start" and not e.ok):
            self._scrolling = None  # it has stopped already (or never started): nothing to stop
            self.close()

    # ------------------------------------------------------------------ uninstall

    def shutdown(self) -> None:
        self._alive = False
        self.close()
        self.slot.release()
        self._unwatch()
        for signal, slot in (
            (self.svc.cursor.moved, self._on_cursor),
            (self.svc.theme.changed, self._relayout),
            (self.svc.screens.changed, self.close),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        for window in [*self.windows, self.mark]:
            window.hide()
            window.deleteLater()


def install(ctx: AppContext, services: UiServices) -> Callable[[], None]:
    """Register ``ui.menu.*`` and the tile interceptor.  Returns a function that undoes it all."""
    menu = ActionMenu(services)
    teardown = Teardown()
    teardown.overlay = menu
    names = register_ui_actions(
        services,
        [
            ("ui.menu.show", menu.show, "Open the action menu where you are looking"),
            ("ui.menu.hide", menu.hide, "Close the action menu"),
            ("ui.menu.select", menu.select, "Pick an item from the action menu"),
        ],
    )
    teardown.add(lambda: [ctx.actions.unregister(name) for name in names])
    teardown.add(menu.shutdown)
    teardown.add(ctx.actions.add_interceptor(menu.intercept, priority=PRIORITY, owner="ui.menu"))
    return teardown


__all__ = ["MAIN_ITEMS", "SCROLL_ITEMS", "ActionMenu", "Lattice", "MenuItem", "find_item", "install", "lattice", "scroll_row"]
