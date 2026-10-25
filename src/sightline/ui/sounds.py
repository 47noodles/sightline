"""Earcons: short, soft sounds for armed eye clicks, clicks, voice results and state changes.

No text-to-speech anywhere, by design: short real sound clips only, and a
voice talking over the user's own speech would also confuse the recogniser.
The clips live in ``assets/sounds`` (made by ``tools/ui_make_sounds.py``)
and are played with ``QSoundEffect``.

The effects live on a thread of their own with its own Qt event loop.  On
some audio stacks (seen over Remote Desktop) Qt Multimedia blocks the
thread that owns an effect for seconds while it loads the clip or opens
the device; that must never be the GUI thread, which draws the pointer.
The thread is a daemon ``threading.Thread`` rather than a ``QThread`` so a
stuck audio stack cannot hold up or crash the exit (Qt aborts when a
running ``QThread`` object is destroyed).  A request that waited too long
is dropped: a click sound half a second late says the wrong thing.
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QUrl, Signal, Slot

from sightline.core import paths
from sightline.core.events import (
    ActionExecuted,
    ClickPerformed,
    EyeHold,
    IntentResult,
    Notice,
    StateChanged,
)
from sightline.core.types import SpeechMode

log = logging.getLogger(__name__)

SOUND_NAMES: tuple[str, ...] = (
    "hold_1",
    "hold_2",
    "click",
    "double_click",
    "right_click",
    "middle_click",
    "recognised",
    "not_understood",
    "sleep",
    "wake",
    "eyes_paused",
    "eyes_resumed",
    "error",
    "calibration_point",
    "success",
)

# Commands whose own effect already makes a sound (a click, a mode change), so the
# "recognised" chime would only stack a second sound on top.
_SELF_SOUNDING_PREFIXES = (
    "ui.chooser.select",
    "ui.menu.select",
    "mouse.click",
    "mouse.down",
    "mouse.up",
    "mouse.drag_toggle",
    "element.click",
    "ui.labels.select",
    "ui.grid.click",
    "speech.mode",
    "pointer.pause",
    "pointer.resume",
    "pointer.toggle",
)

_MIN_REPEAT_S = 0.05  # the same sound more often than this is a storm, not information
_ERROR_GAP_S = 0.4
_STALE_S = 0.35  # a request older than this when the sound thread gets to it is dropped
_JOIN_S = 1.0  # how long stop() waits for the sound thread before leaving it to end with the process


def sounds_dir() -> Path:
    return paths.assets_dir() / "sounds"


class _Player(QObject):
    """Lives on the sound thread: owns the effects and plays them."""

    def __init__(self, paths: dict[str, Path], volume: float, loop: QEventLoop | None) -> None:
        super().__init__()
        self._loop = loop
        self.effects: dict[str, object] = {}
        if not paths:
            return
        from PySide6.QtMultimedia import QSoundEffect

        for name, path in paths.items():
            effect = QSoundEffect(self)
            effect.setSource(QUrl.fromLocalFile(str(path)))
            effect.setVolume(volume)
            self.effects[name] = effect

    @Slot(str, float, float)
    def play(self, name: str, volume: float, requested: float) -> None:
        late = time.monotonic() - requested
        if late > _STALE_S:
            log.debug("Dropped sound %s, %.0f ms late", name, late * 1000)
            return
        effect = self.effects.get(name)
        if effect is None:
            return
        try:
            # Qt queues a play() that comes before the clip has loaded, and it could sound much later.
            if effect.status() != effect.Status.Ready:  # type: ignore[attr-defined]
                log.debug("Sound %s is not loaded yet", name)
                return
            if abs(effect.volume() - volume) > 1e-3:  # type: ignore[attr-defined]
                effect.setVolume(volume)  # type: ignore[attr-defined]
            if effect.isPlaying():  # type: ignore[attr-defined]
                effect.stop()  # type: ignore[attr-defined]
            effect.play()  # type: ignore[attr-defined]
        except Exception:
            log.debug("Could not play %s", name, exc_info=True)

    @Slot()
    def finish(self) -> None:
        for effect in self.effects.values():
            try:
                effect.stop()  # type: ignore[attr-defined]
            except Exception:
                log.debug("Could not stop a sound", exc_info=True)
        if self._loop is not None:
            self._loop.quit()


class Sounds(QObject):
    """The mapping from events to sounds, and the front end of the sound thread."""

    _play_requested = Signal(str, float, float)
    _finish_requested = Signal()

    def __init__(self, enabled: bool = True, volume: float = 0.6, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._enabled = enabled
        self._volume = max(0.0, min(1.0, volume))
        self._paths: dict[str, Path] = {}
        self._last_played: dict[str, float] = {}
        self._last_error = 0.0
        self.played: list[str] = []  # names in order, for tests and the playground (bounded)
        self._available = True
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()  # belongs to the current thread; each new thread gets its own

    # ------------------------------------------------------------------ loading

    def load(self) -> None:
        """Find the clips.  The sound thread starts once sounds are on."""
        try:
            importlib.import_module("PySide6.QtMultimedia")  # here, so the sound thread never loads modules
        except ImportError:  # Qt Multimedia missing from a slim install: stay silent rather than fail
            log.warning("Qt Multimedia is not available; sounds are off")
            self._available = False
            return
        for name in SOUND_NAMES:
            path = sounds_dir() / f"{name}.wav"
            if not path.exists():
                log.warning("Missing sound %s", path)
                continue
            self._paths[name] = path
        self._ensure_thread()

    def loaded(self) -> dict[str, Path]:
        """The clips ``load`` found, by name."""
        return dict(self._paths)

    def configure(self, enabled: bool, volume: float) -> None:
        self._enabled = enabled
        self._volume = max(0.0, min(1.0, volume))
        self._ensure_thread()

    @property
    def enabled(self) -> bool:
        return self._enabled and self._available

    @property
    def thread_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        """Ask the sound thread to finish, waiting at most ``_JOIN_S``."""
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._stopping.set()
        self._finish_requested.emit()
        thread.join(_JOIN_S)
        if thread.is_alive():
            log.info("The sound thread is still busy in Qt Multimedia; it ends with the process")

    def _ensure_thread(self) -> None:
        if self._thread is not None or not self._paths:
            return
        if not self.enabled or self._volume <= 0.0:
            return  # nothing to hear, so no reason to open the audio device
        self._stopping = threading.Event()
        args = (dict(self._paths), self._volume, self._stopping)
        self._thread = threading.Thread(target=self._run, args=args, name="sightline-sounds", daemon=True)
        self._thread.start()

    def _run(self, paths: dict[str, Path], volume: float, stopping: threading.Event) -> None:
        loop = QEventLoop()  # first, so the effects find an event dispatcher on this thread
        try:
            player = _Player(paths, volume, loop)
        except Exception:
            log.exception("Could not set up the sounds")
            return
        # Connected here, with the receiver on this thread, emits from the GUI thread queue to this loop.
        self._play_requested.connect(player.play)
        self._finish_requested.connect(player.finish)
        if stopping.is_set():  # stop() ran before the connections existed
            player.finish()
        else:
            loop.exec()
        del player  # the effects are deleted here, on the thread they belong to
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)  # with anything they left to deleteLater()

    # ------------------------------------------------------------------ playing

    def play(self, name: str) -> None:
        now = time.monotonic()
        if now - self._last_played.get(name, 0.0) < _MIN_REPEAT_S:
            return
        self._last_played[name] = now
        self.played.append(name)
        if len(self.played) > 200:
            del self.played[:100]
        if not self.enabled or self._volume <= 0.0 or name not in self._paths:
            return
        self._ensure_thread()
        self._play_requested.emit(name, self._volume, now)

    # ------------------------------------------------------------------ event mapping

    def on_eye_hold(self, e: EyeHold) -> None:
        if e.cancelled:
            return  # resting the eyes is not an event worth a sound
        if e.stage == 1:
            self.play("hold_1")
        elif e.stage >= 2:
            self.play("hold_2")

    def on_click(self, e: ClickPerformed) -> None:
        if e.button == "right":
            self.play("right_click")
        elif e.button == "middle":
            self.play("middle_click")
        elif e.count >= 2:
            self.play("double_click")
        else:
            self.play("click")

    def on_intent(self, e: IntentResult) -> None:
        if e.kind == "rejected":
            self.play("not_understood")
        elif e.kind == "command":
            if any(a.name.startswith(_SELF_SOUNDING_PREFIXES) for a in e.actions):
                return
            self.play("recognised")

    def on_state(self, e: StateChanged) -> None:
        if e.field == "speech_mode":
            if e.new == SpeechMode.SLEEP:
                self.play("sleep")
            elif e.old == SpeechMode.SLEEP:
                self.play("wake")
        elif e.field == "pointer_paused":
            self.play("eyes_paused" if e.new else "eyes_resumed")

    def on_action(self, e: ActionExecuted) -> None:
        if not e.ok:
            self._error()

    def on_notice(self, e: Notice) -> None:
        if e.level == "error":
            self._error()

    def _error(self) -> None:
        now = time.monotonic()
        if now - self._last_error < _ERROR_GAP_S:
            return
        self._last_error = now
        self.play("error")
