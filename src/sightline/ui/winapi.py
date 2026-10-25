"""Small Win32 helpers for window styles, the dark title bar and the dock's app bar.

Every function is a no-op unless Qt runs on the real ``windows`` platform:
under the ``offscreen`` platform (tests, the playground) ``winId()`` is a
made-up number and calling Win32 with it could touch an unrelated window.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

from sightline.core.types import Rect

log = logging.getLogger(__name__)

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_DONOTROUND = 1


def native_windows() -> bool:
    return sys.platform == "win32" and QGuiApplication.platformName() == "windows"


def _hwnd(widget: QWidget) -> int:
    return int(widget.winId())


def _user32():  # noqa: ANN202 - ctypes library object
    user32 = ctypes.windll.user32
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    return user32


def add_ex_style(widget: QWidget, bits: int) -> bool:
    """OR extended style bits into a top-level widget's native window.  Returns True when applied."""
    if not native_windows():
        return False
    try:
        user32 = _user32()
        hwnd = _hwnd(widget)
        current = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        wanted = current | bits
        if wanted != current:
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, wanted)
            user32.SetWindowPos(
                hwnd, None, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
            )
        return True
    except Exception:
        log.exception("Could not set window styles")
        return False


def make_click_through(widget: QWidget) -> bool:
    """Overlays: input passes to the window underneath, never activated, not in Alt+Tab.

    Qt already sets most of these for FramelessWindowHint, Tool,
    WindowTransparentForInput and WindowDoesNotAcceptFocus with a translucent
    background; setting them again is harmless and covers Qt versions that
    differ.  WS_EX_LAYERED is only safe here because these windows are
    translucent, so Qt paints them with UpdateLayeredWindow.
    """
    return add_ex_style(widget, WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)


def make_no_activate(widget: QWidget) -> bool:
    """The dock and the help sheet: clickable, but a click never takes focus from the user's app."""
    return add_ex_style(widget, WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)


def set_dark_title_bar(widget: QWidget, dark: bool) -> None:
    """Match the native title bar to the theme (Windows 10 2004 and later)."""
    if not native_windows():
        return
    try:
        value = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(_hwnd(widget)), DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value)
        )
    except Exception:
        log.debug("DwmSetWindowAttribute failed", exc_info=True)


def square_corners(widget: QWidget) -> None:
    """Windows 11 rounds every top-level window; the dock and panels are square by design."""
    if not native_windows():
        return
    try:
        value = ctypes.c_int(DWMWCP_DONOTROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(_hwnd(widget)), DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(value), ctypes.sizeof(value)
        )
    except Exception:
        log.debug("Could not turn off rounded corners", exc_info=True)


# --------------------------------------------------------------------------- app bar

ABM_NEW = 0x00
ABM_REMOVE = 0x01
ABM_QUERYPOS = 0x02
ABM_SETPOS = 0x03
ABE_LEFT = 0
ABE_TOP = 1
ABE_RIGHT = 2
ABE_BOTTOM = 3
ABN_POSCHANGED = 0x01
ABN_FULLSCREENAPP = 0x02
WM_USER = 0x0400
APPBAR_CALLBACK = WM_USER + 0x5A1  # arbitrary, private to the dock window

_EDGES = {"left": ABE_LEFT, "top": ABE_TOP, "right": ABE_RIGHT, "bottom": ABE_BOTTOM}


class APPBARDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uCallbackMessage", wintypes.UINT),
        ("uEdge", wintypes.UINT),
        ("rc", wintypes.RECT),
        ("lParam", wintypes.LPARAM),
    ]


class AppBar:
    """Reserves a strip of the screen for the dock, like the taskbar does.

    Without it, maximised windows slide under the dock and their scroll bars
    and close buttons end up behind it.  The shell adjusts every other
    window's work area for us.
    """

    def __init__(self, widget: QWidget) -> None:
        self._widget = widget
        self._registered = False

    @property
    def registered(self) -> bool:
        return self._registered

    def _data(self) -> APPBARDATA:
        data = APPBARDATA()
        data.cbSize = ctypes.sizeof(APPBARDATA)
        data.hWnd = wintypes.HWND(_hwnd(self._widget))
        return data

    def _call(self, message: int, data: APPBARDATA) -> int:
        shell32 = ctypes.windll.shell32
        shell32.SHAppBarMessage.restype = ctypes.c_size_t
        shell32.SHAppBarMessage.argtypes = [wintypes.DWORD, ctypes.POINTER(APPBARDATA)]
        return int(shell32.SHAppBarMessage(message, ctypes.byref(data)))

    def reserve(self, edge: str, monitor: Rect, thickness: int) -> Rect | None:
        """Claim ``thickness`` physical pixels along ``edge`` of ``monitor``.  Returns the granted rectangle."""
        if not native_windows():
            return None
        try:
            if not self._registered:
                data = self._data()
                data.uCallbackMessage = APPBAR_CALLBACK
                if not self._call(ABM_NEW, data):
                    log.warning("The shell refused the dock app bar")
                    return None
                self._registered = True
            data = self._data()
            data.uEdge = _EDGES.get(edge, ABE_RIGHT)
            left, top, right, bottom = int(monitor.left), int(monitor.top), int(monitor.right), int(monitor.bottom)
            if edge == "left":
                right = left + thickness
            elif edge == "top":
                bottom = top + thickness
            else:
                left = right - thickness
            data.rc = wintypes.RECT(left, top, right, bottom)
            self._call(ABM_QUERYPOS, data)
            # The shell may have moved the proposed edge inward past other bars; keep our thickness.
            rc = data.rc
            if edge == "left":
                rc.right = rc.left + thickness
            elif edge == "top":
                rc.bottom = rc.top + thickness
            else:
                rc.left = rc.right - thickness
            data.rc = rc
            self._call(ABM_SETPOS, data)
            return Rect.from_ltrb(data.rc.left, data.rc.top, data.rc.right, data.rc.bottom)
        except Exception:
            log.exception("App bar registration failed")
            return None

    def release(self) -> None:
        if not self._registered or not native_windows():
            self._registered = False
            return
        try:
            self._call(ABM_REMOVE, self._data())
        except Exception:
            log.exception("Could not remove the dock app bar")
        self._registered = False


def parse_native_message(message: object) -> tuple[int, int, int] | None:
    """(msg, wParam, lParam) from the ``message`` pointer that ``nativeEvent`` receives on Windows."""
    try:
        msg = wintypes.MSG.from_address(int(message))  # type: ignore[arg-type]
        return int(msg.message), int(msg.wParam), int(msg.lParam)
    except Exception:
        return None
