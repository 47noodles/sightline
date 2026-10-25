"""Learning from clicks: every deliberate click says where the user was really looking.

When a click lands while the gaze was resting near it, the features from
that fixation and the click point become one more training sample.  Webcam
gaze drifts as people shift in their seat; these samples let the model
follow.  The rules that keep it from learning the wrong thing:

* only clicks preceded by a steady fixation within a few model errors of the
  click point count (glancing elsewhere while clicking teaches nothing);
* for eye and dwell clicks, the fixation used is the one just before the eyes
  closed or the dwell completed, which is the one the click went to; for
  spoken clicks it is the one at the speech onset (``ClickPerformed.aim_t``),
  because by the time the click lands the eyes have often moved on;
* click samples decay (each new one multiplies the older weights by
  ``decay``) and are capped, so old habits fade;
* a refit runs in the background and is kept only if (1) its held-out
  calibration error is no worse than the current model's, beyond a small
  tolerance, and (2) a model trained without the newest clicks predicts them
  better than the current model does, which shows the clicks are teaching
  something that generalises.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

import numpy as np

from sightline.core.events import ClickPerformed

from .features import N_LM_BLOCK, N_RAW
from .filters import GazeState
from .gaze_model import GazeModel, TrainingData, cross_validate, expand, fit_correction
from .gaze_model import _Solver as _RidgeSolver

log = logging.getLogger(__name__)


@dataclass
class LearnConfig:
    enabled: bool = True
    window_s: float = 0.6  # look this far back from the moment of aiming
    min_frames: int = 4
    accept_floor_px: float = 150.0  # drift after shifting in the seat is often 100-200 px; the guard catches bad samples
    accept_cap_px: float = 260.0
    weight: float = 8.0  # one click is worth this many calibration frames (a dot gives about 20)
    decay: float = 0.985  # per new click: the weight of a click halves after about 46 more
    min_weight: float = 0.5
    max_samples: int = 300
    refit_every: int = 4
    refit_min_interval_s: float = 15.0
    tolerance: float = 0.05
    # Suggesting "fix pointer": the median error of the last few clicks has grown well past the calibration's.
    drift_clicks: int = 5
    drift_ratio: float = 1.6
    drift_margin_px: float = 50.0
    drift_window_s: float = 600.0  # only clicks from the last ten minutes count
    drift_notice_gap_s: float = 900.0  # suggest it at most every quarter of an hour


class LearnHost(Protocol):
    def current_gaze_model(self) -> GazeModel | None: ...

    def calibration_samples(self) -> TrainingData | None: ...

    def replace_gaze_model(self, model: GazeModel, clicks: TrainingData) -> None: ...

    def suggest_drift_fix(self, error_px: float) -> None: ...


@dataclass(slots=True)
class _Frame:
    t: float
    raw: np.ndarray
    lm: np.ndarray
    gaze_x: float
    gaze_y: float
    fixating: bool
    fix_x: float
    fix_y: float
    radius: float


@dataclass(slots=True)
class _Pending:
    t: float  # when vision dispatched the click
    anchor: float  # the moment the user was aiming (just before the eyes closed, or the dwell's end)
    x: float
    y: float


class ClickLearner:
    def __init__(self, host: LearnHost, clock: Callable[[], float] = time.perf_counter) -> None:
        self.cfg = LearnConfig()
        self._host = host
        self._clock = clock
        self._lock = threading.Lock()
        self._frames: deque[_Frame] = deque(maxlen=300)  # about 5 s at 60 fps: spoken clicks look back a second or more
        self._pending: deque[_Pending] = deque(maxlen=8)
        self._clicks = TrainingData.empty()
        self._new_since_fit = 0
        self._last_fit = -1e9
        self._refitting = False
        self._baseline: float | None = None  # held-out calibration error of the current model
        self.accepted = 0
        self.rejected = 0
        self.refits_kept = 0
        self.refits_rejected = 0
        self.corrections_kept = 0
        self.drift_suggestions = 0
        self._errors: deque[tuple[float, float]] = deque(maxlen=20)  # (time, distance from fixation to click)
        self._last_drift_notice = -1e18
        self.synchronous = False  # tests: refit on the calling thread

    # ------------------------------------------------------------------ state

    def set_clicks(self, clicks: TrainingData | None, baseline: float | None) -> None:
        """Load saved click samples (or clear them after a new calibration)."""
        with self._lock:
            self._clicks = clicks if clicks is not None else TrainingData.empty()
            self._baseline = baseline
            self._new_since_fit = 0
            self._errors.clear()

    def drift_fixed(self) -> None:
        """The user just fixed the pointer: the old click errors no longer say anything."""
        with self._lock:
            self._errors.clear()

    @property
    def clicks(self) -> TrainingData:
        with self._lock:
            return self._clicks

    # ------------------------------------------------------------------ vision thread

    def record(self, t: float, raw: np.ndarray, lm: np.ndarray, gaze: GazeState | None, eyes_open: bool) -> None:
        if gaze is None or not eyes_open:
            return
        frame = _Frame(t, raw, lm, gaze.x, gaze.y, gaze.fixating, gaze.fixation_x, gaze.fixation_y, gaze.radius_px)
        with self._lock:
            self._frames.append(frame)

    def note_click(self, t: float, anchor: float, x: float, y: float) -> None:
        """Vision dispatched an eye or dwell click aimed at ``anchor`` time."""
        with self._lock:
            self._pending.append(_Pending(t, anchor, x, y))

    # ------------------------------------------------------------------ bus

    def on_click(self, ev: ClickPerformed) -> None:
        if not self.cfg.enabled:
            return
        model = self._host.current_gaze_model()
        if model is None:
            return
        now = self._clock()
        with self._lock:
            anchor = now - 0.1
            if ev.aim_t is not None and ev.source not in ("eye", "dwell"):
                anchor = float(ev.aim_t)  # the speech onset: where the user looked when they began to speak
            if ev.source in ("eye", "dwell"):
                match = None
                for p in reversed(self._pending):
                    # The desktop may snap the click onto the nearest button, so allow some distance.
                    if now - p.t < 5.0 and np.hypot(p.x - ev.x, p.y - ev.y) < 100.0:
                        match = p
                        break
                if match is None:
                    return
                self._pending.remove(match)
                anchor = match.anchor
            frames = [f for f in self._frames if anchor - self.cfg.window_s <= f.t <= anchor and f.fixating]
            fixation = self._fixation(frames)
            if fixation is None:
                self.rejected += 1
                return
            centre, frames = fixation
            drift = self._note_error(now, float(np.hypot(centre[0] - ev.x, centre[1] - ev.y)), model)
            sample = self._sample(frames, centre, ev, model)
            if sample is None:
                self.rejected += 1
            else:
                self._add(sample)
                self.accepted += 1
            due = (
                sample is not None
                and self._new_since_fit >= self.cfg.refit_every
                and now - self._last_fit >= self.cfg.refit_min_interval_s
            )
        if drift is not None:
            self.drift_suggestions += 1
            log.info("Click learning: recent clicks are %.0f px from the gaze; suggesting a pointer fix", drift)
            self._host.suggest_drift_fix(drift)
        if due:
            self._start_refit()

    def _fixation(self, frames: list[_Frame]) -> tuple[np.ndarray, list[_Frame]] | None:
        """Centre of the fixation the user aimed with, and its frames; None without a steady one."""
        cfg = self.cfg
        if len(frames) < cfg.min_frames:
            return None
        # All from one fixation: the estimate stayed put while the user aimed.
        fx = np.array([(f.fix_x, f.fix_y) for f in frames])
        centre = np.median(fx, axis=0)
        radius = max(f.radius for f in frames)
        if float(np.max(np.hypot(*(fx - centre).T))) > radius:
            frames = [f for f, p in zip(frames, fx) if np.hypot(*(p - centre)) <= radius]
            if len(frames) < cfg.min_frames:
                return None
        return centre, frames

    def _note_error(self, now: float, error: float, model: GazeModel) -> float | None:
        """Remember how far the click was from the gaze; the median error to report when it has grown (lock held)."""
        cfg = self.cfg
        self._errors.append((now, min(error, 400.0)))  # one wild click must not dominate
        recent = [e for t, e in self._errors if now - t <= cfg.drift_window_s][-cfg.drift_clicks :]
        if len(recent) < cfg.drift_clicks:
            return None
        base = model.accuracy_px or 60.0
        median = float(np.median(recent))
        if median <= max(cfg.drift_ratio * base, base + cfg.drift_margin_px):
            return None
        if now - self._last_drift_notice < cfg.drift_notice_gap_s:
            return None
        self._last_drift_notice = now
        return median

    def _sample(self, frames: list[_Frame], centre: np.ndarray, ev: ClickPerformed, model: GazeModel) -> TrainingData | None:
        cfg = self.cfg
        accept = min(cfg.accept_cap_px, max(cfg.accept_floor_px, 2.5 * (model.accuracy_px or cfg.accept_floor_px)))
        if float(np.hypot(centre[0] - ev.x, centre[1] - ev.y)) > accept:
            return None
        raw = np.mean([f.raw for f in frames], axis=0)
        lm = np.mean([f.lm for f in frames], axis=0)
        return TrainingData(
            raw.reshape(1, N_RAW), lm.reshape(1, N_LM_BLOCK), np.array([[ev.x, ev.y]]), np.array([-1]),
            np.array([cfg.weight]),
        )  # fmt: skip

    def _add(self, sample: TrainingData) -> None:
        cfg = self.cfg
        clicks = self._clicks
        if len(clicks):
            clicks = TrainingData(clicks.raw, clicks.lm, clicks.y, clicks.groups, clicks.weights * cfg.decay,
                                  clicks.distance_cm)  # fmt: skip
            clicks = clicks.subset(clicks.weights >= cfg.min_weight)
        clicks = clicks.concat(sample)
        if len(clicks) > cfg.max_samples:
            clicks = clicks.subset(np.arange(len(clicks)) >= len(clicks) - cfg.max_samples)
        self._clicks = clicks
        self._new_since_fit += 1

    # ------------------------------------------------------------------ refit

    def _start_refit(self) -> None:
        with self._lock:
            if self._refitting:
                return
            self._refitting = True
            self._last_fit = self._clock()
            self._new_since_fit = 0
        if self.synchronous:
            self._refit()
        else:
            threading.Thread(target=self._refit, name="sightline-gaze-refit", daemon=True).start()

    def refit_now(self) -> bool:
        """Run a refit on this thread (tests and the replay tool).  Returns whether the new model was kept."""
        with self._lock:
            self._refitting = True
            self._new_since_fit = 0
        return self._refit()

    def _refit(self) -> bool:
        try:
            return self._refit_inner()
        except Exception:
            log.exception("Learning from clicks failed")
            return False
        finally:
            with self._lock:
                self._refitting = False

    def _refit_inner(self) -> bool:
        model = self._host.current_gaze_model()
        cal = self._host.calibration_samples()
        with self._lock:
            clicks = self._clicks
            baseline = self._baseline
        if model is None or len(clicks) < 3:
            return False
        base = model
        refitted = False
        if cal is not None and cal.n_targets >= 3:
            candidate = self._full_refit(model, cal, clicks, baseline)
            if candidate is not None:
                base, refitted = candidate, True
        # Drift correction on top of whichever calibrated model is now current.
        pred = base.predict_base(clicks.raw, clicks.lm)
        corr, err_with, err_without = fit_correction(pred, clicks.y, clicks.weights)
        if corr is not None:
            self.corrections_kept += 1
            log.info("Click learning: drift correction kept (clicks %.0f px with it, %.0f px without)", err_with, err_without)
        elif not refitted or len(clicks) < 4:
            # Not enough evidence for a correction of its own: keep the one in use (a "fix pointer" offset,
            # say).  After a refit with four or more clicks, the refit itself accounts for what they show.
            corr = model.correction
        same = (corr is None and model.correction is None) or (
            corr is not None and model.correction is not None and np.allclose(corr, model.correction)
        )
        if not refitted and same:
            return False
        final = replace(base, correction=corr, meta=dict(base.meta, learned_clicks=len(clicks), updated=time.time()))
        self._host.replace_gaze_model(final, clicks)
        return True

    def _full_refit(
        self, model: GazeModel, cal: TrainingData, clicks: TrainingData, baseline: float | None
    ) -> GazeModel | None:
        """Refit the calibrated model with the clicks added; None when a guard says no."""
        cfg = self.cfg
        fs = model.feature_set
        data = cal.concat(clicks)
        x_all = expand(data.raw, data.lm, fs)
        cv = cross_validate(x_all, data.y, data.weights, data.groups)
        if cv is None:
            return None
        if baseline is None:
            baseline = model.accuracy_px
        # Guard 1: the calibration targets, held out in turn, must not get worse.
        if baseline is not None and cv.accuracy_px > baseline * (1.0 + cfg.tolerance) + 2.0:
            self.refits_rejected += 1
            log.info("Click learning: refit rejected (held-out error %.0f px vs %.0f px)", cv.accuracy_px, baseline)
            return None
        # Guard 2: older clicks must help predict the newest ones better than the current model does.
        n_recent = max(2, len(clicks) // 4)
        recent = np.arange(len(clicks)) >= len(clicks) - n_recent
        older = cal.concat(clicks.subset(~recent))
        held = clicks.subset(recent)
        x_old = expand(older.raw, older.lm, fs)
        solver = _RidgeSolver(x_old, older.y, older.weights)
        pred = solver.predict(expand(held.raw, held.lm, fs), solver.coef(cv.lam_x, cv.lam_y))
        err_candidate = float(np.average(np.hypot(*(pred - held.y).T), weights=held.weights))
        pred_cur = model.predict_base(held.raw, held.lm)
        err_current = float(np.average(np.hypot(*(pred_cur - held.y).T), weights=held.weights))
        if err_candidate >= err_current:
            self.refits_rejected += 1
            log.info("Click learning: refit rejected (newest clicks %.0f px with it, %.0f px without)",
                     err_candidate, err_current)  # fmt: skip
            return None
        fit = _RidgeSolver(x_all, data.y, data.weights).fit(cv.lam_x, cv.lam_y)
        with self._lock:
            self._baseline = cv.accuracy_px
        self.refits_kept += 1
        log.info("Click learning: model refitted with %d clicks (held-out %.0f px)", len(clicks), cv.accuracy_px)
        return GazeModel(
            feature_set=fs,
            fit=fit,
            lam_x=cv.lam_x,
            lam_y=cv.lam_y,
            accuracy_px=cv.accuracy_px,
            precision_px=cv.precision_px,
            per_target=cv.per_target,
            targets=cv.targets,
            n_samples=len(data),
            n_targets=cal.n_targets,
            meta=dict(model.meta),
        )
