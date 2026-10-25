"""ctypes declarations for the Win32 calls the desktop module makes.

The module loads its own ``WinDLL`` instances instead of using the shared
``ctypes.windll`` objects, so argtypes set here never clash with argtypes
that another subsystem sets on the same functions.

Every coordinate is a physical pixel: the process is per-monitor DPI aware v2
(``ensure_dpi_awareness`` checks and, in tests and tools, sets it).
"""

from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes as wt

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
try:
    shcore = ctypes.WinDLL("shcore", use_last_error=True)
except OSError:  # Windows 7 has no shcore; Sightline needs 10+, but stay importable
    shcore = None

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
LONG_PTR = ctypes.c_ssize_t

# Tag written into dwExtraInfo of every event Sightline injects, so hooks can tell ours apart.
SIGHTLINE_INPUT_TAG = 0x5349474C  # "SIGL"

# --------------------------------------------------------------------------- constants

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

WHEEL_DELTA = 120

SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_SWAPBUTTON = 23
SM_CXDOUBLECLK = 36
SM_CYDOUBLECLK = 37
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CMONITORS = 80
SM_REMOTESESSION = 0x1000

MAPVK_VK_TO_VSC_EX = 4

GA_ROOT = 2
GA_ROOTOWNER = 3
GW_OWNER = 4
GWL_STYLE = -16
GWL_EXSTYLE = -20

WS_VISIBLE = 0x10000000
WS_MINIMIZE = 0x20000000
WS_MAXIMIZE = 0x01000000
WS_POPUP = 0x80000000
WS_CHILD = 0x40000000
WS_DISABLED = 0x08000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x00080000

SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_MAXIMIZE = 3
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9

WM_NULL = 0x0000
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_SETTINGCHANGE = 0x001A
WM_GETOBJECT = 0x003D
WM_DISPLAYCHANGE = 0x007E
WM_SYSCOMMAND = 0x0112
WM_TIMER = 0x0113
WM_DPICHANGED = 0x02E0
WM_USER = 0x0400

SC_SIZE = 0xF000
SC_MINIMIZE = 0xF020
SC_MAXIMIZE = 0xF030
SC_CLOSE = 0xF060
SC_RESTORE = 0xF120

SMTO_ABORTIFHUNG = 0x0002
SMTO_BLOCK = 0x0001

MONITOR_DEFAULTTONULL = 0
MONITOR_DEFAULTTOPRIMARY = 1
MONITOR_DEFAULTTONEAREST = 2
MONITORINFOF_PRIMARY = 1
MDT_EFFECTIVE_DPI = 0

DWMWA_CLOAKED = 14
DWMWA_EXTENDED_FRAME_BOUNDS = 9

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
TOKEN_QUERY = 0x0008
TokenElevationType = 18  # 1 default (standard user, or UAC off), 2 elevated, 3 administrator running limited
TokenElevation = 20
TokenIntegrityLevel = 25
TokenUIAccess = 26

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_ASYNCWINDOWPOS = 0x4000  # posted to the window's thread, so a hung window cannot hang the caller
WPF_ASYNCWINDOWPLACEMENT = 0x0004

ASFW_ANY = 0xFFFFFFFF

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MENUSTART = 0x0004
EVENT_SYSTEM_MENUEND = 0x0005
EVENT_SYSTEM_MENUPOPUPSTART = 0x0006
EVENT_SYSTEM_MENUPOPUPEND = 0x0007
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_FOCUS = 0x8005
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

SPI_GETWORKAREA = 0x0030
SPI_SETWORKAREA = 0x002F

UOI_NAME = 2
DESKTOP_READOBJECTS = 0x0001

ERROR_ACCESS_DENIED = 5
ERROR_INVALID_WINDOW_HANDLE = 1400

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_CONTINUOUS = 0x80000000
WM_APP = 0x8000

# --------------------------------------------------------------------------- structures


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wt.DWORD),
        ("szDevice", wt.WCHAR * 32),
    ]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("DeviceName", wt.WCHAR * 32),
        ("DeviceString", wt.WCHAR * 128),
        ("StateFlags", wt.DWORD),
        ("DeviceID", wt.WCHAR * 128),
        ("DeviceKey", wt.WCHAR * 128),
    ]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("flags", wt.DWORD),
        ("hwndActive", wt.HWND),
        ("hwndFocus", wt.HWND),
        ("hwndCapture", wt.HWND),
        ("hwndMenuOwner", wt.HWND),
        ("hwndMoveSize", wt.HWND),
        ("hwndCaret", wt.HWND),
        ("rcCaret", RECT),
    ]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wt.UINT),
        ("flags", wt.UINT),
        ("showCmd", wt.UINT),
        ("ptMinPosition", POINT),
        ("ptMaxPosition", POINT),
        ("rcNormalPosition", RECT),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("message", wt.UINT),
        ("wParam", wt.WPARAM),
        ("lParam", wt.LPARAM),
        ("time", wt.DWORD),
        ("pt", POINT),
        ("lPrivate", wt.DWORD),
    ]


WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(RECT), wt.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD
)


def _fn(dll: ctypes.WinDLL, name: str, restype, *argtypes):
    f = getattr(dll, name)
    f.restype = restype
    f.argtypes = list(argtypes)
    return f


# --------------------------------------------------------------------------- user32

SendInput = _fn(user32, "SendInput", wt.UINT, wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
SetCursorPos = _fn(user32, "SetCursorPos", wt.BOOL, ctypes.c_int, ctypes.c_int)
GetCursorPos = _fn(user32, "GetCursorPos", wt.BOOL, ctypes.POINTER(POINT))
GetSystemMetrics = _fn(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
GetDoubleClickTime = _fn(user32, "GetDoubleClickTime", wt.UINT)
MapVirtualKeyExW = _fn(user32, "MapVirtualKeyExW", wt.UINT, wt.UINT, wt.UINT, wt.HKL)
VkKeyScanExW = _fn(user32, "VkKeyScanExW", ctypes.c_short, wt.WCHAR, wt.HKL)
GetKeyboardLayout = _fn(user32, "GetKeyboardLayout", wt.HKL, wt.DWORD)
GetAsyncKeyState = _fn(user32, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
GetKeyState = _fn(user32, "GetKeyState", ctypes.c_short, ctypes.c_int)
GetForegroundWindow = _fn(user32, "GetForegroundWindow", wt.HWND)
SetForegroundWindow = _fn(user32, "SetForegroundWindow", wt.BOOL, wt.HWND)
BringWindowToTop = _fn(user32, "BringWindowToTop", wt.BOOL, wt.HWND)
SetFocus = _fn(user32, "SetFocus", wt.HWND, wt.HWND)
SetActiveWindow = _fn(user32, "SetActiveWindow", wt.HWND, wt.HWND)
SwitchToThisWindow = _fn(user32, "SwitchToThisWindow", None, wt.HWND, wt.BOOL)
AllowSetForegroundWindow = _fn(user32, "AllowSetForegroundWindow", wt.BOOL, wt.DWORD)
AttachThreadInput = _fn(user32, "AttachThreadInput", wt.BOOL, wt.DWORD, wt.DWORD, wt.BOOL)
GetWindowThreadProcessId = _fn(user32, "GetWindowThreadProcessId", wt.DWORD, wt.HWND, ctypes.POINTER(wt.DWORD))
GetGUIThreadInfo = _fn(user32, "GetGUIThreadInfo", wt.BOOL, wt.DWORD, ctypes.POINTER(GUITHREADINFO))
WindowFromPoint = _fn(user32, "WindowFromPoint", wt.HWND, POINT)
WindowFromPhysicalPoint = _fn(user32, "WindowFromPhysicalPoint", wt.HWND, POINT)
GetAncestor = _fn(user32, "GetAncestor", wt.HWND, wt.HWND, wt.UINT)
GetWindow = _fn(user32, "GetWindow", wt.HWND, wt.HWND, wt.UINT)
GetLastActivePopup = _fn(user32, "GetLastActivePopup", wt.HWND, wt.HWND)
GetShellWindow = _fn(user32, "GetShellWindow", wt.HWND)
GetDesktopWindow = _fn(user32, "GetDesktopWindow", wt.HWND)
IsWindow = _fn(user32, "IsWindow", wt.BOOL, wt.HWND)
IsWindowVisible = _fn(user32, "IsWindowVisible", wt.BOOL, wt.HWND)
IsWindowEnabled = _fn(user32, "IsWindowEnabled", wt.BOOL, wt.HWND)
IsIconic = _fn(user32, "IsIconic", wt.BOOL, wt.HWND)
IsZoomed = _fn(user32, "IsZoomed", wt.BOOL, wt.HWND)
IsHungAppWindow = _fn(user32, "IsHungAppWindow", wt.BOOL, wt.HWND)
GetWindowTextLengthW = _fn(user32, "GetWindowTextLengthW", ctypes.c_int, wt.HWND)
GetWindowTextW = _fn(user32, "GetWindowTextW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
GetClassNameW = _fn(user32, "GetClassNameW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
GetWindowLongPtrW = _fn(user32, "GetWindowLongPtrW", LONG_PTR, wt.HWND, ctypes.c_int)
GetWindowRect = _fn(user32, "GetWindowRect", wt.BOOL, wt.HWND, ctypes.POINTER(RECT))
GetWindowPlacement = _fn(user32, "GetWindowPlacement", wt.BOOL, wt.HWND, ctypes.POINTER(WINDOWPLACEMENT))
SetWindowPlacement = _fn(user32, "SetWindowPlacement", wt.BOOL, wt.HWND, ctypes.POINTER(WINDOWPLACEMENT))
SetWindowPos = _fn(user32, "SetWindowPos", wt.BOOL, wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT)
EnumWindows = _fn(user32, "EnumWindows", wt.BOOL, WNDENUMPROC, wt.LPARAM)
EnumChildWindows = _fn(user32, "EnumChildWindows", wt.BOOL, wt.HWND, WNDENUMPROC, wt.LPARAM)
FindWindowW = _fn(user32, "FindWindowW", wt.HWND, wt.LPCWSTR, wt.LPCWSTR)
FindWindowExW = _fn(user32, "FindWindowExW", wt.HWND, wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR)
ShowWindow = _fn(user32, "ShowWindow", wt.BOOL, wt.HWND, ctypes.c_int)
ShowWindowAsync = _fn(user32, "ShowWindowAsync", wt.BOOL, wt.HWND, ctypes.c_int)
PostMessageW = _fn(user32, "PostMessageW", wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
SendMessageTimeoutW = _fn(
    user32,
    "SendMessageTimeoutW",
    LRESULT,
    wt.HWND,
    wt.UINT,
    wt.WPARAM,
    wt.LPARAM,
    wt.UINT,
    wt.UINT,
    ctypes.POINTER(ULONG_PTR),
)
PostThreadMessageW = _fn(user32, "PostThreadMessageW", wt.BOOL, wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
GetMessageW = _fn(user32, "GetMessageW", wt.BOOL, ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT)
PeekMessageW = _fn(user32, "PeekMessageW", wt.BOOL, ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT)
TranslateMessage = _fn(user32, "TranslateMessage", wt.BOOL, ctypes.POINTER(MSG))
DispatchMessageW = _fn(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(MSG))
DefWindowProcW = _fn(user32, "DefWindowProcW", LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
RegisterClassW = _fn(user32, "RegisterClassW", wt.ATOM, ctypes.POINTER(WNDCLASSW))
UnregisterClassW = _fn(user32, "UnregisterClassW", wt.BOOL, wt.LPCWSTR, wt.HINSTANCE)
CreateWindowExW = _fn(
    user32,
    "CreateWindowExW",
    wt.HWND,
    wt.DWORD,
    wt.LPCWSTR,
    wt.LPCWSTR,
    wt.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wt.HWND,
    wt.HMENU,
    wt.HINSTANCE,
    wt.LPVOID,
)
DestroyWindow = _fn(user32, "DestroyWindow", wt.BOOL, wt.HWND)
SetWinEventHook = _fn(
    user32, "SetWinEventHook", wt.HANDLE, wt.DWORD, wt.DWORD, wt.HMODULE, WINEVENTPROC, wt.DWORD, wt.DWORD, wt.DWORD
)
UnhookWinEvent = _fn(user32, "UnhookWinEvent", wt.BOOL, wt.HANDLE)
EnumDisplayMonitors = _fn(user32, "EnumDisplayMonitors", wt.BOOL, wt.HDC, ctypes.POINTER(RECT), MONITORENUMPROC, wt.LPARAM)
GetMonitorInfoW = _fn(user32, "GetMonitorInfoW", wt.BOOL, wt.HMONITOR, ctypes.POINTER(MONITORINFOEXW))
MonitorFromPoint = _fn(user32, "MonitorFromPoint", wt.HMONITOR, POINT, wt.DWORD)
MonitorFromWindow = _fn(user32, "MonitorFromWindow", wt.HMONITOR, wt.HWND, wt.DWORD)
EnumDisplayDevicesW = _fn(user32, "EnumDisplayDevicesW", wt.BOOL, wt.LPCWSTR, wt.DWORD, ctypes.POINTER(DISPLAY_DEVICEW), wt.DWORD)
GetDpiForWindow = _fn(user32, "GetDpiForWindow", wt.UINT, wt.HWND)
SetThreadDpiAwarenessContext = _fn(user32, "SetThreadDpiAwarenessContext", ctypes.c_void_p, ctypes.c_void_p)
GetThreadDpiAwarenessContext = _fn(user32, "GetThreadDpiAwarenessContext", ctypes.c_void_p)
AreDpiAwarenessContextsEqual = _fn(user32, "AreDpiAwarenessContextsEqual", wt.BOOL, ctypes.c_void_p, ctypes.c_void_p)
SetProcessDpiAwarenessContext = _fn(user32, "SetProcessDpiAwarenessContext", wt.BOOL, ctypes.c_void_p)
OpenInputDesktop = _fn(user32, "OpenInputDesktop", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
CloseDesktop = _fn(user32, "CloseDesktop", wt.BOOL, wt.HANDLE)
GetUserObjectInformationW = _fn(
    user32, "GetUserObjectInformationW", wt.BOOL, wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD)
)

# --------------------------------------------------------------------------- kernel32 / advapi32

GetCurrentProcessId = _fn(kernel32, "GetCurrentProcessId", wt.DWORD)
GetCurrentThreadId = _fn(kernel32, "GetCurrentThreadId", wt.DWORD)
GetCurrentProcess = _fn(kernel32, "GetCurrentProcess", wt.HANDLE)
OpenProcess = _fn(kernel32, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
CloseHandle = _fn(kernel32, "CloseHandle", wt.BOOL, wt.HANDLE)
QueryFullProcessImageNameW = _fn(
    kernel32, "QueryFullProcessImageNameW", wt.BOOL, wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)
)
GetProcessTimes = _fn(
    kernel32,
    "GetProcessTimes",
    wt.BOOL,
    wt.HANDLE,
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
)
TerminateProcess = _fn(kernel32, "TerminateProcess", wt.BOOL, wt.HANDLE, wt.UINT)
SetThreadExecutionState = _fn(kernel32, "SetThreadExecutionState", wt.DWORD, wt.DWORD)
GetModuleHandleW = _fn(kernel32, "GetModuleHandleW", wt.HMODULE, wt.LPCWSTR)
GetSystemWindowsDirectoryW = _fn(kernel32, "GetSystemWindowsDirectoryW", wt.UINT, wt.LPWSTR, wt.UINT)

OpenProcessToken = _fn(advapi32, "OpenProcessToken", wt.BOOL, wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE))
GetTokenInformation = _fn(
    advapi32, "GetTokenInformation", wt.BOOL, wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD)
)
GetSidSubAuthorityCount = _fn(advapi32, "GetSidSubAuthorityCount", ctypes.POINTER(ctypes.c_ubyte), ctypes.c_void_p)
GetSidSubAuthority = _fn(advapi32, "GetSidSubAuthority", ctypes.POINTER(wt.DWORD), ctypes.c_void_p, wt.DWORD)

ShellExecuteW = _fn(shell32, "ShellExecuteW", wt.HINSTANCE, wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int)
IsUserAnAdmin = _fn(shell32, "IsUserAnAdmin", wt.BOOL)

DwmGetWindowAttribute = _fn(dwmapi, "DwmGetWindowAttribute", ctypes.c_long, wt.HWND, wt.DWORD, wt.LPVOID, wt.DWORD)

if shcore is not None:
    GetDpiForMonitor = _fn(shcore, "GetDpiForMonitor", ctypes.c_long, wt.HMONITOR, ctypes.c_int, ctypes.POINTER(wt.UINT), ctypes.POINTER(wt.UINT))
else:  # pragma: no cover - Windows 7 only
    GetDpiForMonitor = None


# --------------------------------------------------------------------------- small helpers


def last_error() -> int:
    return ctypes.get_last_error()


def ensure_dpi_awareness() -> bool:
    """Make sure the process is per-monitor DPI aware v2, so every coordinate is a physical pixel.

    The app sets this before Qt starts; tests and tools may not.  Returns True
    when the calling thread ends up per-monitor v2 aware.
    """
    try:
        current = GetThreadDpiAwarenessContext()
        if AreDpiAwarenessContextsEqual(current, ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
            return True
        if SetProcessDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
            return True
        # Someone already chose a different process mode.  Fix this thread at least.
        SetThreadDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
        log.warning("Process is not per-monitor DPI aware v2; coordinates may be scaled on other threads")
        return False
    except (AttributeError, OSError):  # pragma: no cover - very old Windows
        return False


def set_thread_dpi_awareness() -> None:
    """Per-monitor v2 for the calling thread (desktop-owned threads call this first)."""
    try:
        SetThreadDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
    except (AttributeError, OSError):  # pragma: no cover
        pass


def cursor_pos() -> tuple[int, int]:
    p = POINT()
    if not GetCursorPos(ctypes.byref(p)):
        return (0, 0)
    return (p.x, p.y)


def virtual_screen() -> tuple[int, int, int, int]:
    """(left, top, width, height) of the virtual desktop in physical pixels."""
    return (
        GetSystemMetrics(SM_XVIRTUALSCREEN),
        GetSystemMetrics(SM_YVIRTUALSCREEN),
        max(1, GetSystemMetrics(SM_CXVIRTUALSCREEN)),
        max(1, GetSystemMetrics(SM_CYVIRTUALSCREEN)),
    )


def window_text(hwnd: int) -> str:
    n = GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    r = RECT()
    if not GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


def frame_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """The visible frame (without the invisible resize borders Windows 10+ adds), falling back to the window rect."""
    r = RECT()
    if DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) == 0:
        return (r.left, r.top, r.right, r.bottom)
    return window_rect(hwnd)


def window_pid(hwnd: int) -> int:
    pid = wt.DWORD()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def window_thread(hwnd: int) -> int:
    return GetWindowThreadProcessId(hwnd, None)


def is_cloaked(hwnd: int) -> bool:
    """True for windows DWM hides (suspended UWP apps, windows on other virtual desktops)."""
    cloaked = wt.DWORD()
    if DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) != 0:
        return False
    return cloaked.value != 0


def root_window(hwnd: int) -> int:
    if not hwnd:
        return 0
    return GetAncestor(hwnd, GA_ROOT) or hwnd


def window_at(x: float, y: float, look_through_pid: int = 0) -> int:
    """Top-level window under a physical point (0 when none).

    With ``look_through_pid``, a click-through window of that process (a
    Sightline overlay) is looked through to the window a click would reach.
    """
    try:
        hwnd = WindowFromPhysicalPoint(POINT(int(x), int(y)))
    except OSError:  # pragma: no cover
        hwnd = WindowFromPoint(POINT(int(x), int(y)))
    root = root_window(hwnd) if hwnd else 0
    if look_through_pid and root and window_pid(root) == look_through_pid:
        if GetWindowLongPtrW(root, GWL_EXSTYLE) & WS_EX_TRANSPARENT:
            return top_level_at(x, y, skip_pid=look_through_pid)
    return root


def top_level_at(x: float, y: float, skip_pid: int = 0) -> int:
    """The front-most visible top-level window containing the point, skipping one process and
    click-through windows.  Slower than ``window_at`` (a z-order walk), so only a fallback."""
    found = [0]

    def visit(hwnd, _lp) -> bool:
        if not IsWindowVisible(hwnd) or IsIconic(hwnd) or is_cloaked(hwnd):
            return True
        if skip_pid and window_pid(hwnd) == skip_pid:
            return True
        ex = GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        if ex & WS_EX_TRANSPARENT and ex & WS_EX_LAYERED:
            return True
        rect = window_rect(hwnd)
        if rect and rect[0] <= x < rect[2] and rect[1] <= y < rect[3]:
            found[0] = int(hwnd)
            return False
        return True

    EnumWindows(WNDENUMPROC(visit), 0)
    return found[0]


def process_image_path(pid: int) -> str:
    handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        CloseHandle(handle)


def process_name(pid: int) -> str:
    """Executable name without the folder, e.g. ``chrome.exe``.  Empty when the process cannot be opened."""
    path = process_image_path(pid)
    return os.path.basename(path) if path else ""


def foreground_thread_layout() -> int:
    """Keyboard layout (HKL) of the foreground window's thread, so key names map to that window's layout."""
    hwnd = GetForegroundWindow()
    tid = window_thread(hwnd) if hwnd else 0
    return GetKeyboardLayout(tid) or GetKeyboardLayout(0)


def send_message_timeout(hwnd: int, msg: int, wparam: int = 0, lparam: int = 0, timeout_ms: int = 200) -> int | None:
    """SendMessageTimeout that gives up on hung windows.  Returns the result or None on timeout or failure."""
    result = ULONG_PTR()
    ok = SendMessageTimeoutW(hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG | SMTO_BLOCK, timeout_ms, ctypes.byref(result))
    if not ok:
        return None
    return int(result.value)


def input_desktop_name() -> str | None:
    """Name of the desktop receiving input ("Default" normally).

    While a UAC prompt or the sign-in screen is up, the input desktop is the
    secure "Winlogon" desktop and a normal process cannot open it: then this
    returns "Winlogon".  Any other failure returns None (unknown), so a quirk
    of the session never blocks input.
    """
    handle = OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not handle:
        return "Winlogon" if last_error() == ERROR_ACCESS_DENIED else None
    try:
        buf = ctypes.create_unicode_buffer(256)
        needed = wt.DWORD()
        if not GetUserObjectInformationW(handle, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            return None
        return buf.value
    finally:
        CloseDesktop(handle)
