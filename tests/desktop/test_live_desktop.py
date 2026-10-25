"""Live desktop tests: real input, real windows, real UI Automation.

They hold the shared desktop lock, open their own windows (Notepad on a
temporary file, a browser with a throwaway profile, the On-Screen Keyboard)
and close them again.  They refuse to run when Notepad or the On-Screen
Keyboard is already open, so they never touch the user's own windows.

Run them on their own:

    .venv\\Scripts\\python.exe -m pytest tests/desktop -m desktop -s

Timings are printed and saved to ``out/desktop-live-timings.json``.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import winreg
from ctypes import wintypes as wt
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.desktop,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows only"),
]

ROOT = Path(__file__).resolve().parents[2]
TIMINGS: dict[str, object] = {}


def _interactive_session() -> bool:
    try:
        from sightline.desktop import win32 as w

        return w.input_desktop_name() == "Default"
    except Exception:
        return False


if sys.platform == "win32" and not _interactive_session():
    pytestmark.append(pytest.mark.skip(reason="no interactive desktop"))


# ---------------------------------------------------------------------------- fixtures and helpers


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    previous_home = os.environ.get("SIGHTLINE_HOME")
    os.environ["SIGHTLINE_HOME"] = str(tmp_path_factory.mktemp("home"))
    from sightline.core.context import AppContext
    from sightline.desktop.service import WindowsDesktop

    ctx = AppContext.for_tests()
    desktop = WindowsDesktop(ctx)
    ctx.desktop = desktop
    desktop.start()
    desktop.uia.interactive.wait_ready(5)
    desktop.uia.scanner.wait_ready(5)
    yield desktop
    desktop.stop()
    if previous_home is None:
        os.environ.pop("SIGHTLINE_HOME", None)
    else:
        os.environ["SIGHTLINE_HOME"] = previous_home
    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    (out / "desktop-live-timings.json").write_text(json.dumps(TIMINGS, indent=2), "utf-8")
    print("\nDesktop live timings:\n" + json.dumps(TIMINGS, indent=2))


def wait_until(predicate, timeout=5.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


def median_ms(fn, n=20):
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    return round(statistics.median(samples), 2), round(max(samples), 2)


def run_action(service, name, **args):
    from sightline.core.types import Action

    return service._ctx.actions.execute_now(Action(name, args, "test"))


def windows_of(service, predicate):
    return [w for w in service.windows.list_windows() if predicate(w)]


@contextlib.contextmanager
def saved_clipboard():
    """Keep the user's clipboard text across a test that copies."""
    before = read_clipboard()
    try:
        yield
    finally:
        if before is not None:
            write_clipboard(before)


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.OpenClipboard.argtypes = [wt.HWND]
user32.GetClipboardData.restype = wt.HANDLE
user32.GetClipboardData.argtypes = [wt.UINT]
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HGLOBAL
CF_UNICODETEXT = 13


def read_clipboard() -> str | None:
    for _ in range(20):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def write_clipboard(text: str) -> None:
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    handle = kernel32.GlobalAlloc(0x0002, size)  # GMEM_MOVEABLE
    pointer = kernel32.GlobalLock(handle)
    ctypes.memmove(pointer, data, size)
    kernel32.GlobalUnlock(handle)
    for _ in range(20):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return
    try:
        user32.EmptyClipboard()
        user32.SetClipboardData(CF_UNICODETEXT, handle)
    finally:
        user32.CloseClipboard()


# ---------------------------------------------------------------------------- input


def test_cursor_moves_land_on_exact_pixels(service, desktop_lock):
    from sightline.desktop import win32 as w

    start = w.cursor_pos()
    misses = []
    try:
        for monitor in service.display.monitors():
            r = monitor.rect
            points = [
                (int(r.x + 1), int(r.y + 1)),
                (int(r.right - 2), int(r.bottom - 2)),
                (int(r.x + r.w / 3) + 1, int(r.y + r.h / 3) + 1),
                (int(r.center[0]), int(r.center[1])),
            ] + [(int(r.x + i * r.w / 37) + i % 3, int(r.y + i * r.h / 41) + 1) for i in range(1, 36)]
            for x, y in points:
                service.input.move_cursor(x, y)
                if w.cursor_pos() != (x, y):
                    service.input.move_cursor(x, y)  # a remote desktop client can nudge the cursor once
                    if w.cursor_pos() != (x, y):
                        misses.append(((x, y), w.cursor_pos()))
    finally:
        service.input.move_cursor(*start)
    TIMINGS["cursor_fallback_moves"] = service.input.fallback_moves
    TIMINGS["move_cursor_ms"] = median_ms(lambda: service.input.move_cursor(*start), 50)
    assert misses == []


class _KeyboardHook:
    """A low-level keyboard hook that records and swallows Sightline's own injected keys."""

    class KBD(ctypes.Structure):
        _fields_ = [("vk", wt.DWORD), ("scan", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD), ("extra", ctypes.c_size_t)]

    def __init__(self) -> None:
        self.seen: list[tuple[int, int, int, int]] = []
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)
        self._proc = proc_type(self._on_key)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()
        self._tid = 0

    def _on_key(self, code, wparam, lparam):
        from sightline.desktop.win32 import SIGHTLINE_INPUT_TAG

        if code == 0:
            k = ctypes.cast(lparam, ctypes.POINTER(self.KBD)).contents
            if k.extra == SIGHTLINE_INPUT_TAG:
                self.seen.append((wparam, k.vk, k.scan, k.flags))
                return 1  # swallow: the keys never reach an app
        return user32.CallNextHookEx(None, code, wparam, ctypes.c_ssize_t(lparam))

    def _run(self):
        self._tid = kernel32.GetCurrentThreadId()
        user32.SetWindowsHookExW.restype = wt.HHOOK
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wt.HINSTANCE, wt.DWORD]
        hook = user32.SetWindowsHookExW(13, ctypes.cast(self._proc, ctypes.c_void_p), None, 0)
        self._ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(hook)

    def __enter__(self):
        user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, ctypes.c_ssize_t]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        self._thread.start()
        self._ready.wait(2)
        return self

    def __exit__(self, *exc):
        user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)
        self._thread.join(2)


def test_injected_keys_carry_scan_codes_and_extended_flags(service, desktop_lock):
    from sightline.desktop import keys as k

    with _KeyboardHook() as hook:
        service.input.key_combo("ctrl+shift+left")
        service.input.key_combo("numpad 7")
        service.input.key_combo("volume up")  # swallowed by the hook, so the volume does not change
        time.sleep(0.3)
    downs = [(vk, scan, flags & 1) for msg, vk, scan, flags in hook.seen if msg in (0x100, 0x104)]
    assert (k.VK_LCONTROL, 0x1D, 0) in downs
    assert (k.VK_LSHIFT, 0x2A, 0) in downs
    assert (k.VK_LEFT, 0x4B, 1) in downs
    assert (k.VK_NUMPAD0 + 7, 0x47, 0) in downs
    assert (k.VK_VOLUME_UP, 0x30, 1) in downs
    assert all(flags & 0x10 for _m, _v, _s, flags in hook.seen)  # LLKHF_INJECTED
    ups = [vk for msg, vk, _s, _f in hook.seen if msg in (0x101, 0x105)]
    assert sorted(ups) == sorted(vk for vk, _s, _e in downs)  # every key released


# ---------------------------------------------------------------------------- Notepad


def _snap_probe(snapshot, root):
    """A point 10 px outside a named element of ``root`` that no element contains and that element is nearest to."""
    from sightline.desktop import win32 as w
    from sightline.desktop.spatial import element_root

    for el in snapshot.elements:
        if not el.name or element_root(el) != root or el.rect.w < 12 or el.rect.h < 12:
            continue
        cx, cy = el.rect.center
        for px, py in ((el.rect.right + 9, cy), (el.rect.x - 10, cy), (cx, el.rect.bottom + 9), (cx, el.rect.y - 10)):
            if w.window_at(px, py) != root:
                continue
            if snapshot.containing(px, py, root) is None and snapshot.nearest(px, py, 48, root) is el:
                return (int(px), int(py)), el
    return None


def _notepad_windows(service):
    return windows_of(service, lambda w: w.process.lower() == "notepad.exe")


def test_notepad_typing_focus_menu_and_clipboard(service, desktop_lock, tmp_path):
    from sightline.core.events import HoverTarget

    if _notepad_windows(service):
        pytest.skip("Notepad is already open; close it so the test cannot touch your documents")
    path = tmp_path / "sightline-desktop-test.txt"
    path.write_text("", "utf-8")
    subprocess.Popen(["notepad.exe", str(path)])
    found = wait_until(lambda: [w for w in _notepad_windows(service) if path.stem in w.title], 15)
    assert found, "Notepad did not open"
    hwnd = found[0].hwnd
    try:
        assert service.windows.activate(hwnd)
        # The window under a point (the reading scroll's question): Notepad itself, from its edit control.
        middle = found[0].rect.center
        at = service.windows.window_at(*middle)
        assert at is not None and at.hwnd == hwnd, at
        TIMINGS["window_at_ms"] = median_ms(lambda: service.windows.window_at(*middle))
        focus = wait_until(lambda: service.uia.focus_info() if service.uia.focus_info().editable else None, 5)
        assert focus, f"focus never became editable: {service.uia.focus_info()}"
        TIMINGS["notepad_focus"] = {"control_type": focus.control_type, "process": focus.process}
        assert not focus.password

        text = "Sightline café naïve \U0001f600 ok.\nSecond line"
        service.input.type_text(text)
        typed = wait_until(lambda: (service.uia.text_before_caret(500, timeout=1.0) or "").endswith("Second line"), 5)
        before = service.uia.text_before_caret(500, timeout=1.0) or ""
        assert typed, f"read back {before!r}"
        assert before.replace("\r", "\n").endswith(text.replace("\r", "\n"))
        TIMINGS["notepad_text_before_caret_ms"] = median_ms(lambda: service.uia.text_before_caret(200, timeout=1.0))

        with saved_clipboard():
            service.input.key_combo("ctrl+a")
            service.input.key_combo("ctrl+c")
            copied = wait_until(lambda: (read_clipboard() or "").replace("\r\n", "\n") == text, 3)
            assert copied, f"clipboard held {read_clipboard()!r}"
            service.input.key_combo("ctrl+end")  # deselect, caret at the end

            # Editing by content: select a phrase, select a range, put the caret by a word.
            t0 = time.perf_counter()
            result = run_action(service, "text.select", phrase="café naïve")
            TIMINGS["notepad_text_select"] = {"method": result["method"], "ms": round((time.perf_counter() - t0) * 1000, 1)}
            service.input.key_combo("ctrl+c")
            assert wait_until(lambda: read_clipboard() == "café naïve", 3), f"selected {read_clipboard()!r}"
            run_action(service, "text.select_range", start="Sightline", end="second")
            service.input.key_combo("ctrl+c")
            expected = "Sightline café naïve \U0001f600 ok.\nSecond"
            assert wait_until(lambda: (read_clipboard() or "").replace("\r\n", "\n") == expected, 3), read_clipboard()
            result = run_action(service, "text.caret", where="after", phrase="ok")
            TIMINGS["notepad_text_caret_method"] = result["method"]
            service.input.type_text("X")
            assert wait_until(lambda: (service.uia.text_before_caret(100, timeout=1.0) or "").endswith("ok" + "X"), 3)
            service.input.key_combo("backspace")
        service.input.key_combo("ctrl+end")

        t0 = time.perf_counter()
        elements = service.uia.clickables(timeout=3.0)
        TIMINGS["notepad_clickables"] = {"count": len(elements), "ms": round((time.perf_counter() - t0) * 1000, 1)}
        names = {e.name for e in elements}
        assert {"File", "Edit", "View"} <= names, sorted(names)
        centre = found[0].rect.center
        TIMINGS["notepad_element_at_ms"] = median_ms(lambda: service.uia.element_at(centre[0], centre[1], timeout=1.0))
        TIMINGS["focus_poll_ms_mean"] = service.uia.stats()["focus_poll_ms"]
        file_menu = next(e for e in elements if e.name == "File")
        fx, fy = file_menu.rect.center
        near = service.uia.clickables_near(fx, fy, 150)
        assert near and near[0].name == "File", [e.name for e in near[:5]]
        assert [e.rect.distance_to(fx, fy) for e in near] == sorted(e.rect.distance_to(fx, fy) for e in near)
        TIMINGS["clickables_near_ms"] = median_ms(lambda: service.uia.clickables_near(fx, fy, 150), n=200)

        # Hover just outside a named element, where nothing else is: the highlight should snap to it.
        snapshot = service.uia.snapshot
        probe = _snap_probe(snapshot, hwnd)
        assert probe is not None, "no element with free space around it to test snapping"
        (px, py), target = probe
        hovers: list[HoverTarget] = []
        sub = service._ctx.bus.subscribe(HoverTarget, hovers.append)
        try:
            service.input.move_cursor(px, py)
            assert wait_until(lambda: any(h.name == target.name and h.snapped for h in hovers), 3), hovers[-3:]
        finally:
            sub.cancel()

        # Click a menu by name, check its items appear, close it again.
        run_action(service, "element.click", name="Edit")
        menu = wait_until(lambda: [e for e in service.uia.clickables(timeout=2.0) if e.control_type == "MenuItem" and "select all" in e.name.lower()], 4)
        assert menu, "the Edit menu did not open"
        service.input.key_combo("escape")

        # "Snap left", then "undo that": the window goes back to where it was.
        from sightline.core.types import Action
        from sightline.desktop import win32 as w

        time.sleep(0.3)
        before = w.window_rect(hwnd)
        snapped = run_action(service, "window.snap", side="left")
        assert wait_until(lambda: w.window_rect(hwnd) != before, 3), "the window did not snap"
        inverse = service.actions._undo_snap(Action("window.snap", {"side": "left"}, "test"), snapped)
        assert inverse is not None and inverse.name == "window.place"
        service._ctx.actions.execute_now(inverse)
        assert wait_until(lambda: w.window_rect(hwnd) == before, 3), (before, w.window_rect(hwnd))
    finally:
        with contextlib.suppress(Exception):
            if service.windows.activate(hwnd):
                service.input.key_combo("ctrl+a")
                service.input.key_combo("delete")
                service.input.key_combo("ctrl+s")  # saved and unchanged: closing asks nothing
                time.sleep(0.4)
        with contextlib.suppress(Exception):
            service.windows.close(hwnd)
        if not wait_until(lambda: hwnd not in [w.hwnd for w in _notepad_windows(service)], 5):
            from sightline.desktop import win32 as w

            pid = w.window_pid(hwnd)
            if pid:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)


# ---------------------------------------------------------------------------- Explorer and the taskbar


def test_explorer_and_taskbar_clickables(service, desktop_lock, tmp_path):
    from sightline.desktop import win32 as w

    folder = tmp_path / "Sightline Explorer Test"
    folder.mkdir()
    for i in range(30):
        (folder / f"file {i:02d}.txt").write_text("x", "utf-8")
    before = {x.hwnd for x in windows_of(service, lambda x: x.process.lower() == "explorer.exe")}
    subprocess.Popen(["explorer.exe", str(folder)])
    new = wait_until(lambda: [x for x in windows_of(service, lambda x: x.process.lower() == "explorer.exe") if x.hwnd not in before and folder.name in x.title], 10)
    if not new:
        pytest.skip("Explorer opened the folder in an existing window; not touching it")
    hwnd = new[0].hwnd
    try:
        assert service.windows.activate(hwnd)
        time.sleep(1.0)
        samples = []
        counts = []
        for _ in range(3):
            t0 = time.perf_counter()
            elements = service.uia.clickables(timeout=5.0)
            samples.append((time.perf_counter() - t0) * 1000)
            counts.append(len(elements))
        TIMINGS["explorer_clickables"] = {"count": counts[-1], "ms_median": round(statistics.median(samples), 1)}
        names = {e.name for e in elements}
        assert sum(1 for n in names if n.startswith("file ")) >= 10, sorted(names)[:40]
        taskbar = [e for e in elements if e.process == "explorer.exe" and w.class_name(e.handle.root) == "Shell_TrayWnd"]
        TIMINGS["taskbar_clickables"] = len(taskbar)
        assert taskbar, "taskbar buttons missing"
    finally:
        with contextlib.suppress(Exception):
            service.windows.close(hwnd)
        wait_until(lambda: not w.IsWindow(hwnd), 5)


# ---------------------------------------------------------------------------- browsers

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Sightline test page</title></head>
<body style="font: 16px sans-serif">
<h1>Sightline test page</h1>
<input aria-label="Your name" id="name" style="font-size: 20px">
<div>{buttons}</div>
<div style="columns: 4">{links}</div>
</body></html>"""


def _browser_path(exe: str) -> str | None:
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                value, _ = winreg.QueryValueEx(key, "")
                if value and Path(value).exists():
                    return value
        except OSError:
            continue
    return None


@pytest.mark.parametrize("exe", ["chrome.exe", "msedge.exe"])
def test_browser_clickables_click_and_type(service, desktop_lock, tmp_path, exe):
    from sightline.desktop import win32 as w

    path = _browser_path(exe)
    if path is None:
        pytest.skip(f"{exe} is not installed")
    buttons = "".join(f'<button onclick="document.title=\'clicked {i}\'">Button {i}</button> ' for i in range(20))
    links = "".join(f'<p><a href="#l{i}">Link number {i}</a></p>' for i in range(60))
    page = tmp_path / "page.html"
    page.write_text(PAGE.format(buttons=buttons, links=links), "utf-8")
    profile = tmp_path / "profile"
    process = subprocess.Popen(
        [path, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", "--disable-sync",
         "--new-window", page.as_uri()]
    )  # fmt: skip
    try:
        found = wait_until(lambda: [x for x in service.windows.list_windows() if x.title.startswith("Sightline test page")], 20)
        assert found, f"{exe} window did not appear"
        hwnd = found[0].hwnd
        assert service.windows.activate(hwnd)
        assert w.GetForegroundWindow() == hwnd
        time.sleep(1.0)
        first = time.perf_counter()
        elements = wait_until(lambda: [e for e in service.uia.clickables(timeout=5.0) if e.name.startswith(("Link number", "Button"))], 10)
        first_ms = (time.perf_counter() - first) * 1000
        samples = []
        for _ in range(3):
            t0 = time.perf_counter()
            all_elements = service.uia.clickables(timeout=5.0)
            samples.append((time.perf_counter() - t0) * 1000)
        TIMINGS[f"{exe}_clickables"] = {
            "count": len(all_elements),
            "page_targets": len(elements),
            "first_ms_including_accessibility_start": round(first_ms, 1),
            "ms_median": round(statistics.median(samples), 1),
        }
        assert len(elements) >= 40, f"only {len(elements)} page targets"

        run_action(service, "element.click", name="Button 7")
        assert wait_until(lambda: "clicked 7" in w.window_text(hwnd), 5), w.window_text(hwnd)

        run_action(service, "element.click", name="Your name")
        assert wait_until(lambda: service.uia.focus_info().editable, 3), service.uia.focus_info()
        service.input.type_text("hello there")
        assert wait_until(lambda: (service.uia.text_before_caret(100, timeout=1.0) or "").endswith("hello there"), 3)
        TIMINGS[f"{exe}_text_before_caret_ms"] = median_ms(lambda: service.uia.text_before_caret(200, timeout=1.0))

        # Switching away and back by name exercises the foreground-lock workaround.
        others = [x for x in service.windows.list_windows() if x.hwnd != hwnd]
        if others:
            service.windows.activate(others[0].hwnd)
            run_action(service, "window.switch", name="Sightline test page")
            assert wait_until(lambda: w.GetForegroundWindow() == hwnd, 2)
        with contextlib.suppress(Exception):
            service.windows.close(hwnd)
    finally:
        with contextlib.suppress(Exception):
            process.wait(5)
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)


# ---------------------------------------------------------------------------- On-Screen Keyboard


def test_on_screen_keyboard_hover_mode_docked_and_closed(service, desktop_lock):
    from sightline.desktop import accessibility as acc
    from sightline.desktop import win32 as w

    if acc.osk_window():
        pytest.skip("the On-Screen Keyboard is already open")
    hover_ms = service._ctx.settings.desktop.keyboard_hover_ms
    run_action(service, "keyboard.show")
    try:
        hwnd = wait_until(acc.osk_window, 5)
        assert hwnd, "osk.exe did not open"
        values = acc.read_osk_settings()
        assert values.get("Mode") == acc.OSK_MODE_HOVER and values.get("HoverPeriod") == hover_ms
        time.sleep(1.0)
        rect = w.frame_rect(hwnd)
        work = service.display.primary().work_rect
        TIMINGS["osk_rect"] = rect
        TIMINGS["osk_blocked_for_input"] = service.guard.window_blocked(hwnd)
        assert rect is not None and abs(rect[3] - work.bottom) < 60 and (rect[2] - rect[0]) > 0.6 * work.w
    finally:
        run_action(service, "keyboard.hide")
        gone = wait_until(lambda: not acc.osk_visible(), 4)
        TIMINGS["osk_closed"] = "closed" if not acc.osk_window() else "hidden"
    assert gone


# ---------------------------------------------------------------------------- elevated windows


def test_elevated_windows_are_detected_if_any_are_open(service):
    from sightline.desktop import elevation

    if service.guard.is_elevated_self():
        pytest.skip("Sightline itself runs as administrator")
    elevated = [x for x in service.windows.list_windows() if service.guard.window_blocked(x.hwnd)]
    if not elevated:
        pytest.skip("no administrator window is open")
    TIMINGS["elevated_windows"] = [x.title for x in elevated]
    assert elevation.ELEVATED_MESSAGE.startswith("Windows blocks control")
