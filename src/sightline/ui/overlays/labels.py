"""Numbered labels over everything clickable ("show numbers", then "click 12" or look at 12 and blink).

The clickable elements come from UI Automation, which can take a second,
so they are fetched and laid out on a worker thread; the GUI thread only
draws.  While the numbers are up an interceptor turns an eye click (or a
plain "click") on a badge into ``ui.labels.select`` for that badge.  Any
other click goes through and closes the numbers.
"""

from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import QObject, QRectF, Qt
from PySide6.QtGui import QFontMetricsF, QPainter

from sightline.core.actions import ActionError
from sightline.core.types import Action, Rect

from ..coords import ScreenGeometry
from ..geometry import PlacedLabel, label_at, layout_labels
from ..services import UiServices
from .base import OverlayWindow, solid_panel

log = logging.getLogger(__name__)

GAZE_TOLERANCE = 10.0  # logical pixels around a badge that still count as "on" it


class _LabelsWindow(OverlayWindow):
    """Covers one screen and draws that screen's badges."""

    def __init__(self, services: UiServices, screen: ScreenGeometry) -> None:
        super().__init__(services, name="labels")
        self.screen_geometry = screen
        self.labels: list[PlacedLabel] = []
        self.hover: int | None = None

    def set_labels(self, screen: ScreenGeometry, labels: list[PlacedLabel]) -> None:
        self.labels = labels
        self.place_physical(screen.native)
        self.screen_geometry = screen
        self.update()

    def local_rect(self, label: PlacedLabel) -> QRectF:
        smap = self.svc.screens.map
        r = smap.rect_to_logical(label.badge, self.screen_geometry)
        return QRectF(r.x - self.x(), r.y - self.y(), r.w, r.h)

    def paintEvent(self, event) -> None:  # noqa: ANN001
        t = self.svc.theme
        pal = t.palette
        p = QPainter(self)
        p.setFont(t.font("badge"))
        dirty = QRectF(event.rect())
        for label in self.labels:
            rect = self.local_rect(label)
            if not rect.intersects(dirty):
                continue
            hot = label.number == self.hover
            if hot:
                solid_panel(p, rect.adjusted(-1, -1, 1, 1), pal.accent, pal.on_accent)
                p.setPen(pal.on_accent)
            else:
                solid_panel(p, rect, pal.surface, pal.ink)
                p.setPen(pal.ink)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(label.number))
        p.end()


class LabelsOverlay(QObject):
    def __init__(self, services: UiServices) -> None:
        super().__init__()
        self.svc = services
        self.limit = 150
        self._windows: dict[str, _LabelsWindow] = {}
        self._labels: tuple[PlacedLabel, ...] = ()
        self._request = 0
        self._remove_interceptor = None
        self._hover: int | None = None
        services.overlays.register("labels", self._close)
        services.cursor.moved.connect(self._on_cursor)

    @property
    def labels(self) -> tuple[PlacedLabel, ...]:
        return self._labels

    @property
    def is_open(self) -> bool:
        return bool(self._labels)

    @property
    def windows(self) -> list[_LabelsWindow]:
        return [w for w in self._windows.values() if w.isVisible()]

    # ------------------------------------------------------------------ actions (GUI thread)

    def show(self, _action: Action | None = None) -> None:
        desktop = self.svc.ctx.desktop
        if desktop is None:
            raise ActionError("Numbers need the desktop service, which is not running.")
        self._request += 1
        request = self._request
        smap = self.svc.screens.map
        sizes = self._badge_sizes(smap.screens)
        monitors = [s.native for s in smap.screens]
        uia = desktop.uia
        limit = self.limit

        def badge_size(number: int, monitor: Rect) -> tuple[float, float]:
            digits = min(3, len(str(number)))
            return sizes.get((monitor.x, monitor.y), sizes["default"])[digits]

        def work() -> None:
            try:
                elements = uia.clickables(timeout=1.5)
                labels = layout_labels([e.rect for e in elements], monitors, badge_size, limit=limit, pause=lambda: time.sleep(0))
            except Exception:  # UIA can fail on odd windows; tell the user instead of hanging
                log.exception("Could not collect clickable elements")
                self.svc.bridge.post(lambda: self._failed(request))
                return
            self.svc.bridge.post(lambda: self._present(request, labels, len(elements)))

        threading.Thread(target=work, name="sightline-labels", daemon=True).start()

    def hide(self, _action: Action | None = None) -> None:
        self._request += 1  # drops a fetch still in flight
        self.svc.overlays.close("labels")

    def select(self, action: Action) -> None:
        try:
            number = int(action.arg("number"))
        except (TypeError, ValueError):
            raise ActionError("Say the number you want, like click 12.") from None
        label = next((lb for lb in self._labels if lb.number == number), None)
        if label is None:
            if not self._labels:
                raise ActionError("The numbers are not showing.")
            raise ActionError(f"There is no number {number}.")
        x, y = label.point
        self.svc.overlays.close("labels")
        self.svc.ctx.actions.execute_now(
            Action(
                "mouse.click",
                {"button": action.arg("button", "left"), "count": int(action.arg("count", 1)), "x": x, "y": y, "snap": False},
                source=action.source,
                utterance=action.utterance,
            )
        )

    # ------------------------------------------------------------------ internals

    def _badge_sizes(self, screens: tuple[ScreenGeometry, ...]) -> dict:
        """Badge sizes in physical pixels for 1, 2 and 3 digits, per screen (fonts are per DPI)."""
        t = self.svc.theme
        fm = QFontMetricsF(t.font("badge"))
        digit = max(fm.horizontalAdvance(str(d)) for d in range(10))
        pad_x, h = t.pxf(5), fm.height() + t.pxf(2)
        out: dict = {}
        for s in screens:
            out[(s.native.x, s.native.y)] = {n: ((digit * n + 2 * pad_x) * s.dpr, h * s.dpr) for n in (1, 2, 3)}
        out["default"] = out[(screens[0].native.x, screens[0].native.y)]
        return out

    def _failed(self, request: int) -> None:
        if request != self._request:
            return
        self.svc.notify("Could not find anything to number here.", "warn", key="labels")

    def _present(self, request: int, labels: list[PlacedLabel], found: int) -> None:
        if request != self._request:
            return
        if not labels:
            self.svc.notify("Nothing to number here.", "info", key="labels")
            return
        self.svc.overlays.opened("labels")
        self._labels = tuple(labels)
        self._hover = None
        smap = self.svc.screens.map
        by_screen: dict[str, tuple[ScreenGeometry, list[PlacedLabel]]] = {}
        for label in labels:
            screen = smap.screen_at(*label.badge.center)
            by_screen.setdefault(screen.name, (screen, []))[1].append(label)
        for name, window in self._windows.items():
            if name not in by_screen:
                window.hide()
        for name, (screen, items) in by_screen.items():
            window = self._windows.get(name)
            if window is None:
                window = self._windows[name] = _LabelsWindow(self.svc, screen)
            window.hover = None
            window.set_labels(screen, items)
            window.show()
        if self._remove_interceptor is None:
            self._remove_interceptor = self.svc.ctx.actions.add_interceptor(self._intercept, priority=100, owner="ui.labels")
        if found > len(labels) and len(labels) >= self.limit:
            self.svc.notify(f"Showing the first {len(labels)} of {found}.", "info", key="labels")

    def _close(self) -> None:
        """The OverlayState closer: hide everything and stop intercepting."""
        self._request += 1
        if self._remove_interceptor is not None:
            self._remove_interceptor()
            self._remove_interceptor = None
        self._labels = ()
        self._hover = None
        for window in self._windows.values():
            window.hide()
            window.labels = []
        self.svc.overlays.closed("labels")

    def _hide_if_open(self) -> None:
        if self._labels:
            self.svc.overlays.close("labels")

    def _tolerance(self, x: float, y: float) -> float:
        return GAZE_TOLERANCE * self.svc.theme.scale * self.svc.screens.map.screen_at(x, y).dpr

    def _intercept(self, action: Action) -> Action | None:
        """Runs on the action thread: read only immutable snapshots, never block."""
        if action.name != "mouse.click":
            return action
        labels = self._labels
        if not labels:
            return action
        point = self.svc.aim(action)  # a spoken "click" aims where the user looked as they began to speak
        if point is None:
            desktop = self.svc.ctx.desktop
            point = desktop.input.cursor_pos() if desktop is not None else None
        hit = label_at(labels, point[0], point[1], self._tolerance(*point)) if point is not None else None
        if hit is None:
            self.svc.bridge.post(self._hide_if_open)
            return action
        return Action(
            "ui.labels.select",
            {"number": hit.number, "button": action.arg("button", "left"), "count": action.arg("count", 1)},
            source=action.source,
            utterance=action.utterance,
        )

    def _on_cursor(self, x: float, y: float) -> None:
        if not self._labels:
            return
        hit = label_at(self._labels, x, y, self._tolerance(x, y))
        number = hit.number if hit else None
        if number == self._hover:
            return
        previous, self._hover = self._hover, number
        for window in self._windows.values():
            if not window.isVisible():
                continue
            window.hover = number
            for label in window.labels:
                if label.number in (previous, number):
                    window.update(window.local_rect(label).adjusted(-3, -3, 3, 3).toAlignedRect())
