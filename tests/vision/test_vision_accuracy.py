"""The accuracy map: expected gaze error across each screen, from the calibration's held-out errors."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest
from vision_sim import GRID, HEAD_PHASE, Sim, shared_model

from sightline.core.events import PointerSample
from sightline.core.types import Monitor, PointerMode, Rect
from sightline.vision.accuracy import (
    FLOOR_PX,
    AccuracyMap,
    comfort_size,
    default_gaze_targets,
    targets_from_groups,
)
from sightline.vision.gaze_model import GazeModel
from sightline.vision.profile import VisionProfile

LEFT = Monitor("a", "Left", Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1040), 1.0, True, (527.0, 296.0))
RIGHT = Monitor("b", "Right", Rect(1920, 0, 1920, 1080), Rect(1920, 0, 1920, 1040), 1.0, False, (527.0, 296.0))


def test_map_follows_the_dots_and_never_goes_beyond_them():
    m = AccuracyMap([(200, 200), (1700, 900)], [80.0, 20.0], [LEFT])
    assert m.error_px(200, 200) == pytest.approx(80.0, abs=3.0)
    assert m.error_px(1700, 900) == pytest.approx(20.0, abs=3.0)
    middle = m.error_px(950, 550)
    assert middle is not None and 20.0 < middle < 80.0
    values = [m.error_px(x, y) for x in range(0, 1920, 160) for y in range(0, 1080, 120)]
    assert all(v is not None and 20.0 - 1e-6 <= v <= 80.0 + 1e-6 for v in values)
    assert m.mean_px == pytest.approx(50.0)


def test_map_has_a_floor():
    m = AccuracyMap([(500, 500), (1500, 500)], [2.0, 4.0], [LEFT])
    assert m.error_px(500, 500) == FLOOR_PX


def test_map_is_per_monitor():
    m = AccuracyMap([(300, 300), (1600, 800), (960, 540)], [40.0, 50.0, 30.0], [LEFT, RIGHT])
    assert m.error_px(960, 540) is not None
    assert m.error_px(2500, 500) is None  # nobody calibrated the right-hand screen
    assert not m.empty


def test_map_without_monitor_information_covers_the_dots():
    m = AccuracyMap([(300, 300), (1600, 800)], [40.0, 50.0], [])
    assert m.error_px(900, 500) is not None
    assert m.error_px(5000, 5000) is None


def test_bad_values_are_left_out():
    m = AccuracyMap([(300, 300), (1600, 800)], [math.nan, 50.0], [LEFT])
    assert m.error_px(300, 300) == pytest.approx(50.0)
    assert AccuracyMap([], [], [LEFT]).empty


def test_default_gaze_dots_reach_the_corners_and_edges():
    rect = Rect(0, 0, 1920, 1080)
    dots = default_gaze_targets(rect)
    assert len(dots) == 17
    assert dots[0] == (960.0, 540.0)  # the first dot is in the middle, where the eyes start
    assert all(rect.contains(x, y) for x, y in dots)
    for cx, cy in [(0, 0), (1920, 0), (0, 1080), (1920, 1080)]:
        assert min(math.hypot(x - cx, y - cy) for x, y in dots) < 100  # a dot near every corner
    for ex, ey in [(960, 0), (960, 1080), (0, 540), (1920, 540)]:
        assert min(math.hypot(x - ex, y - ey) for x, y in dots) < 70  # and every edge
    # On a second monitor the dots follow its rectangle.
    assert all(RIGHT.rect.contains(x, y) for x, y in default_gaze_targets(RIGHT.rect))


def test_comfort_size_sentence():
    c = comfort_size(40.0, 0.2745)  # 80 px across on a 24 inch 1080p screen: 2.2 cm
    assert c.diameter_px == 80.0
    assert c.diameter_mm == pytest.approx(21.96)
    assert c.message == (
        "At your accuracy, controls smaller than about 2 cm are hard to hit with your eyes alone. "
        "Setting Windows text and app size to 150 percent will help."
    )
    assert "200 percent" in comfort_size(40.0, 0.2745, windows_scale=1.5).message
    assert "head movements" in comfort_size(40.0, 0.2745, windows_scale=2.0).message
    small = comfort_size(10.0, 0.2745)
    assert small.message == "At your accuracy, controls smaller than about 0.5 cm are hard to hit with your eyes alone."
    assert "about 7 cm" in comfort_size(130.0, 0.2745).message  # whole centimetres above 5


def test_targets_from_groups_use_the_cross_validation_order():
    y = np.array([[10, 10], [12, 12], [500, 500], [100, 100], [900, 900]], dtype=float)
    groups = np.array([7, 7, 3, 5, -1])
    assert targets_from_groups(y, groups) == [(500.0, 500.0), (100.0, 100.0), (11.0, 11.0)]


# --------------------------------------------------------------------------- with the pipeline


def test_gaze_report_lists_the_dots_and_a_comfortable_size(vctx, user):
    sim = Sim(vctx, user)
    report = sim.calibrate(GRID + HEAD_PHASE)
    assert report.ok, report.message
    points = report.details["target_points"]
    assert len(points) == len(report.per_point_error_px) == 13
    for (x, y), (tx, ty) in zip(sorted(map(tuple, points)), sorted(GRID + HEAD_PHASE)):
        assert math.hypot(x - tx, y - ty) < 1.0
    assert report.details["comfort"].startswith("At your accuracy, controls smaller than about")
    assert report.details["comfort_px"] == round(2 * report.mean_error_px)
    assert report.details["comfort_mm"] > 0


def test_validate_report_lists_the_dots_and_a_comfortable_size(vctx, user):
    sim = Sim(vctx, user)
    sim.use_model()
    sim.cal.begin("validate")
    dots = [(400, 300), (1500, 300), (960, 800), (300, 900)]
    for x, y in dots:
        sim.dot(x, y)
    report = sim.cal.finish()
    assert report.ok
    assert sorted(map(tuple, report.details["target_points"])) == sorted((float(x), float(y)) for x, y in dots)
    assert "comfort" in report.details


def test_pointer_samples_carry_the_expected_error(vctx, rec, user):
    sim = Sim(vctx, user)
    sim.run(0.5)
    assert all(s.error_px is None for s in rec.of(PointerSample))  # not calibrated yet
    sim.use_model()
    rec.clear()
    sim.look_at = (1500, 300)
    sim.run(0.5)
    sample = rec.of(PointerSample)[-1]
    model = sim.pipe.current_gaze_model()
    assert sample.error_px is not None
    assert FLOOR_PX <= sample.error_px <= max(model.per_target) + 1e-6
    # Head mode does not use the gaze, so it has no gaze error to report.
    vctx.state.update(pointer_mode=PointerMode.HEAD)
    rec.clear()
    sim.run(0.2)
    assert all(s.error_px is None for s in rec.of(PointerSample))


def test_dot_positions_are_saved_with_the_model(vctx):
    model, data = shared_model()
    assert len(model.targets) == len(model.per_target) == 13
    profile = VisionProfile("roundtrip")
    profile.save_gaze(model, data, None)
    loaded, _, _ = profile.load_gaze()
    assert loaded is not None
    assert np.allclose(np.array(loaded.targets), np.array(model.targets))


def test_models_saved_without_dot_positions_still_get_a_map(vctx, user):
    model, data = shared_model()
    old: GazeModel = replace(model, targets=())
    sim = Sim(vctx, user)
    sim.pipe.activate_gaze(old, data)
    assert sim.pipe.error_at(960, 540) is not None
