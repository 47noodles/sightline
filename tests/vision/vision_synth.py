"""A synthetic face for tests: real MediaPipe landmarks from a photo, moved like a person would move them.

``SyntheticFace.frame`` produces ``FaceFrame`` values with a chosen eye
rotation, head pose, lid closure and noise, so the whole pipeline after
landmark extraction runs without a camera.  ``SyntheticUser`` adds screen
geometry: give it the screen point being looked at and the head pose, and
it works out where the irises sit, the way a real eye would.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from sightline.vision import landmarks as lm
from sightline.vision.landmarks import BS, BLENDSHAPE_NAMES, FaceFrame

DATA = Path(__file__).parent / "data"

# Eye groups and their mirror images, for simulating a camera that mirrors its picture.
_PAIRS = list(zip(lm.L_CONTOUR, lm.R_CONTOUR)) + list(zip(lm.L_IRIS, lm.R_IRIS)) + [(lm.FACE_LEFT_EDGE, lm.FACE_RIGHT_EDGE)]
_SWAP = np.arange(lm.N_LANDMARKS)
for a, b in _PAIRS:
    _SWAP[a], _SWAP[b] = b, a
_BS_SWAP = np.arange(len(BLENDSHAPE_NAMES))
for i, name in enumerate(BLENDSHAPE_NAMES):
    if name.endswith("Left"):
        _BS_SWAP[i] = BS[name[:-4] + "Right"]
    elif name.endswith("Right"):
        _BS_SWAP[i] = BS[name[:-5] + "Left"]


def _rot_y(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def _rot_x(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def _rot_z(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


class SyntheticFace:
    def __init__(self, width: int = 1280, height: int = 720, iod_px: float = 150.0) -> None:
        z = np.load(DATA / "face_template.npz")
        tw, th = int(z["width"][0]), int(z["height"][0])
        pts = z["landmarks"].astype(np.float64) * np.array([tw, th, tw])
        iod = float(np.hypot(*(pts[lm.L_OUTER, :2] - pts[lm.R_OUTER, :2])))
        centre = pts[[lm.L_OUTER, lm.R_OUTER, 1, 168]].mean(axis=0)
        self.base = (pts - centre) * (iod_px / iod)  # face-centred, pixel units at the target size
        self.base_bs = z["blendshapes"].astype(np.float64)
        self.width, self.height = width, height
        self.iod_px = iod_px
        # The neck pivot sits about 10 cm behind the eyes (the outer eye corners are about 9 cm apart).
        self.pivot = np.array([0.0, 0.6 * iod_px, 1.1 * iod_px])
        self.eye_w = {
            "A": float(np.hypot(*(self.base[lm.L_OUTER, :2] - self.base[lm.L_INNER, :2]))),
            "B": float(np.hypot(*(self.base[lm.R_OUTER, :2] - self.base[lm.R_INNER, :2]))),
        }

    def frame(
        self,
        t: float,
        *,
        iris: tuple[float, float] = (0.0, 0.0),
        yaw: float = 0.0,
        pitch: float = 0.0,
        roll: float = 0.0,
        shift: tuple[float, float] = (0.0, 0.0),
        closure: tuple[float, float] = (0.0, 0.0),
        noise_px: float = 0.0,
        bs_noise: float = 0.0,
        rng: np.random.Generator | None = None,
        mirrored: bool = False,
    ) -> FaceFrame:
        """``iris``: offset of both irises in eye widths, image terms (x towards image right, y down).

        ``yaw``/``pitch``/``roll``: degrees in the user's terms (+ turned to their right, + up, + tilted
        to their right).  ``closure``: the user's (left, right) eye, 0 open .. 1 shut.
        """
        p = self.base.copy()
        ix, iy = iris
        # Eye A is MediaPipe's left eye = the user's left eye before any mirroring.
        for eye, iris_idx, upper, lower, c in (
            ("A", lm.L_IRIS, lm.L_UPPER, lm.L_LOWER, closure[0]),
            ("B", lm.R_IRIS, lm.R_UPPER, lm.R_LOWER, closure[1]),
        ):
            w = self.eye_w[eye]
            p[list(iris_idx), 0] += ix * w
            p[list(iris_idx), 1] += iy * w
            # Lids follow vertical eye rotation almost one for one.
            p[list(upper), 1] += 0.8 * iy * w
            p[list(lower), 1] += 0.5 * iy * w
            if c > 0:
                up = p[list(upper), 1].copy()
                lo = p[list(lower), 1].copy()
                p[list(upper), 1] = up + c * 0.85 * (lo - up)
                p[list(lower), 1] = lo - c * 0.1 * (lo - up)
        # Head rotation about the neck pivot (image axes: x right, y down, z away from the camera).
        th, ph, ps = math.radians(yaw), math.radians(pitch), math.radians(roll)
        q = p - self.pivot
        x, y, zz = q[:, 0], q[:, 1], q[:, 2]
        x, y = x * math.cos(ps) + y * math.sin(ps), -x * math.sin(ps) + y * math.cos(ps)
        y, zz = y * math.cos(ph) + zz * math.sin(ph), -y * math.sin(ph) + zz * math.cos(ph)
        x, zz = x * math.cos(th) + zz * math.sin(th), -x * math.sin(th) + zz * math.cos(th)
        q = np.column_stack([x, y, zz]) + self.pivot
        q[:, 0] += self.width / 2.0 + shift[0]
        q[:, 1] += self.height / 2.0 + shift[1]
        if rng is not None and noise_px > 0:
            q[:, :2] += rng.normal(0.0, noise_px, size=(len(q), 2))

        bs = self.base_bs.copy()
        bs[BS["eyeBlinkLeft"]] = 0.05 + 0.6 * closure[0]
        bs[BS["eyeBlinkRight"]] = 0.05 + 0.6 * closure[1]
        right_ = max(0.0, ix) * 4.0  # looking towards image right
        left_ = max(0.0, -ix) * 4.0
        bs[BS["eyeLookOutLeft"]], bs[BS["eyeLookInLeft"]] = right_, left_
        bs[BS["eyeLookInRight"]], bs[BS["eyeLookOutRight"]] = right_, left_
        bs[BS["eyeLookDownLeft"]] = bs[BS["eyeLookDownRight"]] = max(0.0, iy) * 6.0
        bs[BS["eyeLookUpLeft"]] = bs[BS["eyeLookUpRight"]] = max(0.0, -iy) * 6.0
        if rng is not None and bs_noise > 0:
            bs += rng.normal(0.0, bs_noise, size=bs.shape)
        bs = np.clip(bs, 0.0, 1.0)

        # MediaPipe's camera space: x right, y up, z towards the viewer.  Camera-terms yaw is + towards image left.
        sign = -1.0 if mirrored else 1.0
        rot = _rot_y(-sign * th) @ _rot_x(-ph) @ _rot_z(sign * ps)
        matrix = np.eye(4)
        matrix[:3, :3] = rot
        matrix[:3, 3] = [shift[0] / self.iod_px * 9.0, -shift[1] / self.iod_px * 9.0, -60.0]

        if mirrored:
            q[:, 0] = self.width - q[:, 0]
            q = q[_SWAP]
            bs = bs[_BS_SWAP]
        landmarks = (q / np.array([self.width, self.height, self.width])).astype(np.float32)
        return FaceFrame(t, landmarks, bs.astype(np.float32), matrix.astype(np.float32), self.width, self.height)


@dataclass
class Screen:
    width: int = 1920
    height: int = 1080
    pitch_mm: float = 0.2745  # the fake display: 527 mm over 1920 px
    distance_mm: float = 600.0
    camera_above_mm: float = 10.0  # camera at the top edge of the screen


class SyntheticUser:
    """Turns "looking at screen point (x, y) with this head pose" into iris offsets, like a real eye."""

    def __init__(self, face: SyntheticFace | None = None, screen: Screen | None = None, seed: int = 0) -> None:
        self.face = face or SyntheticFace()
        self.screen = screen or Screen()
        self.rng = np.random.default_rng(seed)
        self.eye_gain = 0.4  # iris travel in eye widths per unit sine of eye rotation (12 mm eyeball, 30 mm eye)

    def iris_for(self, x: float, y: float, yaw: float = 0.0, pitch: float = 0.0) -> tuple[float, float]:
        s = self.screen
        dx = (x - s.width / 2.0) * s.pitch_mm
        dy = (y - s.height / 2.0) * s.pitch_mm
        gaze_yaw = math.degrees(math.atan2(dx, s.distance_mm))  # + to the user's right
        gaze_pitch = -math.degrees(math.atan2(dy, s.distance_mm))  # + up
        eye_yaw = math.radians(gaze_yaw - yaw)
        eye_pitch = math.radians(gaze_pitch - pitch)
        # Looking to the user's right moves the iris towards the image's left in an unmirrored camera.
        return -self.eye_gain * math.sin(eye_yaw), -self.eye_gain * 0.8 * math.sin(eye_pitch)

    def look(
        self,
        t: float,
        x: float,
        y: float,
        *,
        yaw: float = 0.0,
        pitch: float = 0.0,
        closure: tuple[float, float] = (0.0, 0.0),
        noise_px: float = 0.4,
        bs_noise: float = 0.02,
        mirrored: bool = False,
    ) -> FaceFrame:
        return self.face.frame(
            t,
            iris=self.iris_for(x, y, yaw, pitch),
            yaw=yaw,
            pitch=pitch,
            closure=closure,
            noise_px=noise_px,
            bs_noise=bs_noise,
            rng=self.rng,
            mirrored=mirrored,
        )
