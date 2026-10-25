"""Face gestures: raised eyebrows, a smile, an open mouth and puffed cheeks, from MediaPipe's blendshapes."""

from __future__ import annotations

import numpy as np
import pytest
from vision_sim import BROWS, CHEEKS, EXPRESSIONS, SMILE, Sim, talking_mouth

from sightline.core.events import CalibrationActive, FaceGesture
from sightline.vision.face_gestures import (
    FACE_KINDS,
    FaceGestureConfig,
    FaceGestureDetector,
    FaceGestureProfile,
    Trigger,
    signals,
)
from sightline.vision.landmarks import BLENDSHAPE_NAMES, BS

FPS = 30.0


def bs_with(**values: float) -> np.ndarray:
    bs = np.full(len(BLENDSHAPE_NAMES), 0.05, dtype=np.float32)
    for name, v in values.items():
        bs[BS[name]] = v
    return bs


REST = bs_with()
SMILING = bs_with(mouthSmileLeft=0.8, mouthSmileRight=0.8)
BROWS_UP = bs_with(browInnerUp=0.75, browOuterUpLeft=0.7, browOuterUpRight=0.7)


def trigger(on: float = 0.4, off: float = 0.2) -> Trigger:
    return Trigger(on, off, 0.05, 1.0, 1.0, 0.0, True)


def profile(*kinds: str, look_k: float = 0.5) -> FaceGestureProfile:
    return FaceGestureProfile({k: trigger() for k in kinds}, look_k)


def feed(det, t0: float, seconds: float, bs: np.ndarray, kinds=FACE_KINDS, **kw) -> tuple[float, list]:
    hits = []
    t = t0
    for _ in range(int(round(seconds * FPS))):
        hits += det.update(t, bs, kinds, **kw)
        t += 1 / FPS
    return t, hits


# --------------------------------------------------------------------------- the detector


def test_a_held_smile_fires_once_on_its_rising_edge():
    det = FaceGestureDetector(profile("smile"))
    t, _ = feed(det, 0.0, 0.3, REST)
    last_rest = t - 1 / FPS
    t, hits = feed(det, t, 0.8, SMILING)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.kind == "smile"
    assert hit.onset == pytest.approx(last_rest)  # the pointer position from when the smile began is used
    assert 0.15 <= hit.t - (last_rest + 1 / FPS) < 0.15 + 1.5 / FPS
    _, more = feed(det, t, 0.3, REST)
    assert more == []


def test_a_passing_smile_does_not_count():
    det = FaceGestureDetector(profile("smile"))
    t, _ = feed(det, 0.0, 0.3, REST)
    t, hits = feed(det, t, 0.1, SMILING)  # shorter than the 150 ms hold
    t, more = feed(det, t, 0.5, REST)
    assert hits + more == []


def test_hold_time_follows_the_setting():
    det = FaceGestureDetector(profile("smile"), FaceGestureConfig(hold_s=0.4))
    t, _ = feed(det, 0.0, 0.3, REST)
    _, hits = feed(det, t, 0.3, SMILING)
    assert hits == []


def test_refractory_period_then_a_fresh_rising_edge():
    det = FaceGestureDetector(profile("smile"))
    t, _ = feed(det, 0.0, 0.3, REST)
    t, first = feed(det, t, 0.4, SMILING)
    t, _ = feed(det, t, 0.2, REST)
    # Smiling again inside the 0.8 s refractory period, and holding it past the end: never counts.
    t, second = feed(det, t, 1.2, SMILING)
    t, _ = feed(det, t, 0.3, REST)
    t, third = feed(det, t, 0.4, SMILING)  # after the refractory period: a fresh edge counts
    assert len(first) == 1 and second == [] and len(third) == 1


def test_a_gesture_already_showing_when_watching_starts_waits_for_a_fresh_edge():
    det = FaceGestureDetector(profile("smile"))
    t, hits = feed(det, 0.0, 1.0, SMILING)
    assert hits == []
    t, _ = feed(det, t, 0.2, REST)
    _, hits = feed(det, t, 0.4, SMILING)
    assert len(hits) == 1


def test_blocked_gestures_never_fire_even_when_unblocked_mid_gesture():
    det = FaceGestureDetector(profile("smile"))
    t, _ = feed(det, 0.0, 0.3, REST)
    t, hits = feed(det, t, 0.3, SMILING, blocked=True)
    t, more = feed(det, t, 0.5, SMILING)
    assert hits + more == []


def test_mouth_gestures_wait_while_the_user_talks_but_brows_do_not():
    det = FaceGestureDetector(profile("smile", "brow_raise"))
    asked = []

    def talking(t0: float) -> bool:
        asked.append(t0)
        return True

    t, _ = feed(det, 0.0, 0.3, REST, talking=talking)
    t, smile = feed(det, t, 0.4, SMILING, talking=talking)
    assert smile == [] and asked  # asked about the moment the smile began
    t, _ = feed(det, t, 0.3, REST, talking=talking)
    _, brows = feed(det, t, 0.4, BROWS_UP, talking=talking)
    assert [h.kind for h in brows] == ["brow_raise"]


def test_looking_up_does_not_count_as_raised_eyebrows():
    # Looking up lifts the brows a little on their own; the brow signal has the upward look taken off.
    looking_up = bs_with(browInnerUp=0.45, browOuterUpLeft=0.45, browOuterUpRight=0.45,
                         eyeLookUpLeft=0.6, eyeLookUpRight=0.6)  # fmt: skip
    assert signals(looking_up, 0.5)["brow_raise"] == pytest.approx(0.15, abs=1e-6)
    det = FaceGestureDetector(profile("brow_raise", look_k=0.5))
    t, _ = feed(det, 0.0, 0.3, REST)
    t, hits = feed(det, t, 0.6, looking_up)
    assert hits == []
    t, _ = feed(det, t, 1.0, REST)
    _, hits = feed(det, t, 0.4, BROWS_UP)
    assert len(hits) == 1
    # Without the correction the same look would have fired.
    plain = FaceGestureDetector(profile("brow_raise", look_k=0.0))
    t, _ = feed(plain, 0.0, 0.3, REST)
    _, hits = feed(plain, t, 0.6, looking_up)
    assert len(hits) == 1


def test_nothing_fires_without_a_profile_blendshapes_or_a_chosen_gesture():
    assert feed(FaceGestureDetector(None), 0.0, 1.0, SMILING)[1] == []
    det = FaceGestureDetector(profile("smile"))
    assert feed(det, 0.0, 1.0, None)[1] == []
    t, _ = feed(det, 0.0, 0.3, REST)
    assert feed(det, t, 0.5, SMILING, kinds=["brow_raise"])[1] == []  # smile not chosen


def test_profile_round_trip(tmp_path):
    prof = FaceGestureProfile({"smile": trigger(), "cheek_puff": Trigger(0.3, 0.1, 0.02, 0.97, 0.9, 0.5, True)}, 0.8)
    path = tmp_path / "face_gestures.json"
    prof.save(path)
    loaded = FaceGestureProfile.load(path)
    assert loaded is not None and loaded.triggers == prof.triggers and loaded.look_k == 0.8
    assert loaded.usable("smile") and not loaded.usable("brow_raise")
    path.write_text('{"version": 99}', "utf-8")
    assert FaceGestureProfile.load(path) is None
    assert FaceGestureProfile.load(tmp_path / "missing.json") is None


# --------------------------------------------------------------------------- in the pipeline


def test_face_gestures_are_off_until_trained_and_chosen(vctx, rec, user):
    sim = Sim(vctx, user)
    sim.run(0.5)
    sim.expression = SMILE
    sim.run(0.6)
    assert rec.of(FaceGesture) == [] and rec.actions == []
    # Trained, but the default action for every face gesture is "none".
    sim.pipe.activate_face(profile(*FACE_KINDS))
    sim.expression = {}
    sim.run(0.5)
    sim.expression = SMILE
    sim.run(0.6)
    assert vctx.settings.vision.gesture_actions["smile"] == "none"
    assert rec.of(FaceGesture) == [] and rec.actions == []


def test_a_chosen_smile_clicks_where_the_pointer_was_when_it_began(vctx, rec, user):
    sim = Sim(vctx, user)
    sim.pipe.activate_face(profile("smile"))
    vctx.settings.vision.gesture_actions["smile"] = "click"
    sim.run(0.5)
    start = sim.pipe.engine.position
    sim.expression = SMILE
    sim.run(1 / FPS)
    sim.pipe.engine.nudge(150.0, 0.0)  # the pointer moves while the smile is held
    sim.run(0.5)
    events = rec.of(FaceGesture)
    assert len(events) == 1 and events[0].kind == "smile"
    assert (events[0].x, events[0].y) == pytest.approx(start)
    assert len(rec.actions) == 1
    action = rec.actions[0]
    assert action.name == "mouse.click"
    assert action.args["button"] == "left" and action.source == "eye"
    assert (action.args["x"], action.args["y"]) == pytest.approx(start)


def test_mouth_gestures_wait_until_the_user_stops_talking(vctx, rec, user):
    sim = Sim(vctx, user)
    sim.pipe.activate_face(profile("smile", "brow_raise"))
    actions = vctx.settings.vision.gesture_actions
    actions["smile"] = "click"
    actions["brow_raise"] = "right_click"
    sim.talk = talking_mouth
    sim.run(2.0)
    sim.expression = SMILE  # smiling mid-sentence
    sim.run(0.6)
    assert rec.of(FaceGesture) == []
    sim.expression = BROWS  # eyebrows are not gated: speech does not move them
    sim.run(0.6)
    assert [e.kind for e in rec.of(FaceGesture)] == ["brow_raise"]
    sim.expression = {}
    sim.talk = None
    sim.run(1.5)  # quiet for more than the one-second window
    sim.expression = SMILE
    sim.run(0.6)
    assert [e.kind for e in rec.of(FaceGesture)] == ["brow_raise", "smile"]
    assert [a.args["button"] for a in rec.actions] == ["right", "left"]


def test_face_gestures_are_ignored_while_paused_or_calibrating(vctx, rec, user):
    sim = Sim(vctx, user)
    sim.pipe.activate_face(profile("smile", "cheek_puff"))
    actions = vctx.settings.vision.gesture_actions
    actions["smile"] = "click"
    actions["cheek_puff"] = "pointer.toggle"
    vctx.state.update(pointer_paused=True)
    sim.run(0.5)
    sim.expression = SMILE
    sim.run(0.6)
    assert rec.of(FaceGesture) == [] and rec.actions == []
    sim.expression = {}
    sim.run(0.5)
    sim.expression = CHEEKS  # bound to pause and resume: works while paused
    sim.run(0.6)
    assert [a.name for a in rec.actions] == ["pointer.toggle"]
    vctx.state.update(pointer_paused=False)
    rec.clear()
    sim.expression = {}
    sim.cal.begin("eyes")
    sim.run(1.0)
    sim.expression = SMILE
    sim.run(0.6)
    sim.cal.cancel()
    assert rec.of(FaceGesture) == [] and rec.actions == []
    assert [e.active for e in rec.of(CalibrationActive)] == [True, False]


# --------------------------------------------------------------------------- the trainer


def reading_path(t0: float, *, left=300, right=1500, top=90, line_h=40, step_px=80, fix_s=0.25):
    per_line = int((right - left) / step_px)

    def at(t: float) -> tuple[float, float]:
        u = max(0.0, t - t0)
        line = int(u // (per_line * fix_s))
        k = int((u % (per_line * fix_s)) // fix_s)
        return left + k * step_px, top + (line % 4) * line_h

    return at


def gesture_session(sim: Sim, *, shown=None, talk_extra=None, seconds=12.0):
    """The trainer as the wizard runs it: rest, each gesture, then talking and reading."""
    cal = sim.cal
    cal.begin("gestures")
    sim.run(0.3)
    cal.set_eye_label("neutral")
    sim.run(3.0)
    for kind, expr in (shown or EXPRESSIONS).items():
        cal.set_eye_label(kind)  # type: ignore[arg-type]
        sim.run(0.5)  # the user reacts
        sim.expression = dict(expr)
        sim.run(2.0)
        sim.expression = {}
        cal.set_eye_label(None)
        sim.run(0.8)
    cal.set_eye_label("talking")

    def talk(t: float):
        values, gap = talking_mouth(t)
        if talk_extra is not None:
            values = {**values, **talk_extra(t)}
        return values, gap

    sim.talk = talk
    sim.run(seconds)
    sim.talk = None
    cal.set_eye_label("reading")
    sim.path = reading_path(sim.now)
    sim.run(seconds)
    sim.path = None
    cal.set_eye_label(None)
    sim.run(0.3)
    return cal.finish()


def test_trainer_scores_and_offers_the_gestures_that_work(vctx, user):
    sim = Sim(vctx, user)
    sim.brow_look = 1.2  # this user's eyebrows rise when they look up, as they read the top lines
    sim.run(0.5)
    report = gesture_session(sim)
    assert report.ok and report.kind == "gestures", report.message
    d = report.details
    assert set(d["scores"]) == set(FACE_KINDS)
    assert all(0.0 < v <= 1.0 for v in d["scores"].values())
    assert len(d["offer"]) == 3
    assert d["offer"] == sorted(d["scores"], key=d["scores"].get, reverse=True)[:3]
    assert list(d["suggested_actions"].values()) == ["click", "right_click", "double_click"]
    assert report.message.startswith("These work well for you:")
    assert d["mouth_gate_ok"] is True
    assert d["look_k"] == pytest.approx(1.2, abs=0.3)  # the eyebrows' rise with an upward look was learned
    assert d["triggers"]["brow_raise"]["false_per_min"] == 0.0  # so reading the top lines never fired it
    assert (sim.pipe.profile.folder / "face_gestures.json").exists()
    assert all(sim.pipe.face_profile.usable(k) for k in FACE_KINDS)
    # The wizard writes the user's choice; the pipeline reads it.
    vctx.settings.vision.gesture_actions[d["offer"][0]] = "click"
    sim.run(0.5)
    events: list[FaceGesture] = []
    vctx.bus.subscribe(FaceGesture, events.append)
    sim.expression = EXPRESSIONS[d["offer"][0]]
    sim.run(0.6)
    assert [e.kind for e in events] == [d["offer"][0]]


def test_trainer_turns_down_a_gesture_that_goes_off_while_talking(vctx, user):
    sim = Sim(vctx, user)
    sim.run(0.5)

    def lively(t: float) -> dict[str, float]:
        # This user raises their eyebrows for emphasis every couple of seconds while they talk.
        return dict(BROWS) if (t % 2.5) < 0.45 else {}

    report = gesture_session(sim, talk_extra=lively)
    assert report.ok
    d = report.details
    assert "brow_raise" not in d["scores"] and "brow_raise" not in d["offer"]
    assert d["triggers"]["brow_raise"]["reason"] == "misfires"
    assert d["triggers"]["brow_raise"]["false_per_min"] > 1.0
    assert "Raised eyebrows went off about" in report.message and "so it is not offered" in report.message
    assert not sim.pipe.face_profile.usable("brow_raise")


def test_trainer_turns_down_a_gesture_too_close_to_the_resting_face(vctx, user):
    sim = Sim(vctx, user)
    sim.run(0.5)
    shown = dict(EXPRESSIONS)
    shown["cheek_puff"] = {"cheekPuff": 0.05}  # this user cannot puff their cheeks enough to see
    report = gesture_session(sim, shown=shown, seconds=8.0)
    d = report.details
    assert "cheek_puff" not in d["scores"]
    assert d["triggers"]["cheek_puff"]["reason"] == "not apart"
    assert "Puffed cheeks could not be told apart from your resting face." in report.message


def test_trainer_with_no_usable_gesture_keeps_them_off(vctx, user):
    sim = Sim(vctx, user)
    sim.run(0.5)
    flat = {k: {} for k in EXPRESSIONS}  # asked for each gesture, the face did not change
    report = gesture_session(sim, shown=flat, seconds=8.0)
    assert report.ok
    assert report.details["scores"] == {} and report.details["offer"] == []
    assert report.message.startswith("None of the face gestures could be told apart reliably")
    assert not any(sim.pipe.face_profile.usable(k) for k in FACE_KINDS)


def test_trainer_without_a_resting_face_asks_to_try_again(vctx, user):
    sim = Sim(vctx, user)
    sim.cal.begin("gestures")
    sim.cal.set_eye_label("smile")
    sim.expression = SMILE
    sim.run(2.0)
    report = sim.cal.finish()
    assert not report.ok
    assert report.message == "Not enough data. Keep your face in view of the camera and try again."
    assert report.details["scores"] == {}
    assert sim.pipe.face_profile is None


def test_gestures_sample_count_follows_the_label(vctx, user):
    sim = Sim(vctx, user)
    sim.cal.begin("gestures")
    assert sim.cal.sample_count() == 0
    sim.cal.set_eye_label("neutral")
    sim.run(1.6)
    assert sim.cal.sample_count() == pytest.approx(30, abs=2)  # a second of frames after the reaction time
    sim.cal.cancel()
