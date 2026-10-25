"""The real MediaPipe Face Landmarker on real photos: shapes, which eye is which, head turn, speed.

Photos and licences: ``tests/vision/data/SOURCES.md``.  These tests need the
downloaded model and are skipped without it.
"""

from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from sightline.core.context import AppContext
from sightline.core.events import CameraFrame, EyeGesture, FaceStatus, PointerSample
from sightline.vision import features as feat
from sightline.vision.camera import VideoFileSource
from sightline.vision.eyes import ClosureEstimator
from sightline.vision.landmarks import BS, N_BLENDSHAPES, N_LANDMARKS, FaceLandmarker, ImageLandmarker
from sightline.vision.service import VisionService

pytestmark = pytest.mark.models

DATA = Path(__file__).parent / "data"
# Which of the subject's own eyes is closed.
WINKS = {
    "wink_left_bs70.jpg": "left",
    "wink_right_drost.jpg": "right",
    "wink_right_qz10.jpg": "right",
    "wink_right_simoes.jpg": "right",
}


def photo(name: str, flip: bool = False) -> np.ndarray:
    img = cv2.imread(str(DATA / name))
    assert img is not None, name
    return cv2.flip(img, 1) if flip else img


def on_canvas(name: str, w: int, h: int, face_h: int, dx: int = 0, dy: int = 0) -> np.ndarray:
    """The photo scaled to ``face_h`` pixels tall on a grey frame of a webcam's size."""
    img = photo(name)
    s = face_h / img.shape[0]
    small = cv2.resize(img, (int(img.shape[1] * s), int(img.shape[0] * s)), interpolation=cv2.INTER_AREA)
    out = np.full((h, w, 3), 110, np.uint8)
    y0 = (h - small.shape[0]) // 2 + dy
    x0 = (w - small.shape[1]) // 2 + dx
    out[y0 : y0 + small.shape[0], x0 : x0 + small.shape[1]] = small
    return out


@pytest.fixture
def landmarker(face_model):
    lmk = ImageLandmarker(face_model)
    yield lmk
    lmk.close()


def closure(lmk: ImageLandmarker, img: np.ndarray, mirrored: bool = False):
    face = lmk.detect(img)
    assert face is not None
    m = feat.measure(face)
    left, right = feat.user_eyes(m, mirrored)
    return face, m, (left, right), ClosureEstimator().update(left, right)


def test_output_has_every_point_and_named_blendshape(landmarker):
    face, m, _, _ = closure(landmarker, photo("open_glover.jpg"))
    assert face.landmarks.shape == (N_LANDMARKS, 3) == (478, 3)
    assert face.blendshapes.shape == (N_BLENDSHAPES,) == (52,)
    assert face.matrix.shape == (4, 4)
    xy = face.landmarks[:, :2]
    assert (xy > 0).all() and (xy < 1).all()
    assert ((face.blendshapes >= 0) & (face.blendshapes <= 1)).all()
    # Every name was mapped: the open eyes still score something on the blink shapes.
    assert face.blendshapes[BS["eyeBlinkLeft"]] > 0 and face.blendshapes[BS["eyeBlinkRight"]] > 0
    rot = face.matrix[:3, :3] / np.cbrt(np.linalg.det(face.matrix[:3, :3]))
    np.testing.assert_allclose(rot @ rot.T, np.eye(3), atol=0.05)  # a rotation (possibly scaled)
    assert m.distance_cm is not None and m.iod_px > 50


@pytest.mark.parametrize("flip", [False, True])
def test_open_and_closed_eyes(landmarker, flip):
    _, _, _, (cl, cr) = closure(landmarker, photo("open_glover.jpg", flip))
    assert cl < 0.2 and cr < 0.2
    _, _, _, (cl, cr) = closure(landmarker, photo("closed_dreis.jpg", flip))
    assert cl > 0.7 and cr > 0.7


@pytest.mark.parametrize("name", sorted(WINKS))
def test_winks_land_on_the_right_eye(landmarker, name):
    closed = WINKS[name]
    # Unmirrored photo: MediaPipe's names are the subject's own eyes.
    _, _, (left, right), (cl, cr) = closure(landmarker, photo(name))
    shut, other = (cl, cr) if closed == "left" else (cr, cl)
    shut_bs, other_bs = (left.blink, right.blink) if closed == "left" else (right.blink, left.blink)
    assert shut > other + 0.3, (cl, cr)
    assert shut_bs > other_bs
    # A mirrored picture swaps them ...
    _, _, _, (fl, fr) = closure(landmarker, photo(name, flip=True))
    assert (fr if closed == "left" else fl) > (fl if closed == "left" else fr) + 0.3
    # ... and the "camera mirrors the picture" setting swaps them back.
    _, _, _, (ml, mr) = closure(landmarker, photo(name, flip=True), mirrored=True)
    assert (ml if closed == "left" else mr) > (mr if closed == "left" else ml) + 0.3


@pytest.mark.parametrize("name", ["wink_right_drost.jpg", "wink_right_qz10.jpg"])
def test_head_turn_sign_matches_where_the_nose_points(landmarker, name):
    for flip in (False, True):
        face, m, _, _ = closure(landmarker, photo(name, flip))
        p = face.landmarks[:, :2] * np.array([face.width, face.height])
        # Where the nose tip sits between the face edges: over 0.5 means towards the image's right.
        a, b = p[454, 0], p[234, 0]
        nose = (p[1, 0] - min(a, b)) / abs(a - b)
        yaw, _, _ = feat.user_pose(m, mirrored=False)
        assert abs(yaw) > 12
        # Turning to one's own left moves the nose towards the image's right in an unmirrored picture.
        assert (yaw < 0) == (nose > 0.5), (flip, yaw, nose)
        assert (yaw < 0) == (not flip)
        if flip:
            mirrored_yaw, _, _ = feat.user_pose(m, mirrored=True)
            assert mirrored_yaw < -12  # the setting restores the subject's own direction


def test_video_mode_tracks_a_720p_face_within_budget(face_model):
    from tools.desktop_lock import desktop_lock

    frames = [on_canvas("open_glover.jpg", 1280, 720, 520, int(8 * np.sin(i / 9)), int(5 * np.cos(i / 7))) for i in range(120)]
    lmk = FaceLandmarker(face_model)
    times: list[float] = []
    try:
        try:
            with desktop_lock("pytest vision timing", timeout=180.0, name="cpu"):
                for i in range(20):  # warm up, and let tracking take over from detection
                    lmk.detect(frames[i], i / 30)
                for i, frame in enumerate(frames):
                    t0 = time.perf_counter()
                    face = lmk.detect(frame, (20 + i) / 30)
                    times.append((time.perf_counter() - t0) * 1000.0)
                    assert face is not None
        except TimeoutError:
            pytest.skip("another benchmark held the cpu lock")
    finally:
        lmk.close()
    mean = float(np.mean(times))
    print(f"720p Face Landmarker, VIDEO mode: mean {mean:.1f} ms, p95 {np.percentile(times, 95):.1f} ms")
    assert mean < 20.0


def test_capture_path_from_a_video_file_to_a_blink_click(face_model, tmp_path):
    """Video file -> camera supervisor -> real MediaPipe -> pipeline -> click, on the real vision thread."""
    path = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 30.0, (640, 480))
    # Open eyes for 1.2 s, closed for 0.5 s (a deliberate long blink), open again.
    script = ["open_glover.jpg"] * 36 + ["closed_dreis.jpg"] * 15 + ["open_glover.jpg"] * 18
    for i, name in enumerate(script):
        writer.write(on_canvas(name, 640, 480, 400, int(4 * np.sin(i / 5))))
    writer.release()

    ctx = AppContext.for_tests()
    actions: list = []
    ctx.actions.dispatch = actions.append
    events: list = []
    ctx.bus.subscribe(object, events.append)
    svc = VisionService(
        ctx,
        source_factory=lambda: VideoFileSource(path),
        landmarker_factory=lambda: FaceLandmarker(face_model),
        start_pump=False,
    )
    svc.set_preview(True)
    svc.start()
    assert svc._thread is not None
    svc._thread.join(60.0)
    svc.stop()

    samples = [e for e in events if isinstance(e, PointerSample)]
    assert len(samples) == len(script)
    assert all(e.present for e in events if isinstance(e, FaceStatus))
    assert [e.kind for e in events if isinstance(e, EyeGesture)] == ["long_blink"]
    (click,) = actions
    assert click.name == "mouse.click" and click.source == "eye" and click.args["count"] == 1
    previews = [e for e in events if isinstance(e, CameraFrame)]
    assert len(previews) >= 30 and all(p.landmarks is not None for p in previews)
