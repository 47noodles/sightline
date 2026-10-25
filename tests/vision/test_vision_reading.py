"""A calmer pointer: holding still while the user reads, jumping only on intent, and the reading scroll."""

from __future__ import annotations

import math

import numpy as np
import pytest
from vision_sim import Sim, shared_model

from sightline.core.actions import ActionError
from sightline.core.events import EyeGesture, Notice
from sightline.core.fakes import FakeDisplay
from sightline.core.types import Action, PointerMode, Rect, WindowInfo
from sightline.vision.filters import GazeState
from sightline.vision.head import HeadSample
from sightline.vision.pointer import PointerEngine
from sightline.vision.reading import ReadingDetector, ReadingScroll, ScrollCommand
from sightline.vision.service import VisionService

FPS = 30.0


def gaze(x: float, y: float, fix_s: float = 0.0, fixating: bool | None = None) -> GazeState:
    fixating = fix_s > 0 if fixating is None else fixating
    return GazeState(x, y, fixating, x, y, fix_s, 10.0, 40.0, False)


def head(dx: float = 0.0, dy: float = 0.0, speed: float = 0.02) -> HeadSample:
    return HeadSample(dx, dy, speed, 0.0, 0.0, 0.0, 0.0)


def step(e: PointerEngine, t: float, **kw):
    args = dict(present=True, mode=PointerMode.HYBRID, gaze=None, head=None, frozen=False, paused=False)
    args.update(kw)
    return e.update(t, **args)


def reading_track(seconds: float, *, left=300.0, right=1300.0, top=300.0, line_h=40.0, step_px=80.0, fix_s=0.25,
                  direction=1, noise=2.0, seed=0):  # fmt: skip
    """Smoothed gaze of someone reading: steps along a line, a sweep back, the next line."""
    rng = np.random.default_rng(seed)
    per_line = int((right - left) / step_px)
    out = []
    for i in range(int(seconds * FPS)):
        t = i / FPS
        line = int(t // (per_line * fix_s))
        k = int((t % (per_line * fix_s)) // fix_s)
        x = left + k * step_px if direction > 0 else right - k * step_px
        out.append((t, x + rng.normal(0, noise), top + line * line_h + rng.normal(0, noise)))
    return out


def run_detector(det: ReadingDetector, track, *, head_speed=0.02, t0: float = 0.0) -> list[bool]:
    return [det.update(t0 + t, gaze(x, y), head_speed) for t, x, y in track]


# --------------------------------------------------------------------------- reading detector


def test_reading_along_lines_is_recognised_and_survives_line_changes():
    det = ReadingDetector()
    states = run_detector(det, reading_track(9.0))
    assert not any(states[:20])  # not before there is a line to see
    first = states.index(True)
    assert first / FPS < 2.0
    assert all(states[first:])  # held through the sweeps back to the start of each line


def test_right_to_left_reading_is_recognised():
    det = ReadingDetector()
    assert any(run_detector(det, reading_track(4.0, direction=-1)))


def test_looking_around_the_screen_is_not_reading():
    rng = np.random.default_rng(3)
    det = ReadingDetector()
    states = []
    t = 0.0
    for _ in range(12):  # a new place to look every half second
        x, y = rng.uniform(100, 1800), rng.uniform(100, 1000)
        for _ in range(15):
            states.append(det.update(t, gaze(x + rng.normal(0, 2), y + rng.normal(0, 2)), 0.02))
            t += 1 / FPS
    assert not any(states)


def test_moving_down_the_page_is_not_reading():
    det = ReadingDetector()
    states = [det.update(i / FPS, gaze(900.0, 200.0 + 6.0 * i), 0.02) for i in range(120)]
    assert not any(states)


def test_head_movement_ends_reading_at_once():
    det = ReadingDetector()
    run_detector(det, reading_track(4.0))
    assert det.reading
    assert det.update(4.0, gaze(900, 300), head_speed=0.2) is False
    # And reading while the head moves does not count.
    det = ReadingDetector()
    assert not any(run_detector(det, reading_track(4.0), head_speed=0.09))


def test_a_long_look_at_one_spot_ends_reading():
    det = ReadingDetector()
    run_detector(det, reading_track(4.0))
    assert det.reading
    t = 4.0
    for i in range(20):
        det.update(t, gaze(800, 340, fix_s=i / FPS), 0.02)
        t += 1 / FPS
    assert not det.reading


def test_reading_stops_after_a_pause_with_no_evidence():
    det = ReadingDetector()
    run_detector(det, reading_track(4.0))
    t = 4.0
    states = []
    for _ in range(int(3.5 * FPS)):  # eyes resting on the page, never seen as one long fixation
        states.append(det.update(t, gaze(800, 340), 0.02))
        t += 1 / FPS
    assert states[0] and not states[-1]  # two seconds after the last line-like movement left the window


# --------------------------------------------------------------------------- engine


def engine() -> PointerEngine:
    return PointerEngine(FakeDisplay(), (960.0, 540.0))


def test_reading_holds_the_pointer_in_hybrid_and_gaze_modes():
    e = engine()
    assert step(e, 0.0, gaze=gaze(1500, 300, fix_s=0.3), reading=True) == "fine"
    step(e, 0.1, gaze=gaze(1500, 300, fix_s=0.4), head=head(20, 0), reading=True)
    assert e.position == (960.0, 540.0)  # neither the eyes nor small head movements move it
    assert step(e, 0.2, gaze=gaze(1500, 300, fix_s=0.5)) == "warp"  # reading over: free again
    g = engine()
    step(g, 0.0, mode=PointerMode.GAZE, gaze=gaze(1500, 300), reading=True)
    assert g.position == (960.0, 540.0)
    step(g, 0.1, mode=PointerMode.GAZE, gaze=gaze(1500, 300))
    assert g.position == (1500.0, 300.0)


def test_conservative_jump_waits_for_a_head_movement():
    e = engine()
    e.cfg.warp_on_intent = True
    for i in range(10):
        assert step(e, i / FPS, gaze=gaze(1500, 300, fix_s=0.2 + i / FPS), head=head()) == "fine"
    assert e.position == (960.0, 540.0)  # looked away, did not ask for the pointer
    assert step(e, 10 / FPS, gaze=gaze(1500, 300, fix_s=0.6), head=head(3.0, 0.0)) == "warp"
    assert e.position == (1500.0, 300.0)


def test_conservative_jump_on_a_click_or_a_spoken_command():
    e = engine()
    e.cfg.warp_on_intent = True
    assert e.take_intent(0.0) is None  # nothing looked at yet
    for i in range(10):
        step(e, i / FPS, gaze=gaze(1500, 300, fix_s=0.2 + i / FPS))
    for i in range(10, 20):  # then back near the pointer
        step(e, i / FPS, gaze=gaze(1000, 560, fix_s=0.2))
    assert e.take_intent(18 / FPS) is None  # at that moment the user looked near the pointer
    assert e.take_intent(5 / FPS) == (1500.0, 300.0)  # aimed while looking far away: jump there
    assert e.position == (1500.0, 300.0)
    assert e.take_intent(5 / FPS) is None  # already there


def test_aim_point_after_the_head_already_took_the_pointer_there():
    e = engine()
    e.cfg.warp_on_intent = True
    for i in range(10):
        step(e, i / FPS, gaze=gaze(1500, 300, fix_s=0.2 + i / FPS))
    step(e, 10 / FPS, gaze=gaze(1500, 300, fix_s=0.6), head=head(3.0, 0.0))  # the head asks: jump
    for i in range(11, 30):  # it rests there, then the head steers it a little
        step(e, i / FPS, gaze=gaze(1500, 300, fix_s=0.6), head=head(6.0 if 17 <= i < 20 else 0.0, 0.0))
    steered = e.position
    assert steered[0] > 1500
    # "Click", aimed at a moment before the jump: where the jump came to rest, not back where the
    # pointer was before it, and no second jump.
    assert e.aim_point(5 / FPS) == (1500.0, 300.0)
    assert e.position == steered


def test_immediate_jumping_has_no_intent_to_take():
    e = engine()
    step(e, 0.0, gaze=gaze(1500, 300, fix_s=0.3))
    assert e.position == (1500.0, 300.0)
    assert e.take_intent(0.0) is None


# --------------------------------------------------------------------------- with the pipeline


def reading_path(t0: float, *, left=300, right=1300, top=300, line_h=40, step_px=80, fix_s=0.25):
    per_line = int((right - left) / step_px)

    def at(t: float) -> tuple[float, float]:
        u = max(0.0, t - t0)
        line = int(u // (per_line * fix_s))
        k = int((u % (per_line * fix_s)) // fix_s)
        return left + k * step_px, top + (line % 6) * line_h

    return at


def test_pipeline_holds_the_pointer_while_reading_and_lets_go_on_a_head_movement(vctx, user):
    sim = Sim(vctx, user)
    sim.use_model()
    sim.look_at = (300, 300)
    sim.run(1.0)  # the pointer jumps to the start of the text
    sim.path = reading_path(sim.now)
    sim.run(2.5)
    assert sim.pipe.reading.reading
    held = sim.pipe.engine.position
    sim.run(3.0)
    assert sim.pipe.reading.reading
    assert sim.pipe.engine.position == held  # did not chase the eyes along the lines
    turn = sim.now - 0.04
    sim.head_path = lambda t: (40.0 * max(0.0, t - turn), 0.0)  # a deliberate turn of the head
    sim.run(0.3)
    assert not sim.pipe.reading.reading
    assert sim.pipe.engine.position != held


def test_reading_hold_can_be_turned_off(vctx, user):
    vctx.settings.vision.reading_hold = False
    sim = Sim(vctx, user)
    sim.use_model()
    sim.look_at = (300, 300)
    sim.run(1.0)
    sim.path = reading_path(sim.now)
    sim.run(4.0)
    assert not sim.pipe.reading.reading


def test_a_blink_click_jumps_to_where_the_user_looked_when_jumping_waits_for_intent(vctx, rec, user):
    vctx.settings.vision.warp_on_intent = True
    sim = Sim(vctx, user)
    sim.pipe.apply_settings()
    sim.use_model()
    sim.run(0.8)
    start = sim.pipe.engine.position
    sim.look_at = (1500, 300)
    sim.run(0.8)
    assert sim.pipe.engine.position == start  # looked away without asking for the pointer
    sim.closure = (1.0, 1.0)  # a long blink: "click"
    sim.run(0.5)
    sim.closure = (0.0, 0.0)
    sim.run(0.3)
    clicks = [a for a in rec.actions if a.name == "mouse.click"]
    assert len(clicks) == 1
    x, y = clicks[0].args["x"], clicks[0].args["y"]
    assert math.hypot(x - 1500, y - 300) < 80  # the click went where the user looked
    assert rec.of(EyeGesture)[-1].x == x


def test_service_aim_point_takes_the_intent_first(vctx):
    vctx.settings.vision.warp_on_intent = True
    svc = VisionService(vctx, start_pump=False)
    svc._alive = lambda: True  # type: ignore[method-assign]
    e = svc.pipeline.engine
    for i in range(10):
        step(e, 100.0 + i / FPS, gaze=gaze(1500, 300, fix_s=0.2 + i / FPS))
    assert svc.aim_point(100.2) == (1500.0, 300.0)  # a spoken command is the sign of intent
    assert svc.aim_point(100.2) == (1500.0, 300.0)  # then the resting point, as before


# --------------------------------------------------------------------------- reading scroll

WINDOW = Rect(0, 0, 1000, 1000)  # bands of 200 px at the top and bottom


def scroll_run(rs: ReadingScroll, t0: float, seconds: float, point, *, error=None, window=WINDOW, active=True):
    cmds = []
    t = t0
    for _ in range(int(round(seconds * FPS))):
        cmd = rs.update(t, point, window, error, active)
        if cmd is not None:
            cmds.append(cmd)
        t += 1 / FPS
    return t, cmds


def enabled_scroll() -> ReadingScroll:
    rs = ReadingScroll()
    rs.enabled = True
    return rs


def test_looking_low_in_the_window_scrolls_down_faster_the_deeper_it_is():
    rs = enabled_scroll()
    t, cmds = scroll_run(rs, 0.0, 0.3, (500, 900))
    assert cmds == []  # a glance does nothing
    t, cmds = scroll_run(rs, t, 0.2, (500, 900))
    assert len(cmds) == 1 and cmds[0].start and cmds[0].direction == "down"
    assert cmds[0].speed == pytest.approx(0.25 + 0.95 * 0.5, abs=0.01)
    t, cmds = scroll_run(rs, t, 0.5, (500, 990))  # deeper: faster
    assert len(cmds) == 1 and cmds[0].speed == pytest.approx(0.25 + 0.95 * 0.95, abs=0.01)
    t, cmds = scroll_run(rs, t, 0.5, (500, 500))  # the middle stops it
    assert cmds == [ScrollCommand(False)]
    assert rs.direction is None


def test_looking_high_in_the_window_scrolls_back_up():
    rs = enabled_scroll()
    _, cmds = scroll_run(rs, 0.0, 0.6, (500, 60))
    assert [(c.start, c.direction) for c in cmds] == [(True, "up")]


def test_hysteresis_follows_the_local_error():
    rs = enabled_scroll()
    t, _ = scroll_run(rs, 0.0, 0.6, (500, 900), error=60.0)
    assert rs.direction == "down"
    t, cmds = scroll_run(rs, t, 1.0, (500, 770), error=60.0)  # above the zone, but within 0.75 x 60 px of it
    assert rs.direction == "down" and all(c.start for c in cmds)  # still scrolling, at the slowest speed
    assert cmds[-1].speed == pytest.approx(0.25)
    t, cmds = scroll_run(rs, t, 0.2, (500, 750), error=60.0)
    assert cmds == [ScrollCommand(False)]
    # Starting needs the gaze a quarter of the error inside the zone.
    rs = enabled_scroll()
    _, cmds = scroll_run(rs, 0.0, 1.0, (500, 810), error=60.0)
    assert cmds == []


def test_blinks_are_bridged_but_a_longer_gap_stops():
    rs = enabled_scroll()
    t, _ = scroll_run(rs, 0.0, 0.6, (500, 900))
    t, cmds = scroll_run(rs, t, 0.3, None)
    assert cmds == [] and rs.direction == "down"
    t, _ = scroll_run(rs, t, 0.2, (500, 900))
    _, cmds = scroll_run(rs, t, 0.6, None)
    assert cmds == [ScrollCommand(False)]


def test_looking_outside_the_window_or_turning_it_off_stops():
    rs = enabled_scroll()
    t, _ = scroll_run(rs, 0.0, 0.6, (500, 900))
    _, cmds = scroll_run(rs, t, 0.1, (1300, 900))
    assert cmds == [ScrollCommand(False)]
    rs = enabled_scroll()
    t, _ = scroll_run(rs, 0.0, 0.6, (500, 900))
    _, cmds = scroll_run(rs, t, 0.1, (500, 900), active=False)  # paused, calibrating or no model
    assert cmds == [ScrollCommand(False)]
    rs = enabled_scroll()
    t, _ = scroll_run(rs, 0.0, 0.6, (500, 900))
    rs.enabled = False
    assert rs.update(t, (500, 900), WINDOW, None, True) == ScrollCommand(False)
    assert rs.stop() is None  # nothing left to stop


def scroll_actions(rec) -> list[tuple]:
    out = []
    for a in rec.actions:
        if a.name == "mouse.scroll_start":
            out.append(("start", a.args["direction"]))
            assert a.source == "eye" and 0.25 <= a.args["speed"] <= 1.2
        elif a.name == "mouse.scroll_stop":
            out.append(("stop",))
    return out


def test_pipeline_scrolls_the_window_under_the_pointer(vctx, rec, user):
    vctx.desktop.windows.windows = [WindowInfo(7, "Notes", "notepad.exe", Rect(200, 100, 1200, 800))]
    sim = Sim(vctx, user)
    sim.use_model()
    sim.pipe.set_reading_scroll(True)
    sim.look_at = (800, 450)
    sim.run(1.0)
    assert scroll_actions(rec) == []
    sim.look_at = (800, 860)  # the bottom fifth of the window
    sim.run(1.0)
    assert scroll_actions(rec) == [("start", "down")]
    sim.look_at = (800, 450)
    sim.run(1.0)
    assert scroll_actions(rec) == [("start", "down"), ("stop",)]
    sim.look_at = (800, 140)
    sim.run(1.0)
    assert scroll_actions(rec)[-1] == ("start", "up")
    vctx.state.update(pointer_paused=True)  # pausing eye control stops it
    sim.run(0.2)
    assert scroll_actions(rec)[-1] == ("stop",)


def test_pipeline_asks_for_the_one_window_at_the_pointer(vctx, rec, user):
    windows = vctx.desktop.windows
    windows.windows = [WindowInfo(7, "Notes", "notepad.exe", Rect(200, 100, 1200, 800))]
    asked: list[tuple[float, float]] = []
    answer = windows.window_at

    def window_at(x: float, y: float) -> WindowInfo | None:
        asked.append((x, y))
        return answer(x, y)

    def list_windows() -> list[WindowInfo]:
        raise AssertionError("listing every window is too slow for the vision thread")

    windows.window_at = window_at
    windows.list_windows = list_windows
    sim = Sim(vctx, user)
    sim.use_model()
    sim.pipe.set_reading_scroll(True)
    sim.look_at = (800, 860)  # the bottom fifth of the window
    sim.run(1.0)
    assert scroll_actions(rec) == [("start", "down")]
    assert 1 <= len(asked) <= 5, "asked at most every quarter second"
    assert Rect(200, 100, 1200, 800).contains(*asked[-1]), "asked at the pointer"


def test_pipeline_scrolls_the_work_area_when_the_window_lookup_fails(vctx, rec, user):
    def broken(x: float, y: float) -> WindowInfo | None:
        raise OSError("access denied")

    vctx.desktop.windows.window_at = broken
    sim = Sim(vctx, user)
    sim.use_model()
    sim.pipe.set_reading_scroll(True)
    sim.look_at = (960, 1000)  # bottom fifth of the work area
    sim.run(1.0)
    assert scroll_actions(rec) == [("start", "down")]


def test_pipeline_uses_the_work_area_without_a_window_and_stops_when_turned_off(vctx, rec, user):
    sim = Sim(vctx, user)
    sim.use_model()
    sim.pipe.set_reading_scroll(True)
    sim.look_at = (960, 1000)  # bottom fifth of the work area (1032 px high)
    sim.run(1.0)
    assert scroll_actions(rec) == [("start", "down")]
    sim.pipe.set_reading_scroll(False)
    assert scroll_actions(rec) == [("start", "down"), ("stop",)]
    sim.run(0.5)
    assert len(scroll_actions(rec)) == 2


def test_reading_scroll_action(vctx, rec):
    svc = VisionService(vctx, start_pump=False)
    run = vctx.actions.execute_now
    with pytest.raises(ActionError, match="calibrated first"):
        run(Action("pointer.reading_scroll", {"on": True}, source="voice"))
    model, data = shared_model()
    svc.pipeline.activate_gaze(model, data)
    assert run(Action("pointer.reading_scroll", source="voice")) is True  # toggles when "on" is left out
    assert svc.pipeline.reading_scroll.enabled
    assert [n.text for n in rec.of(Notice) if n.key == "vision-reading-scroll"] == ["Reading scroll on."]
    assert run(Action("pointer.reading_scroll", {"on": "off"}, source="voice")) is False
    assert not svc.pipeline.reading_scroll.enabled
    assert rec.of(Notice)[-1].text == "Reading scroll off."
    with pytest.raises(ActionError, match="on or off"):
        run(Action("pointer.reading_scroll", {"on": "sideways"}, source="voice"))
    with pytest.raises(ActionError, match="nothing to undo"):  # not something "undo that" reverses
        run(Action("action.undo", source="voice"))
    # Rebuilding the pipeline keeps the mode.
    run(Action("pointer.reading_scroll", {"on": True}, source="voice"))
    svc._replace_pipeline()
    assert svc.pipeline.reading_scroll.enabled
