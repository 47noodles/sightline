"""Aiming spoken commands: where the pointer came to rest when the user began to speak.

Also the post-jump quiet period, which keeps the head turn that came with a
large eye movement from pushing the pointer past its target.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from vision_fakes import OPEN, FakeLandmarker, PacedSource, image, wait_for
from vision_synth import SyntheticUser

from sightline.core.events import PointerSample
from sightline.core.fakes import FakeDisplay
from sightline.core.types import PointerMode
from sightline.vision.filters import GazeState
from sightline.vision.head import HeadSample
from sightline.vision.pointer import PointerEngine, find_rest
from sightline.vision.service import VisionService

R, W = 12.0, 0.1  # the engine's defaults: at rest = moved under 12 px over 100 ms


def history(path, t1: float = 3.0, fps: float = 30.0, jitter_s: float = 0.0, noise_px: float = 0.0, seed: int = 0):
    """(t, x, y) samples of ``path(t)`` from 0 to ``t1``, optionally with uneven frame times and noise."""
    rng = np.random.default_rng(seed)
    out, t = [], 0.0
    while t <= t1:
        x, y = path(t)
        if noise_px:
            x, y = x + rng.normal(0, noise_px), y + rng.normal(0, noise_px)
        out.append((t, float(x), float(y)))
        t += 1.0 / fps + (rng.uniform(-jitter_s, jitter_s) if jitter_s else 0.0)
    return out


def glide(a, b, t0: float, t1: float):
    """Rest at ``a``, move at constant speed to ``b`` between ``t0`` and ``t1``, rest at ``b``."""

    def path(t: float):
        f = min(max((t - t0) / (t1 - t0), 0.0), 1.0)
        return a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])

    return path


def near(p, q, tol: float = 1.0) -> bool:
    return p is not None and math.hypot(p[0] - q[0], p[1] - q[1]) <= tol


def test_resting_at_the_onset_gives_that_point():
    h = history(glide((500, 300), (1400, 800), 1.6, 1.9))
    assert near(find_rest(h, 1.0, R, W), (500, 300))
    assert near(find_rest(h, 2.5, R, W), (1400, 800))


def test_moving_at_the_onset_gives_where_it_came_to_rest():
    # The head steers the pointer at 600 px/s while the user starts to speak.
    h = history(glide((500, 300), (800, 300), 1.0, 1.5))
    assert near(find_rest(h, 1.2, R, W), (800, 300), tol=R)


def test_never_resting_gives_the_latest_target():
    h = history(lambda t: (100 + 300 * t, 500))
    assert find_rest(h, 1.0, R, W) == (h[-1][1], h[-1][2])


def test_outside_the_history():
    h = history(lambda t: (700, 400), t1=1.0)
    assert find_rest([], 0.5, R, W) is None
    assert find_rest(h, -0.5, R, W) is None  # older than the history
    assert find_rest(h, 5.0, R, W) == (700, 400)  # newer than the last frame: where it is now


def test_slow_creep_and_jitter_count_as_resting():
    creep = history(lambda t: (600 + 40 * t, 400))  # 40 px/s: under 12 px per 100 ms
    assert near(find_rest(creep, 1.0, R, W), (640, 400), tol=2)
    noisy = history(lambda t: (900, 500), noise_px=2.0, seed=3)
    assert near(find_rest(noisy, 1.0, R, W), (900, 500), tol=8)


def test_uneven_frame_times():
    h = history(glide((500, 300), (800, 300), 1.0, 1.5), jitter_s=0.008, seed=4)
    assert near(find_rest(h, 0.7, R, W), (500, 300))
    assert near(find_rest(h, 1.2, R, W), (800, 300), tol=R)


def test_a_dropped_stretch_of_frames_is_not_mistaken_for_rest():
    h = [s for s in history(glide((500, 300), (900, 300), 1.0, 1.4)) if not 1.05 < s[0] < 1.3]
    # Right after the gap there is too little window to judge; the answer is where it settled.
    assert near(find_rest(h, 1.1, R, W), (900, 300), tol=R)


# --------------------------------------------------------------------------- engine


def gaze(x: float, y: float) -> GazeState:
    return GazeState(x, y, True, x, y, 0.3, 10.0, 30.0, False)


def head(dx: float = 0.0, dy: float = 0.0) -> HeadSample:
    return HeadSample(dx, dy, 0.1, 0.0, 0.0, 0.0, 0.0)


def step(e: PointerEngine, t: float, **kw):
    args = dict(present=True, mode=PointerMode.HYBRID, gaze=None, head=None, frozen=False, paused=False)
    args.update(kw)
    return e.update(t, **args)


def test_engine_keeps_three_seconds_and_answers_only_while_active():
    e = PointerEngine(FakeDisplay(), (960.0, 540.0))
    assert e.rest_point(0.0) is None  # no history yet
    for i in range(150):  # 5 s at 30 fps, head mode, still
        step(e, i / 30, mode=PointerMode.HEAD)
    now = 149 / 30
    assert e.rest_point(now - 2.5) == (960.0, 540.0)
    assert e.rest_point(now - 3.5) is None  # older than the history
    step(e, now + 1 / 30, paused=True)
    assert e.rest_point(now) is None  # vision is not driving the pointer


def test_head_is_ignored_for_a_moment_after_a_jump():
    e = PointerEngine(FakeDisplay(), (960.0, 540.0))
    assert step(e, 0.0, gaze=gaze(1500, 300)) == "warp"
    # The head turn that came with the eye movement keeps arriving for a few frames.
    for i in range(1, 6):
        step(e, i / 30, gaze=gaze(1500, 300), head=head(15.0, 0.0))
    assert e.position == (1500.0, 300.0)
    assert e.cfg.head_quiet_s == pytest.approx(0.2)
    step(e, 0.25, gaze=gaze(1500, 300), head=head(15.0, 0.0))
    assert e.position[0] == pytest.approx(1500 + 15 * e.cfg.fine_scale)


# --------------------------------------------------------------------------- service


def test_service_aim_point(vctx, rec):
    user = SyntheticUser(seed=4)
    svc = VisionService(
        vctx,
        source_factory=lambda: PacedSource([image(OPEN)] * 10, t0=500.0, delay=1 / 30),  # real time
        landmarker_factory=lambda: FakeLandmarker(user),
        start_pump=False,
    )
    assert svc.aim_point(500.0) is None  # not running
    svc.start()
    try:
        assert wait_for(lambda: len(rec.of(PointerSample)) > 40)
        t_now = rec.of(PointerSample)[-1].t
        x, y = svc.pipeline.engine.position
        assert near(svc.aim_point(t_now - 0.5), (x, y))
        assert svc.aim_point(t_now - 60.0) is None  # older than the history
        vctx.state.update(pointer_paused=True)
        assert svc.aim_point(t_now) is None
        vctx.state.update(pointer_paused=False)
        svc.pipeline.set_yielding(True)
        assert svc.aim_point(t_now) is None  # someone is using the mouse
        svc.pipeline.set_yielding(False)
        assert svc.aim_point(t_now) is not None
    finally:
        svc.stop()
    assert svc.aim_point(t_now) is None
