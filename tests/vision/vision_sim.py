"""A wizard, a user and a camera sharing one clock, for tests of the features built on calibration.

``Sim`` drives a ``VisionPipeline`` frame by frame with the synthetic user.
Besides where the user looks, it can give the face an expression (blendshape
values, as MediaPipe would report them) and a talking mouth.  Nothing here
touches the real cursor: the pipeline's desktop is ``FakeDesktop``.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace

import numpy as np
from vision_synth import SyntheticUser

from sightline.vision.attention import LOWER_LIP, UPPER_LIP
from sightline.vision.gaze_model import GazeModel, TrainingData
from sightline.vision.landmarks import BS, FaceFrame
from sightline.vision.pipeline import VisionPipeline

LATENCY = 0.04
GRID = [(x, y) for y in (110, 540, 970) for x in (190, 960, 1730)]
HEAD_PHASE = [(560, 320), (1360, 320), (560, 760), (1360, 760)]

# Expressions as MediaPipe scores them.  The template photo smiles, so the resting face is set explicitly.
NEUTRAL = {"mouthSmileLeft": 0.05, "mouthSmileRight": 0.05, "browInnerUp": 0.05, "browOuterUpLeft": 0.04,
           "browOuterUpRight": 0.04, "jawOpen": 0.04, "cheekPuff": 0.02}  # fmt: skip
SMILE = {"mouthSmileLeft": 0.8, "mouthSmileRight": 0.8}
BROWS = {"browInnerUp": 0.75, "browOuterUpLeft": 0.7, "browOuterUpRight": 0.7}
MOUTH_OPEN = {"jawOpen": 0.6}
CHEEKS = {"cheekPuff": 0.5}
EXPRESSIONS = {"smile": SMILE, "brow_raise": BROWS, "mouth_open": MOUTH_OPEN, "cheek_puff": CHEEKS}
_BROW_NAMES = ("browInnerUp", "browOuterUpLeft", "browOuterUpRight")


def express(
    face: FaceFrame,
    values: dict[str, float],
    lip_gap_px: float | None = None,
    *,
    noise: float = 0.0,
    rng: np.random.Generator | None = None,
) -> FaceFrame:
    """The frame with these blendshape scores (and the inner lips this far apart)."""
    if not values and lip_gap_px is None:
        return face
    bs = face.blendshapes.copy()
    for name, v in values.items():
        if rng is not None and noise > 0:
            v = v + float(rng.normal(0.0, noise))
        bs[BS[name]] = min(max(v, 0.0), 1.0)
    lm = face.landmarks
    if lip_gap_px is not None:
        lm = lm.copy()
        lm[LOWER_LIP, 1] = lm[UPPER_LIP, 1] + lip_gap_px / face.height
    return replace(face, blendshapes=bs.astype(np.float32), landmarks=lm)


def talking_mouth(t: float) -> tuple[dict[str, float], float]:
    """Speech: the jaw and lips open and close about four times a second."""
    phase = math.sin(2 * math.pi * 4.0 * t)
    return {"jawOpen": 0.14 + 0.11 * phase}, 6.0 + 5.0 * phase


class Sim:
    def __init__(self, ctx, user: SyntheticUser, *, mirrored: bool = False, now: float = 50.0) -> None:
        self.now = now
        self.ctx = ctx
        self.user = user
        self.mirrored = mirrored
        self.pipe = VisionPipeline(ctx, clock=lambda: self.now)
        self.cal = self.pipe.calibration
        self.look_at = (960.0, 540.0)
        self.yaw = 0.0
        self.pitch = 0.0
        self.closure = (0.0, 0.0)
        self.expression: dict[str, float] = {}
        self.brow_look = 0.0  # how much looking up raises this user's eyebrows (real faces do it a little)
        self.talk: Callable[[float], tuple[dict[str, float], float]] | None = None
        self.path: Callable[[float], tuple[float, float]] | None = None  # where the eyes are at time t, if moving
        self.head_path: Callable[[float], tuple[float, float]] | None = None  # (yaw, pitch) at time t, if moving
        self.next_frame = self.now
        self.counts: list[int] = []

    def frame(self, t: float) -> FaceFrame:
        x, y = self.path(t) if self.path is not None else self.look_at
        yaw, pitch = self.head_path(t) if self.head_path is not None else (self.yaw, self.pitch)
        f = self.user.look(t, x, y, yaw=yaw, pitch=pitch, closure=self.closure, mirrored=self.mirrored)
        values = dict(NEUTRAL)
        if self.brow_look:
            look = 0.5 * float(f.blendshapes[BS["eyeLookUpLeft"]] + f.blendshapes[BS["eyeLookUpRight"]])
            for name in _BROW_NAMES:
                values[name] += self.brow_look * look
        values.update(self.expression)
        gap = None
        if self.talk is not None:
            extra, gap = self.talk(t)
            values.update(extra)
        return express(f, values, gap, noise=0.02, rng=self.user.rng)

    def run(self, seconds: float) -> None:
        end = self.now + seconds
        while self.next_frame <= end + 1e-9:
            self.now = self.next_frame
            self.pipe.process(self.frame(self.now - LATENCY), self.now)
            self.next_frame += 1 / 30
        self.now = end

    def dot(self, x: float, y: float, *, react: float = 0.2, settle: float = 0.4, collect: float = 0.8, head=None):
        self.cal.set_target(x, y, False)
        self.run(react)
        self.look_at = (x, y)  # the eyes arrive
        if head is not None:
            self.yaw, self.pitch = head
        self.run(settle - react)
        self.cal.set_target(x, y, True)
        self.run(collect)
        self.counts.append(self.cal.sample_count())
        self.cal.set_target(x, y, False)

    def calibrate(self, targets=None, **kw):
        self.cal.begin("gaze")
        rng = np.random.default_rng(7)
        for x, y in targets or GRID + HEAD_PHASE:
            self.dot(x, y, head=(rng.normal(0, 2.5), rng.normal(0, 2.0)), **kw)
        self.yaw = self.pitch = 0.0
        return self.cal.finish()

    def use_model(self) -> None:
        """Make the pipeline calibrated, from one calibration shared by every test that asks."""
        model, data = shared_model()
        self.pipe.activate_gaze(model, data)


_MODEL: dict[str, tuple[GazeModel, TrainingData]] = {}


def shared_model() -> tuple[GazeModel, TrainingData]:
    """A gaze model of the standard synthetic user, fitted once per test run."""
    if "gaze" not in _MODEL:
        from sightline.core.context import AppContext

        sim = Sim(AppContext.for_tests(), SyntheticUser(seed=11))
        report = sim.calibrate()
        assert report.ok, report.message
        model = sim.pipe.current_gaze_model()
        data = sim.pipe.calibration_samples()
        assert model is not None and data is not None
        _MODEL["gaze"] = (model, data)
    return _MODEL["gaze"]
