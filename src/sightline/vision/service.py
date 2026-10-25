"""``VisionService``: the vision subsystem as the rest of Sightline sees it (``VisionAPI``).

Threads:

* ``sightline-vision`` reads the newest camera frame, runs MediaPipe and the
  pipeline, and publishes events at camera rate;
* ``sightline-camera`` (inside ``OpenCVCamera``) only grabs frames;
* ``sightline-cursor`` is the cursor pump.

Nothing opens until ``start()``.  ``create(ctx)`` registers the model and the
``pointer.*`` and ``dwell.*`` actions.

A failure inside the pipeline costs one frame, not the session: it is
logged (at most every few seconds), the per-frame state is reset, and the
next frame carries on.  If frames keep failing, the pipeline is rebuilt
from scratch.  ``pointer.restart`` ("restart tracking") does the same on
request and also restarts the vision thread.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from sightline.core.actions import ActionError
from sightline.core.context import AppContext
from sightline.core.events import ClickPerformed, FaceStatus, Notice, SettingsChanged
from sightline.core.interfaces import CalibrationAPI
from sightline.core.types import Action, Attention, CameraInfo, ClickType, PointerMode

from .attention import AttentionTracker
from .camera import (
    CameraDevice,
    CameraSupervisor,
    FrameHealth,
    FrameSource,
    OpenCVCamera,
    choose_camera,
    list_cameras,
    silence_opencv_logs,
)
from .landmarks import FaceFrame, FaceLandmarker, LandmarkerError
from .models import ALL_MODELS, FACE_LANDMARKER_ID
from .pipeline import HINT_MODEL_MISSING, VisionPipeline
from .pointer import CursorPump

log = logging.getLogger(__name__)

NUDGE = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
    "up_left": (-1, -1),
    "up_right": (1, -1),
    "down_left": (-1, 1),
    "down_right": (1, 1),
}
OPPOSITE = {name: next(k for k, v in NUDGE.items() if v == (-dx, -dy)) for name, (dx, dy) in NUDGE.items()}


def _on_off(value: Any) -> bool:
    """An action's on/off argument: a bool, or "on", "off", "true", "false", "yes", "no", 1 or 0."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("on", "true", "yes", "1"):
        return True
    if text in ("off", "false", "no", "0"):
        return False
    raise ActionError("Say on or off")


class Landmarker(Protocol):
    last_ms: float

    def detect(self, bgr: np.ndarray, t: float) -> FaceFrame | None: ...

    def close(self) -> None: ...


@dataclass
class _ThreadState:
    """What one run of the vision thread keeps between frames."""

    landmarker: Landmarker | None = None
    next_model_try: float = 0.0
    warned: bool = False
    detect_failures: deque[float] = field(default_factory=deque)
    health: FrameHealth = field(default_factory=FrameHealth)


class VisionService:
    def __init__(
        self,
        ctx: AppContext,
        *,
        source_factory: Callable[[], FrameSource | None] | None = None,
        landmarker_factory: Callable[[], Landmarker] | None = None,
        start_pump: bool = True,
    ) -> None:
        self.ctx = ctx
        ctx.models.register_all(ALL_MODELS)
        self._source_factory = source_factory
        self._landmarker_factory = landmarker_factory
        self._start_pump = start_pump
        # Kept across pipeline rebuilds: it is a record of what was seen, not state that can go bad.
        self._attention = AttentionTracker()
        self.pipeline = VisionPipeline(ctx, attention=self._attention)
        self.pump: CursorPump | None = None
        if ctx.desktop is not None:
            self.pump = CursorPump(ctx.desktop.input, self.pipeline.engine)
            self.pump.on_yield = self._on_yield
        self._apply_pump_settings()
        self._devices: list[CameraDevice] = []
        self._device: CameraDevice | None = None
        self._camera_key = self._camera_settings()
        self._supervisor: CameraSupervisor | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()  # a new one per run, so a thread that was slow to stop never resumes
        self._running = False
        self._lifecycle = threading.Lock()
        # Failure handling.  Instance attributes so tests can shorten them.
        self.failure_limit = 20  # this many failed frames ...
        self.failure_window_s = 10.0  # ... within this long rebuild the pipeline
        self.rebuild_gap_s = 30.0  # but not more often than this, so a fault that persists cannot loop
        self.log_every_s = 10.0
        self._failures: deque[float] = deque()
        self._last_failure_log = -math.inf
        self._last_rebuild = -math.inf
        self.rebuilds = 0
        self._register_actions()
        self._subs = [
            ctx.bus.subscribe(SettingsChanged, self._on_settings),
            ctx.bus.subscribe(ClickPerformed, self._on_click),
        ]

    # ------------------------------------------------------------------ VisionAPI

    @property
    def calibration(self) -> CalibrationAPI:
        return self.pipeline.calibration

    def start(self) -> None:
        with self._lifecycle:
            # A file source can end the vision thread by itself, so check the thread rather than a flag.
            if self._thread is not None and self._thread.is_alive():
                return
            self._running = True
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop,), name="sightline-vision", daemon=True)
            self._thread.start()
            if self.pump is not None and self._start_pump:
                self.pump.start()
        log.info("Vision started")

    def stop(self) -> None:
        with self._lifecycle:
            was_running = self._running
            self._running = False
            self._stop.set()
            if self.pump is not None:
                self.pump.stop()
            thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(5.0)
            if thread.is_alive():
                log.warning("The vision thread did not stop within 5 s; it will exit when its frame finishes")
        if was_running:
            log.info("Vision stopped")

    def restart(self) -> None:
        """Rebuild the pipeline and restart the vision thread ("restart tracking")."""
        log.info("Restarting vision on request")
        self.stop()
        self._replace_pipeline()
        self._failures.clear()
        self.start()
        self.ctx.bus.publish(Notice("Eye tracking restarted.", "info", key="vision-restart"))

    def cameras(self) -> list[CameraInfo]:
        self._devices = list_cameras()
        return [CameraInfo(i, d.label) for i, d in enumerate(self._devices)]

    def set_camera(self, index: int) -> None:
        devices = self._devices or list_cameras()
        if not 0 <= index < len(devices):
            raise ValueError(f"no camera {index}")
        device = devices[index]
        cfg = self.ctx.settings.vision
        cfg.camera_index = index
        cfg.camera_name = device.name
        self.ctx.settings_changed("vision")

    def set_preview(self, enabled: bool) -> None:
        self.pipeline.preview = bool(enabled)

    def recenter(self) -> None:
        self.pipeline.request_recenter()

    def aim_point(self, t: float) -> tuple[float, float] | None:
        """Where the pointer came to rest at or just after ``t``; None when vision is not driving it."""
        if not self._alive() or self.ctx.state.pointer_paused:
            return None
        pipe = self.pipeline
        if pipe.yielding or (self.pump is not None and self.pump.yielding):
            return None
        # With conservative jumping, a spoken command is also the sign of intent to jump where the user looked.
        return pipe.engine.aim_point(t)

    def attention(self, t0: float, t1: float) -> Attention | None:
        """Face, facing and mouth movement between ``t0`` and ``t1``; None when vision is not running."""
        if not self._alive():
            return None
        return self._attention.query(t0, t1)

    def _alive(self) -> bool:
        thread = self._thread
        return self._running and thread is not None and thread.is_alive()

    # ------------------------------------------------------------------ wiring that survives a rebuild

    def _on_yield(self, yielding: bool) -> None:
        self.pipeline.set_yielding(yielding)

    def _on_click(self, ev: ClickPerformed) -> None:
        self.pipeline.learner.on_click(ev)

    def _replace_pipeline(self) -> VisionPipeline:
        """A fresh pipeline (filters, gestures, pointer engine) with the saved profile loaded again."""
        old = self.pipeline
        new = VisionPipeline(self.ctx, attention=self._attention)
        new.preview = old.preview
        new.camera_name = old.camera_name
        new.reading_scroll.enabled = old.reading_scroll.enabled  # old.close() stops any scroll in progress
        try:
            old.close()
        except Exception:
            log.exception("Closing the old vision pipeline failed")
        self.pipeline = new
        if self.pump is not None:
            self.pump.engine = new.engine
            self.pump.reset()
        self.rebuilds += 1
        return new

    # ------------------------------------------------------------------ settings

    def _camera_settings(self) -> tuple:
        c = self.ctx.settings.vision
        return (c.camera_name, c.camera_index, c.camera_width, c.camera_height, c.camera_fps)

    def _apply_pump_settings(self) -> None:
        if self.pump is None:
            return
        cfg = self.ctx.settings.vision
        self.pump.yield_enabled = bool(cfg.yield_to_mouse)
        self.pump.yield_s = cfg.yield_ms / 1000.0
        self.pump.set_smoothing(cfg.smoothing)

    def _on_settings(self, ev: SettingsChanged) -> None:
        if ev.section not in ("vision", "general"):
            return
        self.pipeline.apply_settings()
        self._apply_pump_settings()
        key = self._camera_settings()
        if key != self._camera_key:
            self._camera_key = key
            if self._supervisor is not None:
                self._supervisor.request_reopen()

    # ------------------------------------------------------------------ the vision thread

    def _make_source(self) -> FrameSource | None:
        if self._source_factory is not None:
            return self._source_factory()
        cfg = self.ctx.settings.vision
        self._devices = list_cameras()
        device = choose_camera(self._devices, cfg.camera_name)
        self._device = device
        if device is None:
            return None
        if device.virtual:
            log.info("Using %s, which is a virtual camera", device.label)
        return OpenCVCamera(device, cfg.camera_width, cfg.camera_height, cfg.camera_fps)

    def _make_landmarker(self) -> Landmarker | None:
        if self._landmarker_factory is not None:
            return self._landmarker_factory()
        models = self.ctx.models
        if not models.is_ready(FACE_LANDMARKER_ID):
            try:
                # Normally the setup wizard has downloaded it already; this covers a deleted or damaged copy.
                models.ensure([FACE_LANDMARKER_ID])
            except Exception as e:
                log.warning("Face model unavailable: %s", e)
                return None
        try:
            return FaceLandmarker(models.path(FACE_LANDMARKER_ID))
        except LandmarkerError as e:
            log.error("Could not start face tracking: %s", e)
            return None

    def _run(self, stop: threading.Event) -> None:
        silence_opencv_logs()
        supervisor = CameraSupervisor(self._make_source)
        self._supervisor = supervisor
        st = _ThreadState()
        try:
            while not stop.is_set():
                try:
                    if not self._step(stop, supervisor, st):
                        break
                except Exception:
                    # Outside the pipeline (camera handling, frame checks): count it like a failed frame.
                    self._frame_failed(self.pipeline, time.perf_counter())
                    stop.wait(0.05)
        finally:
            supervisor.close()
            if st.landmarker is not None:
                st.landmarker.close()

    def _step(self, stop: threading.Event, supervisor: CameraSupervisor, st: _ThreadState) -> bool:
        """One frame.  False when a file source has ended."""
        if st.landmarker is None:
            now = time.perf_counter()
            if now >= st.next_model_try:
                st.landmarker = self._make_landmarker()
                st.next_model_try = now + 30.0
            if st.landmarker is None:
                if not st.warned:
                    st.warned = True
                    self.ctx.bus.publish(
                        Notice("Face tracking could not start: its model is missing.", "error", key="vision-model")
                    )
                self.ctx.bus.publish(FaceStatus(False, hint=HINT_MODEL_MISSING))
                stop.wait(1.0)
                return True
        got = supervisor.read(stop, timeout=1.0)
        pipeline = self.pipeline
        if got is None:
            if supervisor.ended:
                log.info("Frame source finished")
                return False
            st.health.reset()
            now = time.perf_counter()
            try:
                pipeline.idle(now, supervisor.hint)
            except Exception:
                self._frame_failed(pipeline, now)
            return True
        frame, t = got
        pipeline.camera_name = supervisor.description
        hint = st.health.update(frame, t)
        face = self._detect(st, frame, t)
        landmark_ms = st.landmarker.last_ms if st.landmarker is not None else 0.0
        try:
            pipeline.process(face, t, image=frame, camera_hint=hint, landmark_ms=landmark_ms)
        except Exception:
            self._frame_failed(pipeline, t)
        return True

    def _detect(self, st: _ThreadState, frame: np.ndarray, t: float) -> FaceFrame | None:
        landmarker = st.landmarker
        assert landmarker is not None
        try:
            return landmarker.detect(frame, t)
        except Exception:
            now = time.monotonic()
            fails = st.detect_failures
            fails.append(now)
            while fails and now - fails[0] > self.failure_window_s:
                fails.popleft()
            if self._may_log(now):
                log.exception("Face tracking failed on a frame (%d in the last %.0f s)", len(fails), self.failure_window_s)
            if len(fails) >= self.failure_limit:
                log.error("Face tracking keeps failing; starting the face model again")
                fails.clear()
                try:
                    landmarker.close()
                except Exception:
                    log.debug("closing the face model failed", exc_info=True)
                st.landmarker = None
                st.next_model_try = 0.0
            return None

    def _may_log(self, now: float) -> bool:
        if now - self._last_failure_log >= self.log_every_s:
            self._last_failure_log = now
            return True
        return False

    def _frame_failed(self, pipeline: VisionPipeline, t: float) -> None:
        """Called from an ``except`` block: log (not too often), reset per-frame state, rebuild if it keeps happening."""
        now = time.monotonic()
        fails = self._failures
        fails.append(now)
        while fails and now - fails[0] > self.failure_window_s:
            fails.popleft()
        if self._may_log(now):
            log.exception("A vision frame failed (%d in the last %.0f s)", len(fails), self.failure_window_s)
        try:
            pipeline.recover(t)
        except Exception:
            log.debug("Resetting after a failed frame also failed", exc_info=True)
        if len(fails) >= self.failure_limit and now - self._last_rebuild >= self.rebuild_gap_s:
            fails.clear()
            self._last_rebuild = now
            log.error("Vision frames keep failing; starting the pipeline afresh")
            try:
                self._replace_pipeline()
            except Exception:
                log.exception("Rebuilding the vision pipeline failed")
                return
            self.ctx.bus.publish(Notice("Eye tracking hit a problem and restarted.", "warn", key="vision-restart"))

    # ------------------------------------------------------------------ actions

    def _register_actions(self) -> None:
        reg = self.ctx.actions
        state = self.ctx.state

        def pause(_a: Action) -> None:
            state.update(pointer_paused=True)

        def resume(_a: Action) -> None:
            state.update(pointer_paused=False)

        def toggle(_a: Action) -> None:
            state.update(pointer_paused=not state.pointer_paused)

        def mode(a: Action) -> PointerMode:
            value = str(a.arg("mode", "")).lower()
            try:
                new = PointerMode(value)
            except ValueError:
                raise ActionError("Pointer mode must be hybrid, head or gaze") from None
            old = PointerMode(state.pointer_mode)
            state.update(pointer_mode=new)
            cfg = self.ctx.settings.vision
            if cfg.pointer_mode != new:
                cfg.pointer_mode = new
                self.ctx.settings_changed("vision")
            return old

        def undo_mode(a: Action, old: Any) -> Action | None:
            return Action("pointer.mode", {"mode": str(old)}) if old is not None else None

        def recenter(_a: Action) -> None:
            self.recenter()

        def nudge(a: Action) -> tuple[str, float]:
            direction = str(a.arg("direction", "")).lower().replace(" ", "_").replace("-", "_")
            if direction not in NUDGE:
                raise ActionError("Nudge direction must be up, down, left or right")
            try:
                amount = float(a.arg("amount", 10))
            except (TypeError, ValueError):
                raise ActionError("Nudge amount must be a number") from None
            ux, uy = NUDGE[direction]
            dx, dy = ux * amount, uy * amount
            engine = self.pipeline.engine
            if self._alive() and engine.pump_target().active:
                engine.nudge(dx, dy)  # the cursor pump carries it out
                return direction, amount
            desktop = self.ctx.desktop
            if desktop is None:
                raise ActionError("The pointer cannot be moved right now")
            x, y = desktop.input.cursor_pos()
            desktop.input.move_cursor(x + dx, y + dy)
            engine.adopt(x + dx, y + dy)
            return direction, amount

        def undo_nudge(a: Action, done: Any) -> Action | None:
            if not done:
                return None
            direction, amount = done
            return Action("pointer.nudge", {"direction": OPPOSITE[direction], "amount": amount})

        def set_dwell(on: bool | None) -> Callable[[Action], bool]:
            def handler(_a: Action) -> bool:
                old = bool(state.dwell_enabled)
                state.update(dwell_enabled=(not old) if on is None else on)
                return old

            return handler

        def undo_dwell(a: Action, old: Any) -> Action | None:
            return Action("dwell.on" if old else "dwell.off")

        def click_type(a: Action) -> ClickType:
            value = str(a.arg("type", "")).lower()
            old = ClickType(state.dwell_click)
            try:
                state.update(dwell_click=ClickType(value))
            except ValueError:
                raise ActionError("Click type must be left, right, middle, double or drag") from None
            return old

        def undo_click_type(a: Action, old: Any) -> Action | None:
            return Action("dwell.click_type", {"type": str(old)}) if old is not None else None

        def restart(_a: Action) -> None:
            self.restart()

        def reading_scroll(a: Action) -> bool:
            pipe = self.pipeline
            value = a.arg("on")
            on = (not pipe.reading_scroll.enabled) if value is None else _on_off(value)
            if on and pipe.current_gaze_model() is None:
                raise ActionError("Reading scroll needs eye pointing to be calibrated first")
            pipe.set_reading_scroll(on)
            self.ctx.state.update(reading_scroll=on)
            self.ctx.bus.publish(
                Notice("Reading scroll on." if on else "Reading scroll off.", "info", key="vision-reading-scroll")
            )
            return on

        # (name, handler, description, undo); undo None with history False: pausing and the like are
        # not "the last thing done" for "undo that".
        entries: list[tuple[str, Callable[[Action], Any], str, Callable[[Action, Any], Action | None] | None]] = [
            ("pointer.pause", pause, "Pause eye control", None),
            ("pointer.resume", resume, "Resume eye control", None),
            ("pointer.toggle", toggle, "Pause or resume eye control", None),
            ("pointer.mode", mode, "Choose how the pointer is controlled: hybrid, head or gaze", undo_mode),
            ("pointer.recenter", recenter, "Treat the current head position as straight ahead", None),
            ("pointer.nudge", nudge, "Move the pointer a few pixels", undo_nudge),
            ("pointer.restart", restart, "Restart eye and head tracking", None),
            ("pointer.reading_scroll", reading_scroll, "Scroll by looking near the bottom or top of a window", None),
            ("dwell.on", set_dwell(True), "Turn on dwell clicking", undo_dwell),
            ("dwell.off", set_dwell(False), "Turn off dwell clicking", undo_dwell),
            ("dwell.toggle", set_dwell(None), "Turn dwell clicking on or off", undo_dwell),
            ("dwell.click_type", click_type, "Choose what the next dwell or blink click does", undo_click_type),
        ]
        for name, handler, description, undo in entries:
            reg.register(name, handler, description=description, replace=True, undo=undo, history=undo is not None)
