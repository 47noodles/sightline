from __future__ import annotations

import pytest

from sightline.core.events import DwellProgress, GazeZoneProgress
from sightline.core.fakes import FakeDisplay
from sightline.core.types import ClickType, Rect
from sightline.vision.dwell import DwellClicker, ResumeZone, resume_rect


def run(d: DwellClicker, t0: float, seconds: float, x: float, y: float, active: bool = True, fps: float = 30.0):
    out = []
    n = int(round(seconds * fps))
    for i in range(n):
        u = d.update(t0 + i / fps, x, y, active)
        if u is not None:
            out.append(u)
    return out, t0 + n / fps


def test_dwell_fires_after_the_dwell_time():
    d = DwellClicker(0.9, 40)
    ups, t = run(d, 0.0, 1.2, 500, 500)
    fired = [u for u in ups if u.fired]
    assert len(fired) == 1
    progress = [u.progress for u in ups if not u.fired]
    assert progress == sorted(progress) and progress[-1] > 0.9
    first_fire = next(i for i, u in enumerate(ups) if u.fired)
    assert first_fire / 30.0 == pytest.approx(0.9, abs=0.07)


def test_dwell_needs_leaving_the_radius_before_the_next_one():
    d = DwellClicker(0.5, 40)
    ups, t = run(d, 0.0, 3.0, 500, 500)
    assert sum(u.fired for u in ups) == 1  # resting on the button never repeats
    ups, t = run(d, t, 0.2, 600, 500)  # leave
    ups, t = run(d, t, 0.7, 600, 500)
    assert sum(u.fired for u in ups) == 1


def test_dwell_small_jitter_keeps_going_and_leaving_resets():
    d = DwellClicker(0.6, 40)
    t = 0.0
    fired = 0
    for i in range(30):
        u = d.update(t, 500 + (15 if i % 2 else -15), 500, True)
        fired += bool(u and u.fired)
        t += 1 / 30
    assert fired == 1
    d2 = DwellClicker(0.6, 40)
    run(d2, 0.0, 0.4, 500, 500)
    u = d2.update(0.41, 700, 500, True)
    assert u is not None and u.progress == 0.0  # clears the indicator


def test_dwell_inactive_clears():
    d = DwellClicker(0.6, 40)
    run(d, 0.0, 0.3, 500, 500)
    u = d.update(0.31, 500, 500, False)
    assert u is not None and u.progress == 0.0
    assert d.update(0.32, 500, 500, False) is None


def test_resume_rect_choices():
    mon = FakeDisplay().primary()
    top = resume_rect(mon, "top")
    assert top is not None and top.y == 0 and abs(top.center[0] - 960) < 1
    assert resume_rect(mon, "off") is None
    tl = resume_rect(mon, "top_left")
    tr = resume_rect(mon, "top_right")
    assert tl is not None and tl.x == 0 and tr is not None and tr.right == 1920


def test_resume_zone_fires_after_holding_and_tolerates_blinks():
    z = ResumeZone(1.5)
    rect = Rect(800, 0, 320, 100)
    t = 0.0
    fired_at = None
    for i in range(80):
        point = None if 20 <= i < 24 else (950, 50)  # a blink in the middle
        u = z.update(t, point, rect, True)
        if u is not None and u.fired:
            fired_at = t
            break
        t += 1 / 30
    assert fired_at == pytest.approx(1.5, abs=0.05)


def test_resume_zone_resets_when_looking_away_or_not_paused():
    z = ResumeZone(1.5)
    rect = Rect(800, 0, 320, 100)
    for i in range(30):
        z.update(i / 30, (950, 50), rect, True)
    u = z.update(1.0, (950, 600), rect, True)
    assert u is not None and u.progress == 0.0
    for i in range(30):
        z.update(1.1 + i / 30, (950, 50), rect, True)
    u = z.update(2.2, (950, 50), rect, False)
    assert u is not None and u.progress == 0.0 and not u.fired


# --------------------------------------------------------------------------- through the pipeline


def frames(sim_user, pipe, t: float, seconds: float, x: float = 960, y: float = 540, **kw) -> float:
    for _ in range(int(round(seconds * 30))):
        pipe.process(sim_user.look(t, x, y, **kw), t)
        t += 1 / 30
    return t


def test_pipeline_dwell_dispatches_the_selected_click_type(vctx, rec, user):
    from sightline.vision.pipeline import VisionPipeline

    pipe = VisionPipeline(vctx)
    vctx.state.update(dwell_enabled=True, dwell_click=ClickType.RIGHT)
    t = frames(user, pipe, 10.0, 1.5)
    clicks = [a for a in rec.actions if a.name == "mouse.click"]
    assert len(clicks) == 1
    assert clicks[0].args["button"] == "right" and clicks[0].source == "dwell"
    assert rec.of(DwellProgress)
    vctx.state.update(dwell_click=ClickType.DRAG)
    t = frames(user, pipe, t, 0.2, yaw=6.0)  # move away (head mode, no gaze model)
    frames(user, pipe, t, 1.5, yaw=6.0)
    assert rec.actions[-1].name == "mouse.drag_toggle"


def test_pipeline_dwell_off_or_paused_does_nothing(vctx, rec, user):
    from sightline.vision.pipeline import VisionPipeline

    pipe = VisionPipeline(vctx)
    frames(user, pipe, 10.0, 1.5)
    vctx.state.update(dwell_enabled=True, pointer_paused=True)
    frames(user, pipe, 12.0, 1.5)
    assert not [a for a in rec.actions if a.name.startswith("mouse.")]


def test_pipeline_resume_zone_uses_the_head_before_gaze_calibration(vctx, rec, user):
    from sightline.vision.pipeline import VisionPipeline

    pipe = VisionPipeline(vctx)
    t = frames(user, pipe, 10.0, 1.0)  # learns the neutral pose
    vctx.state.update(pointer_paused=True)
    # Tilting the head up points at the top middle of the screen.
    frames(user, pipe, t, 2.2, pitch=12.0)
    assert [a.name for a in rec.actions] == ["pointer.resume"]
    zones = rec.of(GazeZoneProgress)
    assert zones[-1].progress == 1.0 and zones[-1].zone == "resume" and zones[-1].rect is not None
