"""Cameras: finding them, opening them, and noticing when they misbehave.

* Enumeration uses ``cv2-enumerate-cameras`` (MIT) for real device names
  through both Media Foundation and DirectShow, merged by device path or name.
  Virtual cameras (OBS, VR headsets, effects apps) are labelled as such and
  ranked after physical ones: they are common, and an idle one gives a black
  or frozen picture that would otherwise look like "no face".
* Capture tries Media Foundation first (it handles high resolutions well) and
  falls back to DirectShow (which also sees virtual cameras).  A grabber
  thread keeps only the newest frame, so a slow frame never builds a backlog
  and the pointer always works from the freshest image.
* ``CameraSupervisor`` reopens a camera that was unplugged or taken by
  another app, backing off from 1 to 10 seconds, and turns every failure
  into a short plain-English hint.
* ``VideoFileSource`` and ``ArraySource`` stand in for a camera behind the
  same interface (tests, demos, replays).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import numpy as np

# Media Foundation can take ten seconds or more to open a camera with hardware transforms on.
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

import cv2  # noqa: E402  (the variable above must be set first)

log = logging.getLogger(__name__)

HINT_NOT_FOUND = "Camera not found"
HINT_BUSY = "Another app is using the camera"
HINT_BLACK = "The camera picture is black. Is the camera covered?"
HINT_FROZEN = "The camera picture is not changing. Try another camera."
HINT_STARTING = "Starting the camera"

_VIRTUAL_WORDS = (
    "virtual",
    "obs",
    "quest",
    "oculus",
    "snap camera",
    "manycam",
    "xsplit",
    "vcam",
    "splitcam",
    "e2esoft",
    "chromacam",
    "dummy",
)


class CameraError(Exception):
    def __init__(self, kind: Literal["not_found", "busy", "failed"], message: str = "") -> None:
        super().__init__(message or kind)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class CameraDevice:
    name: str
    label: str  # shown to the user
    msmf_index: int | None = None
    dshow_index: int | None = None
    path: str | None = None
    virtual: bool = False


# --------------------------------------------------------------------------- enumeration


@dataclass(frozen=True, slots=True)
class _Raw:
    index: int
    name: str
    path: str | None
    vid: int | None


def _enumerate(api: int) -> list[_Raw]:
    try:
        from cv2_enumerate_cameras import enumerate_cameras
    except Exception:  # pragma: no cover - optional dependency missing
        log.debug("cv2_enumerate_cameras not available", exc_info=True)
        return []
    try:
        return [_Raw(int(c.index), str(c.name), c.path or None, c.vid) for c in enumerate_cameras(api)]
    except Exception:
        log.warning("Camera enumeration failed for backend %s", api, exc_info=True)
        return []


def _looks_virtual(name: str) -> bool:
    low = name.lower()
    return any(word in low for word in _VIRTUAL_WORDS)


def _path_key(path: str | None) -> str:
    """The device-instance part of a device path.

    Media Foundation and DirectShow can name the same webcam with different
    interface class GUIDs at the end (the ``#{...}`` part), so only the part
    before that is compared.
    """
    if not path:
        return ""
    low = path.lower().lstrip("\\?")
    return low.split("#{", 1)[0]


def merge_devices(msmf: Sequence[_Raw], dshow: Sequence[_Raw]) -> list[CameraDevice]:
    """One entry per physical device, physical cameras first, virtual ones labelled."""
    merged: list[dict] = []
    for c in msmf:
        merged.append({"name": c.name, "msmf": c.index, "dshow": None, "path": c.path, "vid": c.vid})
    for c in dshow:
        free = [m for m in merged if m["dshow"] is None]
        # The device path identifies a device exactly; the name only does when there is one of a kind.
        key = _path_key(c.path)
        match = next((m for m in free if key and _path_key(m["path"]) == key), None)
        if match is None:
            named = [m for m in free if m["name"] == c.name]
            if len(named) == 1 or (named and not key):
                match = named[0]
        if match is not None:
            match["dshow"] = c.index
            match["path"] = match["path"] or c.path
            match["vid"] = match["vid"] or c.vid
        else:
            merged.append({"name": c.name, "msmf": None, "dshow": c.index, "path": c.path, "vid": c.vid})
    devices: list[CameraDevice] = []
    seen: dict[str, int] = {}
    for m in merged:
        # DirectShow-only devices with no hardware path are software cameras.
        virtual = _looks_virtual(m["name"]) or (m["msmf"] is None and not m["path"] and not m["vid"])
        seen[m["name"]] = seen.get(m["name"], 0) + 1
        label = m["name"] if seen[m["name"]] == 1 else f"{m['name']} {seen[m['name']]}"
        if virtual:
            label += " (virtual camera)"
        devices.append(CameraDevice(m["name"], label, m["msmf"], m["dshow"], m["path"], virtual))
    devices.sort(key=lambda d: d.virtual)  # stable: keeps Windows' order within each kind
    return devices


def list_cameras() -> list[CameraDevice]:
    devices = merge_devices(_enumerate(cv2.CAP_MSMF), _enumerate(cv2.CAP_DSHOW))
    if not devices:
        devices = _probe_indices()
    return devices


def _probe_indices(limit: int = 4) -> list[CameraDevice]:
    """Fallback without the enumeration package: try the first few DirectShow indices."""
    found = []
    for i in range(limit):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        try:
            if cap.isOpened():
                found.append(CameraDevice(f"Camera {i + 1}", f"Camera {i + 1}", None, i))
        finally:
            cap.release()
    return found


def choose_camera(devices: Sequence[CameraDevice], name: str = "") -> CameraDevice | None:
    """The saved camera (by name, since indices change when devices come and go), else the first physical one."""
    if not devices:
        return None
    if name:
        for d in devices:
            if d.name == name or d.label == name:
                return d
    for d in devices:
        if not d.virtual:
            return d
    return devices[0]


# --------------------------------------------------------------------------- sources


class FrameSource(Protocol):
    description: str

    def open(self) -> None:
        """Raise CameraError when it cannot deliver frames."""

    def read(self, timeout: float = 1.0) -> tuple[np.ndarray, float] | None:
        """The next BGR frame and its capture time, None on timeout or end of a file.  Raises CameraError on failure."""

    def close(self) -> None: ...


class OpenCVCamera:
    def __init__(self, device: CameraDevice, width: int = 1280, height: int = 720, fps: int = 30) -> None:
        self.device = device
        self.size = (int(width), int(height))
        self.fps = int(fps)
        self.description = device.label
        self.backend = ""
        self._cap: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._cond = threading.Condition()
        self._latest: tuple[np.ndarray, float] | None = None
        self._seq = 0
        self._read_seq = 0
        self._failed = False
        self._stop = threading.Event()

    def open(self) -> None:
        attempts = []
        if self.device.msmf_index is not None:
            attempts.append((cv2.CAP_MSMF, self.device.msmf_index, "Media Foundation"))
        if self.device.dshow_index is not None:
            attempts.append((cv2.CAP_DSHOW, self.device.dshow_index, "DirectShow"))
        if not attempts:
            raise CameraError("not_found", f"{self.device.label} has no usable index")
        for api, index, backend in attempts:
            cap = cv2.VideoCapture(index, api)
            if not cap.isOpened():
                cap.release()
                log.info("%s: could not open with %s", self.device.label, backend)
                continue
            self._configure(cap)
            frame = self._first_frame(cap, 3.0)
            if frame is None:
                cap.release()
                log.info("%s: no picture from %s", self.device.label, backend)
                continue
            h, w = frame.shape[:2]
            log.info("Camera %s open with %s at %dx%d", self.device.label, backend, w, h)
            self.backend = backend
            self._cap = cap
            self._failed = False
            self._stop.clear()
            self._thread = threading.Thread(target=self._grab, name="sightline-camera", daemon=True)
            self._thread.start()
            return
        # The device is listed but will not give pictures: almost always another app holding it.
        raise CameraError("busy", f"{self.device.label} would not start")

    def _configure(self, cap: cv2.VideoCapture) -> None:
        w, h = self.size
        # MJPG lets USB 2 cameras deliver 720p at full frame rate.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    @staticmethod
    def _first_frame(cap: cv2.VideoCapture, timeout: float) -> np.ndarray | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ok, frame = cap.read()
            if ok and frame is not None and frame.ndim == 3 and frame.size:
                return frame
            time.sleep(0.05)
        return None

    def _grab(self) -> None:
        failures = 0
        cap = self._cap
        assert cap is not None
        while not self._stop.is_set():
            ok, frame = cap.read()
            t = time.perf_counter()
            if not ok or frame is None or frame.ndim != 3:
                failures += 1
                if failures > 30:
                    log.warning("Camera %s stopped delivering frames", self.device.label)
                    with self._cond:
                        self._failed = True
                        self._cond.notify_all()
                    return
                time.sleep(0.05)
                continue
            failures = 0
            with self._cond:
                self._latest = (frame, t)
                self._seq += 1
                self._cond.notify_all()

    def read(self, timeout: float = 1.0) -> tuple[np.ndarray, float] | None:
        with self._cond:
            if not self._cond.wait_for(lambda: self._seq != self._read_seq or self._failed, timeout):
                return None
            if self._failed:
                raise CameraError("failed", "camera stopped")
            self._read_seq = self._seq
            return self._latest

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class VideoFileSource:
    """Frames from a video file, timestamped as if live (tests, demos and replays)."""

    def __init__(
        self,
        path: Path,
        *,
        realtime: bool = False,
        loop: bool = False,
        fps: float | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.path = Path(path)
        self.realtime = realtime
        self.loop = loop
        self.description = f"Video {self.path.name}"
        self._fps = fps
        self._clock = clock
        self._cap: cv2.VideoCapture | None = None
        self._t0 = 0.0
        self._i = 0

    def open(self) -> None:
        cap = cv2.VideoCapture(str(self.path))
        if not cap.isOpened():
            cap.release()
            raise CameraError("not_found", f"cannot open {self.path}")
        self._cap = cap
        if self._fps is None:
            fps = cap.get(cv2.CAP_PROP_FPS)
            self._fps = fps if fps and fps > 1 else 30.0
        self._t0 = self._clock()
        self._i = 0

    def read(self, timeout: float = 1.0) -> tuple[np.ndarray, float] | None:
        if self._cap is None:
            raise CameraError("failed", "not open")
        ok, frame = self._cap.read()
        if not ok and self.loop:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._cap.read()
        if not ok or frame is None:
            return None
        assert self._fps is not None
        t = self._t0 + self._i / self._fps
        self._i += 1
        if self.realtime:
            wait = t - self._clock()
            if wait > 0:
                time.sleep(wait)
        return frame, t

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class ArraySource:
    """Frames from a list of images, for tests."""

    def __init__(self, frames: Sequence[np.ndarray], fps: float = 30.0, t0: float = 0.0, loop: bool = False) -> None:
        self.frames = list(frames)
        self.fps = fps
        self.t0 = t0
        self.loop = loop
        self.description = "Test frames"
        self._i = 0

    def open(self) -> None:
        self._i = 0

    def read(self, timeout: float = 1.0) -> tuple[np.ndarray, float] | None:
        if self._i >= len(self.frames):
            if not self.loop or not self.frames:
                return None
        frame = self.frames[self._i % len(self.frames)]
        t = self.t0 + self._i / self.fps
        self._i += 1
        return frame, t

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- health


class FrameHealth:
    """Notices a camera that delivers frames that are all black or never change (an idle virtual camera,
    a closed privacy shutter)."""

    def __init__(self, black_s: float = 1.5, frozen_s: float = 3.0, every: int = 3) -> None:
        self.black_s = black_s
        self.frozen_s = frozen_s
        self.every = every
        self._n = 0
        self._prev: np.ndarray | None = None
        self._black_since: float | None = None
        self._frozen_since: float | None = None
        self.brightness = 0.0
        self.hint: str | None = None

    def reset(self) -> None:
        self._n = 0
        self._prev = None
        self._black_since = None
        self._frozen_since = None
        self.hint = None

    def update(self, frame: np.ndarray, t: float) -> str | None:
        self._n += 1
        if self._n % self.every:
            return self.hint
        small = cv2.resize(frame, (32, 18), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
        self.brightness = float(gray.mean())
        if self.brightness < 12.0:
            self._black_since = self._black_since if self._black_since is not None else t
        else:
            self._black_since = None
        if self._prev is not None and float(np.abs(gray - self._prev).mean()) < 0.2:
            self._frozen_since = self._frozen_since if self._frozen_since is not None else t
        else:
            self._frozen_since = None
        self._prev = gray
        if self._black_since is not None and t - self._black_since >= self.black_s:
            self.hint = HINT_BLACK
        elif self._frozen_since is not None and t - self._frozen_since >= self.frozen_s:
            self.hint = HINT_FROZEN
        else:
            self.hint = None
        return self.hint


# --------------------------------------------------------------------------- supervisor


class CameraSupervisor:
    """Keeps a frame source open: reopens after failures with backoff and reports a hint while it cannot."""

    def __init__(
        self,
        make_source: Callable[[], FrameSource | None],
        *,
        clock: Callable[[], float] = time.perf_counter,
        backoff: Sequence[float] = (1.0, 2.0, 4.0, 8.0, 10.0),
    ) -> None:
        self._make = make_source
        self._clock = clock
        self._backoff = list(backoff)
        self._source: FrameSource | None = None
        self._attempt = 0
        self._next_try = 0.0
        self._reopen = threading.Event()
        self.hint: str | None = HINT_STARTING
        self.description = ""
        self.ended = False  # a file source ran out

    def request_reopen(self) -> None:
        """Settings changed (camera, size, frame rate): close and open again on the next read."""
        self._reopen.set()

    def close(self) -> None:
        if self._source is not None:
            try:
                self._source.close()
            except Exception:
                log.debug("source close failed", exc_info=True)
            self._source = None

    def read(self, stop: threading.Event, timeout: float = 1.0) -> tuple[np.ndarray, float] | None:
        if self._reopen.is_set():
            self._reopen.clear()
            self.close()
            self._attempt = 0
            self._next_try = 0.0
        if self._source is None:
            now = self._clock()
            if now < self._next_try:
                stop.wait(min(0.25, self._next_try - now))
                return None
            if not self._open():
                return None
        assert self._source is not None
        try:
            got = self._source.read(timeout)
        except CameraError as e:
            log.warning("Camera failed (%s); reopening", e)
            self.close()
            self._fail("busy" if e.kind != "not_found" else "not_found")
            return None
        if got is None and isinstance(self._source, (VideoFileSource, ArraySource)):
            self.ended = True
        return got

    def _open(self) -> bool:
        try:
            source = self._make()
        except Exception:
            log.exception("Could not create the camera source")
            source = None
        if source is None:
            self._fail("not_found")
            return False
        try:
            source.open()
        except CameraError as e:
            self._fail(e.kind)
            return False
        except Exception:
            log.exception("Camera open failed")
            self._fail("busy")
            return False
        self._source = source
        self._attempt = 0
        self.hint = None
        self.description = source.description
        return True

    def _fail(self, kind: str) -> None:
        self.hint = HINT_NOT_FOUND if kind == "not_found" else HINT_BUSY
        delay = self._backoff[min(self._attempt, len(self._backoff) - 1)]
        self._attempt += 1
        self._next_try = self._clock() + delay
        log.info("Camera unavailable (%s); trying again in %.0f s", kind, delay)


def silence_opencv_logs() -> None:
    """OpenCV prints a warning to the console for every failed open attempt; keep only errors."""
    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except Exception:  # pragma: no cover - older builds
        pass
