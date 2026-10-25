"""Top-level windows: listing, activation, and the caption-button actions.

Which windows count follows the Alt+Tab rules Raymond Chen describes in "Which
windows appear in the Alt+Tab list?" (The Old New Thing, 2007): visible,
not cloaked by DWM, not a tool window, and the last active popup of its root
owner.  Sightline's own windows are left out.

Bringing a window to the front from a background process runs into the
foreground lock.  The first remedy is adapted from Microsoft PowerToys
(MIT licence, ``src/modules/fancyzones/FancyZonesLib/WindowUtils.cpp``,
``SwitchToWindow``: restore if minimised, one empty ``SendInput`` mouse
event, then ``SetForegroundWindow``).  The empty mouse event makes
this process the last one to receive input, which is one of the conditions
under which ``SetForegroundWindow`` is allowed.  ``AttachThreadInput`` to the
current foreground thread and ``SwitchToThisWindow`` are the fallbacks.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from sightline.core.actions import ActionError
from sightline.core.types import Rect, WindowInfo

from . import win32 as w
from .elevation import ELEVATED_MESSAGE

log = logging.getLogger(__name__)

_SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "DV2ControlHost"})
_UWP_FRAME = "ApplicationFrameWindow"
_UWP_CORE = "Windows.UI.Core.CoreWindow"


class ProcessNames:
    """pid -> executable name, cached briefly (window lists are rebuilt several times a second)."""

    def __init__(self, ttl: float = 30.0) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[int, tuple[float, str]] = {}

    def name(self, pid: int) -> str:
        if not pid:
            return ""
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(pid)
            if hit and now - hit[0] < self._ttl:
                return hit[1]
        name = w.process_name(pid)
        with self._lock:
            if len(self._cache) > 512:
                self._cache.clear()
            self._cache[pid] = (now, name)
        return name


def _uwp_app_pid(frame: int) -> int:
    """The process behind a UWP ApplicationFrameWindow (its CoreWindow child), or 0."""
    frame_pid = w.window_pid(frame)
    found = [0]

    def visit(child, _lp) -> bool:
        if w.class_name(child) == _UWP_CORE:
            pid = w.window_pid(child)
            if pid and pid != frame_pid:
                found[0] = pid
                return False
        return True

    w.EnumChildWindows(frame, w.WNDENUMPROC(visit), 0)
    return found[0]


def is_alt_tab_window(hwnd: int) -> bool:
    if not w.IsWindowVisible(hwnd) or w.is_cloaked(hwnd):
        return False
    ex = w.GetWindowLongPtrW(hwnd, w.GWL_EXSTYLE)
    if ex & w.WS_EX_APPWINDOW == 0 and (ex & w.WS_EX_TOOLWINDOW or ex & w.WS_EX_NOACTIVATE):
        return False
    if w.class_name(hwnd) in _SHELL_CLASSES:
        return False
    # Raymond Chen's walk: the window shown for an owner chain is the last active visible popup.
    walk = w.GetAncestor(hwnd, w.GA_ROOTOWNER) or hwnd
    while True:
        attempt = w.GetLastActivePopup(walk)
        if attempt == walk or not attempt:
            break
        if w.IsWindowVisible(attempt):
            walk = attempt
            break
        walk = attempt
    return walk == hwnd


class Win32WindowManager:
    """The ``WindowManager`` implementation."""

    def __init__(self, nudge: Callable[[], None] | None = None, own_pid: int | None = None) -> None:
        self._nudge = nudge
        self._own_pid = own_pid if own_pid is not None else w.GetCurrentProcessId()
        self.process_names = ProcessNames()

    # ------------------------------------------------------------------ describing windows

    def process_of(self, hwnd: int) -> str:
        pid = w.window_pid(hwnd)
        if w.class_name(hwnd) == _UWP_FRAME:
            pid = _uwp_app_pid(hwnd) or pid
        return self.process_names.name(pid)

    def info(self, hwnd: int) -> WindowInfo | None:
        if not hwnd or not w.IsWindow(hwnd):
            return None
        rect = w.frame_rect(hwnd) or (0, 0, 0, 0)
        return WindowInfo(
            hwnd=int(hwnd),
            title=w.window_text(hwnd),
            process=self.process_of(hwnd),
            rect=Rect.from_ltrb(*rect),
            minimized=bool(w.IsIconic(hwnd)),
            maximized=bool(w.IsZoomed(hwnd)),
        )

    def is_own(self, hwnd: int) -> bool:
        return bool(hwnd) and w.window_pid(hwnd) == self._own_pid

    def foreground(self) -> WindowInfo | None:
        return self.info(w.GetForegroundWindow())

    def list_windows(self) -> list[WindowInfo]:
        handles: list[int] = []

        def visit(hwnd, _lp) -> bool:
            handles.append(hwnd)
            return True

        w.EnumWindows(w.WNDENUMPROC(visit), 0)
        out: list[WindowInfo] = []
        for hwnd in handles:
            try:
                if not is_alt_tab_window(hwnd) or self.is_own(hwnd):
                    continue
                if not w.window_text(hwnd).strip():
                    continue
                info = self.info(hwnd)
            except OSError:
                continue
            if info is not None:
                out.append(info)
        return out

    def window_at(self, x: float, y: float) -> WindowInfo | None:
        """The window under a physical point, as its root owner; None over the desktop, the taskbar or nothing.

        ``WindowFromPhysicalPoint`` and two ``GetAncestor`` calls, so it costs tens of
        microseconds where ``list_windows`` walks every window, and it sends other apps no
        messages, so a hung app cannot hold the caller.  Sightline's own windows are looked
        through, click-through overlays and the dock alike: the answer is the app window a
        person is working in.  A dialog or popup an app owns stands for the app's window,
        unless that window is hidden or minimised.
        """
        try:
            root = w.window_at(x, y, self._own_pid)
            if root and self.is_own(root):
                root = w.top_level_at(x, y, skip_pid=self._own_pid)  # the dock or a chip: what is under it
            if not root:
                return None
            owner = w.GetAncestor(root, w.GA_ROOTOWNER) or root
            if owner != root and (self.is_own(owner) or not w.IsWindowVisible(owner) or w.IsIconic(owner)):
                owner = root
            if w.class_name(owner) in _SHELL_CLASSES:
                return None
            return self.info(owner)
        except OSError:
            return None

    # ------------------------------------------------------------------ activation

    def activate(self, hwnd: int) -> bool:
        if not hwnd or not w.IsWindow(hwnd):
            return False
        if w.IsIconic(hwnd):
            # Async: a hung target must not hang the action thread.
            w.ShowWindowAsync(hwnd, w.SW_RESTORE)
        if w.GetForegroundWindow() == hwnd:
            return True
        if self._nudge is not None:
            self._nudge()
        if w.SetForegroundWindow(hwnd) and _wait_foreground(hwnd, 0.15):
            return True
        if self._attach_and_activate(hwnd) and _wait_foreground(hwnd, 0.15):
            return True
        w.SwitchToThisWindow(hwnd, True)
        ok = _wait_foreground(hwnd, 0.4)
        if not ok:
            log.info("Could not bring window %s to the front", hwnd)  # not its title: titles name documents and people
        return ok

    @staticmethod
    def _attach_and_activate(hwnd: int) -> bool:
        current = w.GetCurrentThreadId()
        fg = w.GetForegroundWindow()
        fg_thread = w.window_thread(fg) if fg else 0
        attached = False
        try:
            if fg_thread and fg_thread != current:
                attached = bool(w.AttachThreadInput(current, fg_thread, True))
            w.BringWindowToTop(hwnd)
            ok = bool(w.SetForegroundWindow(hwnd))
            if attached:
                w.SetFocus(hwnd)
            return ok
        finally:
            if attached:
                w.AttachThreadInput(current, fg_thread, False)

    # ------------------------------------------------------------------ caption buttons

    def _target(self, hwnd: int | None) -> int:
        target = hwnd or w.GetForegroundWindow()
        if not target or not w.IsWindow(target):
            raise ActionError("There is no window to do that to")
        if w.class_name(target) in _SHELL_CLASSES:
            raise ActionError("There is no window to do that to")
        return int(target)

    def _syscommand(self, hwnd: int | None, command: int) -> int:
        target = self._target(hwnd)
        if not w.PostMessageW(target, w.WM_SYSCOMMAND, command, 0):
            if w.last_error() == w.ERROR_ACCESS_DENIED:
                raise ActionError(ELEVATED_MESSAGE)
            raise ActionError("That window did not respond")
        return target

    def close(self, hwnd: int | None = None) -> None:
        self._syscommand(hwnd, w.SC_CLOSE)

    def minimize(self, hwnd: int | None = None) -> None:
        self._syscommand(hwnd, w.SC_MINIMIZE)

    def maximize(self, hwnd: int | None = None) -> None:
        self._syscommand(hwnd, w.SC_MAXIMIZE)

    def restore(self, hwnd: int | None = None) -> None:
        self._syscommand(hwnd, w.SC_RESTORE)

    # ------------------------------------------------------------------ placement (for undo)

    def placement(self, hwnd: int) -> dict[str, Any] | None:
        """Where a window is and how it is shown, as plain numbers an Action can carry."""
        wp = w.WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(wp)
        if not hwnd or not w.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            return None
        n = wp.rcNormalPosition
        return {
            "show": int(wp.showCmd),
            "flags": int(wp.flags),
            "min": [wp.ptMinPosition.x, wp.ptMinPosition.y],
            "max": [wp.ptMaxPosition.x, wp.ptMaxPosition.y],
            "normal": [n.left, n.top, n.right, n.bottom],
            "rect": list(w.window_rect(hwnd) or ()),
        }

    def place(self, hwnd: int, placement: Mapping[str, Any]) -> None:
        """Put a window back as ``placement`` (from ``placement()``) describes it."""
        target = self._target(hwnd)
        try:
            wp = w.WINDOWPLACEMENT()
            wp.length = ctypes.sizeof(wp)
            # Asynchronous, like every other call here: a hung window must not hang the action thread.
            wp.flags = int(placement.get("flags", 0)) | w.WPF_ASYNCWINDOWPLACEMENT
            wp.showCmd = int(placement["show"])
            wp.ptMinPosition = w.POINT(*placement["min"])
            wp.ptMaxPosition = w.POINT(*placement["max"])
            wp.rcNormalPosition = w.RECT(*placement["normal"])
        except (KeyError, TypeError, ValueError):
            raise ActionError("That window cannot be put back") from None
        if not w.SetWindowPlacement(target, ctypes.byref(wp)):
            if w.last_error() == w.ERROR_ACCESS_DENIED:
                raise ActionError(ELEVATED_MESSAGE)
            raise ActionError("That window did not respond")
        rect = placement.get("rect") or ()
        if wp.showCmd == w.SW_SHOWNORMAL and len(rect) == 4:
            # A window snapped to a side reports its pre-snap size as its normal position, so the
            # placement alone may not move it back.  Its exact frame does.
            left, top, right, bottom = (int(v) for v in rect)
            w.SetWindowPos(target, None, left, top, right - left, bottom - top,
                           w.SWP_NOZORDER | w.SWP_NOACTIVATE | w.SWP_ASYNCWINDOWPOS)  # fmt: skip


def _wait_foreground(hwnd: int, timeout: float) -> bool:
    deadline = time.perf_counter() + timeout
    while True:
        if w.GetForegroundWindow() == hwnd:
            return True
        if time.perf_counter() >= deadline:
            return False
        time.sleep(0.01)


def popup_windows_of(hwnd: int, own_pid: int) -> list[int]:
    """Visible top-level windows of the same process stacked above ``hwnd``: menus, drop-downs, flyouts.

    They are separate windows, so a UI Automation search of ``hwnd`` alone misses them.
    """
    pid = w.window_pid(hwnd)
    if not pid or pid == own_pid:
        return []
    found: list[int] = []

    def visit(other, _lp) -> bool:
        if other == hwnd:
            return False  # z-order reached the window itself; anything below is behind it
        if w.IsWindowVisible(other) and not w.is_cloaked(other) and w.window_pid(other) == pid:
            rect = w.window_rect(other)
            if rect and rect[2] - rect[0] > 4 and rect[3] - rect[1] > 4:
                found.append(int(other))
        return True

    w.EnumWindows(w.WNDENUMPROC(visit), 0)
    return found[:6]
