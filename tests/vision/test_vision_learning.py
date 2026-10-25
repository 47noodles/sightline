"""Learning from clicks: it should fix drift, and refuse to learn from misleading clicks."""

from __future__ import annotations

import math

import numpy as np

from sightline.core.events import ClickPerformed
from sightline.vision.pipeline import VisionPipeline

GRID = [(x, y) for y in (110, 540, 970) for x in (190, 960, 1730)] + [(560, 320), (1360, 320), (560, 760), (1360, 760)]
CLICK_TARGETS = [(300, 200), (1600, 250), (900, 850), (1300, 600), (500, 700), (1750, 900), (250, 950), (1100, 150),
                 (700, 400), (1450, 450), (400, 500), (1650, 700), (950, 300), (600, 950), (1200, 900), (850, 650)]  # fmt: skip


class Sim:
    def __init__(self, ctx, user) -> None:
        self.now = 20.0
        self.ctx = ctx
        self.user = user
        self.pipe = VisionPipeline(ctx, clock=lambda: self.now)
        self.yaw = 0.0
        learner = self.pipe.learner
        learner.synchronous = True
        learner.cfg.refit_min_interval_s = 0.0

    def look(self, x: float, y: float, seconds: float) -> None:
        for _ in range(int(round(seconds * 30))):
            self.pipe.process(self.user.look(self.now, x, y, yaw=self.yaw), self.now)
            self.now += 1 / 30

    def calibrate(self) -> None:
        cal = self.pipe.calibration
        cal.begin("gaze")
        rng = np.random.default_rng(3)
        for x, y in GRID:
            self.yaw = rng.normal(0, 2.5)
            cal.set_target(x, y, False)
            self.look(x, y, 0.4)
            cal.set_target(x, y, True)
            self.look(x, y, 0.8)
            cal.set_target(x, y, False)
        self.yaw = 0.0
        assert cal.finish().ok

    def click(self, x: float, y: float, *, at: tuple[float, float] | None = None) -> None:
        """The user looks at (x, y) and clicks (by voice); the click lands at ``at`` (default the same place)."""
        self.look(x, y, 0.7)
        cx, cy = at if at is not None else (x, y)
        self.pipe.learner.on_click(ClickPerformed(cx, cy, "left", 1, "voice"))

    def error(self, points) -> float:
        errs = []
        for x, y in points:
            self.look(x, y, 0.5)
            est = self.pipe.live_gaze()
            assert est is not None
            errs.append(math.hypot(est[0] - x, est[1] - y))
        return float(np.mean(errs))


def test_clicks_correct_drift(vctx, user):
    sim = Sim(vctx, user)
    sim.calibrate()
    probe = [(450, 350), (1400, 800), (960, 540), (1500, 300)]
    before_drift = sim.error(probe)
    # The user sits closer to the screen and the camera than during calibration.
    user.screen.distance_mm = 520.0
    drifted = sim.error(probe)
    assert drifted > before_drift + 15
    for x, y in CLICK_TARGETS:
        sim.click(x, y)
    learner = sim.pipe.learner
    assert learner.accepted >= 12
    # The posture change contradicts the old calibration, so the guarded full refit refuses it;
    # the drift correction on top of the model is what follows the user.
    assert learner.corrections_kept >= 1
    after = sim.error(probe)
    assert after < 0.7 * drifted
    assert sim.pipe.current_gaze_model().correction is not None
    # The updated model and the click samples are saved with the profile.
    folder = sim.pipe.profile.folder
    from sightline.vision.gaze_model import TrainingData

    saved = TrainingData.load(folder / "gaze_samples.npz")
    assert (saved.groups < 0).sum() == len(learner.clicks)


def test_misleading_clicks_are_rejected_by_the_guard(vctx, user):
    sim = Sim(vctx, user)
    sim.calibrate()
    model = sim.pipe.current_gaze_model()
    rng = np.random.default_rng(5)
    for x, y in CLICK_TARGETS:
        # Clicks land somewhere unrelated to where the user looked, but within the acceptance radius.
        off = rng.normal(0, 60, 2)
        sim.click(x, y, at=(x + off[0], y + off[1]))
    learner = sim.pipe.learner
    assert learner.refits_kept == 0
    assert learner.refits_rejected >= 1
    assert sim.pipe.current_gaze_model() is model


def test_clicks_far_from_the_gaze_teach_nothing(vctx, user):
    sim = Sim(vctx, user)
    sim.calibrate()
    sim.click(400, 400, at=(1500, 900))  # looked one place, clicked another
    assert sim.pipe.learner.accepted == 0 and sim.pipe.learner.rejected == 1


def test_learning_can_be_turned_off(vctx, user):
    vctx.settings.vision.learn_from_clicks = False
    sim = Sim(vctx, user)
    sim.calibrate()
    sim.click(400, 400)
    assert sim.pipe.learner.accepted == 0 and sim.pipe.learner.rejected == 0


def test_no_learning_before_calibration(vctx, user):
    sim = Sim(vctx, user)
    sim.click(400, 400)
    assert sim.pipe.learner.accepted == 0


def test_eye_clicks_use_the_fixation_before_the_eyes_closed(vctx, user):
    sim = Sim(vctx, user)
    sim.calibrate()
    sim.look(1300, 400, 0.8)
    t_anchor = sim.now - 0.1
    sim.pipe.learner.note_click(sim.now, t_anchor, 1300, 400)
    # The eyes then close and look elsewhere before the desktop reports the click.
    sim.look(300, 900, 0.4)
    sim.pipe.learner.on_click(ClickPerformed(1310, 395, "left", 1, "eye"))  # snapped a little
    assert sim.pipe.learner.accepted == 1
    sample = sim.pipe.learner.clicks
    assert len(sample) == 1 and tuple(sample.y[0]) == (1310, 395)


def test_spoken_clicks_learn_from_where_the_user_looked_when_they_began_to_speak(vctx, user):
    sim = Sim(vctx, user)
    sim.calibrate()
    learner = sim.pipe.learner
    sim.look(1300, 400, 0.8)
    onset = sim.now - 0.05
    sim.look(300, 900, 0.7)  # the eyes move on before the spoken click lands
    learner.on_click(ClickPerformed(1300, 400, "left", 1, "voice", aim_t=onset))
    assert learner.accepted == 1 and learner.rejected == 0
    # Without the onset the anchor is "just now", when the gaze was far from the click: nothing is learned.
    sim.look(1300, 400, 0.8)
    sim.look(300, 900, 0.7)
    learner.on_click(ClickPerformed(1300, 400, "left", 1, "voice"))
    assert learner.accepted == 1 and learner.rejected == 1


def test_click_weights_decay(vctx, user):
    sim = Sim(vctx, user)
    sim.calibrate()
    learner = sim.pipe.learner
    learner.cfg.refit_every = 1000  # only the bookkeeping here
    for x, y in CLICK_TARGETS[:5]:
        sim.click(x, y)
    w = learner.clicks.weights
    assert len(w) == 5 and w[0] < w[-1] and w[-1] == learner.cfg.weight
