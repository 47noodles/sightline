"""Gaze model: features to screen pixels by ridge regression.

EyeTrax (MIT, https://github.com/ck-zhang/EyeTrax, eyetrax/models/ridge.py and
models/base.py) fits scikit-learn's ``Ridge`` on standardised features.  This
module keeps that recipe but solves it in closed form with NumPy, adds sample
weights, and chooses the penalty for each screen axis by leave-one-target-out
cross-validation.  Frames of one fixation are near copies of each other, so a
calibration of nine dots holds nine observations, not three hundred; scoring
held-out targets is what stops the fit from memorising fixations
(shaal/eye-tracker ADR-0019, MIT).  Standardising first means one penalty
suits every feature, and a feature that hardly varied during calibration
(a still head) is shrunk to nothing instead of being extrapolated.

Several feature sets compete by the same held-out error; the simplest one
within a few percent of the best wins.  Models are saved as ``.npz`` plus
JSON, never pickle.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .features import N_LM_BLOCK, N_RAW, RAW_INDEX

log = logging.getLogger(__name__)

FEATURE_VERSION = 1
FEATURE_SETS: tuple[str, ...] = ("basic", "head", "per_eye", "landmarks")
# Distinct targets a feature set needs before it may compete: wider sets need more observations.
MIN_TARGETS = {"basic": 4, "head": 8, "per_eye": 9, "landmarks": 12}
LAMBDAS = np.logspace(-6, 2, 33)
SIMPLER_WITHIN = 0.03  # prefer a simpler feature set when it is within 3% of the best held-out error

_R = RAW_INDEX


def _cols(raw: np.ndarray, *names: str) -> list[np.ndarray]:
    return [raw[:, _R[n]] for n in names]


def expand(raw: np.ndarray, lm_block: np.ndarray, feature_set: str) -> np.ndarray:
    """Design matrix for ``feature_set`` from raw features (n, N_RAW) and landmark blocks (n, N_LM_BLOCK)."""
    raw = np.atleast_2d(raw)
    ixa, iya, ixb, iyb = _cols(raw, "ixA", "iyA", "ixB", "iyB")
    gx = 0.5 * (ixa + ixb)
    gy = 0.5 * (iya + iyb)
    yaw, pitch, roll, hx, hy, hs = _cols(raw, "yaw", "pitch", "roll", "hx", "hy", "hs")
    if feature_set == "basic":
        up = 0.5 * (raw[:, _R["upA"]] + raw[:, _R["upB"]])
        lo = 0.5 * (raw[:, _R["loA"]] + raw[:, _R["loB"]])
        lh = 0.5 * (raw[:, _R["lhA"]] + raw[:, _R["lhB"]])
        lv = 0.5 * (raw[:, _R["lvA"]] + raw[:, _R["lvB"]])
        cols = [gx, gy, up, lo, lh, lv, yaw, pitch, gx * gx, gy * gy, gx * gy]
    elif feature_set == "head":
        up = 0.5 * (raw[:, _R["upA"]] + raw[:, _R["upB"]])
        lo = 0.5 * (raw[:, _R["loA"]] + raw[:, _R["loB"]])
        lh = 0.5 * (raw[:, _R["lhA"]] + raw[:, _R["lhB"]])
        lv = 0.5 * (raw[:, _R["lvA"]] + raw[:, _R["lvB"]])
        cols = [
            gx, gy, up, lo, lh, lv, yaw, pitch, gx * gx, gy * gy, gx * gy,
            roll, hx, hy, hs, gx * yaw, gy * pitch, gx * hs, gy * hs, gx * hx, gy * hy,
        ]  # fmt: skip
    elif feature_set == "per_eye":
        cols = [raw[:, i] for i in range(14)]  # both eyes' geometry and gaze blendshapes
        cols += [yaw, pitch, roll, hx, hy, hs, gx * gx, gy * gy, gx * gy, gx * yaw, gy * pitch]
    elif feature_set == "landmarks":
        block = np.atleast_2d(lm_block)
        return np.column_stack([block, yaw, pitch, roll])
    else:
        raise ValueError(f"unknown feature set {feature_set!r}")
    return np.column_stack(cols)


# --------------------------------------------------------------------------- training data


@dataclass
class TrainingData:
    """Samples for a fit.  ``groups`` >= 0 are calibration targets (held out in turn); -1 is never held out."""

    raw: np.ndarray  # (n, N_RAW)
    lm: np.ndarray  # (n, N_LM_BLOCK)
    y: np.ndarray  # (n, 2) screen pixels
    groups: np.ndarray  # (n,) int
    weights: np.ndarray  # (n,) float
    distance_cm: np.ndarray = field(default_factory=lambda: np.zeros(0))  # (n,) or empty

    def __post_init__(self) -> None:
        n = len(self.y)
        self.raw = np.asarray(self.raw, dtype=np.float64).reshape(n, N_RAW)
        self.lm = np.asarray(self.lm, dtype=np.float64).reshape(n, N_LM_BLOCK)
        self.y = np.asarray(self.y, dtype=np.float64).reshape(n, 2)
        self.groups = np.asarray(self.groups, dtype=np.int64).reshape(n)
        self.weights = np.asarray(self.weights, dtype=np.float64).reshape(n)
        if len(self.distance_cm) != n:
            self.distance_cm = np.full(n, np.nan)

    @classmethod
    def empty(cls) -> TrainingData:
        return cls(np.zeros((0, N_RAW)), np.zeros((0, N_LM_BLOCK)), np.zeros((0, 2)), np.zeros(0), np.zeros(0))

    def __len__(self) -> int:
        return len(self.y)

    @property
    def n_targets(self) -> int:
        return int(len(np.unique(self.groups[self.groups >= 0])))

    def subset(self, mask: np.ndarray) -> TrainingData:
        return TrainingData(
            self.raw[mask], self.lm[mask], self.y[mask], self.groups[mask], self.weights[mask], self.distance_cm[mask]
        )

    def concat(self, other: TrainingData) -> TrainingData:
        return TrainingData(
            np.vstack([self.raw, other.raw]),
            np.vstack([self.lm, other.lm]),
            np.vstack([self.y, other.y]),
            np.concatenate([self.groups, other.groups]),
            np.concatenate([self.weights, other.weights]),
            np.concatenate([self.distance_cm, other.distance_cm]),
        )

    def save(self, path: Path) -> None:
        _atomic_savez(
            path,
            raw=self.raw,
            lm=self.lm,
            y=self.y,
            groups=self.groups,
            weights=self.weights,
            distance_cm=self.distance_cm,
            version=np.array([FEATURE_VERSION]),
        )

    @classmethod
    def load(cls, path: Path) -> TrainingData:
        with np.load(path, allow_pickle=False) as z:
            if int(z["version"][0]) != FEATURE_VERSION:
                raise ValueError("saved calibration data is from an older version")
            return cls(z["raw"], z["lm"], z["y"], z["groups"], z["weights"], z["distance_cm"])


def gaze_xy(raw: np.ndarray) -> np.ndarray:
    """Mean iris position of both eyes (the primary gaze signal), for outlier tests."""
    raw = np.atleast_2d(raw)
    return np.column_stack(
        [0.5 * (raw[:, _R["ixA"]] + raw[:, _R["ixB"]]), 0.5 * (raw[:, _R["iyA"]] + raw[:, _R["iyB"]])]
    )


def reject_outliers(raw: np.ndarray, groups: np.ndarray, z: float = 3.5) -> np.ndarray:
    """Mask of samples whose iris position is within ``z`` robust deviations of their target's median.

    Catches frames where the eyes were still travelling to the dot, a glance
    away, or a landmark glitch, none of which describe that target.
    """
    keep = np.ones(len(groups), dtype=bool)
    g = gaze_xy(raw)
    for grp in np.unique(groups):
        idx = np.flatnonzero(groups == grp)
        if len(idx) < 5:
            continue
        pts = g[idx]
        med = np.median(pts, axis=0)
        mad = np.median(np.abs(pts - med), axis=0) * 1.4826
        # A floor so a very steady target does not reject ordinary noise.
        mad = np.maximum(mad, 0.004)
        dev = np.max(np.abs(pts - med) / mad, axis=1)
        keep[idx[dev > z]] = False
    return keep


# --------------------------------------------------------------------------- ridge


@dataclass
class RidgeFit:
    mean: np.ndarray  # (p,)
    std: np.ndarray  # (p,)
    coef: np.ndarray  # (p, 2)
    intercept: np.ndarray  # (2,)

    def predict(self, x: np.ndarray) -> np.ndarray:
        return ((np.atleast_2d(x) - self.mean) / self.std) @ self.coef + self.intercept


def _standardise(x: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    sw = w.sum()
    mean = (w[:, None] * x).sum(axis=0) / sw
    var = (w[:, None] * (x - mean) ** 2).sum(axis=0) / sw
    std = np.sqrt(var)
    # A column that never moved gets scale 1: centred it is ~0 and the penalty drives its weight to 0.
    std[std < 1e-9] = 1.0
    return mean, std


class _Solver:
    """Ridge solutions for many penalties from one eigendecomposition of the weighted Gram matrix."""

    def __init__(self, x: np.ndarray, y: np.ndarray, w: np.ndarray) -> None:
        self.mean, self.std = _standardise(x, w)
        xs = (x - self.mean) / self.std
        self.sw = float(w.sum())
        self.y_mean = (w[:, None] * y).sum(axis=0) / self.sw
        yc = y - self.y_mean
        gram = xs.T @ (w[:, None] * xs)
        self.s, self.v = np.linalg.eigh(gram)
        self.s = np.maximum(self.s, 0.0)
        self.vtb = self.v.T @ (xs.T @ (w[:, None] * yc))  # (p, 2)

    def coef(self, lam_x: float, lam_y: float) -> np.ndarray:
        cx = self.v @ (self.vtb[:, 0] / (self.s + lam_x * self.sw))
        cy = self.v @ (self.vtb[:, 1] / (self.s + lam_y * self.sw))
        return np.column_stack([cx, cy])

    def predict(self, x: np.ndarray, coef: np.ndarray) -> np.ndarray:
        return ((x - self.mean) / self.std) @ coef + self.y_mean

    def fit(self, lam_x: float, lam_y: float) -> RidgeFit:
        return RidgeFit(self.mean, self.std, self.coef(lam_x, lam_y), self.y_mean.copy())


@dataclass
class CVResult:
    feature_set: str
    lam_x: float
    lam_y: float
    rmse: float  # per-sample RMS error of held-out targets, each target weighted equally
    accuracy_px: float  # mean distance between a held-out target and the mean prediction for it
    per_target: tuple[float, ...]
    precision_px: float  # RMS spread of held-out predictions around their own mean
    targets: tuple[tuple[float, float], ...]


def cross_validate(
    x: np.ndarray, y: np.ndarray, w: np.ndarray, groups: np.ndarray, lambdas: np.ndarray = LAMBDAS
) -> CVResult | None:
    """Leave-one-target-out: per-axis penalty with the lowest held-out error, and the error it gives."""
    cal = np.unique(groups[groups >= 0])
    if len(cal) < 3:
        return None
    n_l = len(lambdas)
    sq = np.zeros((len(cal), n_l, 2))  # mean squared held-out error per target, penalty, axis
    folds = []
    for gi, g in enumerate(cal):
        test = groups == g
        train = ~test
        if w[train].sum() <= 0 or test.sum() == 0:
            return None
        solver = _Solver(x[train], y[train], w[train])
        preds = []
        for li, lam in enumerate(lambdas):
            pred = solver.predict(x[test], solver.coef(lam, lam))
            err = pred - y[test]
            sq[gi, li] = np.average(err**2, axis=0, weights=w[test])
            preds.append(pred)
        folds.append((test, preds))
    mse = sq.mean(axis=0)  # every target counts the same, however many frames it had
    li_x = int(np.argmin(mse[:, 0]))
    li_y = int(np.argmin(mse[:, 1]))
    per_target = []
    spreads = []
    for (test, preds), g in zip(folds, cal):
        pred = np.column_stack([preds[li_x][:, 0], preds[li_y][:, 1]])
        ww = w[test]
        centre = np.average(pred, axis=0, weights=ww)
        target = np.average(y[test], axis=0, weights=ww)
        per_target.append(float(np.hypot(*(centre - target))))
        spreads.append(float(np.average(np.sum((pred - centre) ** 2, axis=1), weights=ww)))
    targets = tuple(
        (float(np.mean(y[groups == g, 0])), float(np.mean(y[groups == g, 1]))) for g in cal
    )
    return CVResult(
        feature_set="",
        lam_x=float(lambdas[li_x]),
        lam_y=float(lambdas[li_y]),
        rmse=float(np.sqrt(mse[li_x, 0] + mse[li_y, 1])),
        accuracy_px=float(np.mean(per_target)),
        per_target=tuple(per_target),
        precision_px=float(np.sqrt(np.mean(spreads))),
        targets=targets,
    )


# --------------------------------------------------------------------------- the model


@dataclass
class GazeModel:
    feature_set: str
    fit: RidgeFit
    lam_x: float
    lam_y: float
    accuracy_px: float | None = None  # held-out, per target
    precision_px: float | None = None
    per_target: tuple[float, ...] = ()
    targets: tuple[tuple[float, float], ...] = ()  # where each per_target error was measured (the accuracy map)
    n_samples: int = 0
    n_targets: int = 0
    meta: dict[str, Any] = field(default_factory=dict)
    # Drift correction learned from clicks, applied to the output: p + C @ (p_x / 1000, p_y / 1000, 1).
    correction: np.ndarray | None = None

    def predict(self, raw: np.ndarray, lm_block: np.ndarray) -> tuple[float, float]:
        """Screen position for one frame's features."""
        x = expand(raw[None, :], lm_block[None, :], self.feature_set)
        out = apply_correction(self.fit.predict(x), self.correction)[0]
        return float(out[0]), float(out[1])

    def predict_many(self, raw: np.ndarray, lm_block: np.ndarray) -> np.ndarray:
        return apply_correction(self.predict_base(raw, lm_block), self.correction)

    def predict_base(self, raw: np.ndarray, lm_block: np.ndarray) -> np.ndarray:
        """Predictions of the calibrated model alone, without the drift correction."""
        return self.fit.predict(expand(raw, lm_block, self.feature_set))

    def describe(self) -> str:
        acc = "?" if self.accuracy_px is None else f"{self.accuracy_px:.0f} px"
        return f"{self.feature_set}, {self.n_targets} targets, {acc}"

    # ------------------------------------------------------------------ persistence

    def save(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        _atomic_savez(
            folder / "gaze_model.npz",
            mean=self.fit.mean,
            std=self.fit.std,
            coef=self.fit.coef,
            intercept=self.fit.intercept,
        )
        info = {
            "version": FEATURE_VERSION,
            "feature_set": self.feature_set,
            "lam_x": self.lam_x,
            "lam_y": self.lam_y,
            "accuracy_px": self.accuracy_px,
            "precision_px": self.precision_px,
            "per_target": list(self.per_target),
            "targets": [list(p) for p in self.targets],
            "n_samples": self.n_samples,
            "n_targets": self.n_targets,
            "meta": self.meta,
            "correction": None if self.correction is None else self.correction.tolist(),
        }
        _atomic_write_text(folder / "gaze_model.json", json.dumps(info, indent=2))

    @classmethod
    def load(cls, folder: Path) -> GazeModel | None:
        info_path = folder / "gaze_model.json"
        arr_path = folder / "gaze_model.npz"
        if not (info_path.exists() and arr_path.exists()):
            return None
        try:
            info = json.loads(info_path.read_text("utf-8"))
            if info.get("version") != FEATURE_VERSION:
                log.warning("Saved gaze model is from another version; it needs a new calibration")
                return None
            with np.load(arr_path, allow_pickle=False) as z:
                fit = RidgeFit(z["mean"], z["std"], z["coef"], z["intercept"])
            model = cls(
                feature_set=info["feature_set"],
                fit=fit,
                lam_x=float(info["lam_x"]),
                lam_y=float(info["lam_y"]),
                accuracy_px=info.get("accuracy_px"),
                precision_px=info.get("precision_px"),
                per_target=tuple(info.get("per_target", ())),
                targets=tuple((float(p[0]), float(p[1])) for p in info.get("targets", ())),
                n_samples=int(info.get("n_samples", 0)),
                n_targets=int(info.get("n_targets", 0)),
                meta=dict(info.get("meta", {})),
                correction=None if info.get("correction") is None else np.asarray(info["correction"], float).reshape(2, 3),
            )
            # Check the shapes agree with the feature set, so a damaged file fails here and not per frame.
            model.predict(np.zeros(N_RAW), np.zeros(N_LM_BLOCK))
            return model
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("Could not load the gaze model from %s: %s", folder, e)
            return None


@dataclass
class FitOutcome:
    model: GazeModel | None
    cv: CVResult | None
    candidates: dict[str, float]  # feature set -> held-out RMS error
    reason: str = ""


def fit_model(
    data: TrainingData,
    feature_sets: tuple[str, ...] | list[str] = FEATURE_SETS,
    *,
    lambdas: np.ndarray = LAMBDAS,
) -> FitOutcome:
    """Fit every eligible feature set, keep the best by held-out error, refit it on all the data."""
    if len(data) == 0:
        return FitOutcome(None, None, {}, "no samples")
    n_targets = data.n_targets
    results: dict[str, CVResult] = {}
    for fs in feature_sets:
        if n_targets < MIN_TARGETS.get(fs, 4):
            continue
        x = expand(data.raw, data.lm, fs)
        cv = cross_validate(x, data.y, data.weights, data.groups, lambdas)
        if cv is None:
            continue
        cv.feature_set = fs
        results[fs] = cv
    if not results:
        if n_targets >= 1 and len(data) >= 10:
            # Too few targets to hold any out: a cautious fixed penalty, error unknown.
            fs = feature_sets[0]
            x = expand(data.raw, data.lm, fs)
            fit = _Solver(x, data.y, data.weights).fit(0.05, 0.05)
            model = GazeModel(fs, fit, 0.05, 0.05, n_samples=len(data), n_targets=n_targets)
            return FitOutcome(model, None, {}, "too few targets to measure accuracy")
        return FitOutcome(None, None, {}, "not enough targets")
    best = min(results.values(), key=lambda r: r.rmse)
    chosen = best
    for fs in feature_sets:  # ordered simplest first
        r = results.get(fs)
        if r is not None and r.rmse <= best.rmse * (1.0 + SIMPLER_WITHIN):
            chosen = r
            break
    x = expand(data.raw, data.lm, chosen.feature_set)
    fit = _Solver(x, data.y, data.weights).fit(chosen.lam_x, chosen.lam_y)
    model = GazeModel(
        feature_set=chosen.feature_set,
        fit=fit,
        lam_x=chosen.lam_x,
        lam_y=chosen.lam_y,
        accuracy_px=chosen.accuracy_px,
        precision_px=chosen.precision_px,
        per_target=chosen.per_target,
        targets=chosen.targets,
        n_samples=len(data),
        n_targets=n_targets,
        meta={"fitted": time.time(), "candidates": {k: round(v.rmse, 2) for k, v in results.items()}},
    )
    return FitOutcome(model, chosen, {k: v.rmse for k, v in results.items()})


def heldout_error(data: TrainingData, feature_set: str, lam_x: float, lam_y: float) -> float | None:
    """Leave-one-target-out accuracy for a fixed feature set and penalty (used by the click-learning guard)."""
    x = expand(data.raw, data.lm, feature_set)
    cal = np.unique(data.groups[data.groups >= 0])
    if len(cal) < 3:
        return None
    errs = []
    for g in cal:
        test = data.groups == g
        solver = _Solver(x[~test], data.y[~test], data.weights[~test])
        pred = solver.predict(x[test], solver.coef(lam_x, lam_y))
        centre = np.average(pred, axis=0, weights=data.weights[test])
        target = np.average(data.y[test], axis=0, weights=data.weights[test])
        errs.append(float(np.hypot(*(centre - target))))
    return float(np.mean(errs))


# --------------------------------------------------------------------------- drift correction


def add_offset(correction: np.ndarray | None, dx: float, dy: float) -> np.ndarray:
    """The correction with a plain shift added ("fix pointer"): everything moves by (dx, dy)."""
    c = np.zeros((2, 3)) if correction is None else np.array(correction, dtype=np.float64).reshape(2, 3)
    c[0, 2] += dx
    c[1, 2] += dy
    return c


def apply_correction(pred: np.ndarray, correction: np.ndarray | None) -> np.ndarray:
    if correction is None:
        return pred
    basis = np.column_stack([pred[:, 0] / 1000.0, pred[:, 1] / 1000.0, np.ones(len(pred))])
    return pred + basis @ correction.T


CORRECTION_MAX_SHIFT_PX = 300.0
CORRECTION_MAX_SCALE = 0.4


def fit_correction(pred: np.ndarray, target: np.ndarray, weights: np.ndarray, l2: float = 0.5) -> tuple[np.ndarray | None, float, float]:
    """An affine correction of model output from click samples, kept only if it predicts unseen clicks better.

    Returns (correction or None, leave-one-out error with it, without it).
    The scale terms are penalised more than the shift, so a handful of
    clicks mostly moves the estimate rather than stretching it.
    """
    n = len(pred)
    if n < 4:
        return None, math.inf, math.inf
    basis = np.column_stack([pred[:, 0] / 1000.0, pred[:, 1] / 1000.0, np.ones(n)])
    resid = target - pred
    reg = np.diag([l2, l2, 1e-3])

    def solve(mask: np.ndarray) -> np.ndarray:
        b = basis[mask]
        w = weights[mask]
        gram = b.T @ (w[:, None] * b) + reg * float(w.sum()) / max(int(mask.sum()), 1)
        return np.linalg.solve(gram, b.T @ (w[:, None] * resid[mask])).T  # (2, 3)

    loo_with, loo_without = [], []
    for i in range(n):
        mask = np.arange(n) != i
        c = solve(mask)
        loo_with.append(float(np.hypot(*(resid[i] - c @ basis[i]))))
        loo_without.append(float(np.hypot(*resid[i])))
    err_with = float(np.average(loo_with, weights=weights))
    err_without = float(np.average(loo_without, weights=weights))
    corr = solve(np.ones(n, dtype=bool))
    shift = float(np.max(np.abs(corr @ np.array([0.96, 0.54, 1.0]))))  # at the middle of a 1920 x 1080 screen
    scale = float(np.max(np.abs(corr[:, :2]))) / 1000.0  # fraction of the position: 0.1 stretches by 10%
    if err_with >= 0.9 * err_without or shift > CORRECTION_MAX_SHIFT_PX or scale > CORRECTION_MAX_SCALE:
        return None, err_with, err_without
    return corr, err_with, err_without


# --------------------------------------------------------------------------- files


def _atomic_savez(path: Path, **arrays: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        np.savez(fh, **arrays)
    os.replace(tmp, path)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, path)
