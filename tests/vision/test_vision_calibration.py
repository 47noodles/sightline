"""The calibration API, driven the way the wizard drives it, with a simulated user and clock."""

from __future__ import annotations

import math

import numpy as np
import pytest

from sightline.core.events import Notice, PointerSample
from sightline.core.interfaces import CalibrationAPI
from sightline.core.types import Monitor, Rect
from sightline.vision.calibration import CalibrationController, to_degrees
from sightline.vision.pipeline import VisionPipeline

LATENCY = 0.04
GRID = [(x, y) for y in (110, 540, 970) for x in (190, 960, 1730)]
HEAD_PHASE = [(560, 320), (1360, 320), (560, 760), (1360, 760)]


class Sim:
    """A wizard, a user and a camera sharing one clock."""

    def __init__(self, ctx, user, *, mirrored: bool = False) -> None:
        self.now = 50.0
        self.ctx = ctx
        self.user = user
        self.mirrored = mirrored
        self.pipe = VisionPipeline(ctx, clock=lambda: self.now)
        self.cal = self.pipe.calibration
        self.look_at = (960.0, 540.0)
        self.yaw = 0.0
        self.pitch = 0.0
        self.closure = (0.0, 0.0)
        self.next_frame = self.now
        self.counts: list[int] = []

    def run(self, seconds: float) -> None:
        end = self.now + seconds
        while self.next_frame <= end:
            self.now = self.next_frame
            f = self.user.look(self.now - LATENCY, *self.look_at, yaw=self.yaw, pitch=self.pitch, closure=self.closure,
                               mirrored=self.mirrored)  # fmt: skip
            self.pipe.process(f, self.now)
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

    def calibrate(self, targets, **kw):
        self.cal.begin("gaze")
        rng = np.random.default_rng(7)
        for x, y in targets:
            self.dot(x, y, head=(rng.normal(0, 2.5), rng.normal(0, 2.0)), **kw)
        return self.cal.finish()


def test_controller_satisfies_the_protocol(vctx):
    pipe = VisionPipeline(vctx)
    assert isinstance(pipe.calibration, CalibrationAPI)


def test_gaze_calibration_end_to_end(vctx, rec, user):
    sim = Sim(vctx, user)
    sim.run(1.0)
    assert sim.cal.live_estimate() is None
    report = sim.calibrate(GRID + HEAD_PHASE)
    assert report.ok, report.message
    assert report.kind == "gaze"
    assert report.mean_error_px is not None and report.mean_error_px < 45
    assert report.mean_error_deg is not None and report.mean_error_deg < 1.5
    assert report.message == f"Average error {report.mean_error_px:.0f} px (about {report.mean_error_deg:.1f} degrees)."
    assert len(report.per_point_error_px) == 13
    assert report.samples > 13 * 15
    assert all(c >= 15 for c in sim.counts)  # sample_count saw the frames while collecting
    assert vctx.state.gaze_calibrated
    folder = sim.pipe.profile.folder
    assert (folder / "gaze_model.npz").exists() and (folder / "gaze_samples.npz").exists()
    # The model is live: gaze estimates appear in pointer samples and the live estimate.
    sim.look_at = (1500, 300)
    sim.run(0.5)
    est = sim.cal.live_estimate()
    assert est is not None and math.hypot(est[0] - 1500, est[1] - 300) < 80
    sample = rec.of(PointerSample)[-1]
    assert sample.gaze_x is not None


def test_blinks_during_calibration_are_rejected(vctx, user):
    sim = Sim(vctx, user)
    sim.cal.begin("gaze")
    for i, (x, y) in enumerate(GRID + HEAD_PHASE):
        sim.cal.set_target(x, y, False)
        sim.run(0.2)
        sim.look_at = (x, y)
        sim.run(0.2)
        sim.cal.set_target(x, y, True)
        sim.run(0.3)
        if i % 3 == 0:
            sim.closure = (1.0, 1.0)  # a blink in the middle of collecting
            sim.run(0.15)
            sim.closure = (0.0, 0.0)
        sim.run(0.5)
        sim.cal.set_target(x, y, False)
    report = sim.cal.finish()
    assert report.ok
    assert report.details["rejected_blinks"] > 0
    assert report.mean_error_px < 50


def test_pursuit_targets_with_lag_compensation(vctx, user):
    sim = Sim(vctx, user)
    sim.cal.begin("gaze")
    lag = 0.1
    path = []
    t0 = sim.now
    duration = 8.0
    while sim.now < t0 + duration:
        s = (sim.now - t0) / duration * 2 * math.pi
        x = 960 + 760 * math.sin(3 * s)
        y = 540 + 420 * math.sin(2 * s)
        path.append((sim.now, x, y))
        sim.cal.set_target(x, y, True)
        # The eyes trail the dot.
        lagged = [p for p in path if p[0] <= sim.now - lag]
        if lagged:
            sim.look_at = (lagged[-1][1], lagged[-1][2])
        sim.run(1 / 60)
    sim.cal.set_target(path[-1][1], path[-1][2], False)
    report = sim.cal.finish()
    assert report.ok, report.message
    assert report.details["pursuit_lag_ms"] in (66, 100, 133)
    assert report.mean_error_px < 70


def test_validate_reports_the_active_model(vctx, user):
    sim = Sim(vctx, user)
    sim.cal.begin("validate")
    assert not sim.cal.finish().ok  # nothing to validate yet
    assert sim.calibrate(GRID + HEAD_PHASE).ok
    sim.cal.begin("validate")
    for x, y in [(400, 300), (1500, 300), (960, 800), (300, 900)]:
        sim.dot(x, y)
    report = sim.cal.finish()
    assert report.ok and report.kind == "validate"
    assert report.mean_error_px < 70
    assert report.message.startswith("Average error")


def test_cancel_discards_everything(vctx, user):
    sim = Sim(vctx, user)
    sim.cal.begin("gaze")
    for x, y in GRID[:4]:
        sim.dot(x, y)
    sim.cal.cancel()
    assert not sim.cal.active
    assert not vctx.state.gaze_calibrated
    assert not (sim.pipe.profile.folder / "gaze_model.npz").exists()
    assert sim.cal.finish().ok is False


def test_too_little_data_fails_with_a_plain_message(vctx, user):
    sim = Sim(vctx, user)
    sim.cal.begin("gaze")
    sim.dot(960, 540)
    report = sim.cal.finish()
    assert not report.ok
    assert report.message.startswith("Not enough data")


def test_saved_model_is_loaded_at_start(vctx, user):
    sim = Sim(vctx, user)
    assert sim.calibrate(GRID + HEAD_PHASE).ok
    from sightline.core.context import AppContext

    ctx2 = AppContext.for_tests()
    pipe2 = VisionPipeline(ctx2)
    assert ctx2.state.gaze_calibrated
    assert pipe2.current_gaze_model() is not None


def test_changed_screen_layout_needs_recalibration(vctx, user):
    sim = Sim(vctx, user)
    assert sim.calibrate(GRID + HEAD_PHASE).ok
    from sightline.core.context import AppContext
    from sightline.core.fakes import FakeDesktop, FakeDisplay

    ctx2 = AppContext.for_tests()
    desk = FakeDesktop()
    desk.display = FakeDisplay(2560, 1440)
    ctx2.desktop = desk
    notices: list[Notice] = []
    ctx2.bus.subscribe(Notice, notices.append)
    pipe2 = VisionPipeline(ctx2)
    assert pipe2.current_gaze_model() is None
    assert not ctx2.state.gaze_calibrated
    assert notices and "calibrating again" in notices[0].text


# --------------------------------------------------------------------------- eyes


def _eye_session(sim: Sim, closures: dict[str, tuple[float, float]]):
    sim.cal.begin("eyes")
    sim.run(0.5)
    for label in ("open", "closed", "wink_left", "wink_right", "open"):
        sim.cal.set_eye_label(label)  # type: ignore[arg-type]
        sim.run(0.5)  # the user reacts
        sim.closure = closures[label]
        sim.run(1.5)
        sim.closure = (0.0, 0.0)
    sim.cal.set_eye_label(None)
    sim.run(0.3)
    return sim.cal.finish()


def test_eye_calibration_saves_a_profile(vctx, user):
    sim = Sim(vctx, user)
    report = _eye_session(sim, {"open": (0, 0), "closed": (1, 1), "wink_left": (1, 0), "wink_right": (0, 1)})
    assert report.ok, report.message
    assert report.message == "Blinks and winks are set up."
    assert vctx.state.eyes_calibrated
    assert (sim.pipe.profile.folder / "eyes.json").exists()
    assert sim.pipe.gestures.cfg.wink_left and sim.pipe.gestures.cfg.wink_right


def test_eye_calibration_turns_winks_off_for_a_user_who_cannot_wink(vctx, user):
    sim = Sim(vctx, user)
    report = _eye_session(sim, {"open": (0, 0), "closed": (1, 1), "wink_left": (1, 0.85), "wink_right": (0.85, 1)})
    assert report.ok
    assert "wink clicks are off" in report.message
    assert not sim.pipe.gestures.cfg.wink_left and not sim.pipe.gestures.cfg.wink_right


def test_eye_calibration_fixes_a_mirrored_camera(vctx, user):
    sim = Sim(vctx, user, mirrored=True)  # the camera mirrors, the setting does not know yet
    report = _eye_session(sim, {"open": (0, 0), "closed": (1, 1), "wink_left": (1, 0), "wink_right": (0, 1)})
    assert report.ok and "mirror" in report.message
    assert vctx.settings.vision.camera_mirrored is True


# --------------------------------------------------------------------------- head


def test_head_calibration_learns_gains_and_spots_a_mirror(vctx, user):
    for mirrored in (False, True):
        from sightline.core.context import AppContext

        ctx = AppContext.for_tests()
        sim = Sim(ctx, user, mirrored=mirrored)
        sim.run(1.0)
        sim.cal.begin("head")
        for (x, y), (yaw, pitch) in [((960, 540), (0, 0)), ((150, 540), (-12, 0)), ((1770, 540), (12, 0)),
                                     ((960, 100), (0, 8)), ((960, 980), (0, -8))]:  # fmt: skip
            sim.cal.set_target(x, y, True)
            sim.look_at = (x, y)
            steps = 20
            for i in range(steps):
                sim.yaw += (yaw - sim.yaw) * 0.3
                sim.pitch += (pitch - sim.pitch) * 0.3
                sim.run(1.6 / steps)
            sim.cal.set_target(x, y, False)
        report = sim.cal.finish()
        assert report.ok, report.message
        assert ctx.settings.vision.camera_mirrored is mirrored
        prof = sim.pipe.head.profile
        assert prof is not None and 1000 < prof.gain_x < 20000
        assert (sim.pipe.profile.folder / "head.json").exists()
        if mirrored:
            assert "mirror" in report.message


def test_degrees_use_the_screen_size():
    # 40 px on a 0.2745 mm pitch at 60 cm is about 1.05 degrees.
    assert to_degrees(40, 0.2745, 60) == pytest.approx(1.049, abs=0.01)


def test_errors_in_degrees_follow_the_monitor_size(vctx, user):
    sim = Sim(vctx, user)
    report = sim.calibrate(GRID + HEAD_PHASE)
    pitch = 527.0 / 1920
    dist = float(np.median(list(sim.pipe._distance)))
    assert report.mean_error_deg == pytest.approx(to_degrees(report.mean_error_px, pitch, dist), rel=0.15)


def test_unknown_kind_is_refused(vctx):
    pipe = VisionPipeline(vctx)
    with pytest.raises(ValueError):
        pipe.calibration.begin("telepathy")  # type: ignore[arg-type]


def test_segments_split_still_targets_and_pursuit():
    ctrl = CalibrationController(host=None, clock=lambda: 0.0)  # type: ignore[arg-type]
    events = [(0.0, 100, 100, True), (0.5, 100, 100, True), (1.0, 100, 100, False),
              (1.2, 500, 500, True), (2.0, 900, 500, True)]  # fmt: skip
    segs = ctrl._segments(events, 3.0)
    # Still dot, then a dot that jumped elsewhere after 0.8 s without the wizard pausing: three targets.
    assert len(segs) == 3
    assert not segs[0].pursuit and segs[0].end == 1.0
    assert not segs[1].pursuit and segs[1].end == 2.0 and segs[2].path[0][1] == 900
    events = [(0.0, 100, 100, True)] + [(0.0 + i / 60, 100 + 5 * i, 100, True) for i in range(1, 60)]
    segs = ctrl._segments(events, 2.0)
    assert len(segs) == 1 and segs[0].pursuit
    assert segs[0].target_at(0.5)[0] == pytest.approx(100 + 5 * 30, abs=6)


def test_primary_monitor_without_size_uses_a_default_pitch():
    from sightline.vision.calibration import DEFAULT_PITCH_MM, _pitch_mm

    mon = Monitor("m", "M", Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1040), 1.0, True, None)
    assert _pitch_mm([mon], np.array([[960.0, 540.0]])) == DEFAULT_PITCH_MM
