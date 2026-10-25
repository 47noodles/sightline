"""Head pointing: small head movements move the pointer relatively, like a head mouse.

The signal is the image position of the nose bridge, divided by the distance
between the outer eye corners so it means the same at any seating distance.
Turning the head moves this point a lot (it sits well in front of the neck's
pivot), so it is much less noisy than the head angles MediaPipe reports.

Speed-dependent gain is a sigmoid curve, adapted from Project GameFace's
``SigmoidAccel`` (src/accel_graph.py, https://github.com/google/project-gameface):

    Copyright 2023 Google LLC
    Licensed under the Apache License, Version 2.0 (the "License"); you may not
    use this file except in compliance with the License.  You may obtain a copy
    of the License at http://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

Changes: the curve maps speed to a gain between a slow, precise floor and a
fast ceiling (GameFace multiplies the velocity by the sigmoid directly), and
a deadband replaces GameFace's smoothing kernel for tremor, because a
deadband stops tiny jitter completely instead of only averaging it.

Sign conventions, tested: turning or moving the head to the user's right
moves the pointer right; tilting or moving it up moves the pointer up.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .features import FaceMeasures, user_pose
from .filters import OneEuroFilter

log = logging.getLogger(__name__)

# Comfortable head range, in face units (1 unit = outer eye corner distance, about 9 cm):
# turning the head about 10 degrees either way moves the nose bridge roughly 0.2 units.
COMFORT_RANGE_UNITS = 0.42
COMFORT_YAW_DEG = 12.0
COMFORT_PITCH_DEG = 9.0


@dataclass
class HeadProfile:
    """What the head calibration learned.  ``gain_*`` are pixels per face unit at medium speed."""

    gain_x: float
    gain_y: float
    yaw_px_per_deg: float | None = None
    pitch_px_per_deg: float | None = None
    mirrored: bool | None = None
    created: float = field(default_factory=time.time)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), "utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> HeadProfile | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
            return cls(
                gain_x=float(data["gain_x"]),
                gain_y=float(data["gain_y"]),
                yaw_px_per_deg=data.get("yaw_px_per_deg"),
                pitch_px_per_deg=data.get("pitch_px_per_deg"),
                mirrored=data.get("mirrored"),
                created=float(data.get("created", 0.0)),
            )
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("Could not load the head profile %s: %s", path, e)
            return None


def sigmoid_gain(speed: float, lo: float = 0.22, hi: float = 1.6, mid: float = 0.12, width: float = 0.04) -> float:
    """Gain multiplier for a head speed in face units per second: slow moves are precise, fast ones travel."""
    return lo + (hi - lo) / (1.0 + math.exp(-(speed - mid) / width))


@dataclass(slots=True)
class HeadSample:
    dx: float  # pointer movement for this frame, pixels (gain and deadband applied)
    dy: float
    speed: float  # smoothed head speed, face units per second
    yaw: float  # user terms, relative to the neutral pose, degrees
    pitch: float
    x: float  # filtered head position in face units, relative to neutral (user right +, down +)
    y: float


class HeadTracker:
    def __init__(self, screen_w: float = 1920.0, screen_h: float = 1080.0) -> None:
        self.speed_scale = 1.0  # the user's "Head pointer speed"
        self.dead_zone = 0.3  # 0..1 setting
        self.profile: HeadProfile | None = None
        self.mirrored = False
        self._screen = (float(screen_w), float(screen_h))
        self._filter = OneEuroFilter(1.65, 12.0, 1.0)
        self._prev_px: tuple[float, float] | None = None
        self._iod: float | None = None
        self._pos = np.zeros(2)  # integrated head position, face units, user terms
        self._anchor: np.ndarray | None = None  # deadband anchor
        self._speed = 0.0
        self._t: float | None = None
        self._neutral_pose: tuple[float, float] | None = None
        self._neutral_pos = np.zeros(2)
        self._pose_buf: list[tuple[float, float]] = []
        self.last: HeadSample | None = None

    # ------------------------------------------------------------------ settings

    def set_screen(self, w: float, h: float) -> None:
        self._screen = (float(w), float(h))

    def set_smoothing(self, smoothing: float) -> None:
        s = min(max(smoothing, 0.0), 1.0)
        self._filter.min_cutoff = 2.5 - 1.7 * s

    @property
    def deadband(self) -> float:
        """Half-width of the deadband in face units (0.002 is about 0.2 mm of head movement)."""
        return 0.002 + 0.008 * min(max(self.dead_zone, 0.0), 1.0)

    @property
    def gains(self) -> tuple[float, float]:
        """Pixels per face unit at medium speed, before the user's speed setting."""
        if self.profile is not None:
            return abs(self.profile.gain_x), abs(self.profile.gain_y)
        w, h = self._screen
        # The comfortable range spans the screen width; vertical uses the same scale (heads nod less, screens are wide).
        g = w / COMFORT_RANGE_UNITS
        return g, max(g * 0.9, h / (COMFORT_RANGE_UNITS * 0.6))

    # ------------------------------------------------------------------ state

    def recenter(self) -> None:
        """Treat the current pose as neutral."""
        if self.last is not None and self._neutral_pose is not None:
            yaw0, pitch0 = self._neutral_pose
            self._neutral_pose = (yaw0 + self.last.yaw, pitch0 + self.last.pitch)
        else:
            self._neutral_pose = None
            self._pose_buf.clear()
        value = self._filter.value
        if value is not None:
            self._neutral_pos = value.copy()

    def lost(self) -> None:
        """Face gone: forget motion state so the pointer does not jump when it returns."""
        self._prev_px = None
        self._anchor = None
        self._speed = 0.0
        self._t = None
        self._filter.reset()
        self.last = None

    @property
    def has_neutral(self) -> bool:
        return self._neutral_pose is not None

    # ------------------------------------------------------------------ update

    def update(self, t: float, m: FaceMeasures) -> HeadSample:
        yaw, pitch, _roll = user_pose(m, self.mirrored)
        if self._neutral_pose is None:
            # Neutral is the median pose over the first half-second of tracking (or after recenter).
            self._pose_buf.append((yaw, pitch))
            if len(self._pose_buf) >= 15:
                arr = np.array(self._pose_buf)
                self._neutral_pose = (float(np.median(arr[:, 0])), float(np.median(arr[:, 1])))
                self._pose_buf.clear()
        yaw0, pitch0 = self._neutral_pose if self._neutral_pose is not None else (yaw, pitch)

        iod = max(m.iod_px, 1.0)
        self._iod = iod if self._iod is None else 0.95 * self._iod + 0.05 * iod
        if self._prev_px is not None:
            dx_px = m.head_x - self._prev_px[0]
            dy_px = m.head_y - self._prev_px[1]
            # An unmirrored camera sees the user's right on the image's left.
            ux = (dx_px if self.mirrored else -dx_px) / self._iod
            uy = dy_px / self._iod
            self._pos = self._pos + np.array([ux, uy])
        self._prev_px = (m.head_x, m.head_y)

        filtered = self._filter(self._pos, t)
        dt = 1.0 / 30.0 if self._t is None else max(1e-3, min(t - self._t, 0.25))
        self._t = t
        move = np.zeros(2)
        if self._anchor is None:
            self._anchor = filtered.copy()
        else:
            offset = filtered - self._anchor
            dist = float(np.hypot(*offset))
            band = self.deadband
            if dist > band:
                new_anchor = filtered - offset * (band / dist)
                move = new_anchor - self._anchor
                self._anchor = new_anchor
        inst_speed = float(np.hypot(*move)) / dt
        # Smoothed speed (about 80 ms) so one noisy frame does not jump to the fast end of the curve.
        a = 1.0 - math.exp(-dt / 0.08)
        self._speed += a * (inst_speed - self._speed)
        gain = sigmoid_gain(self._speed) * self.speed_scale
        gx, gy = self.gains
        sample = HeadSample(
            dx=float(move[0] * gx * gain),
            dy=float(move[1] * gy * gain),
            speed=self._speed,
            yaw=yaw - yaw0,
            pitch=pitch - pitch0,
            x=float(filtered[0] - self._neutral_pos[0]),
            y=float(filtered[1] - self._neutral_pos[1]),
        )
        self.last = sample
        return sample

    def direction_point(self, rect_x: float, rect_y: float, rect_w: float, rect_h: float) -> tuple[float, float] | None:
        """Where the head points on a screen rectangle, from yaw and pitch relative to neutral.

        Stands in for gaze before gaze calibration (the resume zone uses it).
        """
        s = self.last
        if s is None or self._neutral_pose is None:
            return None
        if self.profile is not None and self.profile.yaw_px_per_deg and self.profile.pitch_px_per_deg:
            kx, ky = abs(self.profile.yaw_px_per_deg), abs(self.profile.pitch_px_per_deg)
        else:
            kx, ky = (rect_w / 2.0) / COMFORT_YAW_DEG, (rect_h / 2.0) / COMFORT_PITCH_DEG
        cx, cy = rect_x + rect_w / 2.0, rect_y + rect_h / 2.0
        # Pointing past an edge means that edge: the zones the user aims for sit at the edges.
        x = min(max(cx + s.yaw * kx, rect_x), rect_x + rect_w - 1.0)
        y = min(max(cy - s.pitch * ky, rect_y), rect_y + rect_h - 1.0)
        return x, y


def fit_head_profile(
    head_xy: np.ndarray, poses: np.ndarray, targets: np.ndarray, *, mirrored: bool
) -> tuple[HeadProfile | None, str, dict]:
    """Head calibration: gains so the user's own range of movement covers the targets they turned towards.

    ``head_xy`` and ``poses`` (yaw, pitch) are per-target medians in user
    terms, ``targets`` the matching screen points.  A negative slope means
    the camera mirrors its picture.
    """
    details: dict = {"targets": len(targets)}
    if len(targets) < 3:
        return None, "Not enough head movement was seen. Turn your head towards each dot and try again.", details
    tx, ty = targets[:, 0], targets[:, 1]

    def slope(x: np.ndarray, y: np.ndarray) -> float | None:
        if float(np.ptp(x)) < 1e-6 or float(np.ptp(y)) < 1.0:
            return None
        return float(np.polyfit(x, y, 1)[0])

    sx = slope(head_xy[:, 0], tx)
    sy = slope(head_xy[:, 1], ty)
    yaw_k = slope(poses[:, 0], tx)
    pitch_k = slope(-poses[:, 1], ty)
    details.update(slope_x=sx, slope_y=sy, yaw_px_per_deg=yaw_k, pitch_px_per_deg=pitch_k)
    if sx is None and sy is None:
        return None, "Not enough head movement was seen. Turn your head towards each dot and try again.", details
    flipped = sx is not None and sx < 0
    new_mirrored = (not mirrored) if flipped else mirrored
    span_x = float(np.ptp(head_xy[:, 0]))
    span_y = float(np.ptp(head_xy[:, 1]))
    details["span_x"] = span_x
    details["span_y"] = span_y
    # Very small movement makes huge gains; cap them so the pointer stays controllable.
    gain_x = min(abs(sx), 40000.0) if sx is not None else None
    gain_y = min(abs(sy), 40000.0) if sy is not None else None
    if gain_x is None:
        gain_x = gain_y
    if gain_y is None:
        gain_y = gain_x
    assert gain_x is not None and gain_y is not None
    profile = HeadProfile(
        gain_x=gain_x,
        gain_y=gain_y,
        yaw_px_per_deg=abs(yaw_k) if yaw_k else None,
        pitch_px_per_deg=abs(pitch_k) if pitch_k else None,
        mirrored=new_mirrored,
    )
    small = span_x < 0.05 and span_y < 0.05
    if small:
        message = "Your head moved very little, so head pointing is set to be extra sensitive."
    else:
        message = "Head pointing now matches how far you move your head."
    if flipped:
        message += " Your camera shows a mirror image, so left and right were swapped to match."
    details["mirrored"] = new_mirrored
    return profile, message, details
