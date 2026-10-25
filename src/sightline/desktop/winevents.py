"""A message-loop thread that hears about foreground, focus, menu and display changes.

Out-of-context WinEvent hooks cost the rest of the system nothing (events
are posted to this thread's queue) and arrive within a frame, so the focus
cache and the clickable scan react at once instead of on the next poll.  A
hidden top-level window receives the WM_DISPLAYCHANGE and WM_SETTINGCHANGE
broadcasts (message-only windows do not get broadcasts).

Callbacks run on this thread and must return quickly; they only signal the
worker threads.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from . import win32 as w

log = logging.getLogger(__name__)

_CLASS_NAME = "SightlineDesktopEvents"
_WM_KEEP_AWAKE = 0x8000 + 0x51  # WM_APP + n: wParam 1 keeps the screen on, 0 lets Windows decide


@dataclass
class WinEventCallbacks:
    foreground: Callable[[int], None] | None = None
    focus: Callable[[int], None] | None = None
    menu: Callable[[bool], None] | None = None  # True when a menu or popup opens
    display: Callable[[], None] | None = None  # monitors, resolution or DPI changed
    work_area: Callable[[], None] | None = None  # taskbar moved or resized


class WinEventThread:
    def __init__(self, callbacks: WinEventCallbacks) -> None:
        self._cb = callbacks
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        # ctypes callbacks must outlive the hooks, so they live on the instance.
        self._winevent_proc = w.WINEVENTPROC(self._on_winevent)
        self._wndproc = w.WNDPROC(self._on_message)
        self._hooks: list[int] = []
        self._hwnd = 0
        self._want_awake = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="sightline-winevents", daemon=True)
        self._thread.start()
        self._ready.wait(2.0)

    def keep_awake(self, on: bool) -> None:
        """Keep the display on and the PC out of sleep while Sightline runs.

        A person who cannot type a password must not find the screen locked
        after a quiet spell.  Windows ties this request to the thread that made
        it, so it is made on this thread, which lives as long as the app.
        """
        self._want_awake = bool(on)
        if self._thread_id:
            w.PostThreadMessageW(self._thread_id, _WM_KEEP_AWAKE, 1 if on else 0, 0)

    def stop(self, timeout: float = 2.0) -> None:
        thread = self._thread
        if thread is None:
            return
        if self._thread_id:
            w.PostThreadMessageW(self._thread_id, w.WM_QUIT, 0, 0)
        thread.join(timeout)
        if thread.is_alive():
            log.warning("Desktop event thread did not stop in time")
        self._thread = None

    # ------------------------------------------------------------------ thread

    def _run(self) -> None:
        w.set_thread_dpi_awareness()
        self._thread_id = w.GetCurrentThreadId()
        hinstance = w.GetModuleHandleW(None)
        atom = 0
        try:
            wc = w.WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinstance
            wc.lpszClassName = _CLASS_NAME
            atom = w.RegisterClassW(ctypes.byref(wc))
            if atom:
                self._hwnd = w.CreateWindowExW(
                    w.WS_EX_TOOLWINDOW | w.WS_EX_NOACTIVATE, _CLASS_NAME, "Sightline desktop events",
                    w.WS_POPUP, 0, 0, 0, 0, None, None, hinstance, None,
                )  # fmt: skip
            # Foreground changes include our own windows (the settings window counts); focus does not.
            self._hook(w.EVENT_SYSTEM_FOREGROUND, w.EVENT_SYSTEM_FOREGROUND, own=True)
            self._hook(w.EVENT_SYSTEM_MENUPOPUPSTART, w.EVENT_SYSTEM_MENUPOPUPEND, own=False)
            self._hook(w.EVENT_SYSTEM_MINIMIZEEND, w.EVENT_SYSTEM_MINIMIZEEND, own=False)
            self._hook(w.EVENT_OBJECT_FOCUS, w.EVENT_OBJECT_FOCUS, own=False)
        except Exception:
            log.exception("Could not set up desktop event hooks")
        finally:
            self._ready.set()
        self._apply_keep_awake(self._want_awake)
        msg = w.MSG()
        try:
            while True:
                result = w.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0 or result == -1:
                    break
                if not msg.hwnd and msg.message == _WM_KEEP_AWAKE:
                    self._apply_keep_awake(bool(msg.wParam))
                    continue
                w.TranslateMessage(ctypes.byref(msg))
                w.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._apply_keep_awake(False)
            for hook in self._hooks:
                w.UnhookWinEvent(hook)
            self._hooks.clear()
            if self._hwnd:
                w.DestroyWindow(self._hwnd)
                self._hwnd = 0
            if atom:
                w.UnregisterClassW(_CLASS_NAME, hinstance)
            self._thread_id = 0

    @staticmethod
    def _apply_keep_awake(on: bool) -> None:
        flags = w.ES_CONTINUOUS | ((w.ES_SYSTEM_REQUIRED | w.ES_DISPLAY_REQUIRED) if on else 0)
        if not w.SetThreadExecutionState(flags):
            log.warning("Windows refused to keep the screen on")

    def _hook(self, first: int, last: int, *, own: bool) -> None:
        flags = w.WINEVENT_OUTOFCONTEXT | (0 if own else w.WINEVENT_SKIPOWNPROCESS)
        hook = w.SetWinEventHook(first, last, None, self._winevent_proc, 0, 0, flags)
        if hook:
            self._hooks.append(hook)
        else:
            log.warning("SetWinEventHook(%#x) failed", first)

    def _on_winevent(self, _hook, event, hwnd, id_object, _id_child, _thread, _time) -> None:
        try:
            if event == w.EVENT_SYSTEM_FOREGROUND or event == w.EVENT_SYSTEM_MINIMIZEEND:
                if self._cb.foreground:
                    self._cb.foreground(int(hwnd or 0))
            elif event == w.EVENT_OBJECT_FOCUS:
                if self._cb.focus:
                    self._cb.focus(int(hwnd or 0))
            elif event in (w.EVENT_SYSTEM_MENUPOPUPSTART, w.EVENT_SYSTEM_MENUPOPUPEND):
                if self._cb.menu:
                    self._cb.menu(event == w.EVENT_SYSTEM_MENUPOPUPSTART)
        except Exception:  # an exception escaping a ctypes callback is only printed; log it properly
            log.exception("WinEvent handler failed")

    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == w.WM_DISPLAYCHANGE or msg == w.WM_DPICHANGED:
                if self._cb.display:
                    self._cb.display()
            elif msg == w.WM_SETTINGCHANGE and wparam == w.SPI_SETWORKAREA:
                if self._cb.work_area:
                    self._cb.work_area()
        except Exception:
            log.exception("Desktop event window handler failed")
        return w.DefWindowProcW(hwnd, msg, wparam, lparam)
