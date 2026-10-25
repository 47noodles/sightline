"""The 3x3 grid: say a number (or look at a cell and blink) to narrow in, then click.

The grid covers the monitor under the pointer.  Each step divides the
current cell into nine and moves the pointer to its centre, so the pointer
always shows where a click would land.  When cells get too small to divide
again, an eye click on one clicks its centre.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QRectF, Qt
from PySide6.QtGui import QFontMetricsF, QPainter, QPainterPath

from sightline.core.actions import ActionError
from sightline.core.types import Action, Rect

from ..geometry import GridState, grid_cell_at, grid_cells
from ..services import UiServices
from .base import OverlayWindow, solid_panel, two_tone_path

log = logging.getLogger(__name__)

MIN_CELL = 16.0  # logical pixels: below this a cell is clicked rather than divided again


class _GridWindow(OverlayWindow):
    def __init__(self, services: UiServices) -> None:
        super().__init__(services, name="grid")
        self.state: GridState | None = None

    def _local(self, rect: Rect) -> QRectF:
        r = self.svc.screens.map.rect_to_logical(rect, self.screen_geometry)
        return QRectF(r.x - self.x(), r.y - self.y(), r.w, r.h)

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        if self.state is None:
            return
        t = self.svc.theme
        pal = t.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        region = self._local(self.state.current)
        cells = [self._local(c) for c in grid_cells(self.state.current)]

        lines = QPainterPath()
        for i in (1, 2):
            x = region.x() + region.width() * i / 3.0
            y = region.y() + region.height() * i / 3.0
            lines.moveTo(x, region.top())
            lines.lineTo(x, region.bottom())
            lines.moveTo(region.left(), y)
            lines.lineTo(region.right(), y)
        two_tone_path(p, lines, pal.ink, pal.halo, t.pxf(1.5), t.pxf(1.0))
        border = QPainterPath()
        inset = t.pxf(2.5)
        border.addRect(region.adjusted(inset, inset, -inset, -inset))
        two_tone_path(p, border, pal.ink, pal.halo, t.pxf(3), t.pxf(1.25))

        cell_side = min(cells[0].width(), cells[0].height())
        size = max(14.0, min(26.0, cell_side / 3.2 / max(t.scale, 0.1)))
        font = t.font("grid", size=size)
        p.setFont(font)
        fm = QFontMetricsF(font)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        h = fm.height() + t.pxf(4)
        # Once cells are small the click point (the centre) must stay visible, so numbers move to a corner.
        in_corner = cell_side < 4.0 * h
        for number, cell in enumerate(cells, start=1):
            text = str(number)
            w = fm.horizontalAdvance(text) + t.pxf(14 if not in_corner else 8)
            if in_corner:
                inset = t.pxf(3)
                badge = QRectF(cell.left() + inset, cell.top() + inset, w, h)
            else:
                badge = QRectF(cell.center().x() - w / 2, cell.center().y() - h / 2, w, h)
            solid_panel(p, badge, pal.surface, pal.ink)
            p.setPen(pal.ink)
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)

        if self.state.depth == 0:
            hint = "Say a number, or look at a square and blink"
            hf = t.font("label")
            p.setFont(hf)
            hw = QFontMetricsF(hf).horizontalAdvance(hint) + t.pxf(28)
            hh = t.pxf(36)
            screen = self.screen_geometry
            bottom = self._local(screen.work_rect).bottom() if screen is not None else region.bottom()
            box = QRectF(region.center().x() - hw / 2, bottom - hh - t.pxf(20), hw, hh)
            solid_panel(p, box, pal.surface, pal.ink)
            p.setPen(pal.ink)
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, hint)
        p.end()


class GridOverlay(QObject):
    def __init__(self, services: UiServices) -> None:
        super().__init__()
        self.svc = services
        self.window = _GridWindow(services)
        self._state: GridState | None = None
        self._remove_interceptor = None
        services.overlays.register("grid", self._close)

    @property
    def state(self) -> GridState | None:
        return self._state

    # ------------------------------------------------------------------ actions (GUI thread)

    def show(self, action: Action | None = None) -> None:
        smap = self.svc.screens.map
        screen = smap.screen_at(*self.svc.here(action))  # the monitor the user was looking at
        self.svc.overlays.opened("grid")
        self._set_state(GridState.start(screen.native))
        self.window.place_physical(screen.native)
        self.window.show()
        if self._remove_interceptor is None:
            self._remove_interceptor = self.svc.ctx.actions.add_interceptor(self._intercept, priority=100, owner="ui.grid")

    def hide(self, _action: Action | None = None) -> None:
        self.svc.overlays.close("grid")

    def select(self, action: Action) -> None:
        state = self._require()
        try:
            cell = int(action.arg("cell", action.arg("number")))
        except (TypeError, ValueError):
            raise ActionError("Say a number from 1 to 9.") from None
        if not 1 <= cell <= 9:
            raise ActionError("Say a number from 1 to 9.")
        self._set_state(state.select(cell))
        self._move_pointer(action)

    def back(self, action: Action) -> None:
        state = self._require()
        self._set_state(state.back())
        if self._state is not None and self._state.depth > 0:
            self._move_pointer(action)

    def click(self, action: Action) -> None:
        state = self._require()
        x, y = state.center
        self.svc.overlays.close("grid")
        self.svc.ctx.actions.execute_now(
            Action(
                "mouse.click",
                {"button": action.arg("button", "left"), "count": int(action.arg("count", 1)), "x": x, "y": y, "snap": False},
                source=action.source,
                utterance=action.utterance,
            )
        )

    # ------------------------------------------------------------------ internals

    def _require(self) -> GridState:
        if self._state is None:
            raise ActionError("The grid is not showing.")
        return self._state

    def _set_state(self, state: GridState) -> None:
        self._state = state
        self.window.state = state
        self.window.update()

    def _move_pointer(self, action: Action) -> None:
        assert self._state is not None
        x, y = self._state.center
        if self.svc.ctx.actions.has("mouse.move"):
            self.svc.ctx.actions.execute_now(Action("mouse.move", {"x": x, "y": y}, source=action.source, utterance=action.utterance))

    def _close(self) -> None:
        if self._remove_interceptor is not None:
            self._remove_interceptor()
            self._remove_interceptor = None
        self._state = None
        self.window.state = None
        self.window.hide()
        self.svc.overlays.closed("grid")

    def _hide_if_open(self) -> None:
        if self._state is not None:
            self.svc.overlays.close("grid")

    def _intercept(self, action: Action) -> Action | None:
        """Action thread: eye clicks inside the grid pick a cell, a plain "click" clicks the current centre."""
        if action.name != "mouse.click":
            return action
        state = self._state
        if state is None:
            return action
        x, y = action.arg("x"), action.arg("y")
        extra = {"button": action.arg("button", "left"), "count": action.arg("count", 1)}
        if x is None or y is None:
            return Action("ui.grid.click", extra, source=action.source, utterance=action.utterance)
        cell = grid_cell_at(state.current, x, y)
        if cell is None:
            self.svc.bridge.post(self._hide_if_open)
            return action
        dpr = self.svc.screens.map.screen_at(x, y).dpr
        if state.can_divide(MIN_CELL * self.svc.theme.scale * dpr):
            return Action("ui.grid.select", {"cell": cell}, source=action.source, utterance=action.utterance)
        cx, cy = grid_cells(state.current)[cell - 1].center
        self.svc.bridge.post(self._hide_if_open)
        return action.with_args(x=cx, y=cy, snap=False)
