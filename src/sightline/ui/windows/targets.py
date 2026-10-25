"""Calibration runs: dots on a full-screen window, or timed instructions, told to vision as they happen.

``DotRun`` drives gaze, validation, head and drift ("fix pointer") runs:
for each dot it says ``set_target(x, y, collecting=False)``, animates the dot
there, waits for the eyes to settle, collects, and moves on; then calls
``finish()`` on a worker thread (it blocks for a second or two).
``LabelRun`` does the same for eye and face-gesture labels.

Either run ends early when vision reports ``CalibrationActive(False)`` that
we did not ask for: a pipeline rebuild cancels the calibration underneath us.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import weakref
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QPainter

from sightline.core.events import CalibrationActive
from sightline.core.interfaces import CalibrationReport
from sightline.core.types import Rect

from ..coords import ScreenGeometry
from ..overlays.base import OverlayWindow, pen
from ..services import UiServices

log = logging.getLogger(__name__)

TIME_SCALE = 1.0  # tests shrink every wait with this

# Fractions of the monitor.  Corners and edges are in on purpose: the Start
# button, the tray, close buttons and the dock all live there.
GAZE_POINTS: tuple[tuple[float, float], ...] = (
    (0.5, 0.5),
    (0.04, 0.05), (0.5, 0.05), (0.96, 0.05),
    (0.96, 0.5),
    (0.96, 0.95), (0.5, 0.95), (0.04, 0.95),
    (0.04, 0.5),
    (0.27, 0.28), (0.73, 0.28), (0.73, 0.72), (0.27, 0.72),
)  # fmt: skip
VALIDATE_POINTS: tuple[tuple[float, float], ...] = ((0.5, 0.5), (0.15, 0.15), (0.85, 0.15), (0.85, 0.85), (0.15, 0.85))
HEAD_POINTS: tuple[tuple[float, float], ...] = ((0.5, 0.5), (0.15, 0.5), (0.85, 0.5), (0.5, 0.15), (0.5, 0.85))
DRIFT_POINTS: tuple[tuple[float, float], ...] = ((0.5, 0.5),)


@dataclass(frozen=True)
class DotTiming:
    move_ms: int = 450
    settle_ms: int = 400
    collect_ms: int = 800


TIMINGS = {
    "gaze": DotTiming(),
    "validate": DotTiming(),
    "head": DotTiming(move_ms=500, settle_ms=600, collect_ms=1400),  # turning the head is slower than moving the eyes
    "drift": DotTiming(move_ms=0, settle_ms=300, collect_ms=1500),
}

INSTRUCTIONS = {
    "gaze": "Look at the dot. Keep your head still.",
    "validate": "Once more, to check: look at the dot.",
    "head": "Point your nose at the dot.",
    "drift": "Look at the dot.",
}


def points_on(screen: ScreenGeometry, fractions: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Physical pixel positions of fractional points on a monitor (the whole monitor, taskbar included)."""
    r = screen.native
    return [(r.x + fx * r.w, r.y + fy * r.h) for fx, fy in fractions]


def _ms(value: float) -> int:
    return max(1, int(value * TIME_SCALE))


class _ActiveRuns:
    """Runs in progress, so "cancel" can stop one from anywhere."""

    runs: weakref.WeakSet = weakref.WeakSet()

    @classmethod
    def cancel_all(cls) -> bool:
        stopped = False
        for run in list(cls.runs):
            if run.running:
                run.cancel()
                stopped = True
        return stopped


def cancel_running() -> bool:
    return _ActiveRuns.cancel_all()


class _Run(QObject):
    """What dot and label runs share: begin, finish on a worker, unexpected ends."""

    finished = Signal(object)  # CalibrationReport, or None when cancelled or stopped
    step = Signal(int, int)  # index, total

    def __init__(self, services: UiServices, kind: str) -> None:
        super().__init__()
        self.svc = services
        self.kind = kind
        self.running = False
        self.stopped_reason = ""
        self._expect_end = False
        self._token = 0
        self._remove = services.bridge.on(CalibrationActive, self._on_active)
        _ActiveRuns.runs.add(self)

    @property
    def calibration(self):  # noqa: ANN201 - CalibrationAPI
        vision = self.svc.ctx.vision
        return vision.calibration if vision is not None else None

    def _begin(self) -> bool:
        cal = self.calibration
        if cal is None:
            self.stopped_reason = "Eye tracking is not running."
            self.finished.emit(None)
            return False
        self._token += 1
        self._expect_end = False
        self.stopped_reason = ""
        try:
            cal.begin(self.kind)
        except Exception:  # an older vision without this kind, or a camera that just went away
            log.exception("Could not begin a %s calibration", self.kind)
            self.stopped_reason = "This calibration is not available yet."
            self.finished.emit(None)
            return False
        self.running = True
        return True

    def cancel(self) -> None:
        if not self.running:
            return
        self._expect_end = True
        self._stop_timers()
        cal = self.calibration
        if cal is not None:
            try:
                cal.cancel()
            except Exception:
                log.exception("Cancelling the calibration failed")
        self.stopped_reason = "Stopped."
        self._end(None)

    def _finish(self) -> None:
        """Fit on a worker thread: ``finish()`` blocks for a second or two."""
        self._expect_end = True
        token = self._token
        cal = self.calibration

        def work() -> None:
            try:
                report = cal.finish() if cal is not None else None
            except Exception as e:  # a failed fit must still end the run and say so
                log.exception("Calibration finish failed")
                report = CalibrationReport(False, self.kind, f"Calibration failed: {e}")
            self.svc.bridge.post(lambda: self._finished(token, report))

        threading.Thread(target=work, name=f"sightline-calibrate-{self.kind}", daemon=True).start()

    def _finished(self, token: int, report: CalibrationReport | None) -> None:
        if token != self._token or not self.running:
            return
        self._end(report)

    def _on_active(self, e: CalibrationActive) -> None:
        if e.active or not self.running or self._expect_end:
            return
        log.info("Calibration %s ended underneath the wizard", self.kind)
        self._stop_timers()
        self.stopped_reason = "Tracking restarted, so the calibration stopped. Try again."
        self._end(None)

    def _end(self, report: CalibrationReport | None) -> None:
        self.running = False
        self._token += 1
        self._cleanup()
        self.finished.emit(report)

    def _stop_timers(self) -> None:
        self._token += 1

    def _cleanup(self) -> None:
        pass

    def close(self) -> None:
        self.cancel()
        self._remove()


class TargetsWindow(OverlayWindow):
    """Covers one monitor with the ground colour and draws the dot.  Takes clicks (so a stray blink
    clicks nothing underneath) but never focus."""

    def __init__(self, services: UiServices) -> None:
        super().__init__(services, click_through=False, name="calibration-dots")
        self.dot: tuple[float, float] | None = None  # physical
        self.shrink = 0.0  # 0 = large ring, 1 = collected
        self.collecting = False
        self.instruction = ""
        self.note = ""

    def cover(self, screen: ScreenGeometry) -> None:
        self.place_physical(screen.native)
        self.screen_geometry = screen

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        t = self.svc.theme
        pal = t.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), pal.ground)
        if self.instruction:
            p.setFont(t.font("title"))
            p.setPen(pal.ink)
            p.drawText(QRectF(0, self.height() * 0.30, self.width(), t.pxf(40)), int(Qt.AlignmentFlag.AlignHCenter), self.instruction)
        if self.note:
            p.setFont(t.font("body"))
            p.setPen(pal.ink_2)
            p.drawText(QRectF(0, self.height() * 0.30 + t.pxf(44), self.width(), t.pxf(30)), int(Qt.AlignmentFlag.AlignHCenter), self.note)
        if self.dot is not None:
            c = self.logical_point(*self.dot)
            outer = t.pxf(34) * (1.0 - 0.7 * self.shrink)
            p.setPen(pen(pal.ink, t.pxf(3)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, outer, outer)
            core = t.pxf(6)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(pal.accent if self.collecting else pal.ink)
            p.drawEllipse(c, core, core)
        p.end()


class DotRun(_Run):
    """Gaze, validation, head or drift dots on one monitor."""

    def __init__(self, services: UiServices, kind: str, screen: ScreenGeometry, fractions: Sequence[tuple[float, float]] | None = None) -> None:
        super().__init__(services, kind)
        self.screen = screen
        default = {"gaze": GAZE_POINTS, "validate": VALIDATE_POINTS, "head": HEAD_POINTS, "drift": DRIFT_POINTS}[kind]
        self.points = points_on(screen, fractions or default)
        self.timing = TIMINGS.get(kind, DotTiming())
        self.window = TargetsWindow(services)
        self.window.instruction = INSTRUCTIONS.get(kind, "")
        self.window.note = "Say cancel to stop."
        self.index = -1
        self._anim = QTimer(self)
        self._anim.setInterval(16)
        self._anim.timeout.connect(self._tick)
        self._from: tuple[float, float] | None = None
        self._move_start = 0.0
        self._phase = ""

    def start(self) -> None:
        if not self._begin():
            return
        self.window.cover(self.screen)
        self.window.show()
        self.window.raise_()
        self.index = -1
        self._next()

    def _next(self) -> None:
        self.index += 1
        token = self._token
        if self.index >= len(self.points):
            self.window.dot = None
            self.window.instruction = "Working it out"
            self.window.note = ""
            self.window.update()
            self._finish()
            return
        self.step.emit(self.index, len(self.points))
        x, y = self.points[self.index]
        self.calibration.set_target(x, y, False)
        self._from = self.window.dot or (x, y)
        self._move_start = time.monotonic()
        self._phase = "move"
        self.window.collecting = False
        self.window.shrink = 0.0
        self._anim.start()
        QTimer.singleShot(_ms(self.timing.move_ms), self, lambda: self._settle(token))

    def _settle(self, token: int) -> None:
        if token != self._token:
            return
        x, y = self.points[self.index]
        self.window.dot = (x, y)
        self._phase = "settle"
        self.calibration.set_target(x, y, False)
        QTimer.singleShot(_ms(self.timing.settle_ms), self, lambda: self._collect(token))

    def _collect(self, token: int) -> None:
        if token != self._token:
            return
        x, y = self.points[self.index]
        self._phase = "collect"
        self._move_start = time.monotonic()
        self.window.collecting = True
        self.calibration.set_target(x, y, True)
        QTimer.singleShot(_ms(self.timing.collect_ms), self, lambda: self._collected(token))

    def _collected(self, token: int) -> None:
        if token != self._token:
            return
        x, y = self.points[self.index]
        self.calibration.set_target(x, y, False)
        self.window.collecting = False
        self.svc.sounds.play("calibration_point")
        self._next()

    def _tick(self) -> None:
        if not self.running:
            self._anim.stop()
            return
        x, y = self.points[max(0, self.index)]
        if self._phase == "move" and self._from is not None:
            span = max(1, _ms(self.timing.move_ms)) / 1000.0
            f = min(1.0, (time.monotonic() - self._move_start) / span)
            f = 0.5 - 0.5 * math.cos(math.pi * f)  # ease in and out, so the eye can follow
            self.window.dot = (self._from[0] + (x - self._from[0]) * f, self._from[1] + (y - self._from[1]) * f)
        elif self._phase == "collect":
            span = max(1, _ms(self.timing.collect_ms)) / 1000.0
            self.window.shrink = min(1.0, (time.monotonic() - self._move_start) / span)
        self.window.update()

    def _cleanup(self) -> None:
        self._anim.stop()
        self.window.hide()


@dataclass(frozen=True)
class LabelStep:
    label: str  # an EyeLabel
    instruction: str
    seconds: float
    note: str = ""


EYE_STEPS: tuple[LabelStep, ...] = (
    LabelStep("open", "Look at the screen with your eyes open.", 3.0),
    LabelStep("closed", "Close both eyes. Open them at the second tick.", 3.0),
    LabelStep("wink_left", "Close only your left eye. Open it at the second tick.", 3.0),
    LabelStep("wink_right", "Close only your right eye. Open it at the second tick.", 3.0),
)
TALKING_TEXT = (
    "Talk as you normally would, for example about what you did yesterday, or read this aloud: "
    "the garden is full of beans this year, and the tomatoes are nearly ready to pick."
)
READING_TEXT = "Now read quietly to yourself, the way you read a web page, until the bar is full."
GESTURE_STEPS: tuple[LabelStep, ...] = (
    LabelStep("neutral", "Relax your face and look at the screen.", 3.0),
    LabelStep("brow_raise", "Raise your eyebrows and hold them up.", 3.0),
    LabelStep("smile", "Smile and hold it.", 3.0),
    LabelStep("mouth_open", "Open your mouth and hold it open.", 3.0),
    LabelStep("cheek_puff", "Puff out your cheeks and hold it.", 3.0),
    LabelStep("talking", "Talk for half a minute.", 30.0, TALKING_TEXT),
    LabelStep("reading", "Read quietly for half a minute.", 30.0, READING_TEXT),
)


class LabelRun(_Run):
    """Timed instructions for kind "eyes" or "gestures": ``set_eye_label`` for each, a gap between."""

    changed = Signal(object, float)  # LabelStep or None, progress 0..1

    GAP_S = 1.2  # time to read the next instruction before it counts

    def __init__(self, services: UiServices, kind: str, steps: Sequence[LabelStep]) -> None:
        super().__init__(services, kind)
        self.steps = list(steps)
        self.index = -1
        self.current: LabelStep | None = None
        self.low_samples: list[str] = []
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._started = 0.0

    def start(self) -> None:
        if not self._begin():
            return
        self.index = -1
        self._next()

    def _next(self) -> None:
        self.index += 1
        token = self._token
        if self.index >= len(self.steps):
            self.current = None
            self.changed.emit(None, 1.0)
            self._finish()
            return
        self.current = self.steps[self.index]
        self.step.emit(self.index, len(self.steps))
        self.changed.emit(self.current, 0.0)
        QTimer.singleShot(_ms(self.GAP_S * 1000), self, lambda: self._collect(token))

    def _collect(self, token: int) -> None:
        if token != self._token or self.current is None:
            return
        self.calibration.set_eye_label(self.current.label)
        self.svc.sounds.play("hold_1")  # first tick: start now
        self._started = time.monotonic()
        self._timer.start()
        QTimer.singleShot(_ms(self.current.seconds * 1000), self, lambda: self._collected(token))

    def _collected(self, token: int) -> None:
        if token != self._token or self.current is None:
            return
        self._timer.stop()
        cal = self.calibration
        if cal.sample_count() < 5:
            self.low_samples.append(self.current.label)
        cal.set_eye_label(None)
        self.svc.sounds.play("calibration_point")  # second tick: done, open your eyes
        self._next()

    def _tick(self) -> None:
        if self.current is None:
            self._timer.stop()
            return
        span = max(0.001, _ms(self.current.seconds * 1000) / 1000.0)
        self.changed.emit(self.current, min(1.0, (time.monotonic() - self._started) / span))

    def _cleanup(self) -> None:
        self._timer.stop()
        cal = self.calibration
        if cal is not None and self.stopped_reason:
            try:
                cal.set_eye_label(None)
            except Exception:
                log.debug("Could not clear the eye label", exc_info=True)


def monitor_mm_per_px(services: UiServices, screen: ScreenGeometry) -> float | None:
    """Millimetres per physical pixel of the monitor, from the panel size the desktop reports."""
    desktop = services.ctx.desktop
    if desktop is None:
        return None
    try:
        monitor = desktop.display.monitor_at(*screen.native.center)
    except Exception:
        log.debug("Could not read the monitor size", exc_info=True)
        return None
    size = getattr(monitor, "size_mm", None)
    rect: Rect = getattr(monitor, "rect", screen.native)
    if not size or not rect.w:
        return None
    return float(size[0]) / float(rect.w)
