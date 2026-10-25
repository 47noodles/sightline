"""Model files: declaration, download, verification.

Each subsystem lists its models in its own ``models.py`` as ``ModelSpec``
values and registers them with the ``ModelManager`` at startup.  Files land
in ``<data_dir>/models/<model id>/``.  Every file is pinned by SHA-256, so a
changed or truncated upstream file is refused rather than loaded.

Archives (``.tar.bz2``, ``.tar.gz``, ``.zip``) are extracted with path
checks after the hash matches.  A ``.ready`` marker records a completed,
verified model so later starts skip the hashing.

A download is refused as soon as it is larger than the file's declared size
plus a small margin, or the server says it will be, so a misbehaving server
cannot fill the disk before the hash check fails.  Redirects are followed
only to https addresses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .bus import EventBus
from .events import ModelProgress

log = logging.getLogger(__name__)

_CHUNK = 256 * 1024
_USER_AGENT = "Sightline/0.1 (+https://github.com/; model downloader)"
SIZE_MARGIN_BYTES = 64 * 1024  # the declared sizes are exact; this only forgives a spec that is a little off
SIZE_MARGIN_FRACTION = 0.01


class ModelError(Exception):
    pass


def size_limit(size: int) -> int:
    """The most bytes accepted for a file declared as ``size`` bytes."""
    return int(size) + max(SIZE_MARGIN_BYTES, int(size * SIZE_MARGIN_FRACTION))


class HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to another https address; anything else ends the download."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201 - urllib's signature
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise ModelError(f"the download server sent Sightline to an address that is not https ({urllib.parse.urlsplit(newurl).scheme or 'none'})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(request: urllib.request.Request, timeout: float):  # noqa: ANN202 - a urllib response
    """Open a model URL: https only, and redirects only to https."""
    if urllib.parse.urlsplit(request.full_url).scheme.lower() != "https":
        raise ModelError("model files are only downloaded over https")
    opener = urllib.request.build_opener(HttpsOnlyRedirects())
    return opener.open(request, timeout=timeout)


@dataclass(frozen=True, slots=True)
class ModelFile:
    url: str
    path: str  # file name inside the model folder (for archives: the archive name)
    sha256: str  # lowercase hex; required, downloads are always verified
    size: int  # bytes, used for progress and a cheap presence check
    extract: bool = False  # extract this archive into the model folder after verifying it
    keep_archive: bool = False


@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str  # folder name, e.g. "face_landmarker"
    title: str  # shown in the setup screen, e.g. "Face and eye tracking model"
    files: tuple[ModelFile, ...]
    license: str  # SPDX id or short licence name
    homepage: str = ""
    required: bool = True  # optional models download on demand
    expect: tuple[str, ...] = field(default=())  # paths that must exist after extraction

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


ProgressFn = Callable[[ModelProgress], None]


class ModelManager:
    def __init__(self, bus: EventBus | None = None, root: Path | None = None) -> None:
        self._bus = bus
        self._root = root
        self._specs: dict[str, ModelSpec] = {}
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root or paths.models_dir()

    # ------------------------------------------------------------------ registry

    def register(self, spec: ModelSpec) -> None:
        with self._lock:
            existing = self._specs.get(spec.id)
            if existing is not None and existing != spec:
                raise ValueError(f"model {spec.id!r} registered twice with different contents")
            self._specs[spec.id] = spec

    def register_all(self, specs: Iterable[ModelSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def spec(self, model_id: str) -> ModelSpec:
        return self._specs[model_id]

    def specs(self) -> list[ModelSpec]:
        return list(self._specs.values())

    # ------------------------------------------------------------------ paths

    def dir(self, model_id: str) -> Path:
        return self.root / model_id

    def path(self, model_id: str, name: str | None = None) -> Path:
        """Path of a model file.  Without ``name``, the first file (or the folder for archives)."""
        spec = self._specs[model_id]
        if name is not None:
            return self.dir(model_id) / name
        first = spec.files[0]
        return self.dir(model_id) if first.extract else self.dir(model_id) / first.path

    def is_ready(self, model_id: str) -> bool:
        spec = self._specs.get(model_id)
        if spec is None:
            return False
        marker = self.dir(model_id) / ".ready"
        if not marker.exists():
            return False
        try:
            recorded = json.loads(marker.read_text("utf-8"))
        except (OSError, ValueError):
            return False
        if recorded.get("files") != [f.sha256 for f in spec.files]:
            return False  # spec changed since this copy was verified
        for f in spec.files:
            if not f.extract and not (self.dir(model_id) / f.path).exists():
                return False
        return all((self.dir(model_id) / p).exists() for p in spec.expect)

    def missing(self, ids: Iterable[str] | None = None) -> list[ModelSpec]:
        chosen = self._select(ids)
        return [s for s in chosen if not self.is_ready(s.id)]

    def missing_bytes(self, ids: Iterable[str] | None = None) -> int:
        return sum(s.total_bytes for s in self.missing(ids))

    def _select(self, ids: Iterable[str] | None) -> list[ModelSpec]:
        if ids is None:
            return [s for s in self._specs.values() if s.required]
        return [self._specs[i] for i in ids]

    # ------------------------------------------------------------------ download

    def ensure(
        self,
        ids: Iterable[str] | None = None,
        *,
        on_progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        """Download and verify every missing model in ``ids`` (default: all required ones).

        Blocks; call from a worker thread.  Raises ModelError on failure or cancel.
        """
        for spec in self.missing(ids):
            self._emit(on_progress, ModelProgress(spec.id, "queued", 0, spec.total_bytes))
        for spec in self.missing(ids):
            try:
                self._fetch_spec(spec, on_progress, cancel)
            except ModelError as e:
                self._emit(on_progress, ModelProgress(spec.id, "error", 0, spec.total_bytes, str(e)))
                raise
            except Exception as e:
                self._emit(on_progress, ModelProgress(spec.id, "error", 0, spec.total_bytes, str(e)))
                raise ModelError(f"{spec.title}: {e}") from e
            self._emit(on_progress, ModelProgress(spec.id, "ready", spec.total_bytes, spec.total_bytes))

    def _fetch_spec(self, spec: ModelSpec, on_progress: ProgressFn | None, cancel: threading.Event | None) -> None:
        folder = self.dir(spec.id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ".ready").unlink(missing_ok=True)
        done_before = 0
        for f in spec.files:
            target = folder / f.path
            if not (target.exists() and target.stat().st_size == f.size and _sha256(target) == f.sha256):
                self._download(spec, f, target, done_before, on_progress, cancel)
            if f.extract:
                self._emit(on_progress, ModelProgress(spec.id, "extracting", done_before + f.size, spec.total_bytes))
                _safe_extract(target, folder)
                if not f.keep_archive:
                    target.unlink(missing_ok=True)
            done_before += f.size
        for rel in spec.expect:
            if not (folder / rel).exists():
                raise ModelError(f"{spec.title}: expected file {rel} missing after download")
        (folder / ".ready").write_text(
            json.dumps({"files": [f.sha256 for f in spec.files], "time": time.time()}), "utf-8"
        )

    def _download(
        self,
        spec: ModelSpec,
        f: ModelFile,
        target: Path,
        done_before: int,
        on_progress: ProgressFn | None,
        cancel: threading.Event | None,
    ) -> None:
        part = target.with_name(target.name + ".part")
        limit = size_limit(f.size)
        last_error: Exception | None = None
        for attempt in range(3):
            if cancel is not None and cancel.is_set():
                raise ModelError("download cancelled")
            try:
                digest = hashlib.sha256()
                received = 0
                last_emit = 0.0
                request = urllib.request.Request(f.url, headers={"User-Agent": _USER_AGENT})
                with _open_url(request, timeout=30) as response, part.open("wb") as out:
                    declared = _content_length(response)
                    if declared is not None and declared > limit:
                        raise ModelError(f"{f.path}: the server offered {declared} bytes, more than the {f.size} expected")
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise ModelError("download cancelled")
                        chunk = response.read(_CHUNK)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > limit:
                            raise ModelError(f"{f.path}: the server sent more than the {f.size} bytes expected")
                        out.write(chunk)
                        digest.update(chunk)
                        now = time.monotonic()
                        if now - last_emit > 0.1:
                            last_emit = now
                            self._emit(
                                on_progress,
                                ModelProgress(spec.id, "downloading", done_before + received, spec.total_bytes),
                            )
                self._emit(on_progress, ModelProgress(spec.id, "verifying", done_before + received, spec.total_bytes))
                actual = digest.hexdigest()
                if actual != f.sha256:
                    raise ModelError(f"{f.path}: checksum mismatch (got {actual[:12]}..., expected {f.sha256[:12]}...)")
                os.replace(part, target)
                return
            except ModelError:
                part.unlink(missing_ok=True)
                raise
            except Exception as e:  # network trouble: retry with backoff
                last_error = e
                part.unlink(missing_ok=True)
                log.warning("Download of %s failed (attempt %d): %s", f.url, attempt + 1, e)
                time.sleep(1.5 * (attempt + 1))
        raise ModelError(f"could not download {f.path}: {last_error}")

    def _emit(self, on_progress: ProgressFn | None, event: ModelProgress) -> None:
        if on_progress is not None:
            try:
                on_progress(event)
            except Exception:
                log.exception("progress callback failed")
        if self._bus is not None:
            self._bus.publish(event)


def _content_length(response: object) -> int | None:
    """The size the server says it will send, when it says one."""
    headers = getattr(response, "headers", None)
    value = headers.get("Content-Length") if headers is not None else None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    """Public helper for pinning new model files."""
    return _sha256(path)


def _safe_extract(archive: Path, dest: Path) -> None:
    dest_resolved = dest.resolve()
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                out = (dest / member.filename).resolve()
                if not out.is_relative_to(dest_resolved):
                    raise ModelError(f"unsafe path in archive: {member.filename}")
            zf.extractall(dest)
        return
    if name.endswith((".tar.bz2", ".tbz2", ".tar.gz", ".tgz", ".tar.xz", ".tar")):
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")
        return
    raise ModelError(f"unsupported archive type: {archive.name}")


def copy_into(model_dir: Path, source: Path) -> None:
    """Copy a locally available file into a model folder (used by tests and offline installs)."""
    model_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, model_dir / source.name)
