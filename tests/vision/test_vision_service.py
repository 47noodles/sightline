"""The vision service as the rest of Sightline sees it: actions, threads and events.

The camera is a list of images and MediaPipe is replaced by the synthetic
face, so the real vision thread runs without hardware.  The pointer only ever
moves the test context's ``FakeInput``.
"""

from __future__ import annotations

import pytest
from vision_fakes import CLOSED, NO_FACE, OPEN, FakeLandmarker, PacedSource, image, wait_for
from vision_synth import SyntheticUser

from sightline.core.actions import ActionError
from sightline.core.events import CameraFrame, FaceStatus, Notice, PointerSample, SettingsChanged
from sightline.core.fakes import FakeInput
from sightline.core.interfaces import CalibrationAPI, VisionAPI
from sightline.core.models import ModelError
from sightline.core.types import Action, ClickType, PointerMode
from sightline.vision import create
from sightline.vision import service as service_mod
from sightline.vision.camera import ArraySource, _Raw, merge_devices
from sightline.vision.models import FACE_LANDMARKER_ID
from sightline.vision.pipeline import HINT_MODEL_MISSING
from sightline.vision.service import VisionService

ACTIONS = [
    "pointer.pause",
    "pointer.resume",
    "pointer.toggle",
    "pointer.mode",
    "pointer.recenter",
    "pointer.nudge",
    "pointer.restart",
    "pointer.reading_scroll",
    "dwell.on",
    "dwell.off",
    "dwell.toggle",
    "dwell.click_type",
]


def test_create_registers_the_model_and_actions_and_opens_nothing(vctx):
    svc = create(vctx)
    assert isinstance(svc, VisionAPI)
    assert isinstance(svc.calibration, CalibrationAPI)
    assert vctx.models.spec(FACE_LANDMARKER_ID).id == FACE_LANDMARKER_ID
    for name in ACTIONS:
        assert vctx.actions.has(name), name
    assert all(vctx.actions.describe()[name] for name in ACTIONS)
    assert isinstance(svc, VisionService) and svc._thread is None
    assert isinstance(vctx.desktop.input, FakeInput)  # the pump drives the fake, never the real cursor


def test_pause_resume_and_mode_actions(vctx):
    VisionService(vctx, start_pump=False)
    run = vctx.actions.execute_now
    run(Action("pointer.pause"))
    assert vctx.state.pointer_paused
    run(Action("pointer.resume"))
    assert not vctx.state.pointer_paused
    run(Action("pointer.toggle"))
    assert vctx.state.pointer_paused
    run(Action("pointer.toggle"))
    assert not vctx.state.pointer_paused
    run(Action("pointer.mode", {"mode": "Head"}))
    assert vctx.state.pointer_mode is PointerMode.HEAD
    assert vctx.settings.vision.pointer_mode is PointerMode.HEAD  # remembered
    with pytest.raises(ActionError, match="hybrid, head or gaze"):
        run(Action("pointer.mode", {"mode": "telepathy"}))
    assert vctx.state.pointer_mode is PointerMode.HEAD


def test_dwell_actions(vctx):
    VisionService(vctx, start_pump=False)
    run = vctx.actions.execute_now
    run(Action("dwell.on"))
    assert vctx.state.dwell_enabled
    run(Action("dwell.toggle"))
    assert not vctx.state.dwell_enabled
    run(Action("dwell.toggle"))
    run(Action("dwell.off"))
    assert not vctx.state.dwell_enabled
    run(Action("dwell.click_type", {"type": "double"}))
    assert vctx.state.dwell_click is ClickType.DOUBLE
    run(Action("dwell.click_type", {"type": "drag"}))
    assert vctx.state.dwell_click is ClickType.DRAG
    with pytest.raises(ActionError, match="left, right, middle, double or drag"):
        run(Action("dwell.click_type", {"type": "sideways"}))


def test_nudge_and_recenter(vctx):
    svc = VisionService(vctx, start_pump=False)
    fake: FakeInput = vctx.desktop.input  # type: ignore[assignment]
    run = vctx.actions.execute_now
    run(Action("pointer.nudge", {"direction": "left", "amount": 25}))
    assert fake.pos == (935.0, 540.0)
    assert svc.pipeline.engine.position == pytest.approx((935.0, 540.0))
    run(Action("pointer.nudge", {"direction": "down right"}))
    assert fake.pos == (945.0, 550.0)
    with pytest.raises(ActionError, match="up, down, left or right"):
        run(Action("pointer.nudge", {"direction": "sideways"}))
    with pytest.raises(ActionError, match="number"):
        run(Action("pointer.nudge", {"direction": "up", "amount": "lots"}))
    run(Action("pointer.recenter"))
    assert svc.pipeline._recenter


def test_the_vision_thread_runs_frames_through_to_events_and_actions(vctx, rec):
    user = SyntheticUser(seed=2)
    script = [OPEN] * 45 + [CLOSED] * 15 + [OPEN] * 15 + [NO_FACE] * 30 + [OPEN] * 15
    frames = [image(k) for k in script]
    made: list[ArraySource] = []
    marks: list[FakeLandmarker] = []

    def make_source():
        made.append(ArraySource(frames, fps=30.0, t0=100.0 + 10.0 * len(made)))  # time keeps moving forward
        return made[-1]

    def make_landmarker():
        marks.append(FakeLandmarker(user))
        return marks[-1]

    svc = VisionService(vctx, source_factory=make_source, landmarker_factory=make_landmarker)
    try:
        # First pass without the preview.
        svc.start()
        assert svc._thread is not None
        svc._thread.join(10.0)
        assert not svc._thread.is_alive()  # the file-like source ended
        assert marks[0].calls == len(frames) and marks[0].closed
        assert rec.of(CameraFrame) == []
        samples = rec.of(PointerSample)
        assert len(samples) == len(frames)
        phases = [s.phase for s in samples]
        assert phases[0] == "head" and "lost" in phases and phases[-1] == "head"
        present = [s.present for s in rec.of(FaceStatus)]
        assert present[0] and not all(present) and present[-1]
        # The long blink (0.5 s) clicked once, as an eye click.
        (click,) = [a for a in rec.actions if a.name == "mouse.click"]
        assert click.source == "eye" and click.args["count"] == 1

        # Second pass with the preview on: the thread starts again after the source ended.
        rec.clear()
        svc.set_preview(True)
        svc.start()
        svc._thread.join(10.0)
        previews = rec.of(CameraFrame)
        # About 15 a second over the 4 s clip.
        assert 50 <= len(previews) <= 62
        assert previews[0].image.shape == (240, 320, 3)
        with_face = [p for p in previews if p.landmarks is not None]
        assert with_face and len(with_face) < len(previews)  # also sent while no face is found
        pts = with_face[0].landmarks
        assert pts.shape[1] == 2 and (pts[:, 0] > 0).all() and (pts[:, 0] < 320).all()
    finally:
        svc.stop()
    assert svc.pump is not None and svc.pump._thread is None


def test_camera_settings_reopen_the_camera_and_other_settings_do_not(vctx):
    user = SyntheticUser(seed=3)
    opened: list[PacedSource] = []

    def make_source():
        opened.append(PacedSource([image(OPEN)] * 10, t0=200.0 + 100.0 * len(opened)))
        return opened[-1]

    svc = VisionService(vctx, source_factory=make_source, landmarker_factory=lambda: FakeLandmarker(user), start_pump=False)
    svc.start()
    try:
        assert wait_for(lambda: len(opened) == 1 and opened[0].reads > 5)
        vctx.settings.vision.smoothing = 0.2
        vctx.settings_changed("vision", save=False)
        reads = opened[0].reads
        assert wait_for(lambda: opened[0].reads > reads + 5)
        assert len(opened) == 1
        vctx.settings.vision.camera_name = "Another camera"
        vctx.settings_changed("vision", save=False)
        assert wait_for(lambda: len(opened) == 2)
    finally:
        svc.stop()


def test_cameras_and_set_camera(vctx, rec, monkeypatch):
    devices = merge_devices(
        [_Raw(0, "Webcam", "p", 1)], [_Raw(0, "OBS Virtual Camera", None, None), _Raw(1, "Webcam", "p", 1)]
    )
    monkeypatch.setattr(service_mod, "list_cameras", lambda: devices)
    svc = VisionService(vctx, start_pump=False)
    infos = svc.cameras()
    assert [(c.index, c.name) for c in infos] == [(0, "Webcam"), (1, "OBS Virtual Camera (virtual camera)")]
    svc.set_camera(1)
    cfg = vctx.settings.vision
    assert cfg.camera_index == 1 and cfg.camera_name == "OBS Virtual Camera"
    assert SettingsChanged("vision") in rec.of(SettingsChanged)
    with pytest.raises(ValueError):
        svc.set_camera(5)


def test_a_missing_model_is_reported_once(vctx, rec, monkeypatch):
    def offline(*_a, **_k):
        raise ModelError("no internet")

    monkeypatch.setattr(vctx.models, "ensure", offline)  # never download in tests
    svc = VisionService(vctx, source_factory=lambda: PacedSource([image(OPEN)]), start_pump=False)
    svc.start()
    try:
        assert wait_for(lambda: len([s for s in rec.of(FaceStatus) if s.hint == HINT_MODEL_MISSING]) >= 2)
    finally:
        svc.stop()
    notices = [n for n in rec.of(Notice) if n.key == "vision-model"]
    assert len(notices) == 1 and notices[0].level == "error"
    assert rec.of(PointerSample) == []


# --------------------------------------------------------------------------- undo


def test_undo_reverses_mode_dwell_click_type_and_nudge(vctx):
    VisionService(vctx, start_pump=False)
    fake: FakeInput = vctx.desktop.input  # type: ignore[assignment]
    reg = vctx.actions
    reg.start()
    try:
        for action in (
            Action("pointer.mode", {"mode": "gaze"}),
            Action("dwell.on"),
            Action("dwell.click_type", {"type": "right"}),
            Action("pointer.nudge", {"direction": "up", "amount": 30}),
            Action("pointer.pause"),  # not something "undo that" refers to
        ):
            reg.dispatch(action)
        for _ in range(4):
            reg.dispatch(Action("action.undo"))
    finally:
        reg.stop()
    assert fake.pos == (960.0, 540.0)
    assert vctx.state.dwell_click is ClickType.LEFT
    assert not vctx.state.dwell_enabled
    assert vctx.state.pointer_mode is PointerMode.HYBRID
    assert vctx.state.pointer_paused  # pausing stays as it was


# --------------------------------------------------------------------------- failures


def test_a_failing_frame_costs_one_frame_not_the_session(vctx, rec):
    user = SyntheticUser(seed=5)
    svc = VisionService(
        vctx,
        source_factory=lambda: ArraySource([image(OPEN)] * 60, t0=300.0),
        landmarker_factory=lambda: FakeLandmarker(user),
        start_pump=False,
    )
    real = svc.pipeline.process
    calls: list[float] = []

    def flaky(face, t, **kw):
        calls.append(t)
        if len(calls) % 10 == 5:
            raise RuntimeError("a bug in one frame")
        real(face, t, **kw)

    svc.pipeline.process = flaky  # type: ignore[method-assign]
    svc.start()
    assert svc._thread is not None
    svc._thread.join(10.0)
    svc.stop()
    assert len(calls) == 60  # the thread carried on after every failure
    assert len(rec.of(PointerSample)) == 54
    assert svc.rebuilds == 0
    assert not [n for n in rec.of(Notice) if n.key == "vision-restart"]


def test_repeated_failures_rebuild_the_pipeline_once(vctx, rec):
    user = SyntheticUser(seed=6)
    svc = VisionService(
        vctx,
        source_factory=lambda: ArraySource([image(OPEN)] * 90, t0=400.0),
        landmarker_factory=lambda: FakeLandmarker(user),
        start_pump=False,
    )
    svc.failure_limit = 5
    first = svc.pipeline

    def broken(face, t, **kw):
        raise RuntimeError("stuck")

    first.process = broken  # type: ignore[method-assign]
    svc.start()
    assert svc._thread is not None
    svc._thread.join(10.0)
    svc.stop()
    assert svc.rebuilds == 1 and svc.pipeline is not first
    texts = [n.text for n in rec.of(Notice) if n.key == "vision-restart"]
    assert texts == ["Eye tracking hit a problem and restarted."]
    assert len(rec.of(PointerSample)) == 90 - 5  # the fresh pipeline handled the rest
    assert svc.pump is not None and svc.pump.engine is svc.pipeline.engine
    assert not first._state_sub.active  # the old pipeline no longer listens to the app


def test_face_model_failures_start_the_face_model_again(vctx):
    user = SyntheticUser(seed=7)
    made: list[FakeLandmarker] = []

    class Broken(FakeLandmarker):
        def detect(self, bgr, t):
            self.calls += 1
            raise RuntimeError("the face model crashed")

    def make():
        made.append(Broken(user) if not made else FakeLandmarker(user))
        return made[-1]

    svc = VisionService(
        vctx, source_factory=lambda: ArraySource([image(OPEN)] * 60, t0=500.0), landmarker_factory=make, start_pump=False
    )
    svc.failure_limit = 5
    svc.start()
    assert svc._thread is not None
    svc._thread.join(10.0)
    svc.stop()
    assert len(made) == 2 and made[0].closed and made[0].calls == 5
    assert made[1].calls == 55


def test_restart_action_rebuilds_the_pipeline_and_the_thread(vctx, rec):
    user = SyntheticUser(seed=8)
    starts: list[float] = []

    def make_source():
        starts.append(600.0 + 100.0 * len(starts))
        return PacedSource([image(OPEN)] * 10, t0=starts[-1])

    svc = VisionService(vctx, source_factory=make_source, landmarker_factory=lambda: FakeLandmarker(user), start_pump=False)
    svc.start()
    try:
        assert wait_for(lambda: len(rec.of(PointerSample)) > 10)
        first, thread = svc.pipeline, svc._thread
        vctx.actions.execute_now(Action("pointer.restart"))
        assert svc.pipeline is not first and not first._state_sub.active
        assert svc._thread is not None and svc._thread is not thread and svc._thread.is_alive()
        assert thread is not None and not thread.is_alive()
        n = len(rec.of(PointerSample))
        assert wait_for(lambda: len(rec.of(PointerSample)) > n + 10)
        assert len(starts) == 2  # the camera was opened again
        assert "Eye tracking restarted." in [x.text for x in rec.of(Notice)]
    finally:
        svc.stop()
