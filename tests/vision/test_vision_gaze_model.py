from __future__ import annotations

import json

import numpy as np
import pytest

from sightline.vision import features as feat
from sightline.vision.gaze_model import (
    FEATURE_SETS,
    GazeModel,
    TrainingData,
    _Solver,
    cross_validate,
    expand,
    fit_model,
    heldout_error,
    reject_outliers,
)

GRID = [(x, y) for y in (110, 540, 970) for x in (190, 960, 1730)]
EXTRA = [(560, 320), (1360, 320), (560, 760), (1360, 760)]


def collect(user, targets, frames=20, *, yaw_sd=1.0, pitch_sd=1.0, noise_px=0.4, start_group=0, seed=0):
    rng = np.random.default_rng(seed)
    raw, lmb, y, groups = [], [], [], []
    for gi, (x, yy) in enumerate(targets):
        yaw0, pitch0 = rng.normal(0, yaw_sd), rng.normal(0, pitch_sd)
        for i in range(frames):
            f = user.look(i / 30.0, x, yy, yaw=yaw0 + rng.normal(0, 0.2), pitch=pitch0 + rng.normal(0, 0.2),
                          noise_px=noise_px)  # fmt: skip
            m = feat.measure(f)
            raw.append(m.raw)
            lmb.append(m.lm_block)
            y.append((x, yy))
            groups.append(start_group + gi)
    n = len(y)
    return TrainingData(np.array(raw), np.array(lmb), np.array(y, float), np.array(groups), np.ones(n))


def test_ridge_matches_least_squares_with_a_tiny_penalty():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(200, 5))
    y = np.column_stack([x @ [1, 2, 3, 0, -1] + 5, x @ [0, 1, 0, 1, 0] - 2])
    s = _Solver(x, y, np.ones(200))
    pred = s.predict(x, s.coef(1e-12, 1e-12))
    a = np.column_stack([x, np.ones(200)])
    ls = a @ np.linalg.lstsq(a, y, rcond=None)[0]
    assert np.allclose(pred, ls, atol=1e-6)


def test_constant_columns_do_not_break_the_fit():
    rng = np.random.default_rng(1)
    x = np.column_stack([rng.normal(size=100), np.full(100, 3.0)])
    y = np.column_stack([x[:, 0] * 10, x[:, 0] * -4])
    s = _Solver(x, y, np.ones(100))
    c = s.coef(1e-3, 1e-3)
    assert np.all(np.isfinite(c)) and abs(c[1, 0]) < 1e-6


def test_weights_limit_the_influence_of_bad_samples():
    rng = np.random.default_rng(2)
    x = rng.normal(size=(100, 1))
    y = np.column_stack([3 * x[:, 0], 3 * x[:, 0]])
    y[:5] += 500.0  # five wild samples
    w = np.ones(100)
    w[:5] = 1e-6
    s = _Solver(x, y, w)
    c = s.coef(1e-9, 1e-9)
    assert c[0, 0] == pytest.approx(3.0 * np.sqrt(np.average((x[:, 0] - x[:, 0].mean()) ** 2)), rel=0.05)


def test_fit_recovers_gaze_from_synthetic_user(user):
    data = collect(user, GRID + EXTRA)
    out = fit_model(data)
    m = out.model
    assert m is not None and out.cv is not None
    assert m.accuracy_px is not None and m.accuracy_px < 40.0
    assert len(m.per_target) == len(GRID) + len(EXTRA)
    # Fresh points never seen in calibration.
    test = collect(user, [(400, 250), (1500, 850), (960, 700), (1200, 300)], seed=9, start_group=100)
    pred = m.predict_many(test.raw, test.lm)
    err = float(np.mean(np.hypot(*(pred - test.y).T)))
    assert err < 50.0
    # The held-out estimate is honest: close to the error on new points, not an optimistic training error.
    assert m.accuracy_px > 0.3 * err


def test_more_targets_unlock_wider_feature_sets(user):
    few = fit_model(collect(user, GRID[:5]))
    assert set(few.candidates) == {"basic"}
    many = fit_model(collect(user, GRID + EXTRA))
    assert {"basic", "head", "per_eye", "landmarks"} <= set(many.candidates)


def test_head_movement_is_tolerated_after_a_head_motion_calibration(user):
    # Calibration with real head variation lets the model separate head pose from gaze.
    data = collect(user, GRID + EXTRA, yaw_sd=4.0, pitch_sd=3.0)
    m = fit_model(data).model
    assert m is not None
    test = collect(user, [(700, 400), (1300, 700)], yaw_sd=0.0, pitch_sd=0.0, seed=4, start_group=50)
    shifted = []
    for (x, y), yaw in [((700, 400), 5.0), ((1300, 700), -5.0)]:
        for i in range(10):
            mm = feat.measure(user.look(i / 30, x, y, yaw=yaw))
            shifted.append((mm.raw, mm.lm_block, x, y))
    raw = np.array([s[0] for s in shifted])
    lm = np.array([s[1] for s in shifted])
    tgt = np.array([(s[2], s[3]) for s in shifted], float)
    err_turned = float(np.mean(np.hypot(*(m.predict_many(raw, lm) - tgt).T)))
    err_still = float(np.mean(np.hypot(*(m.predict_many(test.raw, test.lm) - test.y).T)))
    assert err_turned < 80.0
    assert err_still < 50.0


def test_simpler_feature_set_wins_a_tie(monkeypatch):
    from sightline.vision import gaze_model as gm

    calls = {}

    def fake_cv(x, y, w, groups, lambdas=None):
        p = x.shape[1]
        calls[p] = True
        from sightline.vision.gaze_model import CVResult

        # Every set within 1% of each other: the first (simplest) should be chosen.
        return CVResult("", 0.01, 0.01, 40.0 + 0.001 * p, 30.0, (30.0,), 5.0, ((0.0, 0.0),))

    monkeypatch.setattr(gm, "cross_validate", fake_cv)
    rng = np.random.default_rng(0)
    n = 300
    data = TrainingData(rng.normal(size=(n, feat.N_RAW)), rng.normal(size=(n, feat.N_LM_BLOCK)),
                        rng.normal(size=(n, 2)), np.repeat(np.arange(15), 20), np.ones(n))  # fmt: skip
    out = gm.fit_model(data)
    assert out.model is not None and out.model.feature_set == "basic"


def test_too_few_targets_reports_it():
    rng = np.random.default_rng(0)
    data = TrainingData(rng.normal(size=(40, feat.N_RAW)), rng.normal(size=(40, feat.N_LM_BLOCK)),
                        rng.normal(size=(40, 2)), np.repeat([0, 1], 20), np.ones(40))  # fmt: skip
    out = fit_model(data)
    assert out.model is not None and out.model.accuracy_px is None
    assert "too few targets" in out.reason
    assert fit_model(TrainingData.empty()).model is None


def test_outlier_rejection_flags_glances_away():
    rng = np.random.default_rng(0)
    raw = np.zeros((40, feat.N_RAW))
    raw[:, feat.RAW_INDEX["ixA"]] = rng.normal(0.1, 0.005, 40)
    raw[:, feat.RAW_INDEX["ixB"]] = raw[:, feat.RAW_INDEX["ixA"]]
    raw[7, feat.RAW_INDEX["ixA"]] = raw[7, feat.RAW_INDEX["ixB"]] = 0.25  # looked away for a frame
    keep = reject_outliers(raw, np.zeros(40, int))
    assert not keep[7] and keep.sum() >= 37


def test_model_round_trip_without_pickle(tmp_path, user):
    data = collect(user, GRID + EXTRA)
    m = fit_model(data).model
    assert m is not None
    m.meta["monitors"] = [[0, 0, 1920, 1080]]
    m.save(tmp_path)
    data.save(tmp_path / "samples.npz")
    with np.load(tmp_path / "gaze_model.npz", allow_pickle=False) as z:
        assert set(z.files) == {"mean", "std", "coef", "intercept"}
    loaded = GazeModel.load(tmp_path)
    assert loaded is not None and loaded.feature_set == m.feature_set
    assert np.allclose(loaded.predict_many(data.raw, data.lm), m.predict_many(data.raw, data.lm))
    assert loaded.meta["monitors"] == [[0, 0, 1920, 1080]]
    again = TrainingData.load(tmp_path / "samples.npz")
    assert np.allclose(again.raw, data.raw) and np.array_equal(again.groups, data.groups)


def test_model_from_another_version_is_refused(tmp_path, user):
    m = fit_model(collect(user, GRID)).model
    assert m is not None
    m.save(tmp_path)
    info = json.loads((tmp_path / "gaze_model.json").read_text())
    info["version"] = 999
    (tmp_path / "gaze_model.json").write_text(json.dumps(info))
    assert GazeModel.load(tmp_path) is None
    assert GazeModel.load(tmp_path / "missing") is None


def test_heldout_error_matches_cross_validation(user):
    data = collect(user, GRID + EXTRA)
    m = fit_model(data, ["basic"]).model
    assert m is not None
    err = heldout_error(data, "basic", m.lam_x, m.lam_y)
    assert err == pytest.approx(m.accuracy_px, rel=1e-6)


def test_expand_shapes():
    raw = np.zeros((3, feat.N_RAW))
    lm = np.zeros((3, feat.N_LM_BLOCK))
    for fs in FEATURE_SETS:
        assert expand(raw, lm, fs).shape[0] == 3
    with pytest.raises(ValueError):
        expand(raw, lm, "nonsense")


def test_cross_validate_needs_three_targets():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(20, 3))
    assert cross_validate(x, rng.normal(size=(20, 2)), np.ones(20), np.repeat([0, 1], 10)) is None
