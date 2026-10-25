"""Application entry point: builds the shared services, the four subsystems and the Qt event loop.

Two ways in:

* ``main`` is the app itself (``python -m sightline``, ``sightline-console``).
* ``supervise`` is the ``sightline`` entry point people start: a small watchdog
  process that runs the app as a child and starts it again after an abnormal
  exit, at most three times in ten minutes, because someone who uses only their
  eyes and voice has no other way back into the computer after a crash.  The
  restarted app says "Sightline restarted after a problem".  A normal quit never
  restarts: the app writes a quit file the moment its event loop ends, before
  the teardown that follows, so even a crash while closing is not taken for a
  crash in use.  Windows signing out or shutting down is not a crash either.
  The app keeps the single-instance mutex; the watchdog holds nothing, so a
  second launch still finds the first and exits.

Both say so on screen when they cannot go on (review S-06, S-12): the watchdog
when it gives up restarting, with where the log is, and a second copy when the
single-instance mutex is taken (by the first copy, or by anything else that took
the name).  ``show_message`` is a plain Windows message box behind
``_message_box``, which every test replaces.  The child is started as
``python -P -m sightline`` from the data folder, so neither the working
directory nor a package in it can stand in for Sightline (review S-10).

"Start Sightline when I sign in to Windows" (``general.start_with_windows``)
keeps a value named Sightline under the current user's Run key in step with
the setting: the supervised ``sightline`` program (``Scripts\\sightline.exe``,
or the frozen executable), never ``python -m``.  ``sync_autostart`` takes the
registry as a parameter, so tests use a fake one.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import logging.handlers
import os
import subprocess
import sys
import sysconfig
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path

from sightline import APP_NAME, __version__
from sightline.core import paths
from sightline.core.actions import ActionRegistry
from sightline.core.bus import EventBus
from sightline.core.context import AppContext
from sightline.core.events import AppLifecycle, Notice, SettingsChanged
from sightline.core.log import level_name, setup_logging
from sightline.core.models import ModelManager
from sightline.core.state import AppState
from sightline.core.types import Action
from sightline.settings import Settings

log = logging.getLogger("sightline")
wlog = logging.getLogger("sightline.watchdog")

RESTART_LIMIT = 3  # restarts allowed ...
RESTART_WINDOW_S = 600.0  # ... within this many seconds
RESTART_DELAY_S = 1.0  # a moment for the camera and microphone drivers to let go of the crashed process
RESTARTED_FLAG = "--restarted"
QUIT_FILE_FLAG = "--quit-file"
RESTARTED_NOTICE = "Sightline restarted after a problem."
SM_SHUTTINGDOWN = 0x2000
MUTEX_NAME = "Local\\Sightline.SingleInstance"
ERROR_ALREADY_EXISTS = 183
ALREADY_RUNNING_MESSAGE = "Sightline is already running, so this copy has closed.  Its icon is beside the clock."
GAVE_UP_MESSAGE = (
    "Sightline stopped unexpectedly several times, so it has not been started again.  "
    "Restart the computer to try again.  What happened is written in the log files in {folder}"
)
COULD_NOT_START_MESSAGE = "Sightline could not be started.  What happened is written in the log files in {folder}"
ELEVATED_NOTICE = (
    "Sightline is running as administrator.  It does not need to: start it normally, "
    "so that what it hears cannot reach administrator windows."
)
MB_OK, MB_ICONWARNING, MB_SETFOREGROUND, MB_TOPMOST = 0x0, 0x30, 0x10000, 0x40000


def _message_box(text: str, title: str) -> None:
    """A plain Windows message box, waiting until it is closed.  Every test replaces this function."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MessageBoxW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
    user32.MessageBoxW(None, text, title, MB_OK | MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST)


def show_message(text: str) -> None:
    """Tell the user something on screen when Sightline's own windows cannot (it is not running).  Never raises."""
    try:
        _message_box(text, APP_NAME)
    except Exception:
        logging.getLogger("sightline").exception("Could not show a message")


def _logs_folder() -> str:
    try:
        return str(paths.logs_dir())
    except OSError:
        return "the Sightline folder in your AppData"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="sightline", description="Use your computer with your eyes and voice.")
    p.add_argument("--setup", action="store_true", help="run the first-run setup again")
    p.add_argument("--no-vision", action="store_true", help="do not open the camera (voice only)")
    p.add_argument("--no-speech", action="store_true", help="do not open the microphone (eyes only)")
    p.add_argument("--fake-vision", action="store_true", help="development: use a fake vision service")
    p.add_argument("--fake-speech", action="store_true", help="development: use a fake speech service")
    p.add_argument("--debug", action="store_true", help="detailed logging")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    # From the watchdog only: this run follows a crash, and where to note that the user quit.
    p.add_argument(RESTARTED_FLAG, action="store_true", help=argparse.SUPPRESS)
    p.add_argument(QUIT_FILE_FLAG, dest="quit_file", default=None, help=argparse.SUPPRESS)
    return p.parse_args(argv)


def _enable_dpi_awareness() -> None:
    """Per-monitor DPI awareness v2, so every coordinate in the app is a physical pixel."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass


def _create_mutex(name: str) -> tuple[int, int]:
    """(handle or 0, the error Windows gave)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    handle = kernel32.CreateMutexW(None, False, name)
    return int(handle or 0), ctypes.get_last_error()


def _close_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle(handle)


class _SingleInstance:
    """Named mutex so a second launch exits instead of fighting over the camera and microphone.

    The name is taken when the mutex already exists, and also when Windows refuses to open it
    (another program made it and did not let us in): either way Sightline cannot be sure it is
    alone, so it says so and stops, rather than run twice or vanish without a word.
    """

    def __init__(self, create: Callable[[str], tuple[int, int]] = _create_mutex, close: Callable[[int], None] = _close_handle) -> None:
        self._create = create
        self._close = close
        self._handle = 0

    def acquire(self) -> bool:
        handle, error = self._create(MUTEX_NAME)
        if not handle:
            return False
        if error == ERROR_ALREADY_EXISTS:
            self._close(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle:
            self._close(self._handle)
            self._handle = 0


def already_running() -> None:
    """A second copy, or something else holding the name: say so instead of closing silently."""
    show_message(ALREADY_RUNNING_MESSAGE)


# =========================================================================== starting with Windows

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = APP_NAME


class RunKey:
    """The current user's Run key: programs Windows starts at sign-in."""

    def get(self, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _kind = winreg.QueryValueEx(key, name)
        except OSError:
            return None
        return str(value)

    def set(self, name: str, command: str) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)

    def delete(self, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def autostart_command() -> str | None:
    """What Windows should run at sign-in: the supervised ``sightline`` program, or None when there is none.

    Never ``python -m sightline``: that would start without the watchdog, from whatever folder
    Windows starts it in.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    try:
        program = Path(sysconfig.get_path("scripts")) / "sightline.exe"
    except (KeyError, TypeError):
        return None
    return f'"{program}"' if program.is_file() else None


def sync_autostart(enabled: bool, registry: object | None = None, command: str | None = None) -> str:
    """Make the Run entry match the setting.  Returns "added", "updated", "removed", "unchanged" or "unavailable"."""
    reg = registry if registry is not None else RunKey()
    current = reg.get(RUN_VALUE)  # type: ignore[attr-defined]
    if not enabled:
        if current is None:
            return "unchanged"
        reg.delete(RUN_VALUE)  # type: ignore[attr-defined]
        return "removed"
    wanted = command if command is not None else autostart_command()
    if wanted is None:
        return "unavailable"
    if current == wanted:
        return "unchanged"
    reg.set(RUN_VALUE, wanted)  # type: ignore[attr-defined]
    return "added" if current is None else "updated"


def _sync_autostart_quietly(settings: Settings) -> None:
    try:
        done = sync_autostart(bool(settings.general.start_with_windows))
    except OSError:
        log.warning("Could not change whether Sightline starts with Windows", exc_info=True)
        return
    if done in ("added", "updated", "removed"):
        log.info("Starting with Windows: %s", done)
    elif done == "unavailable":
        log.info("Starting with Windows: no sightline program to start")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _enable_dpi_awareness()

    settings = Settings.load()
    setup_logging("DEBUG" if args.debug else level_name(settings.general.log_level))
    log.info("%s %s starting (data in %s)", APP_NAME, __version__, paths.data_dir())

    instance = _SingleInstance()
    if not instance.acquire():
        log.warning("Another Sightline is already running, or something else holds its name; exiting")
        already_running()
        return 0

    from PySide6.QtWidgets import QApplication

    qt_app = QApplication(sys.argv[:1])
    qt_app.setApplicationName(APP_NAME)
    qt_app.setApplicationVersion(__version__)
    qt_app.setQuitOnLastWindowClosed(False)

    bus = EventBus()
    actions = ActionRegistry(bus)
    ctx = AppContext(bus=bus, actions=actions, state=AppState(bus), settings=settings, models=ModelManager(bus))
    bus.publish(AppLifecycle("starting"))

    from sightline import desktop as desktop_pkg

    ctx.desktop = desktop_pkg.create(ctx)

    if args.fake_vision:
        from sightline.core.fakes import FakeVision

        ctx.vision = FakeVision()
    elif not args.no_vision:
        from sightline import vision as vision_pkg

        ctx.vision = vision_pkg.create(ctx)

    if args.fake_speech:
        from sightline.core.fakes import FakeSpeech

        ctx.speech = FakeSpeech()
    elif not args.no_speech:
        from sightline import speech as speech_pkg

        ctx.speech = speech_pkg.create(ctx)

    from sightline import ui as ui_pkg

    shell = ui_pkg.create(ctx, qt_app)
    actions.register("app.quit", lambda _a: qt_app.quit(), thread="ui", description="Quit Sightline")

    actions.start()
    ctx.desktop.start()
    shell.start()
    # Only now: notices published before the shell started would not be shown.
    if args.restarted:
        log.warning("Started again by the watchdog after a problem")
        announce_restart(bus)
    announce_start_problems(bus, settings, ctx.desktop)
    _sync_autostart_quietly(settings)
    bus.subscribe(SettingsChanged, lambda e: _sync_autostart_quietly(settings) if e.section == "general" else None)

    def start_engines() -> None:
        # Camera and microphone open off the GUI thread; both can take a second or two.
        def run() -> None:
            for name, service in (("vision", ctx.vision), ("speech", ctx.speech)):
                if service is None:
                    continue
                try:
                    service.start()
                except Exception as e:
                    log.exception("Could not start %s", name)
                    bus.publish(Notice(f"Could not start {name}: {e}", "error", key=f"start-{name}"))
            bus.publish(AppLifecycle("ready"))

        threading.Thread(target=run, name="sightline-startup", daemon=True).start()

    if args.setup or not settings.general.first_run_complete:
        shell.run_setup(on_complete=start_engines)
    else:
        shell.ensure_models(on_ready=start_engines)

    try:
        code = qt_app.exec()
        # The event loop only ends when someone quits.  Say so before the teardown, so the watchdog
        # never takes a problem while closing for a crash and brings Sightline back.
        note_quit(args.quit_file)
    finally:
        bus.publish(AppLifecycle("stopping"))
        # Each step on its own: one that fails must not leave the camera, the microphone or a held key behind.
        _quietly("releasing keys", lambda: ctx.actions.execute_now(Action("key.release_all", source="system")))
        for name, service in (("speech", ctx.speech), ("vision", ctx.vision)):
            if service is not None:
                _quietly(f"stopping {name}", service.stop)
        _quietly("stopping the UI", shell.stop)
        _quietly("stopping the desktop services", ctx.desktop.stop)
        _quietly("stopping the action thread", actions.stop)
        try:
            settings.save()
        except OSError:
            log.exception("Could not save settings")
        instance.release()
        log.info("Stopped")
    return code


def _quietly(what: str, step: Callable[[], object]) -> None:
    try:
        step()
    except Exception:
        log.exception("Error %s", what)


def announce_restart(bus: EventBus) -> None:
    """Tell the user Sightline is back after the watchdog restarted it (a toast)."""
    bus.publish(Notice(RESTARTED_NOTICE, "warn", key="app-restarted"))


def announce_start_problems(bus: EventBus, settings: Settings, desktop: object) -> None:
    """Toasts for what start-up found: a damaged settings file, or running as administrator (review S-14)."""
    problem = getattr(settings, "load_problem", None)
    if problem:
        bus.publish(Notice(problem, "warn", key="settings-damaged"))
    try:
        guard = getattr(desktop, "guard", None)
        elevated = bool(guard is not None and guard.is_elevated_self())
    except Exception:
        log.debug("Could not tell whether Sightline runs as administrator", exc_info=True)
        elevated = False
    if elevated:
        log.warning("Running as administrator")
        bus.publish(Notice(ELEVATED_NOTICE, "warn", key="app-elevated"))


def note_quit(path: str | os.PathLike[str] | None) -> None:
    """Leave the watchdog's quit file: the user quit, so whatever the exit code, do not restart."""
    if not path:
        return
    try:
        Path(path).write_text("quit", "utf-8")
    except OSError:
        log.warning("Could not tell the watchdog that Sightline is quitting", exc_info=True)


# =========================================================================== the watchdog


def _run_child(command: Sequence[str]) -> int:
    """Start the app and wait for it.  No console window: the app has its own windows, and a
    console child of a windowless watchdog would otherwise open one.  It runs in the data
    folder, not wherever the shortcut or terminal happened to be (review S-10)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(list(command), stdin=subprocess.DEVNULL, creationflags=flags, cwd=str(paths.data_dir()))
    return process.wait()


def _session_ending() -> bool:
    """Windows is signing out or shutting down: every app is being closed, so nothing to restart."""
    try:
        return bool(ctypes.windll.user32.GetSystemMetrics(SM_SHUTTINGDOWN))
    except (AttributeError, OSError):
        return False


def _exit_text(code: int) -> str:
    """Exit codes as Windows shows them: small numbers plain, crash codes in hex (0xC0000005)."""
    return str(code) if 0 <= code <= 255 else f"0x{code & 0xFFFFFFFF:08X}"


class Watchdog:
    """Runs Sightline as a child process and starts it again after an abnormal exit.

    Everything that touches the world is a parameter, so tests run it with a fake child
    process and a fake clock.  ``run`` returns when Sightline quits normally (exit code 0,
    or the quit file the app leaves as its event loop ends), when Windows is signing out,
    or when Sightline has needed ``limit`` restarts within ``window_s`` already.
    """

    def __init__(
        self,
        command: Sequence[str] | None = None,
        *,
        quit_file: Path | None = None,
        limit: int = RESTART_LIMIT,
        window_s: float = RESTART_WINDOW_S,
        delay_s: float = RESTART_DELAY_S,
        spawn: Callable[[Sequence[str]], int] = _run_child,
        session_ending: Callable[[], bool] = _session_ending,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        message: Callable[[str], None] | None = None,
    ) -> None:
        # -P: the working directory is not put on sys.path, so a "sightline" folder there cannot stand in.
        self.command = list(command) if command is not None else [sys.executable, "-P", "-m", "sightline"]
        self.quit_file = quit_file if quit_file is not None else paths.data_dir() / f"quit-{os.getpid()}.flag"
        self.limit = limit
        self.window_s = window_s
        self.delay_s = delay_s
        self.spawn = spawn
        self.session_ending = session_ending
        self.clock = clock
        self.sleep = sleep
        self.message = message  # shows the user why Sightline is gone (a message box); None in tests
        self.restarts: deque[float] = deque()  # when each restart was decided, within the last window

    def child_command(self, args: Sequence[str], restarted: bool) -> list[str]:
        """The app's command line: the user's own arguments, the quit file, and the restart flag after a crash."""
        # Set-up that finished needs no second run, and set-up that did not runs anyway.
        own = [a for a in args if not (restarted and a == "--setup")]
        extra = [QUIT_FILE_FLAG, str(self.quit_file)] + ([RESTARTED_FLAG] if restarted else [])
        return [*self.command, *own, *extra]

    def run(self, args: Sequence[str] = ()) -> int:
        restarted = False
        while True:
            self._forget_quit()
            try:
                code = self.spawn(self.child_command(args, restarted))
            except OSError:
                wlog.exception("Could not start Sightline")
                self._tell(COULD_NOT_START_MESSAGE.format(folder=_logs_folder()))
                return 1
            if code == 0:
                wlog.info("Sightline closed")
                return 0
            if self._quit_noted():
                wlog.warning("Sightline ended with %s while closing; it was quitting, so it is not started again", _exit_text(code))
                return 0
            if self.session_ending():
                wlog.info("Sightline ended with %s while Windows was signing out or shutting down", _exit_text(code))
                return code
            if not self._may_restart():
                wlog.error(
                    "Sightline stopped unexpectedly again (%s) after %d restarts in %d minutes; it is not started again",
                    _exit_text(code), len(self.restarts), round(self.window_s / 60),
                )  # fmt: skip
                # The person may have no other way to learn why Sightline is gone: say it on screen.
                self._tell(GAVE_UP_MESSAGE.format(folder=_logs_folder()))
                return code
            wlog.warning("Sightline stopped unexpectedly (%s); starting it again", _exit_text(code))
            self.sleep(self.delay_s)
            restarted = True

    def clean_up(self) -> None:
        self._forget_quit()

    def _tell(self, text: str) -> None:
        if self.message is None:
            return
        try:
            self.message(text)
        except Exception:
            wlog.exception("Could not show a message")

    def _may_restart(self) -> bool:
        now = self.clock()
        while self.restarts and now - self.restarts[0] >= self.window_s:
            self.restarts.popleft()
        if len(self.restarts) >= self.limit:
            return False
        self.restarts.append(now)
        return True

    def _quit_noted(self) -> bool:
        try:
            return self.quit_file.exists()
        except OSError:
            return False

    def _forget_quit(self) -> None:
        try:
            self.quit_file.unlink(missing_ok=True)
        except OSError:
            wlog.warning("Could not remove %s", self.quit_file, exc_info=True)


def _watchdog_logging() -> None:
    """The watchdog's own small log, beside the app's: two processes must not rotate one file."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        handler = logging.handlers.RotatingFileHandler(paths.logs_dir() / "watchdog.log", maxBytes=200_000, backupCount=1, encoding="utf-8")
    except OSError:
        return  # no log is better than no Sightline
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)


def supervise(argv: list[str] | None = None) -> int:
    """The ``sightline`` entry point: run the app under the watchdog."""
    args = sys.argv[1:] if argv is None else list(argv)
    if getattr(sys, "frozen", False):
        return main(args)  # a frozen build has no ``python -m sightline`` to start a child with
    try:
        _parse_args(args)  # --help, --version and a mistyped option end here, not in a child taken for a crash
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 0
    _watchdog_logging()
    watchdog = Watchdog(message=show_message)
    wlog.info("%s %s watchdog starting", APP_NAME, __version__)
    try:
        return watchdog.run(args)
    except KeyboardInterrupt:
        return 130
    finally:
        watchdog.clean_up()


if __name__ == "__main__":
    raise SystemExit(main())
