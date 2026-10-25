from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

import pytest

if sys.platform == "win32":
    # Physical pixels everywhere, as in the app (which sets this before Qt starts).
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass

_HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config, items):
    """Live desktop tests move the real mouse and type real keys: only run them when asked by name.

    ``pytest tests/desktop`` runs the unit tests; ``pytest tests/desktop -m desktop`` (or
    SIGHTLINE_LIVE_DESKTOP=1) runs the live ones.
    """
    if "desktop" in (config.getoption("-m") or "") or os.environ.get("SIGHTLINE_LIVE_DESKTOP") == "1":
        return
    skip = pytest.mark.skip(reason="live desktop test: run with -m desktop")
    for item in items:
        if item.get_closest_marker("desktop") is not None and _HERE in Path(str(item.fspath)).resolve().parents:
            item.add_marker(skip)


@pytest.fixture
def recorded_input(monkeypatch):
    """A real SendInputInjector whose SendInput only records events (nothing reaches the desktop)."""
    from sightline.desktop import input as input_mod
    from sightline.desktop import win32
    from sightline.desktop.keys import UsKeyResolver

    injector = input_mod.SendInputInjector()
    injector.press_s = injector.pre_click_settle_s = injector.key_hold_s = injector.key_gap_s = 0.0
    injector.type_chunk_pause_s = 0.0
    sent: list[list[tuple]] = []
    screen = (0, 0, 1920, 1080)
    cursor = [100, 100]
    set_cursor_calls: list[tuple[int, int]] = []

    def fake_send(inputs):
        batch = []
        for event in inputs:
            if event.type == win32.INPUT_KEYBOARD:
                k = event.u.ki
                batch.append(("key", k.wVk, k.wScan, k.dwFlags, k.dwExtraInfo))
            else:
                m = event.u.mi
                if m.dwFlags & win32.MOUSEEVENTF_ABSOLUTE:
                    # How Windows maps a normalised coordinate back to a pixel.
                    cursor[0] = screen[0] + (m.dx * screen[2]) // 65536
                    cursor[1] = screen[1] + (m.dy * screen[3]) // 65536
                batch.append(("mouse", m.dx, m.dy, ctypes.c_int32(m.mouseData).value, m.dwFlags, m.dwExtraInfo))
        sent.append(batch)
        return len(inputs)

    def fake_set_cursor(x, y):
        set_cursor_calls.append((x, y))
        cursor[:] = [x, y]
        return True

    monkeypatch.setattr(injector, "_send", fake_send)
    monkeypatch.setattr(input_mod, "Win32KeyResolver", lambda *a, **k: UsKeyResolver())
    monkeypatch.setattr(win32, "GetAsyncKeyState", lambda vk: 0)
    monkeypatch.setattr(win32, "GetSystemMetrics", lambda i: 0 if i == win32.SM_SWAPBUTTON else 1)
    monkeypatch.setattr(win32, "virtual_screen", lambda: screen)
    monkeypatch.setattr(win32, "cursor_pos", lambda: (cursor[0], cursor[1]))
    monkeypatch.setattr(win32, "SetCursorPos", fake_set_cursor)
    monkeypatch.setattr(win32, "GetDoubleClickTime", lambda: 500)
    injector.sent = sent  # type: ignore[attr-defined]
    injector.cursor = cursor  # type: ignore[attr-defined]
    injector.set_cursor_calls = set_cursor_calls  # type: ignore[attr-defined]
    return injector
