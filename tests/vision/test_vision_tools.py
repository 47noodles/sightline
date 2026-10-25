"""The record and replay tools, end to end, without a camera and without touching the screen."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from sightline.vision.recorder import Recorder
from tools import vision_record, vision_replay

DATA = Path(__file__).parent / "data"

# (label, seconds, what the synthetic user does: a list of (seconds, left closure, right closure, face visible))
GUIDED = [
    ("open", [(3.0, 0, 0, True)]),
    ("natural", [(1.2, 0, 0, True), (0.15, 1, 1, True)] * 4 + [(0.6, 0, 0, True)]),
    ("closed", [(0.3, 0, 0, True), (2.7, 1, 1, True)]),
    ("open", [(2.0, 0, 0, True)]),
    ("long_blinks", [(1.0, 0, 0, True), (0.5, 1, 1, True)] * 4 + [(1.0, 0, 0, True)]),
    ("winks_left", [(1.0, 0, 0, True), (0.3, 1, 0, True)] * 3 + [(1.0, 0, 0, True)]),
    ("wink_left", [(0.3, 0, 0, True), (2.7, 1, 0, True)]),
    ("open", [(2.0, 0, 0, True)]),
    ("wink_right", [(0.3, 0, 0, True), (2.7, 0, 1, True)]),
    ("away", [(1.0, 0, 0, False)]),
    ("back", [(1.0, 0, 0, True)]),
]


def make_guided_recording(folder: Path, user) -> None:
    rec = Recorder(folder, {"camera": "Synthetic camera", "screen": [1920, 1080]})
    t = 50.0
    for label, parts in GUIDED:
        rec.label(t, label)
        for seconds, cl, cr, visible in parts:
            for _ in range(int(round(seconds * 30))):
                face = user.look(t, 960, 540, closure=(cl, cr)) if visible else None
                rec.add(t, face, size=(1280, 720))
                t += 1 / 30
    rec.close()


def test_replay_reports_steps_gestures_blinks_and_eye_fit(tmp_path, user, capsys):
    folder = tmp_path / "guided"
    make_guided_recording(folder, user)
    assert vision_replay.main([str(folder), "--timeline"]) == 0
    out = capsys.readouterr().out
    assert "Synthetic camera" in out
    lines = {line.split()[0]: line for line in out.splitlines() if line.startswith("  ") and line.split()}
    assert "long blink x4" in lines["long_blinks"] and "-> left click x4" in lines["long_blinks"]
    assert "wink left x3" in lines["winks_left"] and "-> middle click x3" in lines["winks_left"]
    assert "Natural blinks: 4" in out
    assert "Eye calibration from this recording: Blinks and winks are set up." in out
    assert "Suggestions: none" in out
    assert "would left click at" in out  # the timeline


def test_replay_with_changed_settings_suggests_fixes(tmp_path, user, capsys):
    folder = tmp_path / "guided"
    make_guided_recording(folder, user)
    vision_replay.main([str(folder), "--set", "blink_click_ms=600", "--set", "winks=false", "--fresh"])
    out = capsys.readouterr().out
    assert "blink_click_ms=600" in out and "winks=False" in out
    lines = {line.split()[0]: line for line in out.splitlines() if line.startswith("  ") and line.split()}
    assert "click" not in lines["long_blinks"].split("(asked")[0]  # 500 ms blinks are now too short
    assert "Only 0 of 4 long blinks were seen" in out
    assert "Winks are turned off in the settings, so the 3 left winks did nothing." in out


def test_replay_refuses_unknown_settings(tmp_path, user):
    folder = tmp_path / "guided"
    make_guided_recording(folder, user)
    with pytest.raises(SystemExit, match="No vision setting called 'nonsense'"):
        vision_replay.main([str(folder), "--set", "nonsense=1"])


def test_list_cameras_runs(capsys):
    assert vision_record.main(["--list-cameras"]) == 0
    assert capsys.readouterr().out.strip()


@pytest.mark.models
def test_record_from_a_video_file_then_replay(face_model, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(vision_record, "model_path", lambda download: face_model)
    clip = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"MJPG"), 30.0, (640, 480))
    for i, name in enumerate(["open_glover.jpg"] * 36 + ["closed_dreis.jpg"] * 15 + ["open_glover.jpg"] * 18):
        img = cv2.imread(str(DATA / name))
        s = 400 / img.shape[0]
        small = cv2.resize(img, (int(img.shape[1] * s), 400), interpolation=cv2.INTER_AREA)
        frame = np.full((480, 640, 3), 110, np.uint8)
        x0 = (640 - small.shape[1]) // 2 + int(4 * np.sin(i / 5))
        frame[40:440, x0 : x0 + small.shape[1]] = small
        writer.write(frame)
    writer.release()
    out_dir = tmp_path / "rec"
    assert vision_record.main(["--source", str(clip), "--free", "10", "--out", str(out_dir), "--quiet"]) == 0
    assert "Saved 69 frames (100% with a face)" in capsys.readouterr().out
    assert vision_replay.main([str(out_dir), "--timeline"]) == 0
    out = capsys.readouterr().out
    assert "long blink x1" in out and "would left click at" in out
