"""Eye closure, blinks and winks.

Closure
-------
Each eye's closure (0 open .. 1 shut) fuses two signals: MediaPipe's
``eyeBlinkLeft/Right`` blendshape and the eye aspect ratio from the lid
landmarks, divided by that eye's own open level (a running percentile, so it
adapts to the person and the camera angle).  Photos of real winks showed the
blendshape alone rising only to 0.25-0.5 for a fully closed eye, so neither
signal is trusted on its own.  The eye calibration replaces the default
fusion with a per-eye logistic model fitted to the user's own open, closed
and winking eyes.

Gestures
--------
The rules port the ideas of shaal/eye-tracker (MIT), ADR-0008 and ADR-0013:
natural blinks are short and close both eyes, so they are rejected by
duration; a wink is told apart by one eye staying clearly open, which a
natural blink never does.  On top of that:

* both eyes closed for ``click_s`` arms a click (EyeHold stage 1), held to
  ``double_s`` arms a double click (stage 2), held past ``cancel_s`` cancels;
  the gesture fires when the eyes open;
* a wink needs both eyes open for ``pre_open_s`` before it, the other eye
  clearly open the whole time, and a duration from ``wink_min_s`` to
  ``wink_max_s``, so the uneven start or end of a natural blink never counts;
* the pointer freezes as soon as either eyelid starts to move, and the click
  goes to where the pointer was just before (``onset`` minus a lookback),
  because a closing lid drags the gaze estimate down;
* losing the face mid-gesture cancels it.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np

from .features import EyeMeasure

log = logging.getLogger(__name__)

DEFAULT_EAR_OPEN = 0.28
EAR_HISTORY = 300  # frames, about 10 s at 30 fps


# --------------------------------------------------------------------------- per-user eye profile


@dataclass
class EyeChannel:
    """Calibrated closure model for one eye: p(closed) = sigmoid(w_blink*blink + w_ratio*ratio + bias)."""

    w_blink: float
    w_ratio: float
    bias: float
    ear_open: float

    def closure(self, blink: float, ratio: float) -> float:
        z = self.w_blink * blink + self.w_ratio * ratio + self.bias
        return 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, z))))


@dataclass
class EyeProfile:
    """What the eye calibration learned about this user.  Saved as JSON in the profile folder."""

    left: EyeChannel | None = None
    right: EyeChannel | None = None
    clear_left: float = 0.3  # the left eye counts as clearly open (during a right wink) below this
    clear_right: float = 0.3
    wink_left_ok: bool = True
    wink_right_ok: bool = True
    mirrored: bool | None = None  # None: not determined
    natural_blink_ms: float | None = None
    created: float = field(default_factory=time.time)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), "utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> EyeProfile | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
            left = EyeChannel(**data["left"]) if data.get("left") else None
            right = EyeChannel(**data["right"]) if data.get("right") else None
            return cls(
                left=left,
                right=right,
                clear_left=float(data.get("clear_left", 0.3)),
                clear_right=float(data.get("clear_right", 0.3)),
                wink_left_ok=bool(data.get("wink_left_ok", True)),
                wink_right_ok=bool(data.get("wink_right_ok", True)),
                mirrored=data.get("mirrored"),
                natural_blink_ms=data.get("natural_blink_ms"),
                created=float(data.get("created", 0.0)),
            )
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("Could not load the eye profile %s: %s", path, e)
            return None


# --------------------------------------------------------------------------- closure


class _OpenLevel:
    """Running estimate of one eye's open aspect ratio (80th percentile of recent, probably open frames)."""

    def __init__(self, initial: float = DEFAULT_EAR_OPEN) -> None:
        self._buf: deque[float] = deque(maxlen=EAR_HISTORY)
        self.value = initial
        self._since = 0

    def add(self, ear: float, blink: float) -> None:
        if blink < 0.35 and 0.05 < ear < 0.8:
            self._buf.append(ear)
        self._since += 1
        if self._since >= 10 and len(self._buf) >= 30:
            self._since = 0
            self.value = float(min(max(np.percentile(self._buf, 80.0), 0.12), 0.5))

    @property
    def ready(self) -> bool:
        return len(self._buf) >= 30


def default_closure(blink: float, ratio: float) -> float:
    """Uncalibrated fusion, chosen from photos of open, closed and winking eyes.

    A narrowed eye (looking down, smiling: ratio 0.75-0.85) contributes
    little; lids that meet (ratio under 0.45) count as fully shut.
    """
    b = min(max((blink - 0.15) / 0.40, 0.0), 1.0)
    r = min(max((0.85 - ratio) / 0.40, 0.0), 1.0)
    return 0.5 * b + 0.5 * r


class ClosureEstimator:
    """Per-frame closure (0..1) of the user's own left and right eye."""

    def __init__(self, profile: EyeProfile | None = None) -> None:
        self.profile = profile
        self._left = _OpenLevel(profile.left.ear_open if profile and profile.left else DEFAULT_EAR_OPEN)
        self._right = _OpenLevel(profile.right.ear_open if profile and profile.right else DEFAULT_EAR_OPEN)
        self.last_ratio = (1.0, 1.0)

    def set_profile(self, profile: EyeProfile | None) -> None:
        self.profile = profile

    def update(self, left: EyeMeasure, right: EyeMeasure) -> tuple[float, float]:
        self._left.add(left.ear, left.blink)
        self._right.add(right.ear, right.blink)
        p = self.profile
        open_l = self._left.value if self._left.ready or not (p and p.left) else p.left.ear_open
        open_r = self._right.value if self._right.ready or not (p and p.right) else p.right.ear_open
        rl = left.ear / max(open_l, 1e-3)
        rr = right.ear / max(open_r, 1e-3)
        self.last_ratio = (rl, rr)
        cl = p.left.closure(left.blink, rl) if p and p.left else default_closure(left.blink, rl)
        cr = p.right.closure(right.blink, rr) if p and p.right else default_closure(right.blink, rr)
        return cl, cr

    @property
    def open_levels(self) -> tuple[float, float]:
        return self._left.value, self._right.value


# --------------------------------------------------------------------------- gesture state machine


@dataclass
class GestureConfig:
    click_s: float = 0.35
    double_s: float = 1.0
    cancel_s: float = 2.5
    wink_min_s: float = 0.18
    wink_max_s: float = 2.5
    pre_open_s: float = 0.15
    close: float = 0.6  # an eye counts as closed above this (hysteresis with ``open``)
    open: float = 0.4
    freeze: float = 0.35  # any eye above this: the lid is moving, freeze the pointer
    clear_left: float = 0.3  # "clearly open" level for the non-winking eye
    clear_right: float = 0.3
    release_frames: int = 2  # open frames in a row before the eyes count as open again
    partial_s: float = 0.5  # one eye still shut this long after a two-eye hold: cancel
    settle_s: float = 0.06  # keep the pointer frozen this long after the eyes open
    lookback_s: float = 0.12  # click where the pointer was this long before the lids started to move
    blinks: bool = True
    wink_left: bool = True
    wink_right: bool = True


HoldEvent = tuple[Literal["hold"], int, bool]  # stage, cancelled
GestureKind = Literal["long_blink", "hold_blink", "wink_left", "wink_right"]


@dataclass(frozen=True, slots=True)
class Gesture:
    kind: GestureKind
    duration: float
    onset: float  # when the lids started to move; the click goes to the pointer just before this
    t: float


@dataclass(frozen=True, slots=True)
class Hold:
    stage: int
    cancelled: bool
    t: float


class EyeGestureDetector:
    """Feed it closure values every frame; it returns Hold and Gesture events."""

    def __init__(self, cfg: GestureConfig | None = None) -> None:
        self.cfg = cfg or GestureConfig()
        self.blink_durations: deque[float] = deque(maxlen=200)  # natural blinks, for diagnostics
        self._reset_all()

    def _reset_all(self) -> None:
        self._closed = [False, False]
        self._open_since: float | None = None
        self._open_run = 0
        self._first_open_t: float | None = None
        self._settle_until = 0.0
        self._present = False
        self._new_episode(None, 0.0)
        self._episode = False

    def _new_episode(self, onset: float | None, open_before: float) -> None:
        self._episode = onset is not None
        self._onset = onset
        self._open_before = open_before
        self._both_since: float | None = None
        self._both_last: float | None = None
        self._partial_since: float | None = None
        self._had_both = False
        self._stage = 0
        self._cancelled = False
        self._wink_eye: int | None = None
        self._wink_start: float | None = None
        self._wink_end: float | None = None
        self._wink_spoiled = False

    # ------------------------------------------------------------------ queries

    @property
    def frozen(self) -> bool:
        return self._episode

    def frozen_at(self, t: float) -> bool:
        return self._episode or t < self._settle_until

    @property
    def onset(self) -> float | None:
        return self._onset if self._episode else None

    @property
    def stage(self) -> int:
        return self._stage

    def state_name(self) -> str:
        if not self._present:
            return "unknown"
        left, right = self._closed
        if left and right:
            return "closed"
        if left:
            return "left_wink"
        if right:
            return "right_wink"
        return "open"

    # ------------------------------------------------------------------ update

    def lost(self, t: float) -> list[Hold | Gesture]:
        """The face disappeared: whatever was in progress is cancelled."""
        events: list[Hold | Gesture] = []
        if self._episode and self._stage > 0 and not self._cancelled and self.cfg.blinks:
            events.append(Hold(0, True, t))
        self._reset_all()
        return events

    def update(self, t: float, closure_left: float, closure_right: float) -> list[Hold | Gesture]:
        cfg = self.cfg
        events: list[Hold | Gesture] = []
        if not self._present:
            self._present = True
            self._open_since = None  # the face just came back: no history of open eyes yet
        c = (closure_left, closure_right)
        for i in (0, 1):
            if self._closed[i]:
                if c[i] < cfg.open:
                    self._closed[i] = False
            elif c[i] > cfg.close:
                self._closed[i] = True
        moving = max(c) > cfg.freeze or self._closed[0] or self._closed[1]

        if not self._episode:
            if not moving:
                if self._open_since is None:
                    self._open_since = t
                return events
            open_before = 0.0 if self._open_since is None else t - self._open_since
            self._open_since = None
            self._new_episode(t, open_before)
            self._open_run = 0

        # ---- inside an episode: some lid is moving
        if moving:
            self._open_run = 0
            self._first_open_t = None
        else:
            self._open_run += 1
            if self._first_open_t is None:
                self._first_open_t = t

        both = self._closed[0] and self._closed[1]
        if both:
            if self._both_since is None:
                self._both_since = t
            self._had_both = True
            self._both_last = t
            self._partial_since = None
            if not self._cancelled:
                held = t - self._both_since
                if held >= cfg.cancel_s:
                    self._cancelled = True
                    if cfg.blinks:
                        events.append(Hold(0, True, t))
                elif held >= cfg.double_s and self._stage < 2:
                    self._stage = 2
                    if cfg.blinks:
                        events.append(Hold(2, False, t))
                elif held >= cfg.click_s and self._stage < 1:
                    self._stage = 1
                    if cfg.blinks:
                        events.append(Hold(1, False, t))
        elif self._had_both and moving:
            # One eye opened after a two-eye hold while the other stays shut.
            if self._partial_since is None:
                self._partial_since = t
            elif t - self._partial_since > cfg.partial_s and not self._cancelled:
                self._cancelled = True
                if cfg.blinks and self._stage > 0:
                    events.append(Hold(0, True, t))

        # ---- wink bookkeeping
        if self._had_both:
            self._wink_spoiled = True
        else:
            clear = (cfg.clear_left, cfg.clear_right)
            more_open = 0 if c[0] < c[1] else 1
            if c[more_open] >= clear[more_open]:
                self._wink_spoiled = True  # both lids moving: a blink, not a wink
            for e in (0, 1):
                if self._closed[e] and not self._closed[1 - e]:
                    if self._wink_eye is None:
                        self._wink_eye = e
                        self._wink_start = t
                        self._wink_end = None
                    elif self._wink_eye != e:
                        self._wink_spoiled = True
            if self._wink_eye is not None and self._wink_end is None and not self._closed[self._wink_eye]:
                self._wink_end = t

        if not moving and self._open_run >= cfg.release_frames:
            end_t = self._first_open_t if self._first_open_t is not None else t
            events.extend(self._finish(end_t, t))
        return events

    def _finish(self, end_t: float, t: float) -> list[Hold | Gesture]:
        """The eyes are open again: decide what the episode was."""
        cfg = self.cfg
        events: list[Hold | Gesture] = []
        onset = self._onset if self._onset is not None else end_t
        if self._had_both and self._both_since is not None:
            held_until = self._both_last if self._both_last is not None else end_t
            duration = max(end_t, held_until) - self._both_since
            if self._cancelled:
                pass
            elif self._stage >= 2 and cfg.blinks:
                events.append(Hold(0, False, t))
                events.append(Gesture("hold_blink", duration, onset, t))
            elif self._stage == 1 and cfg.blinks:
                events.append(Hold(0, False, t))
                events.append(Gesture("long_blink", duration, onset, t))
            elif self._stage == 0:
                self.blink_durations.append(duration)
        elif (
            self._wink_eye is not None
            and not self._wink_spoiled
            and self._wink_start is not None
            and self._open_before >= cfg.pre_open_s
        ):
            wink_end = self._wink_end if self._wink_end is not None else end_t
            duration = wink_end - self._wink_start
            enabled = cfg.wink_left if self._wink_eye == 0 else cfg.wink_right
            if enabled and cfg.wink_min_s <= duration <= cfg.wink_max_s:
                kind: GestureKind = "wink_left" if self._wink_eye == 0 else "wink_right"
                events.append(Gesture(kind, duration, onset, t))
        self._new_episode(None, 0.0)
        self._open_since = end_t
        self._settle_until = t + cfg.settle_s
        return events


# --------------------------------------------------------------------------- eye calibration fit


@dataclass
class EyeSample:
    label: str  # "open", "closed", "wink_left", "wink_right"
    blink_left: float
    blink_right: float
    ear_left: float
    ear_right: float


@dataclass
class EyeFitResult:
    profile: EyeProfile | None
    ok: bool
    message: str
    details: dict


def _logistic(x: np.ndarray, y: np.ndarray, l2: float = 0.05, iters: int = 30) -> np.ndarray:
    """Regularised logistic regression by Newton's method.  x has a trailing column of ones."""
    w = np.zeros(x.shape[1])
    reg = np.full(x.shape[1], l2)
    reg[-1] = 0.0  # do not penalise the bias
    for _ in range(iters):
        z = np.clip(x @ w, -40, 40)
        p = 1.0 / (1.0 + np.exp(-z))
        grad = x.T @ (p - y) + reg * w
        hess = (x.T * (p * (1 - p))) @ x + np.diag(reg + 1e-9)
        step = np.linalg.solve(hess, grad)
        w -= step
        if float(np.max(np.abs(step))) < 1e-6:
            break
    return w


def fit_eye_profile(samples: list[EyeSample], *, previous_mirrored: bool = False) -> EyeFitResult:
    """Fit per-eye closure models from labelled samples, and decide whether this user's winks are usable.

    Open and closed samples train the models; wink samples then test them.
    A wink only counts as usable if, for most of the time the user was
    asked to wink that eye, that eye read as closed while the other read as
    clearly open.  Otherwise winks for that eye are switched off, so they
    can never misfire.
    """
    by = {k: [s for s in samples if s.label == k] for k in ("open", "closed", "wink_left", "wink_right")}
    details: dict = {k: len(v) for k, v in by.items()}
    if len(by["open"]) < 10 or len(by["closed"]) < 10:
        return EyeFitResult(None, False, "Not enough data. Keep your face in view of the camera and try again.", details)

    def arr(lst: list[EyeSample]) -> np.ndarray:
        return np.array([(s.blink_left, s.blink_right, s.ear_left, s.ear_right) for s in lst], dtype=np.float64)

    open_a = arr(by["open"])
    closed_a = arr(by["closed"])
    wl = arr(by["wink_left"]) if by["wink_left"] else np.zeros((0, 4))
    wr = arr(by["wink_right"]) if by["wink_right"] else np.zeros((0, 4))

    # A mirrored camera swaps which eye closes during each wink.  Decide before fitting anything per eye.
    mirrored = previous_mirrored
    swapped_l = len(wl) >= 10 and _eye_diff(wl) < -0.08  # asked for left, the right eye closed more
    swapped_r = len(wr) >= 10 and _eye_diff(wr) > 0.08
    proper_l = len(wl) >= 10 and _eye_diff(wl) > 0.08
    proper_r = len(wr) >= 10 and _eye_diff(wr) < -0.08
    if swapped_l and swapped_r:
        # Both winks closed the "wrong" channel: the camera mapping is backwards.  Swap the channels
        # (the labels stay: they are what the user was asked to do).
        mirrored = not previous_mirrored
        open_a, closed_a, wl, wr = (_swap(a) for a in (open_a, closed_a, wl, wr))
        details["swapped"] = True
    elif proper_l or proper_r:
        mirrored = previous_mirrored

    ear_open_l = float(np.percentile(open_a[:, 2], 80))
    ear_open_r = float(np.percentile(open_a[:, 3], 80))
    channels = []
    for eye, ear_open in ((0, ear_open_l), (1, ear_open_r)):
        xs, ys = [], []
        for a, label in ((open_a, 0.0), (closed_a, 1.0)):
            blink = a[:, eye]
            ratio = a[:, 2 + eye] / max(ear_open, 1e-3)
            xs.append(np.column_stack([blink, ratio, np.ones(len(a))]))
            ys.append(np.full(len(a), label))
        w = _logistic(np.vstack(xs), np.concatenate(ys))
        channels.append(EyeChannel(float(w[0]), float(w[1]), float(w[2]), ear_open))
    left, right = channels

    def probs(a: np.ndarray) -> np.ndarray:
        if len(a) == 0:
            return np.zeros((0, 2))
        return np.array(
            [
                (left.closure(b_l, e_l / ear_open_l), right.closure(b_r, e_r / ear_open_r))
                for b_l, b_r, e_l, e_r in a
            ]
        )

    p_open = probs(open_a)
    p_closed = probs(closed_a)
    # Blink separation: closed frames should read high, open frames low, for both eyes.
    sep = float(min(np.percentile(p_closed, 20, axis=0).min(), 1.0) - np.percentile(p_open, 90, axis=0).max())
    details["separation"] = round(sep, 3)
    if sep < 0.2:
        return EyeFitResult(
            None,
            False,
            "Could not tell when your eyes were closed. More light on your face may help. Try again.",
            details,
        )
    closed_floor = float(np.percentile(p_closed, 10, axis=0).min())

    profile = EyeProfile(left=left, right=right, mirrored=mirrored)
    wink_ok = []
    for eye, a in ((0, wl), (1, wr)):
        p = probs(a)
        if len(p) < 10:
            wink_ok.append(False)
            continue
        other = 1 - eye
        # How open the other eye stays while winking: allow a squint, but stay well below a real blink.
        clear = float(np.clip(np.percentile(p[:, other], 90) + 0.05, 0.3, 0.5))
        clear = min(clear, closed_floor - 0.25)
        good = (p[:, eye] > 0.6) & (p[:, other] < clear)
        frac = float(good.mean())
        details[f"wink_{'left' if eye == 0 else 'right'}_good"] = round(frac, 2)
        ok = frac >= 0.5 and clear >= 0.2
        wink_ok.append(ok)
        if eye == 0:
            profile.clear_right = clear if ok else 0.3
        else:
            profile.clear_left = clear if ok else 0.3
    profile.wink_left_ok, profile.wink_right_ok = wink_ok

    if all(wink_ok):
        message = "Blinks and winks are set up."
    elif wink_ok[0]:
        message = "Blinks and left winks are set up. Your right wink could not be told apart from a blink, so it is off."
    elif wink_ok[1]:
        message = "Blinks and right winks are set up. Your left wink could not be told apart from a blink, so it is off."
    else:
        message = "Blinks are set up. Your winks could not be told apart from blinks, so wink clicks are off."
    if details.get("swapped"):
        message += " Your camera shows a mirror image, so left and right were swapped to match."
    details["wink_left_ok"], details["wink_right_ok"] = wink_ok
    details["mirrored"] = mirrored
    return EyeFitResult(profile, True, message, details)


def _eye_diff(a: np.ndarray) -> float:
    """Mean (left minus right) closeness during a wink: blendshape difference plus aspect-ratio difference."""
    blink = float(np.mean(a[:, 0] - a[:, 1]))
    ratio = float(np.mean(a[:, 3] - a[:, 2]) / max(float(np.mean(a[:, 2:4])), 1e-3))
    return 0.5 * blink + 0.5 * ratio


def _swap(a: np.ndarray) -> np.ndarray:
    if len(a) == 0:
        return a
    return a[:, [1, 0, 3, 2]]
