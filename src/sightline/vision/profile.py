"""Per-user vision data in the profile folder: gaze model, calibration samples, eye and head profiles.

Layout (``paths.profile_dir(name) / "vision"``)::

    gaze_model.npz, gaze_model.json   the active gaze model
    gaze_samples.npz                   calibration frames (groups >= 0) and click samples (group -1)
    eyes.json                          per-eye closure models and wink ability
    head.json                          head pointer gains
    face_gestures.json                 face gesture thresholds from the trainer

Everything is NumPy ``.npz`` (loaded with ``allow_pickle=False``) or JSON:
loading a pickle can run code, and profile folders get copied between machines.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sightline.core import paths

from .eyes import EyeProfile
from .face_gestures import FaceGestureProfile
from .gaze_model import GazeModel, TrainingData
from .head import HeadProfile

log = logging.getLogger(__name__)


class VisionProfile:
    def __init__(self, name: str = "default", folder: Path | None = None) -> None:
        self.name = name
        self._folder = folder

    @property
    def folder(self) -> Path:
        if self._folder is not None:
            self._folder.mkdir(parents=True, exist_ok=True)
            return self._folder
        folder = paths.profile_dir(self.name) / "vision"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    # ------------------------------------------------------------------ gaze

    def load_gaze(self) -> tuple[GazeModel | None, TrainingData | None, TrainingData | None]:
        """(model, calibration samples, click samples).  Missing or unreadable parts come back as None."""
        model = GazeModel.load(self.folder)
        cal = clicks = None
        path = self.folder / "gaze_samples.npz"
        if path.exists():
            try:
                data = TrainingData.load(path)
                cal = data.subset(data.groups >= 0)
                clicks = data.subset(data.groups < 0)
            except (OSError, ValueError, KeyError) as e:
                log.warning("Could not read saved calibration samples: %s", e)
        return model, cal, clicks

    def save_gaze(self, model: GazeModel, calibration: TrainingData | None, clicks: TrainingData | None) -> None:
        model.save(self.folder)
        parts = [d for d in (calibration, clicks) if d is not None and len(d)]
        if parts:
            data = parts[0]
            for extra in parts[1:]:
                data = data.concat(extra)
            data.save(self.folder / "gaze_samples.npz")

    def clear_gaze(self) -> None:
        for name in ("gaze_model.npz", "gaze_model.json", "gaze_samples.npz"):
            (self.folder / name).unlink(missing_ok=True)

    # ------------------------------------------------------------------ eyes and head

    def load_eyes(self) -> EyeProfile | None:
        return EyeProfile.load(self.folder / "eyes.json")

    def save_eyes(self, profile: EyeProfile) -> None:
        profile.save(self.folder / "eyes.json")

    def load_head(self) -> HeadProfile | None:
        return HeadProfile.load(self.folder / "head.json")

    def save_head(self, profile: HeadProfile) -> None:
        profile.save(self.folder / "head.json")

    def load_face(self) -> FaceGestureProfile | None:
        return FaceGestureProfile.load(self.folder / "face_gestures.json")

    def save_face(self, profile: FaceGestureProfile) -> None:
        profile.save(self.folder / "face_gestures.json")
