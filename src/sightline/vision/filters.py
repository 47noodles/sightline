"""Signal filters for gaze and head signals.

``OneEuroFilter`` is adapted from the reference Python implementation of the
1-euro filter (Casiez, Roussel and Vogel, CHI 2012), vectorised so one filter
handles a 2-D point, and driven by the measured time between frames.  Source:
https://github.com/casiez/OneEuroFilter/blob/main/python/OneEuroFilter/OneEuroFilter.py

    OneEuroFilter.py
    Authors: Nicolas Roussel (nicolas.roussel@inria.fr), Gery Casiez https://gery.casiez.net
    Copyright 2019 Inria
    BSD License https://opensource.org/licenses/BSD-3-Clause

    Redistribution and use in source and binary forms, with or without
    modification, are permitted provided that the following conditions are met:
     1. Redistributions of source code must retain the above copyright notice, this list of conditions
        and the following disclaimer.
     2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions
        and the following disclaimer in the documentation and/or other materials provided with the distribution.
     3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or
        promote products derived from this software without specific prior written permission.

    THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED
    WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
    PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY
    DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
    PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
    CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
    OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

``GazeSmoother`` chains a three-sample median (drops single-frame landmark
spikes before they reach the speed estimate), the 1-euro filter, a saccade
gate (jumps are passed through instead of glided) and a fixation clamp whose
radius follows the measured noise.  The chain and the robust spread estimate
port the reasoning of shaal/eye-tracker ADR-0007 and ADR-0014 (MIT).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np


def _alpha(dt: float, cutoff: float) -> float:
    """Smoothing factor of a first-order low-pass filter (the reference's ``__alpha``)."""
    tau = 1.0 / (2.0 * math.pi * cutoff)
    return 1.0 / (1.0 + tau / dt)


class OneEuroFilter:
    """Speed-adaptive low-pass filter: heavy smoothing when still, little lag when moving.

    ``min_cutoff`` (Hz) sets the smoothing at rest; ``beta`` raises the
    cutoff with speed (in signal units per second).  Works on scalars or
    small numpy vectors (all components share one speed estimate, the norm).
    """

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.0, d_cutoff: float = 1.0, *, rate: float = 30.0) -> None:
        if min_cutoff <= 0 or d_cutoff <= 0 or rate <= 0:
            raise ValueError("cutoffs and rate must be positive")
        self.min_cutoff = float(min_cutoff)
        self.beta = float(beta)
        self.d_cutoff = float(d_cutoff)
        self._rate = float(rate)
        self._x: np.ndarray | None = None
        self._dx: np.ndarray | None = None
        self._t: float | None = None

    @property
    def value(self) -> np.ndarray | None:
        return None if self._x is None else self._x.copy()

    def reset(self, value: np.ndarray | float | None = None, t: float | None = None) -> None:
        if value is None:
            self._x = None
            self._dx = None
            self._t = None
            return
        self._x = np.atleast_1d(np.asarray(value, dtype=np.float64)).copy()
        self._dx = np.zeros_like(self._x)
        self._t = t

    def __call__(self, x: np.ndarray | float, t: float | None = None) -> np.ndarray:
        value = np.atleast_1d(np.asarray(x, dtype=np.float64))
        if self._x is None:
            self.reset(value, t)
            assert self._x is not None
            return self._x.copy()
        if t is not None and self._t is not None and t > self._t:
            dt = min(t - self._t, 0.5)  # a long gap is a restart, not a slow-motion step
        else:
            dt = 1.0 / self._rate
        if t is not None:
            self._t = t
        assert self._dx is not None
        # As in the reference: the derivative is taken against the last filtered value.
        dx = (value - self._x) / dt
        a_d = _alpha(dt, self.d_cutoff)
        self._dx = a_d * dx + (1.0 - a_d) * self._dx
        speed = float(np.linalg.norm(self._dx))
        cutoff = self.min_cutoff + self.beta * speed
        a = _alpha(dt, cutoff)
        self._x = a * value + (1.0 - a) * self._x
        return self._x.copy()


class Median3:
    """Median of the last three samples per axis.  One frame of latency, removes isolated spikes."""

    def __init__(self) -> None:
        self._buf: deque[np.ndarray] = deque(maxlen=3)

    def reset(self) -> None:
        self._buf.clear()

    def __call__(self, x: np.ndarray) -> np.ndarray:
        self._buf.append(np.asarray(x, dtype=np.float64))
        if len(self._buf) < 3:
            return self._buf[-1].copy()
        return np.median(np.stack(self._buf), axis=0)


def robust_spread(values: np.ndarray) -> float:
    """Half the 15th to 85th percentile range, averaged over axes.

    Unlike the median absolute deviation it does not report zero for a signal
    flicking between two values, which is exactly what quantised landmark
    noise can look like (shaal/eye-tracker ADR-0014).
    """
    if len(values) < 3:
        return 0.0
    lo, hi = np.percentile(values, [15.0, 85.0], axis=0)
    return float(np.mean((hi - lo) / 2.0))


@dataclass(slots=True)
class GazeState:
    """Output of the gaze smoother for one frame."""

    x: float
    y: float
    fixating: bool
    fixation_x: float
    fixation_y: float
    fixation_s: float  # how long the current fixation has lasted
    noise_px: float  # measured spread of the gaze signal while fixating
    radius_px: float  # fixation radius in use
    saccade: bool  # this frame started a new fixation candidate far from the last one


class GazeSmoother:
    """Median, 1-euro, saccade gate and an adaptive fixation clamp, for screen-pixel gaze."""

    def __init__(
        self,
        *,
        min_cutoff: float = 0.8,
        beta: float = 0.006,
        window_s: float = 0.12,
        radius_floor: float = 28.0,
        radius_ceiling: float = 90.0,
        noise_scale: float = 2.5,
        clamp_s: float = 0.11,
        saccade_px: float = 140.0,
    ) -> None:
        self.one_euro = OneEuroFilter(min_cutoff, beta, 1.0)
        self.median = Median3()
        self.window_s = window_s
        self.radius_floor = radius_floor
        self.radius_ceiling = radius_ceiling
        self.noise_scale = noise_scale
        self.clamp_s = clamp_s
        self.saccade_px = saccade_px
        self._recent: deque[tuple[float, float, float]] = deque()  # (t, x, y) after the median
        # Recent median-filtered points for the noise estimate.  It must not depend on fixation detection:
        # with noise larger than the radius no fixation is ever found, so the radius could never grow.
        self._hist: deque[tuple[float, float]] = deque(maxlen=48)
        self._hist_n = 0
        self._fix_start: float | None = None
        self._fix_centre: np.ndarray | None = None
        self._fix_n = 0
        self._clamped: np.ndarray | None = None
        self._noise = radius_floor / noise_scale
        self.last: GazeState | None = None

    def set_smoothing(self, smoothing: float) -> None:
        """0 (quick) .. 1 (steady), the user's "Pointer smoothing" setting."""
        s = min(max(smoothing, 0.0), 1.0)
        self.one_euro.min_cutoff = 2.0 - 1.6 * s  # 2.0 Hz .. 0.4 Hz at rest
        self.clamp_s = 0.16 - 0.1 * s

    def reset(self) -> None:
        """Forget the trajectory (after a blink or lost face) but keep the learned noise level."""
        self.one_euro.reset()
        self.median.reset()
        self._recent.clear()
        self._end_fixation()
        self.last = None

    @property
    def radius(self) -> float:
        return float(min(max(self.noise_scale * self._noise, self.radius_floor), self.radius_ceiling))

    @property
    def noise(self) -> float:
        return float(self._noise)

    def _update_noise(self, point: np.ndarray) -> None:
        """Noise of the median-filtered gaze, from differences three samples apart.

        Median outputs three apart share no input sample, so the difference
        has twice their variance.  The interquartile range ignores the few
        large differences that saccades produce.  Measured before the 1-euro
        filter and the clamp, which would understate it.
        """
        self._hist.append((float(point[0]), float(point[1])))
        self._hist_n += 1
        if len(self._hist) < 24 or self._hist_n % 3:
            return
        arr = np.array(self._hist)
        d = arr[3:] - arr[:-3]
        q25, q75 = np.percentile(d, [25.0, 75.0], axis=0)
        sigma = float(np.mean(q75 - q25)) / 1.349 / math.sqrt(2.0)
        self._noise = 0.85 * self._noise + 0.15 * sigma

    def _end_fixation(self) -> None:
        self._fix_start = None
        self._fix_centre = None
        self._fix_n = 0
        self._clamped = None

    def update(self, t: float, x: float, y: float) -> GazeState:
        point = self.median(np.array([x, y], dtype=np.float64))
        self._recent.append((t, float(point[0]), float(point[1])))
        while self._recent and t - self._recent[0][0] > self.window_s:
            self._recent.popleft()
        radius = self.radius
        saccade = False
        prev = self.one_euro.value
        if prev is not None and float(np.hypot(*(point - prev))) > max(self.saccade_px, 2.0 * radius):
            # Ballistic jump: teleport the filter rather than gliding through the space in between.
            self.one_euro.reset(point, t)
            self._recent.clear()
            self._recent.append((t, float(point[0]), float(point[1])))
            self._end_fixation()
            saccade = True
        smooth = self.one_euro(point, t)

        fixating = False
        if len(self._recent) >= 2:
            pts = np.array([(px, py) for _, px, py in self._recent])
            centre = pts.mean(axis=0)
            fixating = float(np.max(np.hypot(pts[:, 0] - centre[0], pts[:, 1] - centre[1]))) <= radius
        if fixating:
            if self._fix_centre is None or float(np.hypot(*(point - self._fix_centre))) > radius:
                # A new fixation, or drifted out of the old one: start here.
                self._fix_start = self._recent[0][0]
                self._fix_centre = centre
                self._fix_n = len(self._recent)
                self._clamped = None
            else:
                # Running mean, capped so a long fixation still follows slow drift.
                self._fix_n = min(self._fix_n + 1, 30)
                self._fix_centre = self._fix_centre + (point - self._fix_centre) / self._fix_n
        else:
            self._end_fixation()

        fix_s = 0.0 if self._fix_start is None else t - self._fix_start
        self._update_noise(point)

        out = smooth
        if fixating and fix_s >= self.clamp_s and self._fix_centre is not None:
            # Hold the pointer still inside a fixation: sub-radius jitter makes clicking hard.
            if self._clamped is None or float(np.hypot(*(self._fix_centre - self._clamped))) > 0.5 * radius:
                self._clamped = self._fix_centre.copy()
            out = self._clamped
        fc = self._fix_centre if self._fix_centre is not None else smooth
        self.last = GazeState(
            float(out[0]),
            float(out[1]),
            fixating,
            float(fc[0]),
            float(fc[1]),
            fix_s,
            float(self._noise),
            radius,
            saccade,
        )
        return self.last
