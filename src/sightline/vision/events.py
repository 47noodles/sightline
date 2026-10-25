"""Vision's private events: diagnostics for tools, logs and an optional diagnostics page.

Other subsystems should not depend on these; anything they need lives in
``sightline.core.events``.  ``CalibrationActive`` and ``GazeModelChanged``
moved there and are re-exported here for existing imports.
"""

from __future__ import annotations

from dataclasses import dataclass

from sightline.core.events import CalibrationActive, GazeModelChanged

__all__ = ["CalibrationActive", "GazeModelChanged", "VisionDiagnostics"]


@dataclass(frozen=True, slots=True)
class VisionDiagnostics:
    """Published about twice a second while the camera runs."""

    t: float
    fps: float
    landmark_ms: float  # MediaPipe time per frame
    pipeline_ms: float  # everything after MediaPipe
    camera: str
    face: bool
    closure_left: float
    closure_right: float
    blink_left: float  # raw eyeBlink blendshapes, the user's own eyes
    blink_right: float
    ear_ratio_left: float  # eye aspect ratio over its open level
    ear_ratio_right: float
    gaze_noise_px: float | None
    fixation_radius_px: float | None
    head_speed: float
    yielding: bool
    model: str  # e.g. "head, 13 targets, 41 px", or "none"
    eyes_profile: bool
    natural_blinks: int
    natural_blink_ms: float | None
    reading: bool = False  # the pointer is held because the user is reading
    reading_scroll: str | None = None  # "down" or "up" while the reading scroll runs
