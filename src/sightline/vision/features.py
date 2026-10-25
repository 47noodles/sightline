"""Per-frame measurements of the face: eye geometry, head pose, distance and the gaze feature vectors.

Two feature families feed the gaze model:

* ``raw``: a compact, named vector built from the geometry of each eye
  (iris centre and lids in the eye's own frame, so head roll and camera
  distance do not leak into it), the ``eyeLook*`` blendshapes and head pose.
* ``lm_block``: head-normalised landmark coordinates of the eyes, adapted
  from EyeTrax's ``GazeEstimator.extract_features`` (see the notice below).

The eye-frame construction follows the reasoning in shaal/eye-tracker
ADR-0005 (MIT): project the iris onto axes built from the eye's own corners
and divide by the eye width.

Everything here works in the camera's own (unmirrored) image terms: "A" is
MediaPipe's left eye (landmarks 263/362), "B" its right eye (33/133).  The
gaze regression learns signs from data, so mirroring does not matter to it.
``user_eyes`` and ``user_pose`` turn these into the user's own left and right.

Portions adapted from EyeTrax (https://github.com/ck-zhang/EyeTrax),
eyetrax/gaze.py and eyetrax/constants.py:

    MIT License

    Copyright (c) 2024 ck-zhang

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import landmarks as lm
from .landmarks import BS, FaceFrame

IRIS_DIAMETER_MM = 11.7  # adult horizontal visible iris diameter, nearly constant across people

RAW_NAMES: tuple[str, ...] = (
    "ixA", "iyA", "upA", "loA", "earA",
    "ixB", "iyB", "upB", "loB", "earB",
    "lhA", "lvA", "lhB", "lvB",
    "yaw", "pitch", "roll",
    "hx", "hy", "hs",
)  # fmt: skip
RAW_INDEX = {name: i for i, name in enumerate(RAW_NAMES)}
N_RAW = len(RAW_NAMES)

# Landmark block: iris and contour of both eyes, x and y in the head frame (EyeTrax keeps z too, but
# monocular depth is the noisiest coordinate and doubles the width of an already wide block).
LM_BLOCK_POINTS: tuple[int, ...] = lm.L_IRIS + lm.L_CONTOUR + lm.R_IRIS + lm.R_CONTOUR
N_LM_BLOCK = 2 * len(LM_BLOCK_POINTS)


@dataclass(slots=True)
class EyeMeasure:
    """One eye, measured in its own frame (x along the corners towards image right, y towards image down).

    Positions are in eye widths, so they do not change with distance or head roll.
    """

    ix: float  # iris centre
    iy: float
    upper: float  # upper lid (negative: above the corner line)
    lower: float  # lower lid
    ear: float  # eye aspect ratio, about 0.25-0.3 open and under 0.1 closed
    width_px: float
    iris_px: float  # horizontal iris diameter in image pixels
    blink: float  # eyeBlink blendshape for this eye, 0..1
    look_h: float  # eyeLook blendshapes: towards image right minus towards image left
    look_v: float  # up minus down


@dataclass(slots=True)
class FaceMeasures:
    t: float
    eye_a: EyeMeasure  # MediaPipe "left" eye (263/362)
    eye_b: EyeMeasure  # MediaPipe "right" eye (33/133)
    yaw: float  # degrees; camera terms: + when the face turns towards image left
    pitch: float  # + face up
    roll: float  # + image-right side of the face raised
    tx: float  # centimetres, MediaPipe's estimate under its own assumed camera
    ty: float
    tz: float
    head_x: float  # image pixels of the nose bridge, the steadiest point for head pointing
    head_y: float
    iod_px: float  # outer eye corner distance in pixels
    distance_cm: float | None
    width: int
    height: int
    face_box: tuple[int, int, int, int]  # x0, y0, x1, y1 in pixels around the eyes and nose
    raw: np.ndarray  # RAW_NAMES
    lm_block: np.ndarray  # N_LM_BLOCK


def focal_px(width: int, fov_deg: float) -> float:
    """Focal length in pixels for an assumed horizontal field of view."""
    return (width / 2.0) / math.tan(math.radians(max(10.0, min(170.0, fov_deg))) / 2.0)


def head_angles(matrix: np.ndarray) -> tuple[float, float, float]:
    """Yaw, pitch, roll in degrees from MediaPipe's facial transformation matrix.

    Conventions were checked on photos (see the module docs of landmarks.py):
    the face's forward axis ``R @ (0, 0, 1)`` has a positive x when the face
    turns towards the image's right and a positive y when it tilts up.
    Returned in camera terms: yaw is positive when the face turns towards the
    image's left, which in an unmirrored camera is the user's right.
    """
    m = np.asarray(matrix, dtype=np.float64)[:3, :3]
    # The matrix can carry a little scale; take the nearest pure rotation (as Project GameFace does).
    u, _, vt = np.linalg.svd(m)
    r = u @ vt
    fwd = r[:, 2]
    side = r[:, 0]
    yaw = math.degrees(math.atan2(-fwd[0], fwd[2]))
    pitch = math.degrees(math.atan2(fwd[1], math.hypot(fwd[0], fwd[2])))
    roll = math.degrees(math.atan2(side[1], side[0]))
    return yaw, pitch, roll


def _eye(
    pts: np.ndarray,
    corner_left: int,
    corner_right: int,
    iris: tuple[int, ...],
    upper: tuple[int, ...],
    lower: tuple[int, ...],
    ear_pts: tuple[int, ...],
    blink: float,
    look_h: float,
    look_v: float,
) -> EyeMeasure:
    a = pts[corner_left]
    b = pts[corner_right]
    axis = b - a
    width = float(math.hypot(axis[0], axis[1])) or 1e-6
    ux, uy = axis[0] / width, axis[1] / width
    cx, cy = (a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5
    iris_pts = pts[list(iris)]
    ic = iris_pts.mean(axis=0)
    dx, dy = ic[0] - cx, ic[1] - cy
    ix = (dx * ux + dy * uy) / width
    iy = (-dx * uy + dy * ux) / width
    # Only the three central lid points: the ones near the corners barely move.
    up = pts[list(upper[2:5])].mean(axis=0)
    lo = pts[list(lower[2:5])].mean(axis=0)
    upper_v = (-(up[0] - cx) * uy + (up[1] - cy) * ux) / width
    lower_v = (-(lo[0] - cx) * uy + (lo[1] - cy) * ux) / width
    p1, p2, p3, p4, p5, p6 = (pts[i] for i in ear_pts)
    ear = (
        math.hypot(*(p2 - p6)) + math.hypot(*(p3 - p5))
    ) / (2.0 * (math.hypot(*(p1 - p4)) or 1e-6))
    iris_px = float(math.hypot(*(iris_pts[1] - iris_pts[3])))
    return EyeMeasure(
        float(ix), float(iy), float(upper_v), float(lower_v), float(ear), width, iris_px, blink, look_h, look_v
    )


def landmark_block(frame: FaceFrame) -> np.ndarray:
    """EyeTrax's head-normalised eye landmarks: rotate into a frame fixed to the head, scale by eye distance.

    Adapted from ``GazeEstimator.extract_features`` in EyeTrax: the origin is
    the midpoint of the outer eye corners, x runs between them, y towards the
    top of the head.  Differences: pixel-scaled coordinates (EyeTrax mixes
    width- and height-normalised units), x and y only, and a smaller set of
    points (iris and eye contour).
    """
    w, h = frame.width, frame.height
    p = frame.landmarks.astype(np.float64)
    scale = np.array([w, h, w], dtype=np.float64)
    left_corner = p[lm.R_OUTER] * scale  # 33: image-left outer corner
    right_corner = p[lm.L_OUTER] * scale  # 263
    top = p[lm.FACE_TOP] * scale
    centre = (left_corner + right_corner) / 2.0
    x_axis = right_corner - left_corner
    dist = np.linalg.norm(x_axis)
    if dist < 1e-6:
        return np.zeros(N_LM_BLOCK)
    x_axis /= dist
    y_approx = top - centre
    y_axis = y_approx - np.dot(y_approx, x_axis) * x_axis
    y_axis /= np.linalg.norm(y_axis) + 1e-9
    z_axis = np.cross(x_axis, y_axis)
    rot = np.stack([x_axis, y_axis, z_axis])  # rows: head axes
    sub = p[list(LM_BLOCK_POINTS)] * scale - centre
    local = sub @ rot.T / dist
    return local[:, :2].reshape(-1)


def measure(frame: FaceFrame, fov_deg: float = 62.0) -> FaceMeasures:
    """Everything the pipeline needs from one frame, computed once."""
    w, h = frame.width, frame.height
    p = frame.landmarks[:, :2].astype(np.float64) * np.array([w, h], dtype=np.float64)
    bs = frame.blendshapes
    # Blendshape directions, expressed in image terms so both eyes agree: positive is towards image right.
    look_h_a = float(bs[BS["eyeLookOutLeft"]] - bs[BS["eyeLookInLeft"]])
    look_h_b = float(bs[BS["eyeLookInRight"]] - bs[BS["eyeLookOutRight"]])
    look_v_a = float(bs[BS["eyeLookUpLeft"]] - bs[BS["eyeLookDownLeft"]])
    look_v_b = float(bs[BS["eyeLookUpRight"]] - bs[BS["eyeLookDownRight"]])
    eye_a = _eye(p, lm.L_INNER, lm.L_OUTER, lm.L_IRIS, lm.L_UPPER, lm.L_LOWER, lm.L_EAR,
                 float(bs[BS["eyeBlinkLeft"]]), look_h_a, look_v_a)  # fmt: skip
    eye_b = _eye(p, lm.R_OUTER, lm.R_INNER, lm.R_IRIS, lm.R_UPPER, lm.R_LOWER, lm.R_EAR,
                 float(bs[BS["eyeBlinkRight"]]), look_h_b, look_v_b)  # fmt: skip
    yaw, pitch, roll = head_angles(frame.matrix)
    tx, ty, tz = (float(v) for v in frame.matrix[:3, 3])
    nose = p[list(lm.NOSE_POINTS)].mean(axis=0)
    iod = float(np.hypot(*(p[lm.L_OUTER] - p[lm.R_OUTER])))
    iris_px = 0.5 * (eye_a.iris_px + eye_b.iris_px)
    distance = None
    if iris_px > 1.0:
        distance = focal_px(w, fov_deg) * IRIS_DIAMETER_MM / iris_px / 10.0
    box_pts = p[[lm.R_OUTER, lm.L_OUTER, lm.FACE_TOP, 1]]
    x0, y0 = box_pts.min(axis=0)
    x1, y1 = box_pts.max(axis=0)
    face_box = (int(max(0, x0)), int(max(0, y0)), int(min(w, x1)), int(min(h, y1)))
    raw = np.array(
        [
            eye_a.ix, eye_a.iy, eye_a.upper, eye_a.lower, eye_a.ear,
            eye_b.ix, eye_b.iy, eye_b.upper, eye_b.lower, eye_b.ear,
            look_h_a, look_v_a, look_h_b, look_v_b,
            yaw, pitch, roll,
            nose[0] / w - 0.5, nose[1] / h - 0.5, iod / w,
        ],
        dtype=np.float64,
    )  # fmt: skip
    return FaceMeasures(
        t=frame.t,
        eye_a=eye_a,
        eye_b=eye_b,
        yaw=yaw,
        pitch=pitch,
        roll=roll,
        tx=tx,
        ty=ty,
        tz=tz,
        head_x=float(nose[0]),
        head_y=float(nose[1]),
        iod_px=iod,
        distance_cm=distance,
        width=w,
        height=h,
        face_box=face_box,
        raw=raw,
        lm_block=landmark_block(frame),
    )


def user_eyes(m: FaceMeasures, mirrored: bool) -> tuple[EyeMeasure, EyeMeasure]:
    """The user's own (left, right) eyes.  In an unmirrored camera MediaPipe's names already match."""
    return (m.eye_b, m.eye_a) if mirrored else (m.eye_a, m.eye_b)


def user_pose(m: FaceMeasures, mirrored: bool) -> tuple[float, float, float]:
    """(yaw, pitch, roll) in the user's terms: + turned to their right, + looking up, + tilted to their right."""
    if mirrored:
        return -m.yaw, m.pitch, -m.roll
    return m.yaw, m.pitch, m.roll
