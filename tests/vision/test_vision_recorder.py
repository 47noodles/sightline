"""Recordings: what is saved comes back exactly, and replaying it gives what the live run gave."""

from __future__ import annotations

import json

import numpy as np
import pytest

from sightline.core.context import AppContext
from sightline.core.events import EyeGesture
from sightline.vision import recorder as recorder_mod
from sightline.vision.camera import VideoFileSource
from sightline.vision.pipeline import VisionPipeline
from sightline.vision.recorder import Recorder, Recording

# (seconds, closure of the user's left and right eye, face visible)
SCRIPT = [
    (1.0, (0, 0), True),
    (0.5, (1, 1), True),  # long blink: left click
    (0.6, (0, 0), True),
    (0.3, (0, 1), True),  # right wink: right click
    (0.6, (0, 0), True),
    (0.15, (1, 1), True),  # natural blink: nothing
    (0.6, (0, 0), True),
    (0.4, (0, 0), False),  # face lost
    (0.6, (0, 0), True),
]


def scripted_frames(user, t0: float = 10.0):
    t = t0
    for seconds, closure, visible in SCRIPT:
        for _ in range(int(round(seconds * 30))):
            yield t, (user.look(t, 700, 400, closure=closure) if visible else None)
            t += 1 / 30


def run_pipeline(frames) -> tuple[list, list]:
    ctx = AppContext.for_tests()
    actions: list = []
    ctx.actions.dispatch = actions.append  # record, never run
    gestures: list = []
    ctx.bus.subscribe(EyeGesture, gestures.append)
    pipe = VisionPipeline(ctx)
    for t, face in frames:
        pipe.process(face, t)
    return actions, gestures


def test_round_trip_and_replay_match_the_live_run(tmp_path, user):
    live = list(scripted_frames(user))
    rec = Recorder(tmp_path / "rec", {"camera": "Test camera"})
    rec.label(live[0][0], "Look at the dot")
    for t, face in live:
        rec.add(t, face, size=(1280, 720))
    rec.event(live[40][0], "note", text="blink")
    rec.close()

    loaded = Recording.load(tmp_path / "rec")
    assert len(loaded) == len(live) and loaded.meta["frames"] == len(live)
    assert loaded.meta["camera"] == "Test camera"
    assert loaded.duration == pytest.approx(live[-1][0] - live[0][0])
    for (t1, f1), (t2, f2) in zip(live, loaded.frames()):
        assert t1 == t2
        assert (f1 is None) == (f2 is None)
        if f1 is not None and f2 is not None:
            np.testing.assert_array_equal(f1.landmarks, f2.landmarks)
            np.testing.assert_array_equal(f1.blendshapes, f2.blendshapes)
            np.testing.assert_array_equal(f1.matrix, f2.matrix)
            assert (f2.width, f2.height) == (1280, 720)
    assert loaded.label_at(live[5][0]) == "Look at the dot"
    assert loaded.label_at(live[0][0] - 1.0) is None
    assert loaded.meta["events"] == [{"t": live[40][0], "kind": "note", "text": "blink"}]

    live_actions, live_gestures = run_pipeline(live)
    replay_actions, replay_gestures = run_pipeline(loaded.frames())
    assert [a.args["button"] for a in live_actions] == ["left", "right"]
    assert [(a.name, dict(a.args), a.source) for a in replay_actions] == [
        (a.name, dict(a.args), a.source) for a in live_actions
    ]
    assert [g.kind for g in replay_gestures] == [g.kind for g in live_gestures] == ["long_blink", "wink_right"]


def test_long_recordings_are_split_into_chunks(tmp_path, user, monkeypatch):
    monkeypatch.setattr(recorder_mod, "CHUNK_FRAMES", 50)
    rec = Recorder(tmp_path / "rec")
    t = 0.0
    for i in range(120):
        rec.add(t, user.look(t, 960, 540) if i % 7 else None, size=(1280, 720))
        t += 1 / 30
        if i == 60:
            # A crash now would lose only the frames since the last chunk: what is on disk loads.
            assert len(Recording.load(tmp_path / "rec")) == 50
    rec.close()
    assert len(sorted((tmp_path / "rec").glob("frames-*.npz"))) == 3
    loaded = Recording.load(tmp_path / "rec")
    assert len(loaded) == 120
    assert np.all(np.diff(loaded.t) > 0)
    assert int(loaded.present.sum()) == 120 - len(range(0, 120, 7))


def test_optional_video_is_readable_as_a_camera(tmp_path, user):
    rec = Recorder(tmp_path / "rec", video_fps=30.0)
    rng = np.random.default_rng(0)
    for i in range(20):
        img = rng.integers(0, 255, (120, 160, 3), dtype=np.uint8)
        rec.add(i / 30, None, image=img)
    rec.close()
    src = VideoFileSource(tmp_path / "rec" / "video.avi")
    src.open()
    n = 0
    while (got := src.read()) is not None:
        assert got[0].shape == (120, 160, 3)
        n += 1
    src.close()
    assert n == 20
    loaded = Recording.load(tmp_path / "rec")
    assert tuple(loaded.size[0]) == (160, 120)


def test_recordings_from_a_newer_version_are_refused(tmp_path, user):
    rec = Recorder(tmp_path / "rec")
    rec.add(0.0, user.look(0.0, 960, 540))
    rec.close()
    meta_path = tmp_path / "rec" / "meta.json"
    meta = json.loads(meta_path.read_text("utf-8"))
    meta["format"] = recorder_mod.FORMAT_VERSION + 1
    meta_path.write_text(json.dumps(meta), "utf-8")
    with pytest.raises(ValueError, match="newer version"):
        Recording.load(tmp_path / "rec")
    with pytest.raises(FileNotFoundError):
        Recording.load(tmp_path / "missing")
