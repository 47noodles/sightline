"""Pointer engine and cursor pump.  Every test drives FakeInput; nothing here touches the real cursor."""

from __future__ import annotations

import numpy as np
import pytest

from sightline.core.fakes import FakeDisplay, FakeInput
from sightline.core.types import PointerMode
from sightline.vision.filters import GazeState
from sightline.vision.head import HeadSample, HeadTracker, sigmoid_gain
from sightline.vision.pointer import CursorPump, PointerEngine


def gaze(x: float, y: float, fix_s: float = 0.3, fixating: bool = True) -> GazeState:
    return GazeState(x, y, fixating, x, y, fix_s, 10.0, 30.0, False)


def head(dx: float = 0.0, dy: float = 0.0) -> HeadSample:
    return HeadSample(dx, dy, 0.1, 0.0, 0.0, 0.0, 0.0)


def engine(start=(960.0, 540.0)) -> PointerEngine:
    return PointerEngine(FakeDisplay(), start)


def step(e: PointerEngine, t: float, **kw):
    args = dict(present=True, mode=PointerMode.HYBRID, gaze=None, head=None, frozen=False, paused=False)
    args.update(kw)
    return e.update(t, **args)


# --------------------------------------------------------------------------- engine


def test_hybrid_jumps_after_a_fixation_far_away():
    e = engine()
    assert step(e, 0.0, gaze=gaze(1500, 300, fix_s=0.05)) == "fine"  # not fixated long enough yet
    assert e.position == (960.0, 540.0)
    assert step(e, 0.1, gaze=gaze(1500, 300, fix_s=0.15)) == "warp"
    assert e.position == (1500.0, 300.0)
    assert e.pump_target(0.12).fast  # the pump animates the jump quickly
    assert not e.pump_target(0.3).fast


def test_hybrid_does_not_jump_inside_the_radius():
    e = engine()
    assert step(e, 0.0, gaze=gaze(1060, 560, fix_s=0.5)) == "fine"
    assert e.position == (960.0, 540.0)


def test_hybrid_fine_tunes_with_the_head():
    e = engine()
    step(e, 0.0, gaze=gaze(1000, 540), head=head(10.0, -4.0))
    x, y = e.position
    scale = e.cfg.fine_scale
    assert x == pytest.approx(960 + 10 * scale) and y == pytest.approx(540 - 4 * scale)


def test_hybrid_does_not_pull_back_to_the_same_fixation():
    e = engine()
    step(e, 0.0, gaze=gaze(1500, 300))  # jump
    for i in range(1, 60):
        # The user steers the pointer with the head while still looking at the same spot.
        step(e, i * 0.033, gaze=gaze(1500, 300), head=head(-20.0, 0.0))
    x, _ = e.position
    assert x < 1500 - 1.2 * e.cfg.warp_distance  # moved well away without jumping back
    # Looking somewhere new jumps again.
    assert step(e, 3.0, gaze=gaze(400, 900)) == "warp"


def test_head_mode_ignores_gaze_and_head_moves_the_pointer():
    e = engine()
    assert step(e, 0.0, mode=PointerMode.HEAD, gaze=gaze(1500, 300), head=head(25, 5)) == "head"
    assert e.position == (985.0, 545.0)


def test_gaze_mode_follows_gaze():
    e = engine()
    assert step(e, 0.0, mode=PointerMode.GAZE, gaze=gaze(1234, 567, fixating=False)) == "gaze"
    assert e.position == (1234.0, 567.0)
    assert step(e, 0.1, mode=PointerMode.GAZE, gaze=None) == "gaze"  # no estimate this frame: hold
    assert e.position == (1234.0, 567.0)


def test_hybrid_without_gaze_runs_on_the_head():
    e = engine()
    assert step(e, 0.0, gaze=None, head=head(5, 0)) == "head"
    assert e.position == (965.0, 540.0)


def test_paused_frozen_and_lost_hold_the_pointer():
    e = engine()
    assert step(e, 0.0, paused=True, head=head(50, 50)) == "paused"
    assert not e.pump_target().active
    assert step(e, 0.1, frozen=True, head=head(50, 50), gaze=gaze(100, 100)) == "frozen"
    assert e.pump_target().active  # frozen still holds the cursor in place
    assert step(e, 0.2, present=False) == "lost"
    assert not e.pump_target().active  # pause_when_face_lost
    assert e.position == (960.0, 540.0)


def test_face_returning_waits_for_tracking_to_settle():
    e = engine()
    step(e, 0.0, present=False)
    assert step(e, 0.1, head=head(30, 0)) == "frozen"
    assert step(e, 0.5, head=head(30, 0)) == "head"


def test_lost_without_pausing_keeps_the_pump_active():
    e = engine()
    e.cfg.pause_when_face_lost = False
    assert step(e, 0.0, present=False) == "lost"
    assert e.pump_target().active
    assert step(e, 0.05, head=head(10, 0)) == "head"  # no settling delay


def test_pointer_is_clamped_to_the_desktop():
    e = engine()
    step(e, 0.0, mode=PointerMode.HEAD, head=head(5000, 5000))
    assert e.position == (1919, 1079)
    step(e, 0.1, mode=PointerMode.GAZE, gaze=gaze(-300, -50))
    assert e.position == (0, 0)


def test_position_history_gives_the_pointer_before_a_blink():
    e = engine()
    for i in range(20):
        step(e, i * 0.033, mode=PointerMode.HEAD, head=head(10, 0))
    x_at = e.position_at(10 * 0.033)
    assert x_at == pytest.approx((960 + 110.0, 540.0))
    assert e.position_at(-5.0) == pytest.approx((970.0, 540.0))  # before the history: the oldest entry


def test_nudge_adopt_and_move_to():
    e = engine()
    e.nudge(10, -5)
    assert e.position == (970.0, 535.0)
    e.adopt(100, 200)
    assert e.position == (100.0, 200.0)
    e.move_to(5000, 5000)
    assert e.position == (1919, 1079)


# --------------------------------------------------------------------------- steered


def test_steered_once_the_head_moves_the_pointer_after_a_jump():
    e = engine()
    assert not e.steered
    step(e, 0.0, gaze=gaze(1500, 300))  # jump
    assert e.position == (1500.0, 300.0) and not e.steered
    step(e, 0.1, gaze=gaze(1500, 300), head=head(20.0, 0.0))  # the head turn that came with the jump: ignored
    assert e.position == (1500.0, 300.0) and not e.steered
    step(e, 0.3, gaze=gaze(1500, 300), head=head(4.0, 0.0))  # 1.4 px: too little to see
    assert not e.steered
    step(e, 0.333, gaze=gaze(1500, 300), head=head(4.0, 0.0))  # 2.8 px in all: the user is lining it up
    assert e.steered
    assert step(e, 0.4, gaze=gaze(1500, 300), frozen=True) == "frozen"
    assert e.steered, "the eyes closing for a click keep it"
    step(e, 1.0, gaze=gaze(400, 900))  # gaze places it somewhere new
    assert not e.steered


def test_a_nudge_or_the_mouse_counts_as_steering_but_coming_back_in_place_does_not():
    e = engine()
    step(e, 0.0, gaze=gaze(1500, 300))
    e.adopt(1500.4, 299.6)  # resuming after a pause: only the cursor's own rounding
    assert not e.steered
    e.nudge(0, -10)  # "move up"
    assert e.steered
    step(e, 1.0, gaze=gaze(400, 900))
    assert not e.steered
    e.adopt(700, 200)  # a carer's mouse, or a click that snapped onto a button
    assert e.steered


def test_head_at_a_screen_edge_steers_nothing():
    e = engine(start=(1919.0, 540.0))
    step(e, 0.0, mode=PointerMode.HEAD, head=head(30.0, 0.0))  # pushing past the right edge
    assert e.position == (1919.0, 540.0) and not e.steered
    step(e, 0.1, mode=PointerMode.HEAD, head=head(-30.0, 0.0))
    assert e.steered


def test_gaze_only_pointing_is_never_steered_for_long():
    e = engine()
    step(e, 0.0, mode=PointerMode.GAZE, gaze=gaze(1234, 567, fixating=False))
    e.nudge(5, 0)
    assert e.steered
    step(e, 0.1, mode=PointerMode.GAZE, gaze=gaze(1240, 570, fixating=False))
    assert not e.steered, "gaze put it somewhere again"


def test_pointer_samples_say_when_the_head_has_steered(vctx, rec, user):
    from vision_sim import Sim

    from sightline.core.events import PointerSample

    sim = Sim(vctx, user)
    sim.use_model()
    sim.run(0.5)
    rec.clear()
    sim.look_at = (1500, 300)  # far from the pointer: gaze jumps it there
    sim.run(0.6)
    samples = rec.of(PointerSample)
    jumped = [i for i, s in enumerate(samples) if s.phase == "warp"]
    assert jumped, "gaze jumped the pointer"
    assert not any(s.steered for s in samples[jumped[0] :])
    rec.clear()
    t0 = sim.now
    sim.head_path = lambda t: (min(4.0, 8.0 * max(0.0, t - t0)), 0.0)  # a slow turn of the head, eyes on the spot
    sim.run(1.0)
    samples = rec.of(PointerSample)
    assert samples[-1].steered and samples[-1].phase == "fine"
    assert not any(s.phase == "warp" for s in samples), "the eyes stayed put, so gaze did not jump back"


# --------------------------------------------------------------------------- head sign conventions


@pytest.mark.parametrize("mirrored", [False, True])
def test_turning_the_head_right_moves_the_pointer_right(mirrored):
    from vision_synth import SyntheticFace

    from sightline.vision import features as feat

    face = SyntheticFace()
    tracker = HeadTracker(1920, 1080)
    tracker.mirrored = mirrored
    e = engine()
    t = 0.0
    for i in range(10):
        step(e, t, mode=PointerMode.HEAD, head=tracker.update(t, feat.measure(face.frame(t, mirrored=mirrored))))
        t += 1 / 30
    start = e.position
    for i in range(20):
        m = feat.measure(face.frame(t, yaw=0.5 * i, mirrored=mirrored))
        step(e, t, mode=PointerMode.HEAD, head=tracker.update(t, m))
        t += 1 / 30
    x, y = e.position
    assert x > start[0] + 100
    assert abs(y - start[1]) < 0.25 * (x - start[0])
    assert tracker.last is not None and tracker.last.yaw > 8.0


def test_tilting_the_head_up_moves_the_pointer_up():
    from vision_synth import SyntheticFace

    from sightline.vision import features as feat

    face = SyntheticFace()
    tracker = HeadTracker(1920, 1080)
    e = engine()
    t = 0.0
    for i in range(30):
        m = feat.measure(face.frame(t, pitch=0.4 * max(0, i - 10)))
        step(e, t, mode=PointerMode.HEAD, head=tracker.update(t, m))
        t += 1 / 30
    x, y = e.position
    assert y < 540 - 50
    assert abs(x - 960) < 0.25 * (540 - y)


def test_head_tremor_inside_the_deadband_does_not_move_the_pointer():
    from vision_synth import SyntheticFace

    from sightline.vision import features as feat

    face = SyntheticFace()
    tracker = HeadTracker(1920, 1080)
    rng = np.random.default_rng(0)
    e = engine()
    for i in range(90):
        # Landmark noise of a quarter pixel plus a tiny sway.
        m = feat.measure(face.frame(i / 30, yaw=0.05 * np.sin(i / 5), noise_px=0.25, rng=rng))
        step(e, i / 30, mode=PointerMode.HEAD, head=tracker.update(i / 30, m))
    x, y = e.position
    assert abs(x - 960) < 6 and abs(y - 540) < 6


def test_gain_curve_is_precise_when_slow_and_fast_when_fast():
    assert sigmoid_gain(0.0) < 0.3
    assert sigmoid_gain(1.0) > 1.5
    assert sigmoid_gain(0.05) < sigmoid_gain(0.12) < sigmoid_gain(0.2)


# --------------------------------------------------------------------------- cursor pump


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def pump_setup(start=(960.0, 540.0)):
    inp = FakeInput(pos=start)
    e = engine(start)
    clock = Clock()
    p = CursorPump(inp, e, clock=clock)
    return inp, e, p, clock


def run(p: CursorPump, clock: Clock, seconds: float) -> None:
    for _ in range(int(round(seconds * 120))):
        clock.t += 1 / 120
        p.tick(clock.t)


def test_pump_eases_to_the_target_without_overshoot():
    inp, e, p, clock = pump_setup()
    run(p, clock, 0.05)
    e.move_to(1200, 540)
    xs = []
    for _ in range(60):
        clock.t += 1 / 120
        p.tick(clock.t)
        xs.append(inp.pos[0])
    assert xs[-1] == pytest.approx(1200, abs=1)
    assert max(xs) <= 1200.5  # critically damped: never past the target
    assert xs[3] < 1200 - 10  # and not a jump either


def test_pump_jump_is_fast():
    inp, e, p, clock = pump_setup()
    run(p, clock, 0.05)
    step(e, clock.t, gaze=gaze(1800, 900))  # a warp: fast animation
    for _ in range(12):  # 100 ms
        clock.t += 1 / 120
        p.tick(clock.t)
    assert np.hypot(inp.pos[0] - 1800, inp.pos[1] - 900) < 25


def test_pump_yields_to_a_physical_mouse_and_resumes():
    inp, e, p, clock = pump_setup()
    run(p, clock, 0.1)
    moves_before = inp.names().count("move_cursor")
    # Someone moves the real mouse: the position changes but nothing was injected.
    inp.pos = (300.0, 300.0)
    clock.t += 1 / 120
    p.tick(clock.t)
    assert p.yielding
    e.move_to(1500, 800)  # the eyes want the pointer elsewhere
    run(p, clock, 1.0)
    assert inp.names().count("move_cursor") == moves_before  # never fought the person
    inp.pos = (320.0, 310.0)  # still moving at 1.0 s: the wait restarts
    run(p, clock, 1.2)
    assert p.yielding
    run(p, clock, 0.5)
    assert not p.yielding
    assert e.position == (320.0, 310.0)  # carries on from where the person left it


def test_pump_follows_other_sightline_moves_without_yielding():
    inp, e, p, clock = pump_setup()
    run(p, clock, 0.1)
    clock.t += 0.001
    inp.move_cursor(700, 200)  # e.g. a click that snapped onto a button
    p.tick(clock.t)
    assert not p.yielding
    assert e.position == (700.0, 200.0)


def test_pump_holds_still_after_another_action_moves_the_cursor_then_carries_on_from_there():
    inp, e, p, clock = pump_setup()
    run(p, clock, 0.1)
    e.move_to(1400, 540)  # the pointer is on its way somewhere else ...
    run(p, clock, 0.03)
    assert inp.pos[0] > 1000
    clock.t += 0.001
    inp.move_cursor(700, 200)  # ... when a spoken click puts the cursor at its aim point
    moves = inp.names().count("move_cursor")
    run(p, clock, 0.13)  # the press follows 12 to 40 ms later: the pump must not move the cursor meanwhile
    assert inp.names().count("move_cursor") == moves
    assert inp.pos == (700, 200)
    assert p.holding and not p.yielding
    assert e.position == (700.0, 200.0)
    run(p, clock, 0.05)
    assert not p.holding
    e.nudge(40.0, 0.0)  # the head moves on
    xs = []
    for _ in range(40):
        clock.t += 1 / 120
        p.tick(clock.t)
        xs.append(inp.pos[0])
    assert min(xs) >= 700 and max(xs) <= 740.5  # eases on from the aim point; never back toward 1400
    assert xs[-1] == pytest.approx(740, abs=1)


def test_pump_hold_restarts_when_the_click_moves_the_cursor_again():
    inp, e, p, clock = pump_setup()
    run(p, clock, 0.1)
    inp.move_cursor(700, 200)
    run(p, clock, 0.1)
    inp.move_cursor(706, 204)  # the click snapped a few pixels onto a button
    run(p, clock, 0.1)
    assert p.holding and inp.pos == (706, 204) and e.position == (706.0, 204.0)
    run(p, clock, 0.1)
    assert not p.holding


def test_pump_keeps_easing_through_input_that_does_not_move_the_cursor():
    inp, e, p, clock = pump_setup()
    run(p, clock, 0.1)
    e.move_to(1300, 540)
    for _ in range(36):
        clock.t += 1 / 120
        inp.move_cursor(*inp.pos)  # like a wheel turn of a running scroll: input sent, the cursor stays put
        p.tick(clock.t)
    assert not p.holding and not p.yielding
    assert inp.pos[0] == pytest.approx(1300, abs=2)


class RacingInput(FakeInput):
    """Another thread's move lands just after the pump has read the cursor."""

    pending: tuple[float, float] | None = None

    def cursor_pos(self) -> tuple[int, int]:
        pos = super().cursor_pos()
        if self.pending is not None:
            x, y = self.pending
            self.pending = None
            self.move_cursor(x, y)
        return pos


def test_pump_tells_a_racing_sightline_move_from_a_person():
    inp = RacingInput(pos=(960.0, 540.0))
    e = engine()
    clock = Clock()
    p = CursorPump(inp, e, clock=clock)
    run(p, clock, 0.1)
    inp.pending = (500.0, 300.0)
    clock.t += 1 / 120
    p.tick(clock.t)
    assert not p.yielding and p.holding
    assert e.position == (500.0, 300.0)


def test_pump_yields_to_a_person_during_a_hold():
    inp, e, p, clock = pump_setup()
    run(p, clock, 0.1)
    inp.move_cursor(700, 200)
    run(p, clock, 0.05)
    assert p.holding
    inp.pos = (300.0, 320.0)  # a carer takes the mouse
    clock.t += 1 / 120
    p.tick(clock.t)
    assert p.yielding and not p.holding


def test_pump_leaves_the_cursor_alone_while_paused_then_adopts_its_position():
    inp, e, p, clock = pump_setup()
    run(p, clock, 0.1)
    step(e, clock.t, paused=True)
    n = len(inp.calls)
    inp.pos = (50.0, 60.0)
    run(p, clock, 0.5)
    assert len(inp.calls) == n and not p.yielding
    step(e, clock.t)  # resumed
    run(p, clock, 0.1)
    assert e.position == (50.0, 60.0)


def test_pump_yield_can_be_turned_off():
    inp, e, p, clock = pump_setup()
    p.yield_enabled = False
    run(p, clock, 0.1)
    inp.pos = (300.0, 300.0)
    run(p, clock, 0.5)
    assert not p.yielding
    assert inp.pos[0] == pytest.approx(960, abs=1)


def test_pump_thread_starts_and_stops():
    inp = FakeInput()
    e = engine()
    p = CursorPump(inp, e)
    p.start()
    e.move_to(1000, 600)
    import time

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and abs(inp.pos[0] - 1000) > 1:
        time.sleep(0.01)
    p.stop()
    assert inp.pos[0] == pytest.approx(1000, abs=1)
