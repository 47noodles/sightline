"""The Windows On-Screen Keyboard and Magnifier.

On-Screen Keyboard (osk.exe).  It runs with UIAccess, so it can type into
administrator windows and covers typing without voice (passwords included).
Sightline starts it in hover mode, where resting the pointer on a key types
it, which suits an eye pointer far better than clicking.  The settings live
under ``HKCU\\Software\\Microsoft\\Osk``; osk.exe reads them at start and
writes them back when it closes.  Value names come from osk.exe itself;
``Mode`` uses the Text Services Framework ``TYPEMODE`` numbering
(Softkbdc.h: ClickMouse 0, Hover 1, Scanning 2) and ``HoverPeriod`` is in
milliseconds.  ``tools/desktop_osk_probe.py`` checks them against the
running keyboard's Options dialog.

UIAccess also raises osk.exe above a normal process for User Interface
Privilege Isolation, so a close request can be refused; hiding the window is
the fallback.

The keyboard's settings are the user's own (review I-01).  Before the first
change Sightline keeps the values it is about to replace, and puts them back
when it closes the keyboard it opened, and again as it quits (closing the
keyboard first, since osk.exe writes its settings back as it closes).  It is
started by its full path, never by a bare name (review S-10).

Magnifier (Magnify.exe) is driven by its own shortcuts: Windows+Plus starts
it or zooms in, Windows+Minus zooms out, Windows+Esc turns it off.
"""

from __future__ import annotations

import logging
import threading
import time
import winreg
from collections.abc import Callable, Iterable

from sightline.core.types import Monitor

from . import win32 as w
from .apps import shell_open, system_exe

log = logging.getLogger(__name__)

OSK_CLASS = "OSKMainClass"
OSK_KEY = r"Software\Microsoft\Osk"
OSK_MODE_CLICK = 0
OSK_MODE_HOVER = 1
OSK_MODE_SCAN = 2
MAGNIFIER_CLASS = "MagUIClass"


def osk_window() -> int:
    return int(w.FindWindowW(OSK_CLASS, None) or 0)


def osk_visible() -> bool:
    hwnd = osk_window()
    return bool(hwnd) and bool(w.IsWindowVisible(hwnd)) and not w.IsIconic(hwnd)


def docked_rect(work: tuple[float, float, float, float], fraction: float = 0.36) -> tuple[int, int, int, int]:
    """(left, top, width, height) for a keyboard across the bottom of a work area (x, y, w, h).

    About a third of the height gives keys large enough to rest an eye pointer on.
    """
    x, y, width, height = work
    kb_height = int(max(260, min(height * fraction, 520)))
    return (int(x), int(y + height - kb_height), int(width), kb_height)


def osk_settings(hover_ms: int, rect: tuple[int, int, int, int]) -> dict[str, int]:
    left, top, width, height = rect
    return {
        "Mode": OSK_MODE_HOVER,
        "HoverPeriod": int(max(100, min(hover_ms, 5000))),
        "UseDevice": 0,
        "Dock": 1,
        "WindowLeft": left,
        "WindowTop": top,
        "WindowWidth": width,
        "WindowHeight": height,
    }


def read_osk_settings() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, OSK_KEY) as key:
            i = 0
            while True:
                try:
                    name, value, kind = winreg.EnumValue(key, i)
                except OSError:
                    break
                if kind == winreg.REG_DWORD:
                    out[name] = int(value)
                i += 1
    except OSError:
        pass
    return out


def write_osk_settings(values: dict[str, int]) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, OSK_KEY) as key:
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value) & 0xFFFFFFFF)


RegValue = tuple[object, int]  # (value, registry type), as winreg gives them


def read_osk_values(names: Iterable[str]) -> dict[str, RegValue | None]:
    """These values as they are now; None for each one that is not there."""
    out: dict[str, RegValue | None] = dict.fromkeys(names)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, OSK_KEY) as key:
            for name in out:
                try:
                    value, kind = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                out[name] = (value, kind)
    except OSError:
        pass
    return out


def put_back_osk_values(values: dict[str, RegValue | None]) -> None:
    """Write these values back as they were, removing each one that was not there."""
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, OSK_KEY) as key:
        for name, old in values.items():
            if old is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, name, 0, old[1], old[0])


def start_osk() -> bool:
    # ShellExecute, not CreateProcess: a UIAccess program has to be started through the shell.
    return shell_open(system_exe("osk.exe"))


def close_osk(hwnd: int, timeout: float) -> bool:
    """Ask the keyboard to close and wait for it.  False when it refused or is still open."""
    if not w.PostMessageW(hwnd, w.WM_SYSCOMMAND, w.SC_CLOSE, 0):
        log.info("On-Screen Keyboard refused the close request (error %d)", w.last_error())
        return False
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if not osk_window():
            return True
        time.sleep(0.05)
    log.info("On-Screen Keyboard did not close")
    return False


class OnScreenKeyboard:
    """Shows and hides osk.exe, and gives the user their own keyboard settings back afterwards.

    Everything that touches Windows is a parameter, so tests run it against a fake registry.
    """

    def __init__(
        self,
        primary: Callable[[], Monitor],
        hover_ms: Callable[[], int],
        *,
        find: Callable[[], int] = osk_window,
        start: Callable[[], bool] = start_osk,
        close: Callable[[int, float], bool] = close_osk,
        read: Callable[[Iterable[str]], dict[str, RegValue | None]] = read_osk_values,
        write: Callable[[dict[str, int]], None] = write_osk_settings,
        put_back: Callable[[dict[str, RegValue | None]], None] = put_back_osk_values,
    ) -> None:
        self._primary = primary
        self._hover_ms = hover_ms
        self._find = find
        self._start = start
        self._close = close
        self._read = read
        self._write = write
        self._put_back = put_back
        self._lock = threading.Lock()
        self._saved: dict[str, RegValue | None] | None = None  # the user's values before Sightline changed them

    @property
    def settings_changed(self) -> bool:
        """True while the keyboard has Sightline's settings and the user's are still to be put back."""
        return self._saved is not None

    def show(self, timeout: float = 4.0) -> bool:
        hwnd = self._find()
        if hwnd:
            if not w.IsWindowVisible(hwnd) or w.IsIconic(hwnd):
                w.ShowWindowAsync(hwnd, w.SW_SHOWNOACTIVATE)
            return True
        monitor = self._primary()
        work = monitor.work_rect
        values = osk_settings(self._hover_ms(), docked_rect((work.x, work.y, work.w, work.h)))
        try:
            with self._lock:
                if self._saved is None:  # the first change: keep what the user had, to put back later
                    self._saved = self._read(list(values))
            self._write(values)
        except OSError:
            log.exception("Could not write the On-Screen Keyboard settings")
        if not self._start():
            return False
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self._find():
                return True
            time.sleep(0.05)
        return False

    def hide(self, timeout: float = 2.0) -> bool:
        hwnd = self._find()
        if not hwnd:
            self.put_back_settings()
            return True
        if self._close(hwnd, timeout):
            self.put_back_settings()
            return True
        log.info("Hiding the On-Screen Keyboard instead")
        w.ShowWindowAsync(hwnd, w.SW_HIDE)
        time.sleep(0.1)
        return not w.IsWindowVisible(hwnd)

    def put_back_settings(self) -> bool:
        """Give the user their own keyboard settings back, once the keyboard Sightline set up is closed.

        False while it is still open: osk.exe writes its settings as it closes, which would undo this.
        """
        with self._lock:
            if self._saved is None:
                return True
            if self._find():
                return False
            saved, self._saved = self._saved, None
        try:
            self._put_back(saved)
        except OSError:
            log.exception("Could not put back the On-Screen Keyboard settings")
            return False
        log.info("On-Screen Keyboard settings put back as they were")
        return True

    def close_and_put_back(self, timeout: float = 2.0) -> None:
        """As Sightline quits: close the keyboard it set up, then give the user their settings back."""
        if self._saved is None:
            return
        hwnd = self._find()
        if hwnd and not self._close(hwnd, timeout):
            log.info("On-Screen Keyboard is still open; its settings stay as Sightline set them")
            return
        self.put_back_settings()

    def toggle(self) -> bool:
        """Returns True when the keyboard is now showing."""
        if osk_visible():
            self.hide()
            return False
        return self.show()


def magnifier_running() -> bool:
    return bool(w.FindWindowW(MAGNIFIER_CLASS, None))


class Magnifier:
    def __init__(self, press: Callable[[str], None]) -> None:
        self._press = press

    def toggle(self) -> bool:
        """Returns True when Magnifier is now on."""
        if magnifier_running():
            self._press("win+escape")
            return False
        self._press("win+plus")
        return True

    def zoom(self, direction: str) -> None:
        if direction == "in":
            self._press("win+plus")
        elif direction == "out":
            if magnifier_running():
                self._press("win+minus")
        else:
            raise ValueError(direction)
