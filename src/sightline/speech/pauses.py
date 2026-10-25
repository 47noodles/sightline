"""How long this person pauses inside a command (concept review item 16b).

Slow or breath-limited speakers pause in the middle of commands ("switch
to ... notepad").  A fixed command endpoint cuts such a command in two, and
both halves then fail or, worse, mean something else ("scroll" ... "down"
presses the Down key).

Two kinds of evidence are kept:

* pauses inside utterances that turned out to be commands: the gaps where
  the voice detector heard silence and then speech again before the
  endpoint (``Utterance.pauses_ms``);
* pauses that did cut a command in two: an unfinished command was rejected
  and the rest of it followed a moment later.

From the recent pauses the model learns how long to wait when what has been
said so far is not yet a whole command (the matcher's prefix analysis says
so).  Complete commands keep their short endpoint, so fluent speakers lose
nothing.  The wait never drops below the user's "pause that ends a command"
and never exceeds a ceiling, so a noisy room cannot teach it something silly.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections import deque
from collections.abc import Iterable
from pathlib import Path

from sightline.core.settings_base import loads_strict

log = logging.getLogger(__name__)

MIN_GAP_MS = 96.0  # three VAD frames; shorter gaps sit well inside the shortest endpoint
MIN_COMMANDS = 20  # spoken commands heard before anything is learned
MIN_SAMPLES = 5  # with fewer long pauses than this the speaker does not need a longer wait
MAX_SAMPLES = 400  # recent pauses only, so the value follows a voice that changes over months
QUANTILE = 0.95
MARGIN = 1.2  # wait a fifth longer than the long pauses
CEILING_MS = 1500
SPLIT_MAX_MS = 2500.0  # a longer gap between two halves is two utterances, not one command
VERSION = 1
MAX_FILE_BYTES = 256 * 1024  # 400 pauses take about 4 KB


def quantile(values: list[float], q: float) -> float:
    """Linear-interpolated quantile of sorted values (numpy's default method)."""
    if not values:
        raise ValueError("no values")
    pos = (len(values) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


class PauseModel:
    """Recent pauses inside commands, and the wait they suggest.  Not thread-safe: the service locks around it."""

    def __init__(self, samples: Iterable[float] = (), commands: int = 0) -> None:
        self._samples: deque[float] = deque((float(s) for s in samples), maxlen=MAX_SAMPLES)
        self.commands = int(commands)
        self.dirty = False

    @property
    def samples(self) -> list[float]:
        return list(self._samples)

    def add_command(self, pauses_ms: Iterable[float]) -> None:
        """A spoken utterance became a command; ``pauses_ms`` are the gaps inside it."""
        self.commands += 1
        for gap in pauses_ms:
            if gap >= MIN_GAP_MS:
                self._samples.append(round(float(gap), 1))
        self.dirty = True

    def add_split(self, gap_ms: float) -> None:
        """A pause that cut a command in two: the endpoint ended it unfinished and the rest came ``gap_ms`` later."""
        if MIN_GAP_MS <= gap_ms <= SPLIT_MAX_MS:
            self._samples.append(round(float(gap_ms), 1))
            self.dirty = True

    def learned_ms(self, floor_ms: int, ceiling_ms: int = CEILING_MS) -> int | None:
        """How long to wait for the rest of an unfinished command; None until enough commands have been heard."""
        if self.commands < MIN_COMMANDS:
            return None
        ceiling = max(int(floor_ms), int(ceiling_ms))
        if len(self._samples) < MIN_SAMPLES:
            return int(floor_ms)
        wait = quantile(sorted(self._samples), QUANTILE) * MARGIN
        return int(min(ceiling, max(int(floor_ms), 10 * round(wait / 10))))

    # ------------------------------------------------------------------ the file in the profile folder

    def to_json(self) -> dict:
        return {"version": VERSION, "commands": self.commands, "pauses_ms": list(self._samples)}

    @classmethod
    def from_json(cls, data: object) -> PauseModel:
        if not isinstance(data, dict) or data.get("version") != VERSION:
            return cls()
        try:
            samples = [float(s) for s in data.get("pauses_ms", []) if 0 <= float(s) <= SPLIT_MAX_MS]
            commands = max(0, int(data.get("commands", 0)))
        except (TypeError, ValueError, OverflowError):  # OverflowError: int(1e999)
            return cls()
        return cls(samples, commands)

    @classmethod
    def load(cls, path: Path) -> PauseModel:
        """The model in the file; a fresh one when there is none or it cannot be read.

        A damaged file (not JSON, Infinity or NaN, nested too deeply, too large) is kept beside it
        as ``pauses.broken.json`` and learning starts again: it must never stop speech starting
        (review S-06).
        """
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError(f"larger than {MAX_FILE_BYTES // 1024} KB")
            return cls.from_json(loads_strict(path.read_text("utf-8")))
        except FileNotFoundError:
            return cls()
        except Exception as e:
            log.warning("Could not read %s (%s); learning pauses afresh", path.name, type(e).__name__)
            try:
                path.replace(path.with_suffix(".broken.json"))
            except OSError:
                pass
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_json()), "utf-8")
        os.replace(tmp, path)
        self.dirty = False


def describe(learned_ms: int | None, floor_ms: int, *, enabled: bool = True) -> str:
    """The learned wait in words, for the settings window."""
    if not enabled:
        return "Not learning"
    if learned_ms is None:
        return "Not learned yet"
    if learned_ms <= floor_ms:
        return "No longer than the pause that ends a command"
    seconds = f"{learned_ms / 1000:.2f}".rstrip("0").rstrip(".")
    return "1 second" if seconds == "1" else f"{seconds} seconds"
