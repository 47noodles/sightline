"""Detecting windows Windows will not let Sightline control.

User Interface Privilege Isolation (UIPI) silently drops input that a
process injects into a window of a higher integrity level, typically an app
"run as administrator".  ``SendInput`` still reports success, so the only way
to tell the user is to look first: compare the target process's integrity
level with ours.  A process with UIAccess (signed, in Program Files, manifest
``uiAccess="true"``) is exempt.

While a UAC prompt or the sign-in screen is showing, input goes to the
secure desktop, which no ordinary process can reach at all.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes as wt

from . import win32 as w

log = logging.getLogger(__name__)

# Running Sightline itself as administrator is not offered as the fix: the whole app, camera and
# microphone included, would then run with full rights.  A signed UIAccess build is the planned route.
ELEVATED_MESSAGE = (
    "Windows blocks Sightline from controlling apps running as administrator.  "
    "Windows Voice Access or a helper can handle this window."
)
SECURE_DESKTOP_MESSAGE = "Windows is showing a secure screen, such as a permission prompt.  Sightline cannot control it."

# Integrity RIDs (S-1-16-xxxx).
UNTRUSTED = 0x0000
LOW = 0x1000
MEDIUM = 0x2000
MEDIUM_PLUS = 0x2100
HIGH = 0x3000
SYSTEM = 0x4000


def _token_dword(token: int, info_class: int) -> int | None:
    value = wt.DWORD()
    size = wt.DWORD()
    if not w.GetTokenInformation(token, info_class, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(size)):
        return None
    return value.value


def _token_integrity(token: int) -> int | None:
    size = wt.DWORD()
    w.GetTokenInformation(token, w.TokenIntegrityLevel, None, 0, ctypes.byref(size))
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if not w.GetTokenInformation(token, w.TokenIntegrityLevel, buf, size, ctypes.byref(size)):
        return None
    label = ctypes.cast(buf, ctypes.POINTER(w.TOKEN_MANDATORY_LABEL)).contents
    count = w.GetSidSubAuthorityCount(label.Sid).contents.value
    if count == 0:
        return None
    return int(w.GetSidSubAuthority(label.Sid, count - 1).contents.value)


def process_integrity(pid: int) -> int | None:
    """Integrity RID of a process, or None when Windows refuses to say (protected and system processes)."""
    handle = w.GetCurrentProcess() if pid == w.GetCurrentProcessId() else w.OpenProcess(w.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    token = wt.HANDLE()
    try:
        if not w.OpenProcessToken(handle, w.TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            return _token_integrity(token.value)
        finally:
            w.CloseHandle(token)
    finally:
        if pid != w.GetCurrentProcessId():
            w.CloseHandle(handle)


def own_ui_access() -> bool:
    token = wt.HANDLE()
    if not w.OpenProcessToken(w.GetCurrentProcess(), w.TOKEN_QUERY, ctypes.byref(token)):
        return False
    try:
        return bool(_token_dword(token.value, w.TokenUIAccess))
    finally:
        w.CloseHandle(token)


def account_kind() -> str:
    """How Windows treats this user's requests for administrator rights.

    "admin": an administrator whom Windows asks for consent (the usual set-up).  "standard": a
    standard user, asked for an administrator's password.  "unprompted": never asked (the
    built-in Administrator account, or User Account Control turned off).
    """
    token = wt.HANDLE()
    if not w.OpenProcessToken(w.GetCurrentProcess(), w.TOKEN_QUERY, ctypes.byref(token)):
        return "standard"
    try:
        kind = _token_dword(token.value, w.TokenElevationType)
    finally:
        w.CloseHandle(token)
    if kind in (2, 3):  # elevated, or limited with an elevated twin: a split administrator token
        return "admin"
    return "unprompted" if w.IsUserAnAdmin() else "standard"


def input_blocked(own: int | None, target: int | None, *, ui_access: bool = False) -> bool:
    """Whether UIPI drops our input to a process.  Unknown target levels count as blocked:
    Windows hides the integrity of exactly the processes we may not touch."""
    if ui_access:
        return False
    if own is None:
        return False  # cannot tell; do not cry wolf
    if target is None:
        return True
    return target > own


class ElevationGuard:
    """Answers "will Windows drop our input to this window?" with a short per-process cache."""

    def __init__(self, ttl: float = 5.0) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[int, tuple[float, int | None]] = {}
        self._own: int | None = None
        self._ui_access: bool | None = None
        self._own_pid = w.GetCurrentProcessId()

    def _own_level(self) -> int | None:
        if self._own is None:
            self._own = process_integrity(self._own_pid)
            self._ui_access = own_ui_access()
            log.info(
                "Sightline integrity level %s%s",
                hex(self._own) if self._own is not None else "unknown",
                " with UIAccess" if self._ui_access else "",
            )
        return self._own

    def is_elevated_self(self) -> bool:
        level = self._own_level()
        return level is not None and level >= HIGH

    def _level(self, pid: int) -> int | None:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(pid)
            if hit is not None and now - hit[0] < self._ttl:
                return hit[1]
        level = process_integrity(pid)
        with self._lock:
            if len(self._cache) > 256:
                self._cache.clear()
            self._cache[pid] = (now, level)
        return level

    def window_blocked(self, hwnd: int) -> bool:
        """True when input meant for this window would be dropped by Windows."""
        if not hwnd:
            return False
        pid = w.window_pid(hwnd)
        if not pid or pid == self._own_pid:
            return False
        own = self._own_level()
        return input_blocked(own, self._level(pid), ui_access=bool(self._ui_access))

    def foreground_blocked(self) -> bool:
        return self.window_blocked(w.GetForegroundWindow())

    def point_blocked(self, x: float, y: float) -> bool:
        return self.window_blocked(w.window_at(x, y, self._own_pid))

    @staticmethod
    def secure_desktop_active() -> bool:
        """True while a UAC prompt, the lock screen or Ctrl+Alt+Delete owns the input."""
        name = w.input_desktop_name()
        return name is not None and name.lower() != "default"
