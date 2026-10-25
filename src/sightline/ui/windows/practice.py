"""The practice room: big targets to learn on, a tapping test that measures, and a summary for a therapist.

* "Hit the targets": twelve large targets one at a time, scoring hits,
  misses (clicks outside) and the time to each hit.
* "Tapping test": ISO 9241-9 style multidirectional selection at three
  target sizes; throughput in bits per second and error rate.
* "Summary": those numbers and unwanted actions per hour (undone or
  cancelled within 3 s), saved as a dated HTML page in the profile folder.

The targets are painted in one widget, not made of buttons, so the
desktop's snapping does not reach them: the test measures pointing itself.
The pointer ring and our own hover highlight stay clearly visible.
"""

from __future__ import annotations

import logging
import math
import random
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget

from sightline.core import paths

from .. import measures
from ..overlays.base import pen
from ..services import UiServices
from ..widgets import GazeButton, RuledTable, Rule, SightlineWindow, TableRow, text
from .targets import monitor_mm_per_px

log = logging.getLogger(__name__)

TARGET_COUNT = 12
TARGET_DIAMETERS = (160.0, 120.0, 90.0)  # logical pixels before ui.scale: large on purpose
TARGET_TIMEOUT_S = 20.0
TAPPING_DIAMETERS = (120.0, 80.0, 48.0)
TAPPING_TARGETS = 11
TAPPING_TIMEOUT_S = 120.0


class PracticeCanvas(QWidget):
    """Paints the targets; a left click anywhere is a selection at that point."""

    selected = Signal(QPointF)

    def __init__(self, services: UiServices) -> None:
        super().__init__()
        self.svc = services
        self.targets: list[tuple[QPointF, float]] = []  # (centre, radius), logical
        self.current: int | None = None
        self.message = ""
        self.hover: QPointF | None = None
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("Practice area")

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(event.position())

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        self.hover = event.position()
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: ANN001
        self.hover = None
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        t = self.svc.theme
        pal = t.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), pal.ground)
        for i, (c, r) in enumerate(self.targets):
            if i == self.current:
                hot = self.hover is not None and math.hypot(self.hover.x() - c.x(), self.hover.y() - c.y()) <= r
                p.setPen(pen(pal.ink, t.pxf(3)))
                p.setBrush(pal.accent if hot else pal.ink)
                p.drawEllipse(c, r, r)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(pal.on_accent if hot else pal.ground)
                p.drawEllipse(c, t.pxf(4), t.pxf(4))
            else:
                p.setPen(pen(pal.rule_strong, 1.5))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(c, r, r)
        if self.message:
            p.setFont(t.font("title"))
            p.setPen(pal.ink)
            p.drawText(QRectF(0, 0, self.width(), self.height()), int(Qt.AlignmentFlag.AlignCenter), self.message)
        p.end()


class PracticeWindow(SightlineWindow):
    def __init__(self, services: UiServices, false_activations: measures.FalseActivations) -> None:
        super().__init__(services.theme, "Sightline practice")
        self.svc = services
        self.false_activations = false_activations
        t = services.theme
        self.rng = random.Random()
        self.score = measures.TargetScore()
        self.tapping: list[measures.TappingResult] = []
        self.mode = "menu"
        self.saved_path: Path | None = None

        root = QVBoxLayout(self)
        m = t.px(24)
        root.setContentsMargins(m, t.px(16), m, t.px(16))
        root.setSpacing(t.px(10))
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        # --- menu
        menu = QWidget()
        mbox = QVBoxLayout(menu)
        mbox.setSpacing(t.px(14))
        mbox.addWidget(text("Practice", "display"))
        mbox.addWidget(text("Big targets first, then a short test that measures how quickly and accurately you point. Nothing here clicks anything else.", "body"))
        for label, icon, slot, note in (
            ("Hit the targets", "target", self.start_targets, "Twelve large targets, one at a time. About a minute."),
            ("Tapping test", "crosshair", self.start_tapping, "Targets in a circle at three sizes. About three minutes."),
            ("Summary", "info", self.show_summary, "The numbers so far, and a page to save for a therapist."),
        ):
            line = QHBoxLayout()
            b = GazeButton(t, label, icon, min_height=72)
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            b.setMinimumWidth(t.px(300))
            b.clicked.connect(slot)
            line.addWidget(b)
            note_label = text(note, "body")
            note_label.setProperty("role", "secondary")
            line.addWidget(note_label, 1)
            mbox.addLayout(line)
        mbox.addStretch(1)
        close = GazeButton(t, "Close", "x", min_height=64)
        close.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        close.setMinimumWidth(t.px(190))
        close.clicked.connect(self.close)
        mbox.addWidget(close)
        self.menu_page = menu
        self.stack.addWidget(menu)

        # --- running
        run = QWidget()
        rbox = QVBoxLayout(run)
        rbox.setContentsMargins(0, 0, 0, 0)
        rbox.setSpacing(t.px(8))
        top = QHBoxLayout()
        self.status = text("", "heading", wrap=False)
        top.addWidget(self.status, 1)
        self.stop_button = GazeButton(t, "Stop", "x", min_height=64)
        self.stop_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.stop_button.setMinimumWidth(t.px(160))
        self.stop_button.clicked.connect(self.stop_run)
        top.addWidget(self.stop_button)
        rbox.addLayout(top)
        rbox.addWidget(Rule(t))
        self.canvas = PracticeCanvas(services)
        self.canvas.selected.connect(self._selected)
        rbox.addWidget(self.canvas, 1)
        self.run_page = run
        self.stack.addWidget(run)

        # --- summary
        summary = QWidget()
        sbox = QVBoxLayout(summary)
        sbox.setSpacing(t.px(12))
        sbox.addWidget(text("Summary", "display"))
        self.table = RuledTable(t, ("", ""), (1.2, 2.0))
        sbox.addWidget(self.table)
        self.saved_label = text("", "body")
        self.saved_label.setProperty("role", "secondary")
        sbox.addWidget(self.saved_label)
        sbox.addStretch(1)
        buttons = QHBoxLayout()
        self.save_button = GazeButton(t, "Save for a therapist", "check", min_height=64)
        self.save_button.clicked.connect(self.save_summary)
        back = GazeButton(t, "Back", "arrow-left", min_height=64)
        back.clicked.connect(self.show_menu)
        for b in (back, self.save_button):
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            b.setMinimumWidth(t.px(220))
        buttons.addWidget(back)
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        sbox.addLayout(buttons)
        self.summary_page = summary
        self.stack.addWidget(summary)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._timed_out)
        self._shown_at = 0.0
        self._last_select = 0.0
        # tapping state
        self._condition = 0
        self._sequence: list[int] = []
        self._step = 0
        self._trials: list[measures.Trial] = []
        self._diameter = 0.0
        self._amplitude = 0.0

    # ------------------------------------------------------------------ opening

    def open_on_screen(self) -> None:
        smap = self.svc.screens.map
        screen = smap.screen_at(*self.svc.cursor.pos)
        work = smap.rect_to_logical(screen.work_rect, screen)
        if not self.isVisible():
            w, h = work.w * 0.92, work.h * 0.92
            self.setGeometry(round(work.x + (work.w - w) / 2), round(work.y + (work.h - h) / 2), round(w), round(h))
        self.show_menu()
        self.show()
        self.raise_()
        self.activateWindow()

    def show_menu(self) -> None:
        self.mode = "menu"
        self._timer.stop()
        self.stack.setCurrentWidget(self.menu_page)

    def stop_run(self) -> None:
        if self.mode == "tapping":
            self._end_condition()
        self.mode = "menu"
        self._timer.stop()
        self.canvas.targets = []
        self.show_summary()

    # ------------------------------------------------------------------ hitting targets

    def start_targets(self) -> None:
        self.mode = "targets"
        self.score = measures.TargetScore()
        self.stack.setCurrentWidget(self.run_page)
        self.canvas.message = ""
        QTimer.singleShot(0, self, self._next_target)

    def _next_target(self) -> None:
        if self.mode != "targets":
            return
        if self.score.shown >= TARGET_COUNT:
            self._finish_targets()
            return
        t = self.svc.theme
        r = t.pxf(TARGET_DIAMETERS[self.score.shown % len(TARGET_DIAMETERS)]) / 2.0
        w, h = max(self.canvas.width(), int(4 * r)), max(self.canvas.height(), int(4 * r))
        previous = self.canvas.targets[0][0] if self.canvas.targets else None
        for _ in range(30):
            c = QPointF(self.rng.uniform(r + 8, w - r - 8), self.rng.uniform(r + 8, h - r - 8))
            if previous is None or math.hypot(c.x() - previous.x(), c.y() - previous.y()) > 0.3 * min(w, h):
                break
        self.canvas.targets = [(c, r)]
        self.canvas.current = 0
        self.score.shown += 1
        self._shown_at = time.monotonic()
        self._timer.start(int(TARGET_TIMEOUT_S * 1000))
        self._status_targets()
        self.canvas.update()

    def _status_targets(self) -> None:
        s = self.score
        self.status.setText(f"Target {s.shown} of {TARGET_COUNT}    Hits {s.hits}    Misses {s.misses}")

    def _finish_targets(self) -> None:
        self._timer.stop()
        self.canvas.targets = []
        self.mode = "menu"
        self.svc.sounds.play("success")
        self.show_summary()

    # ------------------------------------------------------------------ tapping test

    def start_tapping(self) -> None:
        self.mode = "tapping"
        self.tapping = []
        self._condition = -1
        self.stack.setCurrentWidget(self.run_page)
        QTimer.singleShot(0, self, self._next_condition)

    def _next_condition(self) -> None:
        if self.mode != "tapping":
            return
        self._condition += 1
        if self._condition >= len(TAPPING_DIAMETERS):
            self.mode = "menu"
            self.canvas.targets = []
            self.svc.sounds.play("success")
            self.show_summary()
            return
        t = self.svc.theme
        self._diameter = t.pxf(TAPPING_DIAMETERS[self._condition])
        w, h = self.canvas.width(), self.canvas.height()
        # Wide enough that neighbouring targets on the circle never touch (11 targets need about 4 widths).
        room = min(w, h) - self._diameter - 16
        self._amplitude = min(room, max(0.8 * min(w, h) - self._diameter, 4.1 * self._diameter))
        centres = measures.circle_targets(w / 2.0, h / 2.0, self._amplitude, TAPPING_TARGETS)
        self.canvas.targets = [(QPointF(x, y), self._diameter / 2.0) for x, y in centres]
        order = measures.tapping_order(TAPPING_TARGETS)
        self._sequence = [*order, order[0]]  # back to the start, so every target is a measured end point once
        self._step = 0
        self._trials = []
        self.canvas.current = self._sequence[0]
        self.canvas.message = ""
        self._timer.start(int(TAPPING_TIMEOUT_S * 1000))
        self._status_tapping()
        self.canvas.update()

    def _status_tapping(self) -> None:
        size = f"size {self._condition + 1} of {len(TAPPING_DIAMETERS)}"
        if self._step == 0:
            self.status.setText(f"Tapping test, {size}: click the dark target to begin")
        else:
            self.status.setText(f"Tapping test, {size}: {self._step} of {len(self._sequence) - 1}")

    def _end_condition(self) -> None:
        dpr = self._dpr()
        result = measures.summarise_tapping(self._trials, self._diameter * dpr, self._amplitude * dpr)
        if result is not None:
            self.tapping.append(result)

    # ------------------------------------------------------------------ selections

    def select_at(self, point: QPointF) -> None:
        """A click at a canvas point (what a real click does; tests call it directly)."""
        self._selected(point)

    def _selected(self, point: QPointF) -> None:
        now = time.monotonic()
        if self.mode == "targets" and self.canvas.targets:
            c, r = self.canvas.targets[0]
            if math.hypot(point.x() - c.x(), point.y() - c.y()) <= r:
                self.score.hits += 1
                self.score.times_ms.append((now - self._shown_at) * 1000.0)
                self._next_target()
            else:
                self.score.misses += 1
                self._status_targets()
        elif self.mode == "tapping" and self.canvas.targets:
            index = self._sequence[self._step]
            c, r = self.canvas.targets[index]
            hit = math.hypot(point.x() - c.x(), point.y() - c.y()) <= r
            if self._step == 0:
                if not hit:
                    return  # the first selection only starts the clock, so it must be on the target
            else:
                prev = self.canvas.targets[self._sequence[self._step - 1]][0]
                self._trials.append(measures.Trial((prev.x(), prev.y()), (c.x(), c.y()), (point.x(), point.y()), (now - self._last_select) * 1000.0, hit))
            self._last_select = now
            self._step += 1
            if self._step >= len(self._sequence):
                self._timer.stop()
                self._end_condition()
                QTimer.singleShot(0, self, self._next_condition)
                return
            self.canvas.current = self._sequence[self._step]
            self._status_tapping()
            self.canvas.update()

    def _timed_out(self) -> None:
        if self.mode == "targets":
            self.score.skipped += 1
            self._next_target()
        elif self.mode == "tapping":
            self._end_condition()
            self._next_condition()

    # ------------------------------------------------------------------ summary

    def _dpr(self) -> float:
        smap = self.svc.screens.map
        g = self.geometry()
        return smap.screen_at_logical(g.center().x(), g.center().y()).dpr

    def _mm_per_px(self) -> float | None:
        smap = self.svc.screens.map
        g = self.geometry()
        return monitor_mm_per_px(self.svc, smap.screen_at_logical(g.center().x(), g.center().y()))

    def summary_rows(self) -> list[TableRow]:
        rows: list[TableRow] = []
        s = self.score
        if s.shown:
            acc = s.accuracy
            rows.append(TableRow(("Hitting targets",), heading=True))
            rows.append(TableRow(("Hits", f"{s.hits} of {s.shown}" + (f", {s.skipped} not reached in time" if s.skipped else ""))))
            rows.append(TableRow(("Clicks that missed", str(s.misses))))
            if acc is not None:
                rows.append(TableRow(("Clicks on target", f"{acc * 100:.0f}%")))
            if s.mean_ms is not None:
                rows.append(TableRow(("Average time to hit", f"{s.mean_ms / 1000:.1f} seconds")))
        if self.tapping:
            rows.append(TableRow(("Tapping test",), heading=True))
            mean_tp = sum(r.throughput_bps for r in self.tapping) / len(self.tapping)
            rows.append(TableRow(("Throughput", f"{mean_tp:.2f} bits per second")))
            for r in self.tapping:
                rows.append(TableRow((f"Targets {r.width_px:.0f} px", f"{r.error_rate * 100:.0f}% errors, {r.movement_ms / 1000:.2f} s each, {r.throughput_bps:.2f} bits/s")))
        fa = self.false_activations
        per_hour = fa.per_hour()
        rows.append(TableRow(("Unwanted actions",), heading=True))
        rows.append(
            TableRow(("Undone or cancelled within 3 s", f"{fa.count} in {fa.hours():.1f} hours" + (f", {per_hour:.1f} per hour" if per_hour is not None else "")))
        )
        return rows

    def show_summary(self) -> None:
        self.table.set_rows(self.summary_rows())
        self.saved_label.setText(f"Saved as {self.saved_path.name} in the profile folder." if self.saved_path else "")
        self.stack.setCurrentWidget(self.summary_page)

    def save_summary(self) -> Path | None:
        settings = self.svc.ctx.settings
        profile = getattr(settings.general, "profile", "default")
        now = datetime.now()
        page = measures.summary_html(
            when=now,
            profile=profile,
            targets=self.score if self.score.shown else None,
            tapping=self.tapping,
            false_count=self.false_activations.count,
            false_hours=self.false_activations.hours(),
            mm_per_px=self._mm_per_px(),
        )
        path = paths.profile_dir(profile) / f"practice-{now:%Y-%m-%d-%H%M}.html"
        try:
            path.write_text(page, encoding="utf-8")
        except OSError:
            log.exception("Could not save the practice summary")
            self.svc.notify("Could not save the summary.", "error", key="practice")
            return None
        self.saved_path = path
        self.saved_label.setText(f"Saved as {path.name} in the profile folder.")
        self.svc.notify(f"Saved {path.name} in the profile folder.", "info", key="practice")
        return path

    def closeEvent(self, event) -> None:  # noqa: ANN001
        self._timer.stop()
        self.mode = "menu"
        super().closeEvent(event)
