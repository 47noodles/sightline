"""Was the user facing the screen, and was their mouth moving?  Speech asks this about each utterance.

A short ring of per-frame records (about 15 seconds) is kept on the vision
thread; ``query`` turns the records in a time window into fractions.

* Facing: head yaw and pitch within ``FACING_DEG`` of the user's neutral
  pose (the head tracker's neutral, which "recenter" resets).
* Mouth moving: the jaw opening (``jawOpen``), the lip closure
  (``mouthClose``) or the gap between the inner lips changed noticeably
  compared with about ``MOUTH_LAG_S`` earlier (each signal is the median of
  the last three frames, so a one-frame flicker does not count).  Speech
  moves the jaw and lips several times a second; a still face changes these
  by a few hundredths at most.  None when the face model gave no
  blendshapes.

The thresholds are first guesses from the size of MediaPipe's frame to
frame noise, not measured on talking people yet.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass

import numpy as np

from sightline.core.types import Attention

from .landmarks import BS, FaceFrame

FACING_DEG = 25.0
HISTORY_S = 15.0
MOUTH_LAG_S = 0.1
JAW_STEP = 0.06  # change in jawOpen or mouthClose (0..1) that counts as moving
GAP_STEP = 0.03  # change in the inner lip gap, in outer eye corner distances (about 3 mm)
UPPER_LIP, LOWER_LIP = 13, 14  # inner lip midpoints

_JAW = BS["jawOpen"]
_CLOSE = BS["mouthClose"]


@dataclass(slots=True)
class _Record:
    t: float
    face: bool
    facing: bool
    mouth: bool | None
    jaw: float = math.nan
    close: float = math.nan
    gap: float = math.nan


def mouth_signals(face: FaceFrame, iod_px: float) -> tuple[float, float, float] | None:
    """(jawOpen, mouthClose, inner lip gap in eye-corner distances), or None without blendshapes."""
    bs = face.blendshapes
    if bs is None or len(bs) <= max(_JAW, _CLOSE) or not np.any(bs > 0):
        return None
    p = face.landmarks
    gap_px = math.hypot((p[UPPER_LIP, 0] - p[LOWER_LIP, 0]) * face.width, (p[UPPER_LIP, 1] - p[LOWER_LIP, 1]) * face.height)
    return float(bs[_JAW]), float(bs[_CLOSE]), gap_px / max(iod_px, 1.0)


class AttentionTracker:
    """Written by the vision thread, read from any thread."""

    def __init__(self, history_s: float = HISTORY_S) -> None:
        self.history_s = history_s
        self._lock = threading.Lock()
        self._recs: deque[_Record] = deque()
        self._raw: deque[tuple[float, float, float]] = deque(maxlen=3)  # vision thread only

    def clear(self) -> None:
        with self._lock:
            self._recs.clear()
        self._raw.clear()

    def lost(self, t: float) -> None:
        self._raw.clear()
        self._add(_Record(t, False, False, None))

    def update(self, t: float, yaw: float, pitch: float, face: FaceFrame, iod_px: float) -> None:
        self.update_signals(t, yaw, pitch, mouth_signals(face, iod_px))

    def update_signals(self, t: float, yaw: float, pitch: float, signals: tuple[float, float, float] | None) -> None:
        """One frame with a face, from already measured mouth signals (the gesture trainer replays these)."""
        facing = math.hypot(yaw, pitch) <= FACING_DEG
        if signals is None:
            self._raw.clear()
            self._add(_Record(t, True, facing, None))
            return
        # Median of the last three frames: a one-frame flicker of the face model is not a mouth movement.
        self._raw.append(signals)
        jaw, close, gap = (_median(column) for column in zip(*self._raw))
        with self._lock:
            prev = self._before(t - MOUTH_LAG_S)
        mouth: bool | None = None
        if prev is not None:
            mouth = abs(jaw - prev.jaw) > JAW_STEP or abs(close - prev.close) > JAW_STEP or abs(gap - prev.gap) > GAP_STEP
        self._add(_Record(t, True, facing, mouth, jaw, close, gap))

    def _before(self, t: float) -> _Record | None:
        """The newest record with mouth signals at or before ``t``, if it is recent enough to compare with."""
        for rec in reversed(self._recs):
            if rec.t <= t:
                if t - rec.t > MOUTH_LAG_S or math.isnan(rec.jaw):
                    return None  # a gap in tracking: nothing to compare with
                return rec
        return None

    def _add(self, rec: _Record) -> None:
        with self._lock:
            self._recs.append(rec)
            while self._recs and rec.t - self._recs[0].t > self.history_s:
                self._recs.popleft()

    def query(self, t0: float, t1: float) -> Attention:
        """Fractions of the frames between ``t0`` and ``t1``.  Never waits long: an empty answer if busy."""
        if not self._lock.acquire(timeout=0.05):
            return Attention(0.0, 0.0, None, 0)
        try:
            recs = [r for r in self._recs if t0 <= r.t <= t1]
        finally:
            self._lock.release()
        return summarise(recs)

    def talking(self, t0: float, t1: float, share: float = 0.25) -> bool:
        """Was the mouth moving in at least ``share`` of the measurable frames between ``t0`` and ``t1``?"""
        a = self.query(t0, t1)
        return a.mouth is not None and a.mouth >= share


def _median(values: tuple[float, ...]) -> float:
    """Median of one to three numbers (NumPy's costs more than the rest of the frame's attention work)."""
    v = sorted(values)
    n = len(v)
    return float(v[n // 2]) if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])


def summarise(recs: list[_Record]) -> Attention:
    n = len(recs)
    if n == 0:
        return Attention(0.0, 0.0, None, 0)
    measured = [r.mouth for r in recs if r.mouth is not None]
    return Attention(
        face=sum(r.face for r in recs) / n,
        facing=sum(r.facing for r in recs) / n,
        mouth=(sum(measured) / len(measured)) if measured else None,
        frames=n,
    )
