"""Fix pointer: the "drift" calibration shifts the gaze model, and click learning suggests it when needed."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest
from vision_sim import Sim

from sightline.core.events import ClickPerformed, GazeModelChanged, Notice
from sightline.vision.gaze_model import TrainingData
from sightline.vision.pipeline import DRIFT_NOTICE
from sightline.vision.profile import VisionProfile


def fix_pointer(sim: Sim, dots=((960, 540),), seconds: float = 1.5):
    """The "fix pointer" wizard: a dot, looked at until it goes, then finish."""
    sim.cal.begin("drift")
    for x, y in dots:
        sim.cal.set_target(x, y, True)
        sim.run(0.15)  # the eyes travel to the dot
        sim.look_at = (x, y)
        sim.run(seconds - 0.15)
        sim.cal.set_target(x, y, False)
    return sim.cal.finish()


def gaze_error(sim: Sim, x: float, y: float) -> float:
    sim.look_at = (x, y)
    sim.run(0.6)
    est = sim.pipe.live_gaze()
    assert est is not None
    return math.hypot(est[0] - x, est[1] - y)


def offset_of(model) -> tuple[float, float]:
    c = model.correction
    return (0.0, 0.0) if c is None else (float(c[0, 2]), float(c[1, 2]))


def test_fix_pointer_shifts_the_model_by_the_measured_offset(vctx, rec, user):
    sim = Sim(vctx, user)
    sim.use_model()
    before = gaze_error(sim, 1400, 300)
    sim.pipe.apply_drift(-70.0, 45.0)  # the user slumped: estimates now land 83 px off
    drifted = gaze_error(sim, 1400, 300)
    assert drifted > before + 50
    base = offset_of(sim.pipe.current_gaze_model())
    rec.clear()
    report = fix_pointer(sim)
    assert report.ok and report.kind == "drift", report.message
    dx, dy = report.details["offset"]
    assert dx == pytest.approx(70.0, abs=15) and dy == pytest.approx(-45.0, abs=15)
    assert report.mean_error_px == pytest.approx(math.hypot(dx, dy), abs=0.2)
    assert report.message == f"Pointer corrected by {report.mean_error_px:.0f} px."
    assert report.details["dots"] == 1 and len(report.per_point_error_px) == 1
    changed = rec.of(GazeModelChanged)
    assert [e.source for e in changed] == ["drift"]
    model = sim.pipe.current_gaze_model()
    assert offset_of(model) == pytest.approx((base[0] + dx, base[1] + dy), abs=0.2)  # no refit: only the shift
    assert gaze_error(sim, 1400, 300) < before + 25
    saved, _, _ = VisionProfile(vctx.settings.general.profile).load_gaze()
    assert saved is not None and offset_of(saved) == pytest.approx(offset_of(model), abs=0.01)


def test_fix_pointer_with_several_dots_uses_their_mean_offset(vctx, user):
    sim = Sim(vctx, user)
    sim.use_model()
    sim.pipe.apply_drift(0.0, -90.0)
    report = fix_pointer(sim, dots=((500, 300), (1400, 800)), seconds=1.2)
    assert report.ok, report.message
    assert report.details["dots"] == 2 and len(report.per_point_error_px) == 2
    dx, dy = report.details["offset"]
    assert abs(dx) < 20 and dy == pytest.approx(90.0, abs=20)


def test_fix_pointer_counts_samples_only_after_the_eyes_land(vctx, user):
    sim = Sim(vctx, user)
    sim.use_model()
    sim.cal.begin("drift")
    sim.cal.set_target(700, 400, True)
    sim.look_at = (700, 400)
    sim.run(0.3)
    assert sim.cal.sample_count() <= 2
    sim.run(1.0)
    assert sim.cal.sample_count() >= 25
    sim.cal.cancel()


def test_fix_pointer_refuses_an_offset_too_large_to_be_drift(vctx, rec, user):
    sim = Sim(vctx, user)
    sim.use_model()
    sim.pipe.apply_drift(-560.0, 0.0)
    model = sim.pipe.current_gaze_model()
    rec.clear()
    report = fix_pointer(sim)
    assert not report.ok
    assert report.message == "The pointer is too far off to fix this way. Please calibrate again."
    assert sim.pipe.current_gaze_model() is model
    assert rec.of(GazeModelChanged) == []


def test_fix_pointer_needs_a_calibration_and_a_steady_look(vctx, user):
    sim = Sim(vctx, user)
    report = fix_pointer(sim)
    assert not report.ok and report.message == "Calibrate eye pointing first."
    sim.use_model()
    report = fix_pointer(sim, seconds=0.35)  # the dot went before the eyes settled on it
    assert not report.ok
    assert report.message == "Not enough data. Look at the dot until it disappears, then try again."


def test_a_click_learning_result_from_before_the_fix_is_dropped(vctx, user):
    sim = Sim(vctx, user)
    sim.use_model()
    before = sim.pipe.current_gaze_model()
    stale = replace(before, correction=None, meta=dict(before.meta, learned_clicks=4))  # started before the fix
    fixed = sim.pipe.apply_drift(25.0, -30.0)
    sim.pipe.replace_gaze_model(stale, TrainingData.empty())
    assert sim.pipe.current_gaze_model() is fixed
    later = replace(fixed, meta=dict(fixed.meta, learned_clicks=5))  # started after it: kept
    sim.pipe.replace_gaze_model(later, TrainingData.empty())
    assert sim.pipe.current_gaze_model() is later


def test_learning_keeps_the_fix_when_clicks_say_nothing_new(vctx, user):
    sim = Sim(vctx, user)
    sim.use_model()
    learner = sim.pipe.learner
    learner.synchronous = True
    fixed = sim.pipe.apply_drift(40.0, 0.0)
    # A few clicks agree with the fixed pointer.
    for x, y in [(400, 300), (1500, 400), (900, 800)]:
        sim.look_at = (x, y)
        sim.run(0.7)
        est = sim.pipe.live_gaze()
        learner.on_click(ClickPerformed(est[0], est[1], "left", 1, "voice"))
    learner.refit_now()
    assert offset_of(sim.pipe.current_gaze_model())[0] == pytest.approx(offset_of(fixed)[0], abs=10)


def test_growing_click_errors_suggest_fixing_the_pointer(vctx, rec, user):
    sim = Sim(vctx, user)
    sim.use_model()
    learner = sim.pipe.learner
    learner.synchronous = True
    sim.pipe.apply_drift(-210.0, 0.0)  # estimates land 210 px left of where the user looks
    targets = [(500, 300), (1500, 350), (900, 800), (1300, 600), (600, 900), (1700, 200), (300, 500)]
    for i, (x, y) in enumerate(targets):
        sim.look_at = (x, y)
        sim.run(0.7)
        learner.on_click(ClickPerformed(x, y, "left", 1, "voice"))
        notices = [n for n in rec.of(Notice) if n.key == "vision-drift"]
        assert len(notices) == (1 if i >= 4 else 0)  # after five clicks, and only once
    assert notices[0].text == DRIFT_NOTICE == 'The pointer seems to have drifted. Say "fix pointer" to correct it.'
    assert learner.drift_suggestions == 1
    # Fixing the pointer starts the count again.
    sim.pipe.apply_drift(210.0, 0.0)
    assert len(learner._errors) == 0
