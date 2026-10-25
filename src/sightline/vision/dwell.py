"""Dwell clicking and the resume zone.

Dwell: holding the pointer still (within ``radius``) for ``dwell_s`` clicks.
After a click the pointer must leave the radius before the next one can
start, so resting on a button never repeats.

Resume zone: while eye control is paused, looking at a fixed spot (the top
middle of the primary screen by default) for ``hold_s`` turns it back on.
That is the eyes-only way back after pausing.  Short gaps (a blink) do not
reset the timer; looking away does.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from sightline.core.types import Monitor, Rect


@dataclass(frozen=True, slots=True)
class DwellUpdate:
    x: float  # centre of the dwell
    y: float
    progress: float  # 0 clears the indicator, 1 means it fired
    fired: bool


class DwellClicker:
    def __init__(self, dwell_s: float = 0.9, radius: float = 40.0) -> None:
        self.dwell_s = dwell_s
        self.radius = radius
        self._anchor: tuple[float, float, float] | None = None  # x, y, start time
        self._armed = True
        self._showing = False

    def reset(self) -> DwellUpdate | None:
        """Stop any dwell in progress.  Returns a clearing update when an indicator was showing."""
        self._anchor = None
        if self._showing:
            self._showing = False
            return DwellUpdate(0.0, 0.0, 0.0, False)
        return None

    def update(self, t: float, x: float, y: float, active: bool) -> DwellUpdate | None:
        if not active:
            return self.reset()
        if self._anchor is None:
            self._anchor = (x, y, t)
            return None
        ax, ay, t0 = self._anchor
        if math.hypot(x - ax, y - ay) > self.radius:
            self._anchor = (x, y, t)
            self._armed = True
            if self._showing:
                self._showing = False
                return DwellUpdate(ax, ay, 0.0, False)
            return None
        if not self._armed:
            return None
        progress = (t - t0) / max(self.dwell_s, 1e-3)
        if progress >= 1.0:
            self._armed = False
            self._showing = False
            self._anchor = (x, y, t)
            return DwellUpdate(ax, ay, 1.0, True)
        self._showing = True
        return DwellUpdate(ax, ay, progress, False)


def resume_rect(primary: Monitor, zone: str) -> Rect | None:
    """Where the resume zone is on the primary monitor, or None when it is turned off."""
    r = primary.rect
    if zone == "off":
        return None
    if zone == "top_left":
        w, h = max(0.12 * r.w, 200.0), max(0.12 * r.h, 120.0)
        return Rect(r.x, r.y, w, h)
    if zone == "top_right":
        w, h = max(0.12 * r.w, 200.0), max(0.12 * r.h, 120.0)
        return Rect(r.right - w, r.y, w, h)
    w, h = max(0.18 * r.w, 240.0), max(0.10 * r.h, 90.0)
    return Rect(r.x + (r.w - w) / 2.0, r.y, w, h)


@dataclass(frozen=True, slots=True)
class ZoneUpdate:
    progress: float
    fired: bool


class ResumeZone:
    def __init__(self, hold_s: float = 1.5, margin_px: float = 60.0, gap_s: float = 0.5) -> None:
        self.hold_s = hold_s
        self.margin_px = margin_px  # gaze is a few degrees off at best: be generous at the edges
        self.gap_s = gap_s
        self._start: float | None = None
        self._last_seen: float | None = None
        self._progress = 0.0
        self._armed = True  # after firing, the eyes must leave the zone before it can fire again

    def reset(self) -> ZoneUpdate | None:
        self._start = None
        self._last_seen = None
        self._armed = True
        if self._progress > 0.0:
            self._progress = 0.0
            return ZoneUpdate(0.0, False)
        return None

    def update(self, t: float, point: tuple[float, float] | None, rect: Rect | None, active: bool) -> ZoneUpdate | None:
        if not active or rect is None:
            return self.reset()
        if point is None:
            # Eyes closed or no estimate this frame: tolerate a short gap, like a blink.
            if self._last_seen is not None and t - self._last_seen > self.gap_s:
                return self.reset()
            return None
        inside = rect.inflate(self.margin_px).contains(point[0], point[1])
        if not inside:
            return self.reset()
        if not self._armed:
            return None
        self._last_seen = t
        if self._start is None:
            self._start = t
        progress = min((t - self._start) / max(self.hold_s, 1e-3), 1.0)
        if progress >= 1.0:
            self._start = None
            self._progress = 0.0
            self._armed = False
            return ZoneUpdate(1.0, True)
        self._progress = progress
        return ZoneUpdate(progress, False)
