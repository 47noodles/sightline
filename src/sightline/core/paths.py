"""Where Sightline keeps its files.

* ``config_dir``: roaming settings and per-user profiles (%APPDATA%\\Sightline)
* ``data_dir``: models, logs and recordings (%LOCALAPPDATA%\\Sightline)

Set ``SIGHTLINE_HOME`` to put everything under one folder (tests do this).
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

from sightline import APP_NAME


def _home_override() -> Path | None:
    value = os.environ.get("SIGHTLINE_HOME")
    return Path(value) if value else None


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir() -> Path:
    override = _home_override()
    if override:
        return _ensure(override / "config")
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return _ensure(Path(base) / APP_NAME)


def data_dir() -> Path:
    override = _home_override()
    if override:
        return _ensure(override / "data")
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return _ensure(Path(base) / APP_NAME)


def settings_file() -> Path:
    return config_dir() / "settings.json"


def profile_dir(profile: str = "default") -> Path:
    safe = "".join(ch for ch in profile if ch.isalnum() or ch in "-_ ").strip() or "default"
    return _ensure(config_dir() / "profiles" / safe)


def models_dir() -> Path:
    return _ensure(data_dir() / "models")


def logs_dir() -> Path:
    return _ensure(data_dir() / "logs")


def recordings_dir() -> Path:
    return _ensure(data_dir() / "recordings")


def assets_dir() -> Path:
    """Read-only files shipped inside the package (sounds, fonts, icons)."""
    return Path(str(resources.files("sightline") / "assets"))
