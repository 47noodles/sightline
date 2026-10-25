"""How accurate eye pointing is at each point of the screen, from the calibration's own held-out errors.

The gaze fit holds each calibration dot out in turn and measures how far the
prediction for it lands from it (``GazeModel.per_target``).  Those errors are
not uniform: webcam gaze is usually best near the middle and worst in the
corners.  ``AccuracyMap`` interpolates them across each calibrated monitor
(inverse distance weighting, softened so the map varies smoothly and never
goes beyond the measured values), so snapping, the number chooser and target
sizes can follow the real error instead of fixed constants.  A monitor
without calibration dots has no map: ``error_px`` is None there.

``default_gaze_targets`` is the dot layout for one monitor.  It has dots near
the corners and edges, where the Start button, the tray and close buttons
live, because that is where the map most needs measurements.
``comfort_size`` turns the error into a plain sentence about the smallest
controls that are easy to hit with the eyes alone.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from sightline.core.types import Monitor, Rect

FLOOR_PX = 10.0  # no point anywhere is better than this: tracking noise remains even where the fit is perfect
SOFTEN_FRACTION = 0.1  # of the monitor's diagonal: how far one dot's error spreads before its neighbours take over


@dataclass(frozen=True)
class _MonitorMap:
    rect: Rect
    points: np.ndarray  # (n, 2) screen pixels
    errors: np.ndarray  # (n,) held-out error at each point, pixels
    soften2: float


class AccuracyMap:
    """Expected gaze error at a screen point.  Immutable, so any thread may read it."""

    def __init__(
        self, points: Sequence[Sequence[float]], errors: Sequence[float], monitors: Sequence[Monitor]
    ) -> None:
        pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
        errs = np.asarray(errors, dtype=np.float64).reshape(-1)
        if len(pts) != len(errs):
            raise ValueError("one error per point")
        ok = np.isfinite(errs) & np.all(np.isfinite(pts), axis=1)
        pts, errs = pts[ok], errs[ok]
        self.mean_px: float | None = float(np.mean(errs)) if len(errs) else None
        self.points = pts
        self.errors = errs
        regions: list[Rect] = [m.rect for m in monitors]
        if not regions and len(pts):
            # No monitor information (tests, tools): one region around the dots.
            x0, y0 = pts.min(axis=0)
            x1, y1 = pts.max(axis=0)
            regions = [Rect.from_ltrb(x0 - 100.0, y0 - 100.0, x1 + 100.0, y1 + 100.0)]
        self._maps: list[_MonitorMap] = []
        owner = [self._owner(regions, x, y) for x, y in pts]
        for ri, rect in enumerate(regions):
            sel = np.array([o == ri for o in owner], dtype=bool)
            if not sel.any():
                continue
            diag = math.hypot(rect.w, rect.h)
            self._maps.append(_MonitorMap(rect, pts[sel], errs[sel], (SOFTEN_FRACTION * diag) ** 2))

    @staticmethod
    def _owner(regions: Sequence[Rect], x: float, y: float) -> int:
        for i, r in enumerate(regions):
            if r.contains(x, y):
                return i
        return min(range(len(regions)), key=lambda i: regions[i].distance_to(x, y))

    def error_px(self, x: float, y: float) -> float | None:
        """Expected error around (x, y), or None on a monitor the calibration did not cover."""
        for mm in self._maps:
            if mm.rect.contains(x, y):
                d2 = (mm.points[:, 0] - x) ** 2 + (mm.points[:, 1] - y) ** 2
                w = 1.0 / (d2 + mm.soften2)
                return max(FLOOR_PX, float(np.dot(w, mm.errors) / np.sum(w)))
        return None

    @property
    def empty(self) -> bool:
        return not self._maps


def targets_from_groups(y: np.ndarray, groups: np.ndarray) -> list[tuple[float, float]]:
    """Mean position of each calibration target, in the order cross-validation reports its errors."""
    out = []
    for g in np.unique(groups[groups >= 0]):
        sel = groups == g
        out.append((float(np.mean(y[sel, 0])), float(np.mean(y[sel, 1]))))
    return out


def default_gaze_targets(rect: Rect) -> list[tuple[float, float]]:
    """The gaze calibration dots for one monitor, in the order to show them.

    The middle, then a ring a quarter of the way in, then the corners and edge
    midpoints a little in from the border (17 dots).  The outer ring is where
    the Start button, the tray and close buttons are, and where webcam gaze is
    least accurate, so it needs measuring most.
    """
    ix = max(40.0, 0.035 * rect.w)
    iy = max(40.0, 0.035 * rect.h)
    left, right = rect.x + ix, rect.x + rect.w - ix
    top, bottom = rect.y + iy, rect.y + rect.h - iy
    cx, cy = rect.x + rect.w / 2.0, rect.y + rect.h / 2.0
    qx1, qx3 = rect.x + 0.25 * rect.w, rect.x + 0.75 * rect.w
    qy1, qy3 = rect.y + 0.25 * rect.h, rect.y + 0.75 * rect.h
    inner = [(cx, cy), (qx1, qy1), (cx, qy1), (qx3, qy1), (qx3, cy), (qx3, qy3), (cx, qy3), (qx1, qy3), (qx1, cy)]
    outer = [(left, top), (cx, top), (right, top), (right, cy), (right, bottom), (cx, bottom), (left, bottom), (left, cy)]
    return [(float(x), float(y)) for x, y in inner + outer]


@dataclass(frozen=True)
class Comfort:
    diameter_px: float
    diameter_mm: float
    message: str


def comfort_size(mean_error_px: float, pitch_mm: float, windows_scale: float = 1.0) -> Comfort:
    """The smallest controls that are easy to hit with the eyes alone, as a sentence for the wizard.

    A control is easy to hit when the typical error fits inside it, so its
    diameter should be about twice the mean error.  Head fine-tuning, snapping
    and the number chooser make smaller controls usable; the sentence says so
    when it suggests a larger Windows scale.
    """
    diameter_px = 2.0 * max(mean_error_px, FLOOR_PX)
    mm = diameter_px * pitch_mm
    cm = mm / 10.0
    step = 0.5 if cm < 5.0 else 1.0
    shown = max(step, round(cm / step) * step)
    message = f"At your accuracy, controls smaller than about {shown:g} cm are hard to hit with your eyes alone."
    if shown >= 1.5:
        if windows_scale < 1.45:
            message += " Setting Windows text and app size to 150 percent will help."
        elif windows_scale < 1.95:
            message += " Setting Windows text and app size to 200 percent will help."
        else:
            message += " Small head movements fine-tune the pointer on small controls."
    return Comfort(diameter_px, mm, message)
