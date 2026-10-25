"""Logging setup: a rotating file in the data folder, plus the console when there is one.

What is said and typed stays out of the log (review S-02): speech logs the
kind and length of each utterance at DEBUG, and the words only when a
developer turns on ``general.log_speech``; ``Action.describe`` reduces text,
addresses and names to their length.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys

from . import paths

_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(threadName)-18s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def level_name(level: object, default: str = "INFO") -> str:
    """A real logging level name for a setting anyone can edit, or ``default``."""
    name = level.strip().upper() if isinstance(level, str) else ""
    return name if name in LEVELS else default


def setup_logging(level: object = "INFO", *, to_file: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level_name(level)))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT, _DATEFMT)
    if to_file:
        file_handler = logging.handlers.RotatingFileHandler(
            paths.logs_dir() / "sightline.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    if sys.stderr is not None and getattr(sys.stderr, "isatty", lambda: False)():
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        root.addHandler(console)

    # Third-party libraries that are chatty at INFO.
    for noisy in ("comtypes", "urllib3", "httpx", "huggingface_hub", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
