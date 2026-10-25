"""Reading: hold the pointer while the user reads, and the optional reading scroll.

Reading hold.  Webcam gaze is too coarse to see single reading fixations,
but reading keeps a shape the estimate can follow: the gaze runs steadily
along a line (left to right in most scripts, right to left in some), jumps
back at the end of the line, and the head stays still.  While that shape
holds, the pointer is held where it is instead of chasing the eyes, which
would set off tooltips, hover menus and video controls.  A deliberate head
movement releases it at once, and so does a fixation long enough to mean
the reading has stopped (the user is looking at something to click).

Reading scroll (``pointer.reading_scroll``).  Looking in the bottom fifth of
the window under the pointer scrolls down at a rate that grows with how
deep the gaze is; the top fifth scrolls back; the middle stops.  The zone
edges have hysteresis as wide as the local gaze error (``error_px``), so the
error cannot flick scrolling on and off, and a zone must be looked at for a
moment before scrolling starts, so a glance does nothing.

The numbers are first guesses for webcam gaze and have not been tried on
real readers yet.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

from sightline.core.types import Rect

from .filters import GazeState


@dataclass
class ReadingConfig:
    window_s: float = 1.2  # gaze history one judgement looks at
    step_s: float = 0.1  # the history is resampled at this step
    min_run_px: float = 110.0  # net movement along the line over the window
    max_run_px: float = 1100.0  # faster than this is a sweep across the screen, not reading
    min_forward: float = 0.7  # share of steps that go the reading way (noise sends some back)
    max_line_dev_px: float = 45.0  # RMS distance of the samples from a straight line
    sweep_px: float = 220.0  # a jump back at least this far after a run is a return sweep
    enter_s: float = 0.8  # line-like movement for this long before the pointer is held
    still_speed: float = 0.06  # head speed (face units per second) that counts as still
    exit_speed: float = 0.12  # a deliberate head movement
    long_fixation_s: float = 0.5  # a fixation this long ends reading
    idle_exit_s: float = 2.0  # no sign of reading for this long ends it
    gap_s: float = 1.0  # no gaze for longer than this (eyes shut, face lost) starts afresh


class ReadingDetector:
    """Fed the smoothed gaze each frame on the vision thread; ``reading`` says whether to hold the pointer."""

    def __init__(self, cfg: ReadingConfig | None = None) -> None:
        self.cfg = cfg or ReadingConfig()
        self._pts: deque[tuple[float, float, float]] = deque()
        self.reading = False
        self._run_s = 0.0
        self._direction = 0  # +1 left to right, -1 right to left
        self._last_evidence = -math.inf
        self._last_t: float | None = None
        self._last_sweep_t = -math.inf
        self._judged_t = -math.inf  # the line shape is judged at the resampling step, not every frame
        self._judged = 0

    def reset(self) -> None:
        self._pts.clear()
        self.reading = False
        self._run_s = 0.0
        self._direction = 0
        self._last_evidence = -math.inf
        self._last_t = None
        self._judged_t = -math.inf
        self._judged = 0

    def update(self, t: float, gaze: GazeState | None, head_speed: float) -> bool:
        cfg = self.cfg
        if head_speed > cfg.exit_speed:
            # A deliberate head movement: the user is pointing, not reading.
            self._run_s = 0.0
            self.reading = False
            self._last_t = t
            return False
        if gaze is None:
            if self._last_t is not None and t - self._last_t > cfg.gap_s:
                self.reset()
            return self.reading
        dt = 1.0 / 30.0 if self._last_t is None else min(max(t - self._last_t, 0.0), 0.2)
        self._last_t = t
        self._pts.append((t, gaze.x, gaze.y))
        while self._pts and t - self._pts[0][0] > cfg.window_s + 1.0:
            self._pts.popleft()

        if t - self._judged_t >= 0.9 * cfg.step_s:
            self._judged_t = t
            self._judged = self._line_direction(t)
        direction = self._judged
        still = head_speed < cfg.still_speed
        if direction and still:
            if self._direction and direction != self._direction:
                self._run_s = 0.0  # changed direction: not one line being read
            self._direction = direction
            self._run_s += dt
            self._last_evidence = t
        else:
            self._run_s = max(0.0, self._run_s - 0.5 * dt)
        if self._direction and still and self._return_sweep(t):
            # Jumping back to the start of the next line after a run confirms reading.
            self._last_evidence = t
            if self._run_s >= 0.4 * cfg.enter_s:
                self._run_s = max(self._run_s, cfg.enter_s)

        if not self.reading and self._run_s >= cfg.enter_s:
            self.reading = True
        elif self.reading:
            if gaze.fixating and gaze.fixation_s >= cfg.long_fixation_s:
                self.reading = False  # looking at something: let the pointer go there
                self._run_s = 0.0
            elif t - self._last_evidence > cfg.idle_exit_s:
                self.reading = False
                self._run_s = 0.0
        return self.reading

    def _resampled(self, t: float) -> np.ndarray | None:
        cfg = self.cfg
        if not self._pts or self._pts[0][0] > t - 0.8 * cfg.window_s:
            return None  # not enough history yet
        arr = np.array(self._pts)
        times = np.arange(t - cfg.window_s, t + 1e-9, cfg.step_s)
        xs = np.interp(times, arr[:, 0], arr[:, 1])
        ys = np.interp(times, arr[:, 0], arr[:, 2])
        return np.column_stack([xs, ys])

    def _line_direction(self, t: float) -> int:
        """+1 or -1 when the last window looks like reading along a line in that direction, else 0."""
        cfg = self.cfg
        pts = self._resampled(t)
        if pts is None or len(pts) < 4:
            return 0
        net = pts[-1] - pts[0]
        run = abs(float(net[0]))
        if not (cfg.min_run_px <= run <= cfg.max_run_px):
            return 0
        direction = 1 if net[0] > 0 else -1
        steps = np.diff(pts[:, 0])
        forward = float(np.mean(direction * steps > -3.0))  # held still between saccades counts as forward
        if forward < cfg.min_forward:
            return 0
        if abs(float(net[1])) > 0.35 * run + 30.0:
            return 0  # moving diagonally or down the page, not along a line
        # Distance of each sample from the straight line between the ends.
        rel = pts - pts[0]
        cross = rel[:, 0] * net[1] - rel[:, 1] * net[0]
        dev = float(np.sqrt(np.mean((cross / max(float(np.hypot(*net)), 1e-6)) ** 2)))
        if dev > cfg.max_line_dev_px:
            return 0
        return direction

    def _return_sweep(self, t: float) -> bool:
        """A jump back against the reading direction, a little down the page, within the last 0.3 s."""
        cfg = self.cfg
        if len(self._pts) < 3 or t - self._last_sweep_t < 0.5:
            return False
        now_t, x1, y1 = self._pts[-1]
        for pt, x0, y0 in reversed(self._pts):
            if now_t - pt > 0.3:
                dx, dy = x1 - x0, y1 - y0
                if self._direction * dx <= -cfg.sweep_px and -40.0 <= dy <= 200.0:
                    self._last_sweep_t = t
                    return True
                return False
        return False


# --------------------------------------------------------------------------- reading scroll


@dataclass(frozen=True, slots=True)
class ScrollCommand:
    start: bool  # False: stop scrolling
    direction: str = ""  # "down" or "up"
    speed: float = 0.0


class ReadingScroll:
    """Decides when to start, speed up and stop scrolling from where the user looks in a window."""

    def __init__(
        self,
        *,
        start_s: float = 0.35,
        min_speed: float = 0.25,
        max_speed: float = 1.2,
        gap_s: float = 0.4,
        band_fraction: float = 0.2,
    ) -> None:
        self.enabled = False
        self.start_s = start_s  # a zone must be looked at this long before scrolling starts
        self.min_speed = min_speed  # scroll speed at the zone edge (the desktop's speed factor) ...
        self.max_speed = max_speed  # ... and at the window edge
        self.gap_s = gap_s  # eyes closed or no estimate this long stops scrolling
        self.band_fraction = band_fraction  # the bottom and top fifths of the window
        self.direction: str | None = None
        self._zone: str | None = None
        self._zone_since = 0.0
        self._last_seen: float | None = None
        self._speed = 0.0
        self._last_sent = -math.inf

    def stop(self) -> ScrollCommand | None:
        """Stop if scrolling.  The command to send, or None when nothing was scrolling."""
        self._zone = None
        if self.direction is None:
            return None
        self.direction = None
        return ScrollCommand(False)

    def _speed_for(self, depth: float) -> float:
        d = min(max(depth, 0.0), 1.0)
        return round(self.min_speed + (self.max_speed - self.min_speed) * d, 2)

    def update(
        self, t: float, point: tuple[float, float] | None, window: Rect | None, error_px: float | None, active: bool
    ) -> ScrollCommand | None:
        if not (self.enabled and active) or window is None or window.h <= 0:
            self._last_seen = None
            return self.stop()
        if point is None:
            if self._last_seen is not None and t - self._last_seen > self.gap_s:
                self._last_seen = None
                return self.stop()
            return None  # a blink: carry on as before
        self._last_seen = t
        x, y = point
        band = self.band_fraction * window.h
        # Hysteresis as wide as the local error: deeper to start, well clear of the zone to stop.
        e = min(max(error_px if error_px is not None else 60.0, 30.0), 0.5 * band)
        if not (window.left - e <= x <= window.right + e and window.top - e <= y <= window.bottom + e):
            return self.stop()  # looking outside the window
        low_edge = window.bottom - band
        high_edge = window.top + band
        if self.direction == "down":
            if y < low_edge - 0.75 * e:
                return self.stop()
            depth = (y - low_edge) / band
        elif self.direction == "up":
            if y > high_edge + 0.75 * e:
                return self.stop()
            depth = (high_edge - y) / band
        else:
            zone = "down" if y > low_edge + 0.25 * e else "up" if y < high_edge - 0.25 * e else None
            if zone != self._zone:
                self._zone, self._zone_since = zone, t
            if zone is None or t - self._zone_since < self.start_s:
                return None
            self.direction = zone
            depth = (y - low_edge) / band if zone == "down" else (high_edge - y) / band
            self._speed = self._speed_for(depth)
            self._last_sent = t
            return ScrollCommand(True, zone, self._speed)
        speed = self._speed_for(depth)
        if abs(speed - self._speed) >= 0.15 and t - self._last_sent >= 0.3:
            self._speed = speed
            self._last_sent = t
            return ScrollCommand(True, self.direction, speed)
        return None
