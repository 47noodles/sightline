"""Eye gestures and pointer behaviour through the whole pipeline, with synthetic faces."""

from __future__ import annotations

import pytest

from sightline.core.events import EyeGesture, EyeHold, EyeState, FaceStatus, PointerSample
from sightline.core.types import ClickType, PointerMode
from sightline.vision.pipeline import HINT_NO_FACE, VisionPipeline, click_action


class Run:
    def __init__(self, ctx, user) -> None:
        self.pipe = VisionPipeline(ctx)
        self.user = user
        self.t = 10.0

    def look(self, seconds: float, x: float = 960, y: float = 540, **kw) -> None:
        for _ in range(int(round(seconds * 30))):
            self.pipe.process(self.user.look(self.t, x, y, **kw), self.t)
            self.t += 1 / 30

    def gone(self, seconds: float) -> None:
        for _ in range(int(round(seconds * 30))):
            self.pipe.process(None, self.t)
            self.t += 1 / 30


def test_long_blink_clicks_where_the_pointer_was_before_the_eyes_closed(vctx, rec, user):
    r = Run(vctx, user)
    r.look(1.0)
    before = r.pipe.engine.position
    # The head drifts while the eyes close; the click must still land where the pointer was before.
    r.look(0.5, closure=(1, 1), yaw=4.0)
    r.look(0.4, yaw=4.0)
    (action,) = [a for a in rec.actions if a.name == "mouse.click"]
    assert action.source == "eye"
    assert action.args["button"] == "left" and action.args["count"] == 1
    assert action.args["x"] == pytest.approx(before[0], abs=2) and action.args["y"] == pytest.approx(before[1], abs=2)
    holds = rec.of(EyeHold)
    assert [h.stage for h in holds] == [1, 0]
    (g,) = rec.of(EyeGesture)
    assert g.kind == "long_blink"


def test_natural_blinks_in_the_pipeline_never_click(vctx, rec, user):
    r = Run(vctx, user)
    r.look(1.0)
    for _ in range(10):
        r.look(0.15, closure=(1, 1))
        r.look(1.0)
    assert rec.actions == []
    assert rec.of(EyeGesture) == [] and rec.of(EyeHold) == []
    assert len(r.pipe.gestures.blink_durations) == 10


def test_winks_map_to_right_and_middle_click(vctx, rec, user):
    r = Run(vctx, user)
    r.look(1.0)
    r.look(0.3, closure=(0, 1))
    r.look(0.5)
    r.look(0.3, closure=(1, 0))
    r.look(0.5)
    buttons = [a.args["button"] for a in rec.actions]
    assert buttons == ["right", "middle"]
    assert [g.kind for g in rec.of(EyeGesture)] == ["wink_right", "wink_left"]


def test_mirrored_camera_setting_keeps_winks_on_the_right_eye(vctx, rec, user):
    vctx.settings.vision.camera_mirrored = True
    r = Run(vctx, user)
    r.look(1.0, mirrored=True)
    r.look(0.3, closure=(0, 1), mirrored=True)  # the user's own right eye
    r.look(0.5, mirrored=True)
    assert [a.args["button"] for a in rec.actions] == ["right"]


def test_hold_blink_double_clicks_and_click_follows_the_click_type(vctx, rec, user):
    r = Run(vctx, user)
    r.look(1.0)
    r.look(1.2, closure=(1, 1))
    r.look(0.4)
    assert rec.actions[-1].args["count"] == 2
    vctx.state.update(dwell_click=ClickType.MIDDLE)
    r.look(0.5, closure=(1, 1))
    r.look(0.4)
    assert rec.actions[-1].args["button"] == "middle"


def test_gestures_are_ignored_while_paused_calibrating_or_yielding(vctx, rec, user):
    r = Run(vctx, user)
    r.look(1.0)
    vctx.state.update(pointer_paused=True)
    r.look(0.5, closure=(1, 1))
    r.look(0.4)
    vctx.state.update(pointer_paused=False)
    r.pipe.set_yielding(True)
    r.look(0.5, closure=(1, 1))
    r.look(0.4)
    r.pipe.set_yielding(False)
    r.pipe.calibration.begin("eyes")
    r.look(0.5, closure=(1, 1))
    r.look(0.4)
    r.pipe.calibration.cancel()
    assert rec.actions == []
    assert rec.of(EyeGesture) == []


def test_gesture_bound_to_resume_works_while_paused(vctx, rec, user):
    vctx.settings.vision.gesture_actions["wink_left"] = "pointer.toggle"
    r = Run(vctx, user)
    r.look(1.0)
    vctx.state.update(pointer_paused=True)
    r.look(0.3, closure=(1, 0))
    r.look(0.4)
    assert [a.name for a in rec.actions] == ["pointer.toggle"]


def test_face_lost_mid_blink_cancels(vctx, rec, user):
    r = Run(vctx, user)
    r.look(1.0)
    r.look(0.45, closure=(1, 1))
    r.gone(0.5)
    r.look(0.5)
    assert rec.actions == []
    assert rec.of(EyeHold)[-1].cancelled
    assert rec.of(EyeState)[-1].state == "open"
    assert any(e.state == "unknown" for e in rec.of(EyeState))


def test_pointer_samples_and_face_status(vctx, rec, user):
    r = Run(vctx, user)
    r.look(1.0)
    samples = rec.of(PointerSample)
    assert len(samples) == 30
    assert samples[-1].phase == "head" and samples[-1].gaze_x is None  # not calibrated: head only
    statuses = rec.of(FaceStatus)
    assert statuses and statuses[-1].present
    assert statuses[-1].distance_cm == pytest.approx(58, abs=8)
    assert vctx.state.face_present
    r.gone(1.0)
    assert not vctx.state.face_present
    assert rec.of(PointerSample)[-1].phase == "lost"
    assert rec.of(FaceStatus)[-1].hint == HINT_NO_FACE


def test_face_status_hints(vctx, rec, user):
    from vision_synth import SyntheticFace, SyntheticUser

    near = SyntheticUser(SyntheticFace(iod_px=330.0))  # face very close to the camera
    r = Run(vctx, near)
    r.look(1.5)
    assert rec.of(FaceStatus)[-1].hint == "Move back a little"
    rec.clear()
    far = SyntheticUser(SyntheticFace(iod_px=80.0))
    r2 = Run(vctx, far)
    r2.look(1.5)
    assert rec.of(FaceStatus)[-1].hint == "Move closer"


def test_recenter_in_head_mode_brings_the_pointer_home(vctx, user):
    r = Run(vctx, user)
    r.look(1.0)
    r.look(0.7, yaw=8.0)
    assert r.pipe.engine.position[0] > 1100
    r.pipe.request_recenter()
    r.look(0.1, yaw=8.0)
    assert r.pipe.engine.position[0] == pytest.approx(960, abs=30)
    assert r.pipe.head.last is not None and abs(r.pipe.head.last.yaw) < 1.0


def test_settings_change_reaches_the_components(vctx):
    pipe = VisionPipeline(vctx)
    cfg = vctx.settings.vision
    cfg.blink_click_ms = 500
    cfg.blink_double_ms = 400  # inconsistent on purpose: double must stay after click
    cfg.warp_distance_px = 250
    cfg.dwell_ms = 1200
    cfg.pointer_mode = PointerMode.GAZE
    vctx.settings_changed("vision", save=False)
    pipe.apply_settings()
    g = pipe.gestures.cfg
    assert g.click_s == 0.5 and g.double_s >= 0.7 and g.cancel_s > g.double_s
    assert pipe.engine.cfg.warp_distance == 250
    assert pipe.dwell.dwell_s == pytest.approx(1.2)
    assert vctx.state.pointer_mode is PointerMode.GAZE


def test_click_action_mapping():
    a = click_action("click", 1, 2, ClickType.DOUBLE, "eye")
    assert a is not None and a.name == "mouse.click" and a.args == {"button": "left", "count": 2, "x": 1, "y": 2}
    assert click_action("right_click", 1, 2, ClickType.LEFT, "eye").args["button"] == "right"  # type: ignore[union-attr]
    assert click_action("drag", 1, 2, ClickType.LEFT, "eye").name == "mouse.drag_toggle"  # type: ignore[union-attr]
    assert click_action("none", 1, 2, ClickType.LEFT, "eye") is None
    assert click_action("ui.labels.show", 1, 2, ClickType.LEFT, "eye").name == "ui.labels.show"  # type: ignore[union-attr]
    assert click_action("telepathy", 1, 2, ClickType.LEFT, "eye") is None
