"""Blink and wink detection.  The rule above all: natural blinks and unclear movements never click."""

from __future__ import annotations

import numpy as np
import pytest

from sightline.vision.eyes import (
    ClosureEstimator,
    EyeGestureDetector,
    EyeProfile,
    EyeSample,
    Gesture,
    GestureConfig,
    Hold,
    default_closure,
    fit_eye_profile,
)
from sightline.vision.features import EyeMeasure


class Driver:
    """Feeds closure values at a camera rate and collects events."""

    def __init__(self, fps: float = 30.0, cfg: GestureConfig | None = None, noise: float = 0.0, seed: int = 0) -> None:
        self.det = EyeGestureDetector(cfg or GestureConfig())
        self.dt = 1.0 / fps
        self.t = 10.0
        self.events: list = []
        self.noise = noise
        self.rng = np.random.default_rng(seed)
        self.frozen_frames = 0

    def hold(self, left: float, right: float, seconds: float) -> None:
        end = self.t + seconds - 1e-9
        while self.t < end:
            l, r = left, right
            if self.noise:
                l = float(np.clip(l + self.rng.normal(0, self.noise), 0, 1))
                r = float(np.clip(r + self.rng.normal(0, self.noise), 0, 1))
            self.events += self.det.update(self.t, l, r)
            if self.det.frozen:
                self.frozen_frames += 1
            self.t += self.dt

    def lost(self, seconds: float) -> None:
        end = self.t + seconds - 1e-9
        while self.t < end:
            self.events += self.det.lost(self.t)
            self.t += self.dt

    def opened(self, seconds: float = 0.5) -> None:
        self.hold(0.05, 0.05, seconds)

    @property
    def gestures(self) -> list[Gesture]:
        return [e for e in self.events if isinstance(e, Gesture)]

    @property
    def holds(self) -> list[Hold]:
        return [e for e in self.events if isinstance(e, Hold)]


# --------------------------------------------------------------------------- natural blinks


@pytest.mark.parametrize("ms", [80, 120, 150, 200, 280])
@pytest.mark.parametrize("fps", [15.0, 30.0, 60.0])
def test_natural_blinks_never_click(ms, fps):
    d = Driver(fps)
    d.opened(1.0)
    for _ in range(5):
        d.hold(0.95, 0.95, ms / 1000.0)
        d.opened(0.8)
    assert d.gestures == []
    assert d.holds == []
    assert len(d.det.blink_durations) == 5


def test_natural_blink_freezes_the_pointer_while_lids_move():
    d = Driver()
    d.opened(0.5)
    d.hold(0.95, 0.95, 0.12)
    assert d.det.frozen
    d.opened(0.3)
    assert not d.det.frozen


def test_asymmetric_natural_blink_is_not_a_wink():
    d = Driver(60.0)
    d.opened(1.0)
    # The left lid leads by about 30 ms and trails on the way up, as real blinks often do.
    d.hold(0.9, 0.2, 0.03)
    d.hold(0.95, 0.95, 0.12)
    d.hold(0.2, 0.9, 0.03)
    d.opened(0.5)
    assert d.gestures == []


@pytest.mark.parametrize("fps", [15.0, 30.0])
def test_one_eye_closing_first_at_low_frame_rate_is_not_a_wink(fps):
    d = Driver(fps)
    d.opened(1.0)
    d.hold(0.95, 0.1, 1.0 / fps)  # one frame catches only the left eye shut
    d.opened(0.5)
    assert d.gestures == []


# --------------------------------------------------------------------------- deliberate blinks


def test_long_blink_arms_then_clicks_on_opening():
    d = Driver()
    d.opened(0.5)
    d.hold(0.95, 0.95, 0.5)
    assert [h.stage for h in d.holds] == [1]
    assert d.gestures == []  # nothing fires while the eyes are still shut
    d.opened(0.3)
    g = d.gestures
    assert len(g) == 1 and g[0].kind == "long_blink"
    assert g[0].duration == pytest.approx(0.5, abs=0.05)
    assert [h.stage for h in d.holds] == [1, 0]
    assert not d.holds[-1].cancelled


def test_hold_blink_arms_double_click():
    d = Driver()
    d.opened(0.5)
    d.hold(0.95, 0.95, 1.3)
    assert [h.stage for h in d.holds] == [1, 2]
    d.opened(0.3)
    assert [g.kind for g in d.gestures] == ["hold_blink"]


def test_resting_eyes_cancels_and_never_clicks():
    d = Driver()
    d.opened(0.5)
    d.hold(0.95, 0.95, 4.0)
    assert [(h.stage, h.cancelled) for h in d.holds] == [(1, False), (2, False), (0, True)]
    d.opened(0.5)
    assert d.gestures == []


def test_click_onset_is_when_the_lids_started_to_move():
    d = Driver()
    d.opened(0.5)
    start = d.t
    d.hold(0.5, 0.5, 0.1)  # lids drooping
    d.hold(0.95, 0.95, 0.5)
    d.opened(0.3)
    (g,) = d.gestures
    assert g.onset == pytest.approx(start, abs=0.04)


def test_noise_while_open_does_nothing_and_one_noisy_frame_does_not_release_a_hold():
    d = Driver(noise=0.05, seed=3)
    d.opened(3.0)
    assert d.events == []
    d.hold(0.95, 0.95, 0.4)
    d.hold(0.3, 0.3, 1.0 / 30.0)  # a single frame that reads as open
    d.hold(0.95, 0.95, 0.75)
    d.opened(0.3)
    assert [g.kind for g in d.gestures] == ["hold_blink"]


def test_blinks_can_be_turned_off():
    d = Driver(cfg=GestureConfig(blinks=False))
    d.opened(0.5)
    d.hold(0.95, 0.95, 0.6)
    d.opened(0.5)
    assert d.events == []


def test_one_eye_left_shut_after_a_hold_cancels():
    d = Driver()
    d.opened(0.5)
    d.hold(0.95, 0.95, 0.5)
    d.hold(0.1, 0.95, 1.0)  # right eye stays shut long after the left opened
    d.opened(0.5)
    assert d.gestures == []
    assert d.holds[-1].cancelled


# --------------------------------------------------------------------------- winks


@pytest.mark.parametrize("eye", ["left", "right"])
def test_wink(eye):
    d = Driver()
    d.opened(0.5)
    if eye == "left":
        d.hold(0.95, 0.05, 0.3)
    else:
        d.hold(0.05, 0.95, 0.3)
    d.opened(0.3)
    assert [g.kind for g in d.gestures] == [f"wink_{eye}"]
    assert d.holds == []


def test_short_wink_is_ignored():
    d = Driver()
    d.opened(0.5)
    d.hold(0.95, 0.05, 0.1)
    d.opened(0.3)
    assert d.gestures == []


def test_wink_right_after_a_blink_is_ignored():
    d = Driver()
    d.opened(0.5)
    d.hold(0.95, 0.95, 0.12)
    d.hold(0.05, 0.05, 2.0 / 30.0)  # open only 67 ms: less than the 150 ms required
    d.hold(0.95, 0.05, 0.3)
    d.opened(0.4)
    assert d.gestures == []


def test_wink_with_the_other_eye_squinting_is_ignored():
    d = Driver()
    d.opened(0.5)
    d.hold(0.95, 0.45, 0.3)
    d.opened(0.4)
    assert d.gestures == []


def test_very_long_wink_is_ignored():
    d = Driver()
    d.opened(0.5)
    d.hold(0.05, 0.95, 3.0)  # resting one eye
    d.opened(0.4)
    assert d.gestures == []


def test_winks_can_be_turned_off_per_eye():
    d = Driver(cfg=GestureConfig(wink_right=False))
    d.opened(0.5)
    d.hold(0.05, 0.95, 0.3)
    d.opened(0.3)
    d.hold(0.95, 0.05, 0.3)
    d.opened(0.3)
    assert [g.kind for g in d.gestures] == ["wink_left"]


def test_calibrated_clear_level_allows_a_squinting_winker():
    d = Driver(cfg=GestureConfig(clear_right=0.5))
    d.opened(0.5)
    d.hold(0.95, 0.42, 0.3)
    d.opened(0.3)
    assert [g.kind for g in d.gestures] == ["wink_left"]


# --------------------------------------------------------------------------- face lost


def test_face_lost_mid_hold_cancels():
    d = Driver()
    d.opened(0.5)
    d.hold(0.95, 0.95, 0.5)
    d.lost(0.3)
    d.opened(0.5)
    assert d.gestures == []
    assert d.holds[-1].stage == 0 and d.holds[-1].cancelled


def test_face_lost_mid_wink_cancels_and_needs_open_eyes_after():
    d = Driver()
    d.opened(0.5)
    d.hold(0.95, 0.05, 0.2)
    d.lost(0.2)
    d.hold(0.95, 0.05, 0.25)  # back with one eye shut: there was no open period first
    d.opened(0.4)
    assert d.gestures == []


def test_state_names():
    det = EyeGestureDetector()
    assert det.state_name() == "unknown"
    det.update(0.0, 0.0, 0.0)
    assert det.state_name() == "open"
    det.update(0.1, 0.9, 0.0)
    assert det.state_name() == "left_wink"
    det.update(0.2, 0.9, 0.9)
    assert det.state_name() == "closed"


# --------------------------------------------------------------------------- closure


def _eye(ear: float, blink: float) -> EyeMeasure:
    return EyeMeasure(0.0, 0.0, -0.1, 0.1, ear, 30.0, 10.0, blink, 0.0, 0.0)


def test_default_closure_bounds_and_order():
    assert default_closure(0.05, 1.0) == 0.0
    assert default_closure(0.9, 0.3) == 1.0
    # A narrowed but open eye (looking down, smiling) stays well below the closed threshold.
    assert default_closure(0.25, 0.8) < 0.3


def test_closure_estimator_learns_the_open_level():
    est = ClosureEstimator()
    for _ in range(200):
        cl, cr = est.update(_eye(0.20, 0.05), _eye(0.36, 0.05))
    assert est.open_levels[0] == pytest.approx(0.20, abs=0.01)
    assert est.open_levels[1] == pytest.approx(0.36, abs=0.01)
    assert cl < 0.1 and cr < 0.1
    cl, cr = est.update(_eye(0.07, 0.6), _eye(0.36, 0.05))
    assert cl > 0.8 and cr < 0.1


# --------------------------------------------------------------------------- eye calibration


def _samples(rng, label: str, n: int, left_closed: float, right_closed: float) -> list[EyeSample]:
    out = []
    for _ in range(n):
        bl = 0.08 + 0.55 * left_closed + rng.normal(0, 0.03)
        br = 0.08 + 0.55 * right_closed + rng.normal(0, 0.03)
        el = 0.30 * (1 - 0.75 * left_closed) + rng.normal(0, 0.01)
        er = 0.30 * (1 - 0.75 * right_closed) + rng.normal(0, 0.01)
        out.append(EyeSample(label, bl, br, el, er))
    return out


def test_eye_calibration_winker():
    rng = np.random.default_rng(0)
    s = _samples(rng, "open", 60, 0, 0) + _samples(rng, "closed", 60, 1, 1)
    s += _samples(rng, "wink_left", 60, 1, 0.1) + _samples(rng, "wink_right", 60, 0.1, 1)
    r = fit_eye_profile(s)
    assert r.ok and r.profile is not None
    assert r.profile.wink_left_ok and r.profile.wink_right_ok
    assert r.message == "Blinks and winks are set up."
    p = r.profile
    assert p.left is not None and p.right is not None
    assert p.left.closure(0.08, 1.0) < 0.2 and p.left.closure(0.63, 0.25) > 0.8


def test_eye_calibration_user_who_cannot_wink_gets_winks_turned_off():
    rng = np.random.default_rng(1)
    s = _samples(rng, "open", 60, 0, 0) + _samples(rng, "closed", 60, 1, 1)
    # Asked to wink, both eyes close.
    s += _samples(rng, "wink_left", 60, 1, 0.9) + _samples(rng, "wink_right", 60, 0.9, 1)
    r = fit_eye_profile(s)
    assert r.ok and r.profile is not None
    assert not r.profile.wink_left_ok and not r.profile.wink_right_ok
    assert "winks could not be told apart from blinks" in r.message
    assert "off" in r.message


def test_eye_calibration_only_one_wink():
    rng = np.random.default_rng(2)
    s = _samples(rng, "open", 60, 0, 0) + _samples(rng, "closed", 60, 1, 1)
    s += _samples(rng, "wink_left", 60, 1, 0.05) + _samples(rng, "wink_right", 60, 0.8, 0.9)
    r = fit_eye_profile(s)
    assert r.profile is not None and r.profile.wink_left_ok and not r.profile.wink_right_ok
    assert "right wink could not be told apart" in r.message


def test_eye_calibration_detects_a_mirrored_camera():
    rng = np.random.default_rng(3)
    s = _samples(rng, "open", 60, 0, 0) + _samples(rng, "closed", 60, 1, 1)
    # Asked for the left eye, the channel we call right closes, and the other way round.
    s += _samples(rng, "wink_left", 60, 0.05, 1) + _samples(rng, "wink_right", 60, 1, 0.05)
    r = fit_eye_profile(s, previous_mirrored=False)
    assert r.profile is not None and r.profile.mirrored is True
    assert r.profile.wink_left_ok and r.profile.wink_right_ok
    assert "mirror" in r.message


def test_eye_calibration_fails_clearly_without_separation():
    rng = np.random.default_rng(4)
    s = _samples(rng, "open", 60, 0, 0) + _samples(rng, "closed", 60, 0.05, 0.05)
    r = fit_eye_profile(s)
    assert not r.ok and r.profile is None
    assert "Could not tell when your eyes were closed" in r.message


def test_eye_profile_round_trip(tmp_path):
    rng = np.random.default_rng(5)
    s = _samples(rng, "open", 40, 0, 0) + _samples(rng, "closed", 40, 1, 1)
    s += _samples(rng, "wink_left", 40, 1, 0.1) + _samples(rng, "wink_right", 40, 0.1, 1)
    p = fit_eye_profile(s).profile
    assert p is not None
    p.save(tmp_path / "eyes.json")
    q = EyeProfile.load(tmp_path / "eyes.json")
    assert q is not None and q.left == p.left and q.wink_right_ok == p.wink_right_ok
