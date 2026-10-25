"""Recording face tracking data for offline tuning, and reading it back.

A recording is a folder in ``paths.recordings_dir()``::

    meta.json         camera, screen layout, settings, labels (what the user was asked to do), events
    frames-000.npz    per-frame timestamps, landmarks, blendshapes, transform (one file per minute)
    video.avi         optional, MJPG

Only numbers derived from the face are stored unless video is asked for, and
everything stays on this computer.  ``tools/vision_replay.py`` runs a
recording through the same pipeline the app uses.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from sightline import __version__

from .landmarks import N_BLENDSHAPES, N_LANDMARKS, FaceFrame

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
CHUNK_FRAMES = 1800  # one file a minute at 30 fps, so a crash loses at most a minute


class Recorder:
    def __init__(self, folder: Path, meta: dict[str, Any] | None = None, *, video_fps: float | None = None) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.meta: dict[str, Any] = {
            "format": FORMAT_VERSION,
            "app_version": __version__,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "labels": [],
            "events": [],
        }
        self.meta.update(meta or {})
        self._video_fps = video_fps
        self._writer = None
        self._chunk = 0
        self._reset_buffers()
        self.frames = 0

    def _reset_buffers(self) -> None:
        self._t: list[float] = []
        self._present: list[bool] = []
        self._lm: list[np.ndarray] = []
        self._bs: list[np.ndarray] = []
        self._mx: list[np.ndarray] = []
        self._size: list[tuple[int, int]] = []

    def add(self, t: float, face: FaceFrame | None, image: np.ndarray | None = None, size: tuple[int, int] = (0, 0)) -> None:
        self._t.append(t)
        self._present.append(face is not None)
        if face is not None:
            self._lm.append(face.landmarks.astype(np.float32))
            self._bs.append(face.blendshapes.astype(np.float32))
            self._mx.append(face.matrix.astype(np.float32))
            self._size.append((face.width, face.height))
        else:
            self._lm.append(np.zeros((N_LANDMARKS, 3), np.float32))
            self._bs.append(np.zeros(N_BLENDSHAPES, np.float32))
            self._mx.append(np.eye(4, dtype=np.float32))
            self._size.append(size if image is None else (image.shape[1], image.shape[0]))
        if image is not None and self._video_fps:
            self._write_video(image)
        self.frames += 1
        if len(self._t) >= CHUNK_FRAMES:
            self._flush()

    def label(self, t: float, text: str) -> None:
        """What the user was asked to do from time ``t`` on (guided recordings)."""
        self.meta["labels"].append([t, text])

    def event(self, t: float, kind: str, **data: Any) -> None:
        self.meta["events"].append({"t": t, "kind": kind, **data})

    def _write_video(self, image: np.ndarray) -> None:
        import cv2

        if self._writer is None:
            h, w = image.shape[:2]
            self._writer = cv2.VideoWriter(
                str(self.folder / "video.avi"), cv2.VideoWriter_fourcc(*"MJPG"), float(self._video_fps), (w, h)
            )
        self._writer.write(image)

    def _flush(self) -> None:
        if not self._t:
            return
        path = self.folder / f"frames-{self._chunk:03d}.npz"
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as fh:
            np.savez_compressed(
                fh,
                t=np.array(self._t, dtype=np.float64),
                present=np.array(self._present, dtype=bool),
                landmarks=np.stack(self._lm),
                blendshapes=np.stack(self._bs),
                matrix=np.stack(self._mx),
                size=np.array(self._size, dtype=np.int32),
            )
        os.replace(tmp, path)
        self._chunk += 1
        self._reset_buffers()
        self._write_meta()

    def _write_meta(self) -> None:
        self.meta["frames"] = self.frames
        tmp = self.folder / "meta.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=2), "utf-8")
        os.replace(tmp, self.folder / "meta.json")

    def close(self) -> None:
        self._flush()
        self._write_meta()
        if self._writer is not None:
            self._writer.release()
            self._writer = None


@dataclass
class Recording:
    folder: Path
    meta: dict[str, Any]
    t: np.ndarray
    present: np.ndarray
    landmarks: np.ndarray
    blendshapes: np.ndarray
    matrix: np.ndarray
    size: np.ndarray
    labels: list[tuple[float, str]] = field(default_factory=list)

    @classmethod
    def load(cls, folder: Path) -> Recording:
        folder = Path(folder)
        meta = json.loads((folder / "meta.json").read_text("utf-8"))
        if int(meta.get("format", 0)) > FORMAT_VERSION:
            raise ValueError("this recording was made by a newer version of Sightline")
        chunks = sorted(folder.glob("frames-*.npz"))
        if not chunks:
            raise ValueError(f"no frames in {folder}")
        parts: dict[str, list[np.ndarray]] = {k: [] for k in ("t", "present", "landmarks", "blendshapes", "matrix", "size")}
        for path in chunks:
            with np.load(path, allow_pickle=False) as z:
                for k in parts:
                    parts[k].append(z[k])
        arrays = {k: np.concatenate(v) for k, v in parts.items()}
        labels = [(float(t), str(text)) for t, text in meta.get("labels", [])]
        return cls(folder, meta, labels=labels, **arrays)

    def __len__(self) -> int:
        return len(self.t)

    @property
    def duration(self) -> float:
        return float(self.t[-1] - self.t[0]) if len(self.t) > 1 else 0.0

    def frames(self) -> Iterator[tuple[float, FaceFrame | None]]:
        for i in range(len(self.t)):
            t = float(self.t[i])
            if not self.present[i]:
                yield t, None
                continue
            w, h = (int(v) for v in self.size[i])
            yield t, FaceFrame(t, self.landmarks[i], self.blendshapes[i], self.matrix[i], w, h)

    def label_at(self, t: float) -> str | None:
        current = None
        for lt, text in self.labels:
            if lt <= t:
                current = text
            else:
                break
        return current
