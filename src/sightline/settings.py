"""All persistent settings, composed from each subsystem's config dataclass."""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from sightline.core import paths
from sightline.core.settings_base import from_jsonable, loads_strict, setting, to_jsonable
from sightline.desktop.config import DesktopConfig
from sightline.speech.config import SpeechConfig
from sightline.ui.config import UIConfig
from sightline.vision.config import VisionConfig

log = logging.getLogger(__name__)

SECTIONS = ("general", "vision", "speech", "desktop", "ui")
MAX_SETTINGS_BYTES = 1_000_000  # the real file is a few kilobytes
BROKEN_SETTINGS_NOTICE = "Your settings file was damaged, so Sightline started with its usual settings.  The old file is kept as settings.broken.json."


@dataclass
class GeneralConfig:
    first_run_complete: bool = setting(False, label="Setup finished", hidden=True)
    # Off until someone turns it on in Settings: a first run on someone's PC never adds a Run entry unasked.
    start_with_windows: bool = setting(
        False,
        label="Start Sightline when I sign in to Windows",
        help="Windows starts Sightline for you each time you sign in, so you never need a mouse or keyboard to open it.",
        group="Startup",
    )
    profile: str = setting("default", label="Profile", hidden=True)
    log_level: str = setting("INFO", label="Log detail", advanced=True, choices={"INFO": "Normal", "DEBUG": "Detailed"})
    # For developers only, so it is not in the settings window: with "Detailed" logging, write the words
    # heard and what they became.  Otherwise the log keeps only the kind and length of each utterance.
    log_speech: bool = setting(False, label="Write what is heard to the log (developers)", hidden=True)


@dataclass
class Settings:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)
    desktop: DesktopConfig = field(default_factory=DesktopConfig)
    ui: UIConfig = field(default_factory=UIConfig)

    _lock = threading.Lock()
    # Set when the file could not be used, for a notice once the UI is up.  Not saved.
    load_problem: ClassVar[str | None] = None

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        """The saved settings, or the defaults.

        A file that cannot be used (unreadable, not JSON, NaN or Infinity, numbers too large,
        nesting too deep, too big) is moved aside as ``settings.broken.json`` and Sightline
        starts from the defaults: someone who uses only their eyes and voice cannot repair it.
        """
        path = path or paths.settings_file()
        if not path.exists():
            return cls()
        try:
            if path.stat().st_size > MAX_SETTINGS_BYTES:
                raise ValueError("the file is too large")
            data = loads_strict(path.read_text("utf-8"))
            return from_jsonable(cls, data)
        except Exception as e:  # whatever is wrong with the file must not stop Sightline starting
            log.error("Could not read %s (%s); starting from defaults", path, e)
            backup = path.with_suffix(".broken.json")
            try:
                path.replace(backup)
            except OSError:
                pass
            settings = cls()
            settings.load_problem = BROKEN_SETTINGS_NOTICE
            return settings

    def save(self, path: Path | None = None) -> None:
        path = path or paths.settings_file()
        with self._lock:
            data = to_jsonable(self)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=False), "utf-8")
            os.replace(tmp, path)
