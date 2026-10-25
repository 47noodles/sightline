"""The per-frame vision pipeline: from a ``FaceFrame`` to events and actions.

It knows nothing about cameras or MediaPipe, so tests, the replay tool and the
live service all drive it the same way: call ``process(face, t)`` once per
camera frame (``face`` None when no face was found).

Per frame: measure the face; estimate each eye's closure and run the eye
gesture and face gesture detectors; track the head; predict and smooth the
gaze; judge whether the user is reading; move the pointer target; run
dwell, the resume zone and the reading scroll; hand the frame to an active
calibration; remember it for learning from clicks and for ``attention``;
and publish ``PointerSample`` (with the accuracy map's ``error_px``),
``EyeState`` and, a few times a second, ``FaceStatus``.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace

import numpy as np

from sightline.core.context import AppContext
from sightline.core.events import (
    CalibrationActive,
    CameraFrame,
    DwellProgress,
    EyeGesture,
    EyeHold,
    EyeState,
    FaceGesture,
    FaceStatus,
    GazeModelChanged,
    GazeZoneProgress,
    Notice,
    PointerSample,
    StateChanged,
)
from sightline.core.types import Action, ClickType, Monitor, PointerMode, Rect

from . import features as feat
from .accuracy import AccuracyMap, targets_from_groups
from .attention import AttentionTracker, mouth_signals
from .calibration import CalibrationController, FrameRecord, fingerprint_matches
from .dwell import DwellClicker, ResumeZone, resume_rect
from .events import VisionDiagnostics
from .eyes import ClosureEstimator, EyeGestureDetector, EyeProfile, Gesture, GestureConfig, Hold
from .face_gestures import FACE_KINDS, FaceGestureConfig, FaceGestureDetector, FaceGestureProfile, FaceHit
from .filters import GazeSmoother, GazeState
from .gaze_model import GazeModel, TrainingData, add_offset
from .head import HeadProfile, HeadSample, HeadTracker
from .landmarks import PREVIEW_POINTS, FaceFrame
from .learning import ClickLearner
from .pointer import PointerEngine
from .profile import VisionProfile
from .reading import ReadingDetector, ReadingScroll, ScrollCommand

log = logging.getLogger(__name__)

STATUS_PERIOD_S = 0.33
DIAG_PERIOD_S = 0.5
PREVIEW_PERIOD_S = 1.0 / 15.0
PREVIEW_WIDTH = 320
WINDOW_REFRESH_S = 1.0  # how often the reading scroll asks Windows which window is under the pointer

HINT_NO_FACE = "Sit where the camera can see your face"
HINT_CLOSER = "Move closer"
HINT_BACK = "Move back a little"
HINT_LIGHT = "More light would help"
HINT_FACE_SCREEN = "Face the screen"
HINT_MODEL_MISSING = "The face tracking model is missing"
DRIFT_NOTICE = 'The pointer seems to have drifted. Say "fix pointer" to correct it.'

# Actions that may run from an eye or face gesture while eye control is paused.
_ALLOWED_WHILE_PAUSED = ("pointer.resume", "pointer.toggle")


def click_action(name: str, x: float, y: float, click_type: ClickType, source: str) -> Action | None:
    """The action for a gesture or dwell setting such as "click", "right_click" or "pointer.toggle"."""
    name = (name or "").strip().lower()
    if name in ("", "none", "nothing"):
        return None
    if name == "click":
        name = {
            ClickType.LEFT: "left_click",
            ClickType.RIGHT: "right_click",
            ClickType.MIDDLE: "middle_click",
            ClickType.DOUBLE: "double_click",
            ClickType.DRAG: "drag",
        }[ClickType(click_type)]
    clicks = {
        "left_click": ("left", 1),
        "double_click": ("left", 2),
        "right_click": ("right", 1),
        "middle_click": ("middle", 1),
    }
    if name in clicks:
        button, count = clicks[name]
        return Action("mouse.click", {"button": button, "count": count, "x": x, "y": y}, source=source)  # type: ignore[arg-type]
    if name in ("drag", "drag_toggle"):
        return Action("mouse.drag_toggle", {"x": x, "y": y}, source=source)  # type: ignore[arg-type]
    if "." in name:
        return Action(name, {}, source=source)  # type: ignore[arg-type]
    log.warning("Unknown gesture action %r", name)
    return None


def _mapped(name: str | None) -> bool:
    return (name or "").strip().lower() not in ("", "none", "nothing")


class VisionPipeline:
    """Owns every per-frame component.  ``process`` runs on one thread (the vision thread)."""

    def __init__(
        self,
        ctx: AppContext,
        *,
        profile: VisionProfile | None = None,
        clock: Callable[[], float] = time.perf_counter,
        load_profile: bool = True,
        attention: AttentionTracker | None = None,
    ) -> None:
        self.ctx = ctx
        self.bus = ctx.bus
        self.state = ctx.state
        self.clock = clock
        self.display = ctx.desktop.display if ctx.desktop is not None else None
        self.profile = profile or VisionProfile(ctx.settings.general.profile)
        start = None
        if ctx.desktop is not None:
            try:
                start = tuple(float(v) for v in ctx.desktop.input.cursor_pos())
            except Exception:
                start = None
        self.closure = ClosureEstimator()
        self.gestures = EyeGestureDetector()
        self.face = FaceGestureDetector()
        self.smoother = GazeSmoother()
        self.head = HeadTracker()
        self.engine = PointerEngine(self.display, start)  # type: ignore[arg-type]
        self.reading = ReadingDetector()
        self.reading_scroll = ReadingScroll()
        self.dwell = DwellClicker()
        self.resume = ResumeZone()
        self.calibration = CalibrationController(self, clock)
        self.learner = ClickLearner(self, clock)
        self.attention = attention if attention is not None else AttentionTracker()

        self._model: GazeModel | None = None
        self._cal_data: TrainingData | None = None
        self._accuracy: AccuracyMap | None = None
        self._eye_profile: EyeProfile | None = None
        self._head_profile: HeadProfile | None = None
        self._face_profile: FaceGestureProfile | None = None
        self._yielding = False
        self._recenter = False
        self.preview = False
        self.camera_name = ""
        self.camera_hint: str | None = None

        self._present: bool | None = None
        self._frame_times: deque[float] = deque(maxlen=90)
        self._last_status = -1e9
        self._last_diag = -1e9
        self._last_preview = -1e9
        self._brightness: float | None = None
        self._distance: deque[float] = deque(maxlen=15)
        self._last_measures: feat.FaceMeasures | None = None
        self._last_closure = (0.0, 0.0)
        self._landmark_ms = 0.0
        self._pipeline_ms = 0.0
        self._last_face_t: float | None = None
        self._zone_rect: Rect | None = None
        self._last_bright_t = -1e9
        self._window_rect: Rect | None = None
        self._window_checked = -1e9

        self.apply_settings()
        if load_profile:
            self.load_profile()
        self._state_sub = self.bus.subscribe(StateChanged, self._on_state)

    def close(self) -> None:
        """Stop reacting to the rest of the app (this pipeline is being replaced)."""
        self._state_sub.cancel()
        if self.calibration.active:
            self.calibration.cancel()
        self._send_scroll(self.reading_scroll.stop())

    # ------------------------------------------------------------------ settings and profile

    @property
    def cfg(self):
        return self.ctx.settings.vision

    def apply_settings(self) -> None:
        cfg = self.cfg
        g = self.gestures.cfg
        g.click_s = cfg.blink_click_ms / 1000.0
        g.double_s = max(cfg.blink_double_ms / 1000.0, g.click_s + 0.2)
        g.cancel_s = max(cfg.blink_cancel_ms / 1000.0, g.double_s + 0.5)
        g.wink_min_s = cfg.wink_min_ms / 1000.0
        g.wink_max_s = g.cancel_s
        g.blinks = bool(cfg.blink_clicks)
        prof = self._eye_profile
        g.wink_left = bool(cfg.winks) and (prof is None or prof.wink_left_ok)
        g.wink_right = bool(cfg.winks) and (prof is None or prof.wink_right_ok)
        if prof is not None:
            g.clear_left, g.clear_right = prof.clear_left, prof.clear_right
        self.face.cfg.hold_s = cfg.face_hold_ms / 1000.0
        self.smoother.set_smoothing(cfg.smoothing)
        self.head.set_smoothing(cfg.smoothing)
        self.head.speed_scale = cfg.head_speed
        self.head.dead_zone = cfg.head_dead_zone
        self.head.mirrored = bool(cfg.camera_mirrored)
        primary = self._primary()
        if primary is not None:
            self.head.set_screen(primary.rect.w, primary.rect.h)
        e = self.engine.cfg
        e.warp_distance = float(cfg.warp_distance_px)
        e.warp_dwell_s = cfg.warp_dwell_ms / 1000.0
        e.pause_when_face_lost = bool(cfg.pause_when_face_lost)
        e.warp_on_intent = bool(cfg.warp_on_intent)
        self.dwell.dwell_s = cfg.dwell_ms / 1000.0
        self.dwell.radius = float(cfg.dwell_radius_px)
        self.resume.hold_s = cfg.resume_ms / 1000.0
        self.learner.cfg.enabled = bool(cfg.learn_from_clicks)
        self._zone_rect = resume_rect(primary, cfg.resume_zone) if primary is not None else None
        if self.state.pointer_mode != cfg.pointer_mode:
            self.state.update(pointer_mode=PointerMode(cfg.pointer_mode))

    def load_profile(self) -> None:
        try:
            model, cal, clicks = self.profile.load_gaze()
        except Exception:
            log.exception("Could not load the gaze model")
            model, cal, clicks = None, None, None
        if model is not None and not fingerprint_matches(model.meta.get("monitors"), self.monitors()):
            log.info("Screen layout changed since calibration; the gaze model is not used")
            self.bus.publish(
                Notice("Your screens changed, so eye pointing needs calibrating again.", "warn", key="vision-recalibrate")
            )
            model = None
        self._model = model
        self._cal_data = cal
        self._rebuild_accuracy()
        self.learner.set_clicks(clicks if model is not None else None, model.accuracy_px if model else None)
        self._eye_profile = self.profile.load_eyes()
        self.closure.set_profile(self._eye_profile)
        self._head_profile = self.profile.load_head()
        self.head.profile = self._head_profile
        self._face_profile = self.profile.load_face()
        self.face.set_profile(self._face_profile)
        self.apply_settings()
        self.state.update(gaze_calibrated=model is not None, eyes_calibrated=self._eye_profile is not None)
        if model is not None:
            log.info("Gaze model loaded: %s", model.describe())
            self.bus.publish(GazeModelChanged("profile", model.accuracy_px, model.n_targets, model.n_samples))

    def _primary(self) -> Monitor | None:
        if self.display is None:
            return None
        try:
            return self.display.primary()
        except Exception:
            return None

    def _on_state(self, ev: StateChanged) -> None:
        if ev.field == "pointer_paused":
            self.dwell.reset()
            if not ev.new:
                cleared = self.resume.reset()
                if cleared is not None:
                    self.bus.publish(GazeZoneProgress("resume", 0.0, self._zone_rect))

    # ------------------------------------------------------------------ accuracy map

    def _rebuild_accuracy(self) -> None:
        """The accuracy map of the current gaze model: its held-out errors at the dots it was calibrated on."""
        model = self._model
        self._accuracy = None
        if model is None or not model.per_target:
            return
        targets = list(model.targets)
        if len(targets) != len(model.per_target) and self._cal_data is not None:
            # Saved before the dots' positions were stored with the model: the calibration samples have them.
            targets = targets_from_groups(self._cal_data.y, self._cal_data.groups)
        if len(targets) != len(model.per_target):
            return
        try:
            self._accuracy = AccuracyMap(targets, model.per_target, self.monitors())
        except Exception:
            log.exception("Could not build the accuracy map")

    def error_at(self, x: float, y: float, mode: PointerMode | None = None) -> float | None:
        """Expected gaze error near (x, y); None before calibration, in head mode, or off the calibrated screens."""
        acc = self._accuracy
        mode = PointerMode(self.state.pointer_mode) if mode is None else mode
        if acc is None or self._model is None or mode == PointerMode.HEAD:
            return None
        return acc.error_px(x, y)

    # ------------------------------------------------------------------ host for calibration and learning

    def current_gaze_model(self) -> GazeModel | None:
        return self._model

    def calibration_samples(self) -> TrainingData | None:
        return self._cal_data

    def live_gaze(self) -> tuple[float, float] | None:
        if self._model is None or self.smoother.last is None:
            return None
        return self.smoother.last.x, self.smoother.last.y

    def monitors(self) -> list[Monitor]:
        if self.display is None:
            return []
        try:
            return list(self.display.monitors())
        except Exception:
            return []

    def mirrored(self) -> bool:
        return bool(self.cfg.camera_mirrored)

    def gesture_configs(self) -> tuple[FaceGestureConfig, GestureConfig]:
        return replace(self.face.cfg), replace(self.gestures.cfg)

    def activate_gaze(self, model: GazeModel, data: TrainingData) -> None:
        self._model = model
        self._cal_data = data
        self._rebuild_accuracy()
        self.smoother.reset()
        self.learner.set_clicks(None, model.accuracy_px)
        try:
            self.profile.save_gaze(model, data, None)
        except OSError:
            log.exception("Could not save the gaze model")
        self.state.update(gaze_calibrated=True)
        self.bus.publish(GazeModelChanged("calibration", model.accuracy_px, model.n_targets, model.n_samples))

    def replace_gaze_model(self, model: GazeModel, clicks: TrainingData) -> None:
        current = self._model
        if current is not None and any(current.meta.get(k) != model.meta.get(k) for k in ("fitted", "drift_fixed")):
            # A calibration or "fix pointer" landed while this refit ran from the model before it.
            log.info("Click learning result dropped: the gaze model changed meanwhile")
            return
        self._model = model  # a reference swap: the vision thread sees the old or the new one, never half
        self._rebuild_accuracy()
        try:
            self.profile.save_gaze(model, self._cal_data, clicks)
        except OSError:
            log.exception("Could not save the updated gaze model")
        self.bus.publish(GazeModelChanged("clicks", model.accuracy_px, model.n_targets, model.n_samples))

    def apply_drift(self, dx: float, dy: float) -> GazeModel | None:
        """"Fix pointer": shift the current model's output by (dx, dy).  No refit."""
        model = self._model
        if model is None:
            return None
        fixed = replace(model, correction=add_offset(model.correction, dx, dy),
                        meta=dict(model.meta, drift_fixed=time.time()))  # fmt: skip
        self._model = fixed
        self.smoother.reset()
        self.learner.drift_fixed()
        try:
            self.profile.save_gaze(fixed, self._cal_data, self.learner.clicks)
        except OSError:
            log.exception("Could not save the corrected gaze model")
        self.bus.publish(GazeModelChanged("drift", fixed.accuracy_px, fixed.n_targets, fixed.n_samples))
        return fixed

    def suggest_drift_fix(self, error_px: float) -> None:
        self.bus.publish(Notice(DRIFT_NOTICE, "info", key="vision-drift"))

    def activate_eyes(self, profile: EyeProfile) -> None:
        self._eye_profile = profile
        self.closure.set_profile(profile)
        try:
            self.profile.save_eyes(profile)
        except OSError:
            log.exception("Could not save the eye profile")
        self._sync_mirror(profile.mirrored)
        self.apply_settings()
        self.state.update(eyes_calibrated=True)

    def activate_head(self, profile: HeadProfile) -> None:
        self._head_profile = profile
        self.head.profile = profile
        try:
            self.profile.save_head(profile)
        except OSError:
            log.exception("Could not save the head profile")
        self._sync_mirror(profile.mirrored)

    def activate_face(self, profile: FaceGestureProfile) -> None:
        self._face_profile = profile
        self.face.set_profile(profile)
        try:
            self.profile.save_face(profile)
        except OSError:
            log.exception("Could not save the face gesture profile")

    @property
    def face_profile(self) -> FaceGestureProfile | None:
        return self._face_profile

    def _sync_mirror(self, mirrored: bool | None) -> None:
        if mirrored is None or bool(mirrored) == bool(self.cfg.camera_mirrored):
            return
        log.info("Calibration found the camera %s mirrored", "is" if mirrored else "is not")
        self.cfg.camera_mirrored = bool(mirrored)
        try:
            self.ctx.settings_changed("vision")
        except OSError:
            log.exception("Could not save settings")

    def calibration_changed(self, active: bool) -> None:
        cleared = self.dwell.reset()
        if cleared is not None:
            self.bus.publish(DwellProgress(0.0, 0.0, 0.0, self.dwell.radius))
        self._send_scroll(self.reading_scroll.stop())
        self.bus.publish(CalibrationActive(active, self.calibration.kind))

    # ------------------------------------------------------------------ requests from other threads

    def request_recenter(self) -> None:
        self._recenter = True

    def set_yielding(self, yielding: bool) -> None:
        self._yielding = yielding

    @property
    def yielding(self) -> bool:
        return self._yielding

    def set_reading_scroll(self, on: bool) -> None:
        """Turn the reading scroll on or off (any thread; the vision thread acts on the next frame)."""
        self.reading_scroll.enabled = bool(on)
        if not on:
            self._send_scroll(self.reading_scroll.stop())

    # ------------------------------------------------------------------ per frame

    def idle(self, t: float, hint: str | None) -> None:
        """No frame arrived (camera starting, failed or gone).  Keeps status flowing and the face marked lost."""
        self.camera_hint = hint
        if self._last_face_t is None or t - self._last_face_t > 0.5:
            if self._present is not False:
                self.process(None, t, count=False)
                return
        self._maybe_status(t)

    def process(
        self,
        face: FaceFrame | None,
        t: float,
        *,
        image: np.ndarray | None = None,
        camera_hint: str | None = None,
        landmark_ms: float = 0.0,
        count: bool = True,
    ) -> None:
        started = time.perf_counter()
        self._landmark_ms = landmark_ms
        self.camera_hint = camera_hint
        if count:
            self._frame_times.append(t)
        paused = bool(self.state.pointer_paused)
        calibrating = self.calibration.active
        if face is None:
            self._process_lost(t, paused, calibrating)
        else:
            self._process_face(face, t, paused, calibrating, image)
        if image is not None:
            # Also without a face: the setup wizard needs the picture most when the face is not found.
            self._maybe_preview(image, face, t)
        self._pipeline_ms = (time.perf_counter() - started) * 1000.0
        self._maybe_status(t)
        self._maybe_diagnostics(t)

    def recover(self, t: float) -> None:
        """A frame failed part way through: start the next one from clean per-frame state."""
        for ev in self.gestures.lost(t):
            if isinstance(ev, Hold):
                self.bus.publish(EyeHold(t, ev.stage, ev.cancelled))
        self.face.reset()
        self.reading.reset()
        self.head.lost()
        self.smoother.reset()
        if self.dwell.reset() is not None:
            self.bus.publish(DwellProgress(0.0, 0.0, 0.0, self.dwell.radius))
        if self.resume.reset() is not None:
            self.bus.publish(GazeZoneProgress("resume", 0.0, self._zone_rect))
        self._recenter = False
        self._last_measures = None

    def _process_lost(self, t: float, paused: bool, calibrating: bool) -> None:
        self._set_present(False, t)
        self.attention.lost(t)
        for ev in self.gestures.lost(t):
            if isinstance(ev, Hold) and not paused and not calibrating:
                self.bus.publish(EyeHold(t, ev.stage, ev.cancelled))
        self.face.reset()
        self.reading.update(t, None, 0.0)
        self.head.lost()
        self.smoother.reset()
        self.engine.update(t, present=False, mode=PointerMode.HEAD, gaze=None, head=None, frozen=True, paused=paused,
                           inactive=calibrating)  # fmt: skip
        cleared = self.dwell.reset()
        if cleared is not None:
            self.bus.publish(DwellProgress(0.0, 0.0, 0.0, self.dwell.radius))
        self._run_resume(t, None, paused, calibrating)
        x, y = self.engine.position
        error = self.error_at(x, y)
        self._run_reading_scroll(t, None, x, y, error, paused, calibrating)
        self.bus.publish(PointerSample(t, x, y, None, None, "lost", 0.0, error, self.engine.steered))
        self.bus.publish(EyeState(t, 0.0, 0.0, "unknown"))

    def _process_face(
        self, face: FaceFrame, t: float, paused: bool, calibrating: bool, image: np.ndarray | None
    ) -> None:
        cfg = self.cfg
        self._set_present(True, t)
        self._last_face_t = t
        m = feat.measure(face, cfg.camera_fov_deg)
        self._last_measures = m
        if m.distance_cm is not None:
            self._distance.append(m.distance_cm)
        mirrored = bool(cfg.camera_mirrored)
        left, right = feat.user_eyes(m, mirrored)
        cl, cr = self.closure.update(left, right)
        self._last_closure = (cl, cr)
        was_frozen = self.gestures.frozen
        events = self.gestures.update(t, cl, cr)
        frozen = self.gestures.frozen_at(t)
        if self.gestures.frozen and not was_frozen:
            # The lids started to move: what the smoother saw last is already being dragged down.
            self.smoother.reset()
        head = self.head.update(t, m)
        if self._recenter:
            self._recenter = False
            self._do_recenter()
        mouth = mouth_signals(face, m.iod_px)
        self.attention.update_signals(t, head.yaw, head.pitch, mouth)

        model = self._model
        gaze_raw: tuple[float, float] | None = None
        gaze: GazeState | None = None
        if model is not None:
            try:
                gaze_raw = model.predict(m.raw, m.lm_block)
            except Exception:
                log.exception("Gaze prediction failed; dropping the model")
                self._model = None
                self._accuracy = None
                self.state.update(gaze_calibrated=False)
            if gaze_raw is not None and all(math.isfinite(v) for v in gaze_raw) and not frozen:
                gaze = self.smoother.update(t, gaze_raw[0], gaze_raw[1])

        mode = PointerMode(self.state.pointer_mode)
        effective = mode if self._model is not None else PointerMode.HEAD
        reading = self._update_reading(t, gaze, head, effective, paused, calibrating)
        phase = self.engine.update(t, present=True, mode=effective, gaze=gaze, head=head, frozen=frozen, paused=paused,
                                   inactive=calibrating, reading=reading)  # fmt: skip

        self._handle_gestures(events, t, paused, calibrating)
        self._handle_face_gestures(face, t, paused, calibrating, frozen)
        self._run_dwell(t, paused, calibrating, frozen, phase)
        point = None
        if not frozen:
            if gaze is not None:
                point = (gaze.x, gaze.y)
            elif self._model is None and self._zone_rect is not None:
                primary = self._primary()
                if primary is not None:
                    r = primary.rect
                    point = self.head.direction_point(r.x, r.y, r.w, r.h)
        self._run_resume(t, point, paused, calibrating)
        x, y = self.engine.position
        error = self.error_at(x, y, effective)
        self._run_reading_scroll(t, (gaze.x, gaze.y) if gaze is not None else None, x, y, error, paused, calibrating)

        if calibrating:
            self.calibration.on_frame(
                FrameRecord(
                    t=t,
                    raw=m.raw,
                    lm=m.lm_block,
                    closure=(cl, cr),
                    blink=(left.blink, right.blink),
                    ear=(left.ear, right.ear),
                    head_xy=(head.x, head.y),
                    pose=(head.yaw, head.pitch),
                    distance_cm=m.distance_cm,
                    gaze=gaze_raw,
                    blendshapes=face.blendshapes,
                    mouth=mouth,
                )
            )
        self.learner.record(t, m.raw, m.lm_block, gaze, eyes_open=not frozen)

        smoothed = self.smoother.last if self._model is not None and not frozen else None
        confidence = 1.0 if not frozen else 0.5
        self.bus.publish(
            PointerSample(
                t,
                x,
                y,
                None if smoothed is None else smoothed.x,
                None if smoothed is None else smoothed.y,
                phase,
                confidence,
                error,
                self.engine.steered,
            )
        )
        self.bus.publish(EyeState(t, cl, cr, self.gestures.state_name()))  # type: ignore[arg-type]
        if image is not None:
            self._measure_brightness(image, m, t)

    # ------------------------------------------------------------------ pieces

    def _do_recenter(self) -> None:
        self.head.recenter()
        if self._model is None or PointerMode(self.state.pointer_mode) == PointerMode.HEAD:
            # With the head driving, "recenter" also brings the pointer to the middle of its screen.
            if self.display is not None:
                x, y = self.engine.position
                try:
                    cx, cy = self.display.monitor_at(x, y).rect.center
                except Exception:
                    return
                self.engine.move_to(cx, cy)

    def _update_reading(
        self, t: float, gaze: GazeState | None, head: HeadSample, mode: PointerMode, paused: bool, calibrating: bool
    ) -> bool:
        if not self.cfg.reading_hold or mode == PointerMode.HEAD or paused or calibrating:
            if self.reading.reading:
                self.reading.reset()
            return False
        return self.reading.update(t, gaze, head.speed)

    def _gesture_point(self, action: Action | None, aimed_at: float) -> tuple[float, float]:
        """Where a gesture's click goes: the pointer when the user aimed, or the jump they meant to make."""
        x, y = self.engine.position_at(aimed_at)
        if action is not None and action.name.startswith("mouse."):
            intent = self.engine.take_intent(aimed_at)
            if intent is not None:
                x, y = intent
        return x, y

    def _handle_gestures(self, events: list[Hold | Gesture], t: float, paused: bool, calibrating: bool) -> None:
        cfg = self.cfg
        for ev in events:
            if isinstance(ev, Hold):
                if not paused and not calibrating and not self._yielding:
                    self.bus.publish(EyeHold(t, ev.stage, ev.cancelled))
                continue
            if calibrating:
                continue
            if self._yielding:
                log.info("Eye %s ignored: someone is using the mouse", ev.kind)
                continue
            name = cfg.gesture_actions.get(ev.kind, "")
            probe = click_action(name, 0.0, 0.0, ClickType(self.state.dwell_click), "eye")
            if paused and (probe is None or probe.name not in _ALLOWED_WHILE_PAUSED):
                continue
            aimed_at = ev.onset - self.gestures.cfg.lookback_s
            x, y = self._gesture_point(probe, aimed_at)
            action = click_action(name, x, y, ClickType(self.state.dwell_click), "eye")
            self.bus.publish(EyeGesture(t, ev.kind, ev.duration, x, y))
            if action is not None:
                if action.name.startswith("mouse."):
                    self.learner.note_click(t, aimed_at, x, y)
                self.ctx.actions.dispatch(action)

    def _handle_face_gestures(self, face: FaceFrame, t: float, paused: bool, calibrating: bool, frozen: bool) -> None:
        prof = self._face_profile
        if prof is None:
            return
        # Only gestures the trainer found reliable and the user gave an action: the rest stay off.
        mapping = self.cfg.gesture_actions
        kinds = [k for k in FACE_KINDS if prof.usable(k) and _mapped(mapping.get(k, ""))]
        hits = self.face.update(
            t, face.blendshapes, kinds, blocked=calibrating or self._yielding or frozen, talking=self._talking_before
        )
        for hit in hits:
            self._fire_face_gesture(hit, t, paused)

    def _talking_before(self, t0: float) -> bool:
        cfg = self.face.cfg
        return self.attention.talking(t0 - cfg.talk_window_s, t0 - 0.05, cfg.talk_share)

    def _fire_face_gesture(self, hit: FaceHit, t: float, paused: bool) -> None:
        name = self.cfg.gesture_actions.get(hit.kind, "")
        click_type = ClickType(self.state.dwell_click)
        probe = click_action(name, 0.0, 0.0, click_type, "eye")
        if probe is None or (paused and probe.name not in _ALLOWED_WHILE_PAUSED):
            return
        # The eyes stay open and on the target, so no lookback past the moment the face began to move.
        x, y = self._gesture_point(probe, hit.onset)
        action = click_action(name, x, y, click_type, "eye")
        self.bus.publish(FaceGesture(t, hit.kind, x, y))  # type: ignore[arg-type]
        if action is not None:
            if action.name.startswith("mouse."):
                self.learner.note_click(t, hit.onset, x, y)
            self.ctx.actions.dispatch(action)

    def _run_dwell(self, t: float, paused: bool, calibrating: bool, frozen: bool, phase: str) -> None:
        enabled = bool(self.state.dwell_enabled)
        active = enabled and not paused and not calibrating and not self._yielding and phase not in ("lost", "paused")
        if frozen and active:
            return  # the eyes are closing: neither progress nor reset
        x, y = self.engine.position
        upd = self.dwell.update(t, x, y, active)
        if upd is None:
            return
        self.bus.publish(DwellProgress(upd.x, upd.y, upd.progress, self.dwell.radius))
        if upd.fired:
            click_type = ClickType(self.state.dwell_click)
            if self.state.drag_active:
                action = Action("mouse.drag_toggle", {"x": x, "y": y}, source="dwell")
            else:
                action = click_action("click", x, y, click_type, "dwell")
            if action is not None:
                self.learner.note_click(t, t, x, y)
                self.ctx.actions.dispatch(action)

    def _run_resume(self, t: float, point: tuple[float, float] | None, paused: bool, calibrating: bool) -> None:
        upd = self.resume.update(t, point, self._zone_rect, paused and not calibrating)
        if upd is None:
            return
        self.bus.publish(GazeZoneProgress("resume", upd.progress, self._zone_rect))
        if upd.fired:
            log.info("Resume zone: resuming eye control")
            self.ctx.actions.dispatch(Action("pointer.resume", source="eye"))

    def _run_reading_scroll(
        self,
        t: float,
        gaze: tuple[float, float] | None,
        x: float,
        y: float,
        error: float | None,
        paused: bool,
        calibrating: bool,
    ) -> None:
        rs = self.reading_scroll
        if not rs.enabled and rs.direction is None:
            return
        active = self._model is not None and not paused and not calibrating and not self._yielding
        window = self._window_under(x, y, t) if rs.enabled and active else None
        self._send_scroll(rs.update(t, gaze, window, error, active))

    def _send_scroll(self, cmd: ScrollCommand | None) -> None:
        if cmd is None:
            return
        if cmd.start:
            action = Action("mouse.scroll_start", {"direction": cmd.direction, "speed": cmd.speed}, source="eye")
        else:
            action = Action("mouse.scroll_stop", {}, source="eye")
        self.ctx.actions.dispatch(action)

    def _window_under(self, x: float, y: float, t: float) -> Rect | None:
        """The window under the pointer, asked of Windows about once a second (the work area if none)."""
        rect = self._window_rect
        age = t - self._window_checked
        if rect is not None and 0.0 <= age < WINDOW_REFRESH_S and (rect.contains(x, y) or age < 0.25):
            return rect
        self._window_checked = t
        rect = None
        desktop = self.ctx.desktop
        if desktop is not None:
            try:
                # One lookup at the point, not a list of every window: this runs on the vision thread.
                found = desktop.windows.window_at(x, y)
            except Exception:
                log.debug("Looking up the window under the pointer failed", exc_info=True)
                found = None
            if found is not None and not found.minimized and not found.rect.is_empty():
                rect = found.rect
        if rect is None and self.display is not None:
            try:
                rect = self.display.monitor_at(x, y).work_rect
            except Exception:
                rect = None
        self._window_rect = rect
        return rect

    def _set_present(self, present: bool, t: float) -> None:
        if self._present is present:
            return
        self._present = present
        self.state.update(face_present=present)
        self._last_status = -1e9  # publish a status now

    # ------------------------------------------------------------------ status, diagnostics, preview

    def fps(self) -> float:
        times = self._frame_times
        if len(times) < 2:
            return 0.0
        span = times[-1] - times[0]
        return (len(times) - 1) / span if span > 0 else 0.0

    def _measure_brightness(self, image: np.ndarray, m: feat.FaceMeasures, t: float) -> None:
        if t - self._last_bright_t < 0.5:
            return
        self._last_bright_t = t
        x0, y0, x1, y1 = m.face_box
        if x1 - x0 < 4 or y1 - y0 < 4:
            return
        roi = image[y0:y1:4, x0:x1:4]
        if roi.size:
            # Rec. 601 luma of the downsampled face region.
            b, g, r = roi[..., 0].mean(), roi[..., 1].mean(), roi[..., 2].mean()
            self._brightness = float(0.114 * b + 0.587 * g + 0.299 * r)

    def face_hint(self) -> str | None:
        if not self._present:
            if self.camera_hint:
                return self.camera_hint
            return HINT_NO_FACE
        if self._distance:
            d = float(np.median(self._distance))
            if d < 35.0:
                return HINT_BACK
            if d > 90.0:
                return HINT_CLOSER
        h = self.head.last
        if h is not None and self.head.has_neutral and (abs(h.yaw) > 25.0 or abs(h.pitch) > 25.0):
            return HINT_FACE_SCREEN
        if self._brightness is not None and self._brightness < 60.0:
            return HINT_LIGHT
        return None

    def _maybe_status(self, t: float) -> None:
        if t - self._last_status < STATUS_PERIOD_S:
            return
        self._last_status = t
        h = self.head.last
        self.bus.publish(
            FaceStatus(
                present=bool(self._present),
                fps=round(self.fps(), 1),
                distance_cm=float(np.median(self._distance)) if self._distance and self._present else None,
                brightness=self._brightness if self._present else None,
                yaw_deg=h.yaw if h is not None and self._present else 0.0,
                pitch_deg=h.pitch if h is not None and self._present else 0.0,
                hint=self.face_hint(),
            )
        )

    def _maybe_diagnostics(self, t: float) -> None:
        if t - self._last_diag < DIAG_PERIOD_S or not self.bus.subscriber_count(VisionDiagnostics):
            return
        self._last_diag = t
        m = self._last_measures
        left = right = None
        if m is not None:
            left, right = feat.user_eyes(m, bool(self.cfg.camera_mirrored))
        ratio_l, ratio_r = self.closure.last_ratio
        blinks = self.gestures.blink_durations
        self.bus.publish(
            VisionDiagnostics(
                t=t,
                fps=self.fps(),
                landmark_ms=self._landmark_ms,
                pipeline_ms=self._pipeline_ms,
                camera=self.camera_name,
                face=bool(self._present),
                closure_left=self._last_closure[0],
                closure_right=self._last_closure[1],
                blink_left=left.blink if left else 0.0,
                blink_right=right.blink if right else 0.0,
                ear_ratio_left=ratio_l,
                ear_ratio_right=ratio_r,
                gaze_noise_px=self.smoother.noise if self._model is not None else None,
                fixation_radius_px=self.smoother.radius if self._model is not None else None,
                head_speed=self.head.last.speed if self.head.last else 0.0,
                yielding=self._yielding,
                model=self._model.describe() if self._model is not None else "none",
                eyes_profile=self._eye_profile is not None,
                natural_blinks=len(blinks),
                natural_blink_ms=float(np.median(blinks) * 1000.0) if blinks else None,
                reading=self.reading.reading,
                reading_scroll=self.reading_scroll.direction,
            )
        )

    def _maybe_preview(self, image: np.ndarray, face: FaceFrame | None, t: float) -> None:
        # The slack keeps every second frame of a 30 fps camera: two frame intervals can come out a
        # hair under 1/15 s, which would otherwise drop the preview to every third frame.
        if not self.preview or t - self._last_preview < PREVIEW_PERIOD_S - 0.01:
            return
        self._last_preview = t
        publish_preview(self.bus, image, face, t)


def publish_preview(bus, image: np.ndarray, face: FaceFrame | None, t: float) -> None:
    """Downscaled frame with the eye and iris points, for the setup wizard's camera view."""
    import cv2

    h, w = image.shape[:2]
    scale = PREVIEW_WIDTH / float(w)
    small = cv2.resize(image, (PREVIEW_WIDTH, max(1, int(round(h * scale)))), interpolation=cv2.INTER_AREA)
    pts = None
    if face is not None:
        pts = face.landmarks[list(PREVIEW_POINTS), :2].astype(np.float32) * np.array(
            [small.shape[1], small.shape[0]], dtype=np.float32
        )
    bus.publish(CameraFrame(small, pts, t))
