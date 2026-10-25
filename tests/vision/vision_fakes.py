"""A camera made of test images and a face model made of the synthetic face, for service tests.

The first pixel of each test image says what the synthetic user is doing,
so the real vision thread can run without hardware or MediaPipe.
"""

from __future__ import annotations

import time

import numpy as np
from vision_synth import SyntheticUser

from sightline.vision.camera import ArraySource

NO_FACE, OPEN, CLOSED = 0, 1, 2


def image(kind: int) -> np.ndarray:
    img = np.full((240, 320, 3), 90, np.uint8)
    img[0, 0, 0] = kind
    return img


class FakeLandmarker:
    """Stands in for MediaPipe: reads the first pixel of each test frame."""

    def __init__(self, user: SyntheticUser) -> None:
        self.user = user
        self.last_ms = 2.0
        self.calls = 0
        self.closed = False

    def detect(self, bgr: np.ndarray, t: float):
        self.calls += 1
        kind = int(bgr[0, 0, 0])
        if kind == NO_FACE:
            return None
        closure = (1.0, 1.0) if kind == CLOSED else (0.0, 0.0)
        return self.user.look(t, 960, 540, closure=closure)

    def close(self) -> None:
        self.closed = True


class PacedSource(ArraySource):
    """An endless test camera.  ``delay`` per frame keeps it from spinning a CPU core (1/30 for real time)."""

    def __init__(self, frames, t0: float = 0.0, delay: float = 0.003) -> None:
        super().__init__(frames, fps=30.0, t0=t0, loop=True)
        self.delay = delay
        self.reads = 0

    def read(self, timeout: float = 1.0):
        time.sleep(self.delay)
        self.reads += 1
        return super().read(timeout)


def wait_for(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()
