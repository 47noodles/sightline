from __future__ import annotations

import numpy as np
import pytest

from sightline.vision.filters import GazeSmoother, Median3, OneEuroFilter, robust_spread


def test_one_euro_constant_signal_passes_unchanged():
    f = OneEuroFilter(1.0, 0.01)
    out = [f(5.0, i / 30.0) for i in range(30)]
    assert out[-1] == pytest.approx(5.0)


def test_one_euro_reduces_jitter_at_rest():
    rng = np.random.default_rng(0)
    f = OneEuroFilter(0.8, 0.0)
    raw = 100.0 + rng.normal(0, 5.0, 300)
    out = np.array([f(v, i / 30.0)[0] for i, v in enumerate(raw)])
    assert np.std(out[60:]) < 0.4 * np.std(raw[60:])


def test_one_euro_beta_cuts_lag_when_moving():
    t = np.arange(60) / 30.0
    ramp = 1000.0 * t  # 1000 px/s
    slow = OneEuroFilter(0.5, 0.0)
    fast = OneEuroFilter(0.5, 0.05)
    lag_slow = ramp[-1] - [slow(v, ti) for v, ti in zip(ramp, t)][-1][0]
    lag_fast = ramp[-1] - [fast(v, ti) for v, ti in zip(ramp, t)][-1][0]
    assert lag_fast < 0.3 * lag_slow


def test_one_euro_irregular_timestamps_and_vectors():
    f = OneEuroFilter(1.0, 0.01)
    times = [0.0, 0.03, 0.035, 0.1, 0.4, 0.41]
    for i, t in enumerate(times):
        out = f(np.array([i, -i], dtype=float), t)
        assert out.shape == (2,)
        assert np.all(np.isfinite(out))
    f.reset()
    assert f.value is None


def test_median3_drops_single_spikes():
    m = Median3()
    out = [m(np.array([v]))[0] for v in [1, 1, 50, 1, 1]]
    assert max(out[2:]) == 1


def test_robust_spread_is_not_fooled_by_two_level_flicker():
    flicker = np.array([[0.0, 0.0], [4.0, 4.0]] * 20)
    assert robust_spread(flicker) > 1.0  # a median absolute deviation would say 0 here


def _fixation(s: GazeSmoother, t0: float, x: float, y: float, n: int, rng, noise: float):
    out = []
    for i in range(n):
        out.append(s.update(t0 + i / 30.0, x + rng.normal(0, noise), y + rng.normal(0, noise)))
    return out


def test_gaze_smoother_holds_still_during_a_fixation():
    rng = np.random.default_rng(1)
    s = GazeSmoother()
    states = _fixation(s, 0.0, 800, 400, 60, rng, 12.0)
    late = np.array([(g.x, g.y) for g in states[20:]])
    assert all(g.fixating for g in states[10:])
    # The clamp holds the output: it barely moves while the raw signal jitters by about 12 px.
    assert np.ptp(late[:, 0]) < 8 and np.ptp(late[:, 1]) < 8
    assert np.hypot(late[-1, 0] - 800, late[-1, 1] - 400) < 10


def test_gaze_smoother_jumps_on_a_saccade():
    rng = np.random.default_rng(2)
    s = GazeSmoother()
    _fixation(s, 0.0, 300, 300, 30, rng, 8.0)
    after = _fixation(s, 1.0, 1500, 800, 6, rng, 8.0)
    # Median of three costs one frame; by the third frame the output is at the new place, not gliding.
    assert np.hypot(after[2].x - 1500, after[2].y - 800) < 40
    assert any(g.saccade for g in after[:3])


def test_gaze_smoother_noise_estimate_tracks_noise():
    rng = np.random.default_rng(3)
    quiet, loud = GazeSmoother(), GazeSmoother()
    _fixation(quiet, 0.0, 500, 500, 150, rng, 3.0)
    _fixation(loud, 0.0, 500, 500, 150, rng, 35.0)
    assert loud.noise > 2.5 * quiet.noise
    assert quiet.radius == pytest.approx(quiet.radius_floor)  # quiet signal: the floor applies
    assert loud.radius > quiet.radius


def test_gaze_smoother_smoothing_setting_changes_cutoff():
    s = GazeSmoother()
    s.set_smoothing(0.0)
    quick = s.one_euro.min_cutoff
    s.set_smoothing(1.0)
    assert s.one_euro.min_cutoff < quick
