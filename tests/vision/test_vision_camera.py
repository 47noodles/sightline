"""Camera enumeration, sources and failure handling, without a physical camera."""

from __future__ import annotations

import threading

import cv2
import numpy as np
import pytest

from sightline.vision.camera import (
    HINT_BLACK,
    HINT_BUSY,
    HINT_FROZEN,
    HINT_NOT_FOUND,
    ArraySource,
    CameraError,
    CameraSupervisor,
    FrameHealth,
    VideoFileSource,
    _Raw,
    choose_camera,
    list_cameras,
    merge_devices,
)


def test_merge_matches_devices_and_labels_virtual_cameras():
    msmf = [_Raw(0, "Integrated Webcam", r"\\?\usb#vid_0c45&pid_6a10#abc", 0x0C45)]
    dshow = [
        _Raw(0, "OBS Virtual Camera", None, None),
        _Raw(1, "Integrated Webcam", r"\\?\USB#VID_0C45&PID_6A10#abc", None),
        _Raw(2, "Meta Quest 3", None, None),
        _Raw(3, "Some Capture Card", None, None),
    ]
    devices = merge_devices(msmf, dshow)
    assert [d.label for d in devices] == [
        "Integrated Webcam",
        "OBS Virtual Camera (virtual camera)",
        "Meta Quest 3 (virtual camera)",
        "Some Capture Card (virtual camera)",
    ]
    webcam = devices[0]
    assert webcam.msmf_index == 0 and webcam.dshow_index == 1 and not webcam.virtual
    assert devices[1].dshow_index == 0 and devices[1].msmf_index is None


def test_two_identical_webcams_get_distinct_labels():
    msmf = [_Raw(0, "USB Camera", "a", 1), _Raw(1, "USB Camera", "b", 1)]
    devices = merge_devices(msmf, [_Raw(0, "USB Camera", "b", 1), _Raw(1, "USB Camera", "a", 1)])
    assert [d.label for d in devices] == ["USB Camera", "USB Camera 2"]
    assert devices[0].dshow_index == 1 and devices[1].dshow_index == 0  # matched by path, not by order


def test_choose_prefers_the_saved_camera_then_a_physical_one():
    devices = merge_devices([_Raw(0, "Webcam", "p", 1)], [_Raw(0, "OBS Virtual Camera", None, None), _Raw(1, "Webcam", "p", 1)])
    assert choose_camera(devices).name == "Webcam"
    assert choose_camera(devices, "OBS Virtual Camera").name == "OBS Virtual Camera"
    assert choose_camera(devices, "Gone camera").name == "Webcam"
    only_virtual = merge_devices([], [_Raw(0, "Meta Quest 3", None, None)])
    assert choose_camera(only_virtual).virtual
    assert choose_camera([]) is None


def test_list_cameras_runs_on_this_machine():
    # This machine has only virtual cameras; whatever is listed must come back labelled and ordered.
    devices = list_cameras()
    assert all(d.label for d in devices)
    flags = [d.virtual for d in devices]
    assert flags == sorted(flags)


def test_frame_health_spots_black_and_frozen_pictures():
    h = FrameHealth(every=1)
    black = np.zeros((72, 128, 3), np.uint8)
    for i in range(60):
        h.update(black, i / 30)
    assert h.hint == HINT_BLACK
    h.reset()
    still = np.full((72, 128, 3), 120, np.uint8)
    cv2.putText(still, "OBS", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
    for i in range(120):
        h.update(still, i / 30)
    assert h.hint == HINT_FROZEN
    h.reset()
    rng = np.random.default_rng(0)
    for i in range(120):
        frame = np.clip(still.astype(int) + rng.normal(0, 3, still.shape), 0, 255).astype(np.uint8)  # sensor noise
        h.update(frame, i / 30)
    assert h.hint is None
    assert h.brightness > 100


def test_video_file_source_reads_frames_with_timestamps(tmp_path):
    path = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 30.0, (64, 48))
    for i in range(10):
        writer.write(np.full((48, 64, 3), i * 20, np.uint8))
    writer.release()
    src = VideoFileSource(path, clock=lambda: 100.0)
    src.open()
    got = []
    while (item := src.read()) is not None:
        got.append(item)
    src.close()
    assert len(got) == 10
    assert got[0][1] == pytest.approx(100.0) and got[-1][1] == pytest.approx(100.0 + 9 / 30)
    assert got[0][0].shape == (48, 64, 3)
    with pytest.raises(CameraError):
        VideoFileSource(tmp_path / "missing.avi").open()


class FlakySource:
    """Opens on demand, fails reading after a few frames."""

    def __init__(self, frames_before_failure: int, open_error: str | None = None) -> None:
        self.description = "Flaky"
        self.left = frames_before_failure
        self.open_error = open_error
        self.opened = 0
        self.closed = 0

    def open(self) -> None:
        self.opened += 1
        if self.open_error:
            raise CameraError(self.open_error)  # type: ignore[arg-type]

    def read(self, timeout: float = 1.0):
        if self.left <= 0:
            raise CameraError("failed")
        self.left -= 1
        return np.zeros((4, 4, 3), np.uint8), 0.0

    def close(self) -> None:
        self.closed += 1


def test_supervisor_reconnects_with_backoff_and_hints():
    clock = [0.0]
    sources = [FlakySource(3), FlakySource(0, "busy"), FlakySource(5)]
    made = []

    def make():
        s = sources[min(len(made), len(sources) - 1)]
        made.append(s)
        return s

    sup = CameraSupervisor(make, clock=lambda: clock[0], backoff=(1.0, 2.0))
    stop = threading.Event()
    frames = [sup.read(stop) for _ in range(3)]
    assert all(f is not None for f in frames) and sup.hint is None
    assert sup.read(stop) is None  # the camera stopped
    assert sup.hint == HINT_BUSY and sources[0].closed == 1
    assert sup.read(stop) is None and len(made) == 1  # waiting out the backoff
    clock[0] += 1.1
    assert sup.read(stop) is None and len(made) == 2  # another app holds it
    assert sup.hint == HINT_BUSY
    clock[0] += 2.1
    assert sup.read(stop) is not None and len(made) == 3
    assert sup.hint is None


def test_supervisor_without_a_camera_says_so():
    sup = CameraSupervisor(lambda: None, clock=lambda: 0.0)
    assert sup.read(threading.Event()) is None
    assert sup.hint == HINT_NOT_FOUND


def test_supervisor_reopens_on_request_and_notices_the_end_of_a_file():
    src = ArraySource([np.zeros((2, 2, 3), np.uint8)] * 2)
    sup = CameraSupervisor(lambda: src)
    stop = threading.Event()
    assert sup.read(stop) is not None and sup.read(stop) is not None
    assert sup.read(stop) is None and sup.ended
    sup.request_reopen()
    sup.ended = False
    assert sup.read(stop) is not None  # reopened from the start
