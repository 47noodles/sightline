"""The calibration API the setup and calibration wizards drive.

The wizard runs on the GUI thread and only says what is on screen:
``set_target(x, y, collecting)`` for dots, ``set_eye_label`` for "close your
eyes" and the like.  Every processed camera frame arrives here from the
vision thread with its capture time.  Alignment happens at ``finish`` by
timestamp: a frame belongs to whatever the screen showed when it was
captured (less the camera's own delay), so frames still in flight when the
wizard moved on are not mislabelled.

Kinds:

* ``gaze``: dots (still or moving).  Fits the gaze model (see gaze_model.py).
* ``validate``: dots, but only measures the current model.
* ``eyes``: open, closed and wink periods.  Fits per-eye closure models.
* ``head``: dots the user turns their head towards.  Fits head pointer gains.
* ``gestures``: the resting face, each face gesture, and about half a minute
  each of talking and reading (``set_eye_label``).  Fits the face gesture
  triggers and scores them (see face_gestures.py).
* ``drift``: one dot (or a few) looked at for about 1.5 s.  Shifts the
  current gaze model by the mean offset ("fix pointer"); no refit.

The gaze and validate reports carry the per-dot errors with the dots'
positions (``details["target_points"]``) and the comfortable control size
for that accuracy (``details["comfort"]``, a sentence for the wizard).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from sightline.core.interfaces import CalibrationKind, CalibrationReport, EyeLabel
from sightline.core.types import Monitor

from .accuracy import comfort_size
from .eyes import EyeFitResult, EyeProfile, EyeSample, GestureConfig, fit_eye_profile
from .face_gestures import FaceGestureConfig, FaceGestureProfile, TrainingFrame, fit_face_gestures
from .features import N_LM_BLOCK, N_RAW, RAW_INDEX
from .gaze_model import FEATURE_SETS, GazeModel, TrainingData, expand, fit_model, gaze_xy, reject_outliers
from .gaze_model import _Solver as _RidgeSolver
from .head import HeadProfile, fit_head_profile

log = logging.getLogger(__name__)

CAMERA_LATENCY_S = 0.04  # capture timestamps are taken when the frame arrives, a little after exposure
STATIC_SETTLE_S = 0.10  # the wizard already waits for the eyes to land; this trims the tail of the saccade
PURSUIT_SETTLE_S = 0.35  # the eyes need a moment to lock on to a moving dot
PURSUIT_CHUNK_S = 0.5  # moving-dot samples are grouped in half-second chunks for cross-validation
PURSUIT_LAGS = (0.0, 0.033, 0.066, 0.1, 0.133, 0.166, 0.2)
EYE_REACTION_S = 0.6  # after "close your eyes", give the user time to do it
DRIFT_SETTLE_S = 0.3  # "fix pointer": the dot appears where the user was not looking, so allow the eyes to land
DRIFT_MIN_FRAMES = 8  # per dot
DRIFT_ON_TARGET_PX = 8.0  # smaller offsets are reported as "already on target"
BLINK_REJECT = 0.35  # gaze samples with either eye closing past this are useless
DEFAULT_DISTANCE_CM = 60.0
DEFAULT_PITCH_MM = 0.23  # pixel pitch when the monitor does not report its size


@dataclass(slots=True)
class FrameRecord:
    t: float
    raw: np.ndarray
    lm: np.ndarray
    closure: tuple[float, float]  # user's left, right
    blink: tuple[float, float]
    ear: tuple[float, float]
    head_xy: tuple[float, float]  # face units relative to neutral, user terms
    pose: tuple[float, float]  # yaw, pitch relative to neutral, user terms
    distance_cm: float | None
    gaze: tuple[float, float] | None  # the active model's unsmoothed estimate
    blendshapes: np.ndarray | None = None  # MediaPipe's 52 scores, for the face gesture trainer
    mouth: tuple[float, float, float] | None = None  # attention.mouth_signals, for its speech check


@dataclass
class _Segment:
    start: float
    end: float
    path: list[tuple[float, float, float]] = field(default_factory=list)  # (t, x, y)
    pursuit: bool = False

    def target_at(self, t: float) -> tuple[float, float]:
        if not self.pursuit or len(self.path) == 1:
            return self.path[0][1], self.path[0][2]
        ts = [p[0] for p in self.path]
        i = int(np.searchsorted(ts, t, side="right")) - 1
        if i < 0:
            return self.path[0][1], self.path[0][2]
        if i >= len(self.path) - 1:
            return self.path[-1][1], self.path[-1][2]
        t0, x0, y0 = self.path[i]
        t1, x1, y1 = self.path[i + 1]
        a = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
        return x0 + a * (x1 - x0), y0 + a * (y1 - y0)


class CalibrationHost(Protocol):
    """What the calibration needs from the running vision pipeline."""

    def current_gaze_model(self) -> GazeModel | None: ...

    def live_gaze(self) -> tuple[float, float] | None: ...

    def monitors(self) -> list[Monitor]: ...

    def mirrored(self) -> bool: ...

    def activate_gaze(self, model: GazeModel, data: TrainingData) -> None: ...

    def activate_eyes(self, profile: EyeProfile) -> None: ...

    def activate_head(self, profile: HeadProfile) -> None: ...

    def activate_face(self, profile: FaceGestureProfile) -> None: ...

    def apply_drift(self, dx: float, dy: float) -> GazeModel | None: ...

    def gesture_configs(self) -> tuple[FaceGestureConfig, GestureConfig]: ...

    def calibration_changed(self, active: bool) -> None: ...


class CalibrationController:
    """Implements ``CalibrationAPI``.  Methods are safe from any thread; ``on_frame`` runs on the vision thread."""

    def __init__(self, host: CalibrationHost, clock: Callable[[], float] = time.perf_counter) -> None:
        self._host = host
        self._clock = clock
        self._lock = threading.Lock()
        self._kind: CalibrationKind | None = None
        self._events: list[tuple[float, float, float, bool]] = []
        self._labels: list[tuple[float, str | None]] = []
        self._frames: list[FrameRecord] = []
        self.latency_s = CAMERA_LATENCY_S

    @property
    def active(self) -> bool:
        return self._kind is not None

    @property
    def kind(self) -> CalibrationKind | None:
        return self._kind

    # ------------------------------------------------------------------ CalibrationAPI

    def begin(self, kind: CalibrationKind) -> None:
        if kind not in ("eyes", "gaze", "validate", "head", "gestures", "drift"):
            raise ValueError(f"unknown calibration kind {kind!r}")
        with self._lock:
            self._kind = kind
            self._events = []
            self._labels = []
            self._frames = []
        log.info("Calibration started: %s", kind)
        self._host.calibration_changed(True)

    def set_target(self, x: float, y: float, collecting: bool) -> None:
        with self._lock:
            if self._kind is None:
                return
            self._events.append((self._clock(), float(x), float(y), bool(collecting)))

    def set_eye_label(self, label: EyeLabel | None) -> None:
        with self._lock:
            if self._kind is None:
                return
            self._labels.append((self._clock(), label))

    def sample_count(self) -> int:
        with self._lock:
            if self._kind is None:
                return 0
            now = self._clock()
            if self._kind in ("eyes", "gestures"):
                if not self._labels or self._labels[-1][1] is None:
                    return 0
                since = self._labels[-1][0] + EYE_REACTION_S
                return sum(1 for f in reversed(self._frames) if f.t - self.latency_s >= since)
            if not self._events or not self._events[-1][3]:
                return 0
            # Start of the current collecting run.
            start = self._events[-1][0]
            for t, _x, _y, col in reversed(self._events):
                if not col:
                    break
                start = t
            drift = self._kind == "drift"
            since = start + (DRIFT_SETTLE_S if drift else STATIC_SETTLE_S)
            count = 0
            for f in reversed(self._frames):
                te = f.t - self.latency_s
                if te < since:
                    break
                if te <= now and max(f.closure) < BLINK_REJECT and (f.gaze is not None or not drift):
                    count += 1
            return count

    def live_estimate(self) -> tuple[float, float] | None:
        return self._host.live_gaze()

    def cancel(self) -> None:
        with self._lock:
            was = self._kind
            self._kind = None
            self._frames = []
        if was is not None:
            log.info("Calibration cancelled: %s", was)
            self._host.calibration_changed(False)

    def finish(self) -> CalibrationReport:
        with self._lock:
            kind = self._kind
            events = list(self._events)
            labels = list(self._labels)
            frames = list(self._frames)
            self._kind = None
            self._frames = []
        if kind is None:
            return CalibrationReport(False, "none", "No calibration was running.")
        try:
            if kind == "gaze":
                report = self._finish_gaze(events, frames)
            elif kind == "validate":
                report = self._finish_validate(events, frames)
            elif kind == "eyes":
                report = self._finish_eyes(labels, frames)
            elif kind == "gestures":
                report = self._finish_gestures(labels, frames)
            elif kind == "drift":
                report = self._finish_drift(events, frames)
            else:
                report = self._finish_head(events, frames)
        except Exception:
            log.exception("Calibration fit failed")
            report = CalibrationReport(False, kind, "Something went wrong. Please try again.")
        finally:
            self._host.calibration_changed(False)
        log.info("Calibration %s finished: ok=%s %s", kind, report.ok, report.message)
        return report

    # ------------------------------------------------------------------ vision thread

    def on_frame(self, frame: FrameRecord) -> None:
        with self._lock:
            if self._kind is not None:
                self._frames.append(frame)

    # ------------------------------------------------------------------ alignment

    def _segments(self, events: list[tuple[float, float, float, bool]], end_t: float) -> list[_Segment]:
        segments: list[_Segment] = []
        cur: _Segment | None = None
        last_change = 0.0
        for t, x, y, col in events:
            if not col:
                if cur is not None:
                    cur.end = t
                    segments.append(cur)
                    cur = None
                continue
            if cur is None:
                cur = _Segment(t, end_t, [(t, x, y)])
                last_change = t
                continue
            _, lx, ly = cur.path[-1]
            if math.hypot(x - lx, y - ly) < 1.0:
                continue
            if t - last_change >= 0.25 and math.hypot(x - lx, y - ly) > 50.0 and not cur.pursuit:
                # A new still dot without a pause in between: close the old target.
                cur.end = t
                segments.append(cur)
                cur = _Segment(t, end_t, [(t, x, y)])
            else:
                cur.pursuit = True
                cur.path.append((t, x, y))
            last_change = t
        if cur is not None:
            segments.append(cur)
        return segments

    def _assign(
        self, segments: list[_Segment], frames: list[FrameRecord], lag: float
    ) -> list[tuple[FrameRecord, float, float, int]]:
        out = []
        for f in frames:
            te = f.t - self.latency_s
            for si, s in enumerate(segments):
                settle = PURSUIT_SETTLE_S if s.pursuit else STATIC_SETTLE_S
                if s.start + settle <= te < s.end:
                    if s.pursuit:
                        x, y = s.target_at(te - lag)
                        group = si * 1000 + int((te - s.start) / PURSUIT_CHUNK_S)
                    else:
                        x, y = s.target_at(te)
                        group = si * 1000
                    out.append((f, x, y, group))
                    break
        return out

    # ------------------------------------------------------------------ gaze

    def _gaze_data(
        self, events: list[tuple[float, float, float, bool]], frames: list[FrameRecord]
    ) -> tuple[TrainingData, dict]:
        details: dict = {}
        end_t = frames[-1].t if frames else self._clock()
        segments = self._segments(events, end_t)
        details["targets_shown"] = len(segments)
        lag = 0.0
        if any(s.pursuit for s in segments):
            lag = self._pursuit_lag(segments, frames)
            details["pursuit_lag_ms"] = round(lag * 1000)
        assigned = self._assign(segments, frames, lag)
        if not assigned:
            return TrainingData.empty(), details
        recs = [a[0] for a in assigned]
        raw = np.array([r.raw for r in recs])
        lmb = np.array([r.lm for r in recs])
        y = np.array([(a[1], a[2]) for a in assigned])
        groups = np.array([a[3] for a in assigned])
        dist = np.array([r.distance_cm if r.distance_cm is not None else np.nan for r in recs])
        closure = np.array([max(r.closure) for r in recs])
        t = np.array([r.t for r in recs])
        keep = closure < BLINK_REJECT
        # Frames just after a blink still have the lids settling.
        for i in np.flatnonzero(~keep):
            near = np.abs(t - t[i]) <= 0.1
            keep &= ~near
        details["rejected_blinks"] = int((~keep).sum())
        # Saccades: frames whose iris position jumped compared with the frame before.
        g = gaze_xy(raw)
        step = np.zeros(len(g))
        step[1:] = np.hypot(*(np.diff(g, axis=0).T))
        same = np.zeros(len(g), dtype=bool)
        same[1:] = groups[1:] == groups[:-1]
        typical = float(np.median(step[same])) if same.any() else 0.0
        jumpy = same & (step > max(4.0 * typical, 0.02))
        keep &= ~jumpy
        details["rejected_saccades"] = int(jumpy.sum())
        # Outliers per target, judged only against the frames still in play.
        idx = np.flatnonzero(keep)
        if len(idx):
            fine = reject_outliers(raw[idx], groups[idx])
            keep[idx[~fine]] = False
        data = TrainingData(raw, lmb, y, groups, np.ones(len(y)), dist).subset(keep)
        # Drop targets with too few good frames to say anything.
        counts = {g_: int((data.groups == g_).sum()) for g_ in np.unique(data.groups)}
        good = np.array([counts[g_] >= 4 for g_ in data.groups], dtype=bool) if len(data) else np.zeros(0, bool)
        data = data.subset(good)
        details["samples"] = len(data)
        details["targets"] = data.n_targets
        return data, details

    def _pursuit_lag(self, segments: list[_Segment], frames: list[FrameRecord]) -> float:
        """How far the eyes trail a moving dot: the lag that best explains the data."""
        pursuit = [s for s in segments if s.pursuit]
        best, best_err = 0.0, math.inf
        for lag in PURSUIT_LAGS:
            assigned = self._assign(pursuit, frames, lag)
            if len(assigned) < 30:
                return 0.08
            raw = np.array([a[0].raw for a in assigned])
            lmb = np.array([a[0].lm for a in assigned])
            y = np.array([(a[1], a[2]) for a in assigned])
            x = expand(raw, lmb, "basic")
            solver = _RidgeSolver(x, y, np.ones(len(y)))
            pred = solver.predict(x, solver.coef(1e-3, 1e-3))
            err = float(np.sqrt(np.mean(np.sum((pred - y) ** 2, axis=1))))
            if err < best_err:
                best, best_err = lag, err
        return best

    def _finish_gaze(self, events, frames) -> CalibrationReport:
        data, details = self._gaze_data(events, frames)
        if data.n_targets < 4:
            return CalibrationReport(
                False,
                "gaze",
                "Not enough data. Keep your face in view and your eyes on each dot, then try again.",
                samples=len(data),
                details=details,
            )
        outcome = fit_model(data, FEATURE_SETS)
        model = outcome.model
        if model is None:
            return CalibrationReport(False, "gaze", "Could not work out where you were looking. Please try again.",
                                     samples=len(data), details=details)  # fmt: skip
        details["feature_set"] = model.feature_set
        details["candidates"] = {k: round(v, 1) for k, v in outcome.candidates.items()}
        details["lambda"] = (model.lam_x, model.lam_y)
        details["precision_px"] = model.precision_px
        yaw_pitch = data.raw[:, [RAW_INDEX["yaw"], RAW_INDEX["pitch"]]]
        details["head_compensation"] = bool(float(np.max(np.std(yaw_pitch, axis=0))) >= 3.0)
        monitors = self._host.monitors()
        model.meta["monitors"] = _monitor_fingerprint(monitors, data.y)
        model.meta["mirrored"] = self._host.mirrored()
        error_px = model.accuracy_px
        if error_px is None:
            # Too few targets to hold one out: report the fit error honestly as such.
            pred = model.predict_many(data.raw, data.lm)
            error_px = float(np.mean(np.hypot(*(pred - data.y).T)))
            details["error_is_training_error"] = True
        error_deg = to_degrees(error_px, _pitch_mm(monitors, data.y), _median_distance(data.distance_cm))
        width = _span(monitors, data.y)
        if error_deg > 6.0 or error_px > 0.25 * width:
            return CalibrationReport(
                False,
                "gaze",
                f"The result was too rough to use (about {error_deg:.0f} degrees off). "
                "Sit steady with light on your face, then try again.",
                error_px,
                error_deg,
                tuple(model.per_target),
                len(data),
                details,
            )
        self._host.activate_gaze(model, data)
        details["target_points"] = [[round(x, 1), round(y, 1)] for x, y in model.targets]
        details.update(_comfort_details(error_px, monitors, data.y))
        message = f"Average error {error_px:.0f} px (about {error_deg:.1f} degrees)."
        if error_deg > 3.0:
            message += " That is a bit rough; calibrating again with more light may help."
        return CalibrationReport(True, "gaze", message, error_px, error_deg, tuple(model.per_target), len(data), details)

    def _finish_validate(self, events, frames) -> CalibrationReport:
        if self._host.current_gaze_model() is None:
            return CalibrationReport(False, "validate", "Calibrate eye pointing first.")
        end_t = frames[-1].t if frames else self._clock()
        segments = self._segments(events, end_t)
        assigned = [a for a in self._assign(segments, frames, 0.08) if a[0].gaze is not None]
        assigned = [a for a in assigned if max(a[0].closure) < BLINK_REJECT]
        groups: dict[int, list[tuple[FrameRecord, float, float]]] = {}
        for f, x, y, g in assigned:
            groups.setdefault(g, []).append((f, x, y))
        per_point = []
        spreads = []
        targets = []
        for items in groups.values():
            if len(items) < 4:
                continue
            est = np.array([f.gaze for f, _, _ in items], dtype=np.float64)
            tgt = np.array([(x, y) for _, x, y in items])
            centre = np.median(est, axis=0)
            per_point.append(float(np.hypot(*(centre - tgt.mean(axis=0)))))
            spreads.append(float(np.sqrt(np.mean(np.sum((est - centre) ** 2, axis=1)))))
            targets.append(tgt.mean(axis=0))
        if len(per_point) < 2:
            return CalibrationReport(False, "validate", "Not enough data. Keep your eyes on each dot and try again.",
                                     samples=len(assigned))  # fmt: skip
        monitors = self._host.monitors()
        tgt_arr = np.array(targets)
        dist = np.array([f.distance_cm if f.distance_cm is not None else np.nan for f, *_ in assigned])
        error_px = float(np.mean(per_point))
        error_deg = to_degrees(error_px, _pitch_mm(monitors, tgt_arr), _median_distance(dist))
        details = {"precision_px": float(np.mean(spreads)), "targets": len(per_point)}
        details["target_points"] = [[round(float(x), 1), round(float(y), 1)] for x, y in targets]
        details.update(_comfort_details(error_px, monitors, tgt_arr))
        return CalibrationReport(
            True,
            "validate",
            f"Average error {error_px:.0f} px (about {error_deg:.1f} degrees).",
            error_px,
            error_deg,
            tuple(per_point),
            len(assigned),
            details,
        )

    # ------------------------------------------------------------------ eyes

    def _labelled(
        self, labels: list[tuple[float, str | None]], frames: list[FrameRecord]
    ) -> list[tuple[FrameRecord, str, int]]:
        """Frames with the label on screen when they were captured, once the user had time to react.

        The third item numbers the requests: frames of one request share it.
        """
        out = []
        for f in frames:
            te = f.t - self.latency_s
            label = None
            since = -math.inf
            index = -1
            for i, (t, lab) in enumerate(labels):
                if t <= te:
                    label, since, index = lab, t, i
                else:
                    break
            if label is None or te - since < EYE_REACTION_S:
                continue
            out.append((f, label, index))
        return out

    def _finish_eyes(self, labels: list[tuple[float, str | None]], frames: list[FrameRecord]) -> CalibrationReport:
        samples: list[EyeSample] = []
        for f, label, _ in self._labelled(labels, frames):
            samples.append(EyeSample(label, f.blink[0], f.blink[1], f.ear[0], f.ear[1]))
        result: EyeFitResult = fit_eye_profile(samples, previous_mirrored=self._host.mirrored())
        if result.ok and result.profile is not None:
            self._host.activate_eyes(result.profile)
        return CalibrationReport(result.ok, "eyes", result.message, samples=len(samples), details=result.details)

    # ------------------------------------------------------------------ face gestures

    def _finish_gestures(self, labels: list[tuple[float, str | None]], frames: list[FrameRecord]) -> CalibrationReport:
        training = [
            TrainingFrame(f.t, label, index, f.blendshapes, f.mouth, f.closure, f.pose)
            for f, label, index in self._labelled(labels, frames)
            if f.blendshapes is not None
        ]
        face_cfg, eye_cfg = self._host.gesture_configs()
        result = fit_face_gestures(training, face_cfg, eye_cfg)
        if result.ok and result.profile is not None:
            self._host.activate_face(result.profile)
        return CalibrationReport(result.ok, "gestures", result.message, samples=len(training), details=result.details)

    # ------------------------------------------------------------------ fix pointer

    def _finish_drift(self, events, frames) -> CalibrationReport:
        if self._host.current_gaze_model() is None:
            return CalibrationReport(False, "drift", "Calibrate eye pointing first.")
        end_t = frames[-1].t if frames else self._clock()
        offsets: list[tuple[float, float]] = []
        targets: list[tuple[float, float]] = []
        distances: list[float] = []
        used = 0
        for s in self._segments(events, end_t):
            if s.pursuit:
                continue
            items = [
                f
                for f in frames
                if s.start + DRIFT_SETTLE_S <= f.t - self.latency_s < s.end
                and f.gaze is not None
                and max(f.closure) < BLINK_REJECT
            ]
            if len(items) < DRIFT_MIN_FRAMES:
                continue
            # The median ignores the odd glance away from the dot.
            est = np.median(np.array([f.gaze for f in items], dtype=np.float64), axis=0)
            tx, ty = s.target_at(s.start)
            offsets.append((tx - float(est[0]), ty - float(est[1])))
            targets.append((tx, ty))
            distances.extend(f.distance_cm for f in items if f.distance_cm is not None)
            used += len(items)
        if not offsets:
            return CalibrationReport(
                False, "drift", "Not enough data. Look at the dot until it disappears, then try again.", samples=used
            )
        dx, dy = (float(v) for v in np.mean(np.array(offsets), axis=0))
        size = math.hypot(dx, dy)
        monitors = self._host.monitors()
        tgt = np.array(targets)
        deg = to_degrees(size, _pitch_mm(monitors, tgt), _median_distance(np.array(distances, dtype=np.float64)))
        per_point = tuple(math.hypot(ox, oy) for ox, oy in offsets)
        details = {"offset": [round(dx, 1), round(dy, 1)], "dots": len(offsets)}
        if size > max(400.0, 0.25 * _span(monitors, tgt)):
            # That is not drift: the model no longer fits (the camera or the screen moved).
            return CalibrationReport(
                False, "drift", "The pointer is too far off to fix this way. Please calibrate again.",
                size, deg, per_point, used, details,
            )  # fmt: skip
        self._host.apply_drift(dx, dy)
        if size < DRIFT_ON_TARGET_PX:
            message = "The pointer was already on target."
        else:
            message = f"Pointer corrected by {size:.0f} px."
        return CalibrationReport(True, "drift", message, size, deg, per_point, used, details)

    # ------------------------------------------------------------------ head

    def _finish_head(self, events, frames) -> CalibrationReport:
        end_t = frames[-1].t if frames else self._clock()
        segments = self._segments(events, end_t)
        # Turning the head takes longer than moving the eyes: use the second half of each dot.
        assigned = []
        for si, s in enumerate(segments):
            mid = s.start + 0.5 * (s.end - s.start)
            for f in frames:
                te = f.t - self.latency_s
                if mid <= te < s.end:
                    x, y = s.target_at(te)
                    assigned.append((f, x, y, si))
        groups: dict[int, list] = {}
        for f, x, y, g in assigned:
            groups.setdefault(g, []).append((f, x, y))
        head_xy, poses, targets = [], [], []
        for items in groups.values():
            if len(items) < 3:
                continue
            head_xy.append(np.median([f.head_xy for f, _, _ in items], axis=0))
            poses.append(np.median([f.pose for f, _, _ in items], axis=0))
            targets.append(np.mean([(x, y) for _, x, y in items], axis=0))
        profile, message, details = fit_head_profile(
            np.array(head_xy).reshape(-1, 2),
            np.array(poses).reshape(-1, 2),
            np.array(targets).reshape(-1, 2),
            mirrored=self._host.mirrored(),
        )
        if profile is None:
            return CalibrationReport(False, "head", message, samples=len(assigned), details=details)
        self._host.activate_head(profile)
        return CalibrationReport(True, "head", message, samples=len(assigned), details=details)


# --------------------------------------------------------------------------- units


def to_degrees(error_px: float, pitch_mm: float, distance_cm: float) -> float:
    """Visual angle of an on-screen error, given the pixel pitch and the viewing distance."""
    return math.degrees(math.atan2(error_px * pitch_mm, distance_cm * 10.0))


def _monitor_for(monitors: list[Monitor], points: np.ndarray) -> Monitor | None:
    if not monitors:
        return None
    if len(points) == 0:
        return next((m for m in monitors if m.primary), monitors[0])
    cx, cy = float(np.mean(points[:, 0])), float(np.mean(points[:, 1]))
    for m in monitors:
        if m.rect.contains(cx, cy):
            return m
    return min(monitors, key=lambda m: m.rect.distance_to(cx, cy))


def _pitch_mm(monitors: list[Monitor], points: np.ndarray) -> float:
    m = _monitor_for(monitors, points)
    if m is None or not m.size_mm or m.rect.w <= 0 or m.rect.h <= 0:
        return DEFAULT_PITCH_MM
    w_mm, h_mm = m.size_mm
    if w_mm <= 0 or h_mm <= 0:
        return DEFAULT_PITCH_MM
    return 0.5 * (w_mm / m.rect.w + h_mm / m.rect.h)


def _span(monitors: list[Monitor], points: np.ndarray) -> float:
    m = _monitor_for(monitors, points)
    return m.rect.w if m is not None else 1920.0


def _comfort_details(error_px: float, monitors: list[Monitor], points: np.ndarray) -> dict:
    """The comfortable control size for this accuracy, for the wizard's report."""
    m = _monitor_for(monitors, points)
    comfort = comfort_size(error_px, _pitch_mm(monitors, points), m.scale if m is not None else 1.0)
    return {
        "comfort_px": round(comfort.diameter_px),
        "comfort_mm": round(comfort.diameter_mm, 1),
        "comfort": comfort.message,
    }


def _median_distance(distance_cm: np.ndarray) -> float:
    d = distance_cm[np.isfinite(distance_cm)] if len(distance_cm) else distance_cm
    if len(d) == 0:
        return DEFAULT_DISTANCE_CM
    return float(min(max(np.median(d), 25.0), 150.0))


def _monitor_fingerprint(monitors: list[Monitor], points: np.ndarray) -> list[list[float]]:
    """Rectangles of the monitors the calibration dots were on.  If those change, the model no longer fits."""
    used = []
    for m in monitors:
        if any(m.rect.contains(float(x), float(y)) for x, y in points[:: max(1, len(points) // 50)]):
            used.append([m.rect.x, m.rect.y, m.rect.w, m.rect.h])
    return used


def fingerprint_matches(saved: list[list[float]] | None, monitors: list[Monitor]) -> bool:
    if not saved:
        return True
    current = [[m.rect.x, m.rect.y, m.rect.w, m.rect.h] for m in monitors]
    return all(any(np.allclose(s, c) for c in current) for s in saved)


__all__ = [
    "CalibrationController",
    "CalibrationHost",
    "FrameRecord",
    "N_LM_BLOCK",
    "N_RAW",
    "fingerprint_matches",
    "to_degrees",
]
