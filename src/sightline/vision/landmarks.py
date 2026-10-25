"""Face landmarks: the ``FaceFrame`` record and the MediaPipe Face Landmarker wrapper.

Everything after this module works on ``FaceFrame`` values, so the whole
pipeline can be tested and replayed without a camera or MediaPipe.

Left and right
--------------
MediaPipe names its landmark groups and blendshapes for the subject's own
eyes, as ARKit does.  This was checked against photos of people winking
(tests/vision/data, see SOURCES.md): ``eyeBlinkLeft`` rises when the eye
around landmarks 263/362 closes, and in an unmirrored camera image that eye
is on the image's right, which is the subject's left.  A camera that mirrors
its picture swaps all of this, which is what the ``camera_mirrored`` setting
(or the eye and head calibrations) corrects.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

N_LANDMARKS = 478

# --------------------------------------------------------------------------- landmark indices
# "L" is the subject's own left eye (image right in an unmirrored frame), "R" the right eye.

L_OUTER, L_INNER = 263, 362
R_OUTER, R_INNER = 33, 133
# Iris: centre first, then four points on the iris edge.
L_IRIS = (473, 474, 475, 476, 477)
R_IRIS = (468, 469, 470, 471, 472)
# Lids, from the outer corner towards the inner corner, without the corners.
L_UPPER = (466, 388, 387, 386, 385, 384, 398)
L_LOWER = (249, 390, 373, 374, 380, 381, 382)
R_UPPER = (246, 161, 160, 159, 158, 157, 173)
R_LOWER = (7, 163, 144, 145, 153, 154, 155)
# The six points of the classic eye aspect ratio (Soukupova and Cech 2016): p1 p2 p3 p4 p5 p6.
L_EAR = (362, 385, 387, 263, 373, 380)
R_EAR = (33, 160, 158, 133, 153, 144)
# Full 16-point eye contours, used for the preview and the landmark gaze features.
L_CONTOUR = (263, 249, 390, 373, 374, 380, 381, 382, 362, 398, 384, 385, 386, 387, 388, 466)
R_CONTOUR = (33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246)
# Points along the nose bridge and tip: they move with the head and hardly at all with expressions.
NOSE_POINTS = (168, 6, 197, 195, 5, 4, 1)
FACE_TOP, FACE_BOTTOM, FACE_LEFT_EDGE, FACE_RIGHT_EDGE = 10, 152, 454, 234

PREVIEW_POINTS = L_CONTOUR + R_CONTOUR + L_IRIS + R_IRIS

# --------------------------------------------------------------------------- blendshapes

BLENDSHAPE_NAMES: tuple[str, ...] = (
    "_neutral",
    "browDownLeft",
    "browDownRight",
    "browInnerUp",
    "browOuterUpLeft",
    "browOuterUpRight",
    "cheekPuff",
    "cheekSquintLeft",
    "cheekSquintRight",
    "eyeBlinkLeft",
    "eyeBlinkRight",
    "eyeLookDownLeft",
    "eyeLookDownRight",
    "eyeLookInLeft",
    "eyeLookInRight",
    "eyeLookOutLeft",
    "eyeLookOutRight",
    "eyeLookUpLeft",
    "eyeLookUpRight",
    "eyeSquintLeft",
    "eyeSquintRight",
    "eyeWideLeft",
    "eyeWideRight",
    "jawForward",
    "jawLeft",
    "jawOpen",
    "jawRight",
    "mouthClose",
    "mouthDimpleLeft",
    "mouthDimpleRight",
    "mouthFrownLeft",
    "mouthFrownRight",
    "mouthFunnel",
    "mouthLeft",
    "mouthLowerDownLeft",
    "mouthLowerDownRight",
    "mouthPressLeft",
    "mouthPressRight",
    "mouthPucker",
    "mouthRight",
    "mouthRollLower",
    "mouthRollUpper",
    "mouthShrugLower",
    "mouthShrugUpper",
    "mouthSmileLeft",
    "mouthSmileRight",
    "mouthStretchLeft",
    "mouthStretchRight",
    "mouthUpperUpLeft",
    "mouthUpperUpRight",
    "noseSneerLeft",
    "noseSneerRight",
)
BS = {name: i for i, name in enumerate(BLENDSHAPE_NAMES)}
N_BLENDSHAPES = len(BLENDSHAPE_NAMES)


@dataclass(slots=True)
class FaceFrame:
    """One tracked face, as the Face Landmarker reports it.

    ``landmarks`` x and y are normalised to the image (0..1); z uses roughly
    the same scale as x, negative towards the camera.  ``matrix`` maps the
    canonical face model into camera space (x right, y up, z towards the
    viewer, centimetres).
    """

    t: float
    landmarks: np.ndarray  # (478, 3) float32
    blendshapes: np.ndarray  # (52,) float32, BLENDSHAPE_NAMES order
    matrix: np.ndarray  # (4, 4) float32
    width: int
    height: int

    def points_px(self, indices: tuple[int, ...] | list[int]) -> np.ndarray:
        """Image-pixel x, y of the given landmarks."""
        pts = self.landmarks[list(indices), :2].astype(np.float64)
        pts[:, 0] *= self.width
        pts[:, 1] *= self.height
        return pts

    def blend(self, name: str) -> float:
        return float(self.blendshapes[BS[name]])


def empty_frame(t: float = 0.0, width: int = 640, height: int = 480) -> FaceFrame:
    """A zero-filled frame (tests and padding in recordings)."""
    return FaceFrame(
        t,
        np.zeros((N_LANDMARKS, 3), np.float32),
        np.zeros(N_BLENDSHAPES, np.float32),
        np.eye(4, dtype=np.float32),
        width,
        height,
    )


# --------------------------------------------------------------------------- MediaPipe


class LandmarkerError(RuntimeError):
    pass


class _ResultConverter:
    """Turns a MediaPipe result into a ``FaceFrame``, mapping blendshapes by name."""

    def __init__(self) -> None:
        self._bs_order: list[int] | None = None

    def convert(self, result, t: float, width: int, height: int) -> FaceFrame | None:
        if not result.face_landmarks:
            return None
        lms = result.face_landmarks[0]
        if len(lms) < N_LANDMARKS:
            raise LandmarkerError(f"the face model returned {len(lms)} points; it needs the iris points too")
        landmarks = np.array([(p.x, p.y, p.z) for p in lms], dtype=np.float32)
        blendshapes = np.zeros(N_BLENDSHAPES, np.float32)
        if result.face_blendshapes:
            cats = result.face_blendshapes[0]
            if self._bs_order is None or len(self._bs_order) != len(cats):
                # Map by name once: the order has been stable, but a different model file could change it.
                self._bs_order = [BS.get(c.category_name, -1) for c in cats]
                unknown = [c.category_name for c, i in zip(cats, self._bs_order) if i < 0]
                if unknown:
                    log.warning("Unknown blendshapes ignored: %s", ", ".join(unknown))
            for c, i in zip(cats, self._bs_order):
                if i >= 0:
                    blendshapes[i] = c.score
        if result.facial_transformation_matrixes:
            matrix = np.asarray(result.facial_transformation_matrixes[0], dtype=np.float32).reshape(4, 4)
        else:
            matrix = np.eye(4, dtype=np.float32)
        return FaceFrame(t, landmarks, blendshapes, matrix, int(width), int(height))


def _options(model_path: Path, video: bool, min_confidence: float):
    from mediapipe.tasks.python import vision
    from mediapipe.tasks.python.core.base_options import BaseOptions

    if not Path(model_path).exists():
        raise LandmarkerError(f"face model not found: {model_path}")
    # Read the file ourselves: MediaPipe cannot open paths with non-ASCII characters on Windows,
    # and a user name with an accent would otherwise break tracking.
    data = Path(model_path).read_bytes()
    return vision.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_buffer=data),
        running_mode=vision.RunningMode.VIDEO if video else vision.RunningMode.IMAGE,
        num_faces=1,
        min_face_detection_confidence=min_confidence,
        min_face_presence_confidence=min_confidence,
        min_tracking_confidence=min_confidence,
        output_face_blendshapes=True,
        output_facial_transformation_matrixes=True,
    )


class FaceLandmarker:
    """MediaPipe Face Landmarker in VIDEO mode (tracks between frames, so it skips detection most of the time).

    Not thread-safe: create and use it on one thread.
    """

    def __init__(self, model_path: Path, *, min_confidence: float = 0.5) -> None:
        try:
            import mediapipe as mp
            from mediapipe.tasks.python import vision
        except Exception as e:  # pragma: no cover - import failure is environment specific
            raise LandmarkerError(f"MediaPipe is not available: {e}") from e
        self._mp = mp
        self._landmarker = vision.FaceLandmarker.create_from_options(_options(model_path, True, min_confidence))
        self._converter = _ResultConverter()
        self._last_ms = -1
        self.last_ms: float = 0.0  # inference time of the last call, for diagnostics

    def detect(self, bgr: np.ndarray, t: float) -> FaceFrame | None:
        """Track the face in a BGR frame captured at ``t`` (perf_counter seconds)."""
        import cv2

        started = time.perf_counter()
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        # VIDEO mode needs strictly increasing integer milliseconds.
        ts = int(t * 1000.0)
        if ts <= self._last_ms:
            ts = self._last_ms + 1
        self._last_ms = ts
        result = self._landmarker.detect_for_video(image, ts)
        frame = self._converter.convert(result, t, bgr.shape[1], bgr.shape[0])
        self.last_ms = (time.perf_counter() - started) * 1000.0
        return frame

    def close(self) -> None:
        if self._landmarker is not None:
            try:
                self._landmarker.close()
            except Exception:  # pragma: no cover - best effort on shutdown
                log.debug("landmarker close failed", exc_info=True)
            self._landmarker = None  # type: ignore[assignment]


class ImageLandmarker:
    """The same conversion in IMAGE mode, for single photos (tests and tools)."""

    def __init__(self, model_path: Path, *, min_confidence: float = 0.5) -> None:
        import mediapipe as mp
        from mediapipe.tasks.python import vision

        self._mp = mp
        self._landmarker = vision.FaceLandmarker.create_from_options(_options(model_path, False, min_confidence))
        self._converter = _ResultConverter()

    def detect(self, bgr: np.ndarray, t: float = 0.0) -> FaceFrame | None:
        import cv2

        rgb = np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        result = self._landmarker.detect(self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb))
        return self._converter.convert(result, t, bgr.shape[1], bgr.shape[0])

    def close(self) -> None:
        self._landmarker.close()
