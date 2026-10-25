"""Display, elevation, app scanning and accessibility-tool helpers.  Read-only: nothing on screen changes."""

from __future__ import annotations

import json
import os
import time

import pytest

from sightline.core.types import AppInfo, Monitor, Rect
from sightline.desktop import accessibility, apps, elevation
from sightline.desktop import win32 as w
from sightline.desktop.display import WindowsDisplay, nearest_monitor, parse_edid_size_mm


def edid(cm=(52, 29), mm=(527, 296), clock=0x1234) -> bytes:
    data = bytearray(128)
    data[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    data[21], data[22] = cm
    data[54], data[55] = clock & 0xFF, clock >> 8
    data[66], data[67] = mm[0] & 0xFF, mm[1] & 0xFF
    data[68] = ((mm[0] >> 8) << 4) | (mm[1] >> 8)
    return bytes(data)


def test_edid_prefers_millimetres_when_they_agree():
    assert parse_edid_size_mm(edid()) == (527.0, 296.0)


def test_edid_falls_back_to_centimetres_when_dtd_is_nonsense():
    assert parse_edid_size_mm(edid(mm=(16, 9))) == (520.0, 290.0)


def test_edid_with_aspect_ratio_bytes_uses_the_dtd():
    # A laptop panel seen on this machine: bytes 21-22 are (79, 0), the DTD says 344 x 195 mm.
    assert parse_edid_size_mm(edid(cm=(79, 0), mm=(344, 195))) == (344.0, 195.0)


def test_edid_rejects_garbage():
    assert parse_edid_size_mm(b"\x00" * 128) is None
    assert parse_edid_size_mm(b"") is None
    assert parse_edid_size_mm(edid(cm=(0, 0), clock=0)) is None


def test_nearest_monitor():
    left = Monitor("a", "A", Rect(-1920, 0, 1920, 1080), Rect(-1920, 0, 1920, 1040), 1.0, False)
    right = Monitor("b", "B", Rect(0, 0, 2560, 1440), Rect(0, 0, 2560, 1400), 1.5, True)
    assert nearest_monitor([left, right], -5, 5).id == "a"
    assert nearest_monitor([left, right], 100, 1400).id == "b"
    assert nearest_monitor([left, right], 5000, 200).id == "b"  # off to the right: nearest edge


def test_display_reads_this_machine():
    display = WindowsDisplay()
    monitors = display.monitors()
    assert monitors and sum(m.primary for m in monitors) == 1
    primary = display.primary()
    assert primary.rect.w > 0 and primary.work_rect.h <= primary.rect.h
    assert 0.5 <= primary.scale <= 4.0
    vr = display.virtual_rect()
    for m in monitors:
        assert vr.x <= m.rect.x and m.rect.right <= vr.right
    assert display.monitor_at(primary.rect.center[0], primary.rect.center[1]) == primary


def test_window_at_answers_with_the_root_owner_and_looks_through_sightline(monkeypatch):
    from sightline.core.types import WindowInfo
    from sightline.desktop.windows import Win32WindowManager

    own = 4242
    # hwnd: (process id, class, root owner, visible, minimised)
    table = {
        1: (200, "Chrome_WidgetWin_1", 1, True, False),  # a browser window
        2: (200, "Chrome_WidgetWin_2", 1, True, False),  # its drop-down, a window the browser owns
        3: (300, "#32770", 4, True, False),  # a dialog ...
        4: (300, "Notepad", 4, True, True),  # ... whose owner is minimised
        5: (own, "Qt6110QWindowIcon", 5, True, False),  # Sightline's dock
        6: (0, "Shell_TrayWnd", 6, True, False),  # the taskbar
        7: (300, "#32770", 5, True, False),  # another app's window owned by one of Sightline's (rare)
    }
    under = {(10, 10): 2, (20, 20): 3, (30, 30): 5, (40, 40): 6, (50, 50): 0}
    behind_dock: list[int] = [1]
    monkeypatch.setattr(w, "window_at", lambda x, y, look_through_pid=0: under[(x, y)])
    monkeypatch.setattr(w, "top_level_at", lambda x, y, skip_pid=0: behind_dock[0] if skip_pid == own else 0)
    monkeypatch.setattr(w, "window_pid", lambda h: table[h][0])
    monkeypatch.setattr(w, "class_name", lambda h: table[h][1])
    monkeypatch.setattr(w, "GetAncestor", lambda h, flag: table[h][2] if flag == w.GA_ROOTOWNER else h)
    monkeypatch.setattr(w, "IsWindowVisible", lambda h: table[h][3])
    monkeypatch.setattr(w, "IsIconic", lambda h: table[h][4])
    manager = Win32WindowManager(own_pid=own)
    manager.info = lambda h: WindowInfo(h, f"window {h}", "app.exe", Rect(0, 0, 800, 600))  # type: ignore[method-assign]

    assert manager.window_at(10, 10).hwnd == 1, "a drop-down stands for the browser that owns it"
    assert manager.window_at(20, 20).hwnd == 3, "unless the owner is minimised"
    assert manager.window_at(30, 30).hwnd == 1, "the dock is looked through to the app under it"
    behind_dock[0] = 0
    assert manager.window_at(30, 30) is None, "nothing under the dock"
    assert manager.window_at(40, 40) is None, "the taskbar is not an app window"
    assert manager.window_at(50, 50) is None
    monkeypatch.setattr(w, "window_at", lambda x, y, look_through_pid=0: 7)
    assert manager.window_at(60, 60).hwnd == 7, "never one of Sightline's own windows, even as an owner"


def test_window_at_gives_up_quietly(monkeypatch):
    from sightline.desktop.windows import Win32WindowManager

    def broken(*_args):
        raise OSError("the window went away")

    monkeypatch.setattr(w, "window_at", broken)
    assert Win32WindowManager(own_pid=1).window_at(5, 5) is None


def test_input_blocked_logic():
    assert elevation.input_blocked(elevation.MEDIUM, elevation.HIGH)
    assert not elevation.input_blocked(elevation.MEDIUM, elevation.MEDIUM)
    assert not elevation.input_blocked(elevation.MEDIUM, elevation.LOW)
    assert elevation.input_blocked(elevation.MEDIUM, None)  # Windows hides the level of protected processes
    assert not elevation.input_blocked(elevation.MEDIUM, elevation.HIGH, ui_access=True)
    assert not elevation.input_blocked(None, elevation.HIGH)
    assert not elevation.input_blocked(elevation.HIGH, elevation.HIGH)


def test_own_process_is_never_blocked():
    guard = elevation.ElevationGuard()
    level = elevation.process_integrity(w.GetCurrentProcessId())
    assert level is not None and level >= elevation.LOW
    assert guard.window_blocked(0) is False


def test_elevated_message_is_the_agreed_wording():
    assert elevation.ELEVATED_MESSAGE == (
        "Windows blocks Sightline from controlling apps running as administrator.  "
        "Windows Voice Access or a helper can handle this window."
    )
    # Running the whole app as administrator is no longer offered as the fix (review risk R3).
    assert "Run Sightline as administrator" not in elevation.ELEVATED_MESSAGE


def test_account_kind_reads_this_process_token():
    assert elevation.account_kind() in ("admin", "standard", "unprompted")


def test_shortcut_scan_and_junk_filter(tmp_path):
    root = tmp_path / "Programs"
    (root / "Tools").mkdir(parents=True)
    for name in ("Notepad++.lnk", "Tools/Uninstall Tools.lnk", "Tools/Tool Docs.url", "Tools/My Tool.lnk", "Readme.lnk", "Thing.appref-ms"):
        (root / name).write_bytes(b"")
    found = sorted(a.name for a in apps.scan_shortcuts([root]))
    assert found == ["My Tool", "Notepad++", "Thing"]


def test_merge_prefers_shortcuts():
    merged = apps.merge(
        [AppInfo("Firefox", r"C:\x\Firefox.lnk", "lnk")],
        [AppInfo("Firefox", "shell:AppsFolder\\Firefox", "uwp"), AppInfo("Calculator", "shell:AppsFolder\\Calc!App", "uwp")],
    )
    assert [(a.name, a.kind) for a in merged] == [("Calculator", "uwp"), ("Firefox", "lnk")]


def test_app_cache_round_trip_and_staleness(tmp_path, monkeypatch):
    cache = tmp_path / "apps.json"
    launcher = apps.StartMenuApps(cache_path=cache)
    monkeypatch.setattr(apps, "scan_shortcuts", lambda roots=None: [AppInfo("Notepad", "n.lnk", "lnk")])
    monkeypatch.setattr(apps, "scan_apps_folder", lambda: [AppInfo("Calculator", "shell:AppsFolder\\c", "uwp")])
    assert [a.name for a in launcher.apps()] == ["Notepad"]  # quick answer before the full scan
    assert launcher.stale()
    changed: list[bool] = []
    launcher.on_change(lambda: changed.append(True))
    thread = launcher.refresh_async(force=True)
    assert thread is not None
    thread.join(5)
    assert [a.name for a in launcher.apps()] == ["Calculator", "Notepad"]
    assert changed == [True]
    assert not launcher.stale()
    data = json.loads(cache.read_text("utf-8"))
    assert data["version"] == apps.CACHE_VERSION and len(data["apps"]) == 2
    again = apps.StartMenuApps(cache_path=cache)
    assert [a.name for a in again.apps()] == ["Calculator", "Notepad"]
    assert not again.stale()
    assert again.refresh_async(force=False) is None  # fresh cache: no rescan within the hour


def test_app_scan_falls_back_to_get_start_apps(tmp_path, monkeypatch):
    launcher = apps.StartMenuApps(cache_path=tmp_path / "apps.json")
    monkeypatch.setattr(apps, "scan_shortcuts", lambda roots=None: [])

    def broken():
        raise OSError("no shell")

    monkeypatch.setattr(apps, "scan_apps_folder", broken)
    monkeypatch.setattr(apps, "scan_get_start_apps", lambda: [AppInfo("Paint", "shell:AppsFolder\\p", "uwp")])
    launcher.refresh_async(force=True).join(5)
    assert [a.name for a in launcher.apps()] == ["Paint"]


def test_launch_gets_foreground_rights_first(tmp_path, monkeypatch):
    calls: list[str] = []
    launcher = apps.StartMenuApps(nudge=lambda: calls.append("nudge"), cache_path=tmp_path / "a.json")
    monkeypatch.setattr(apps, "shell_open", lambda target: calls.append(f"open {target}") or True)
    monkeypatch.setattr(w, "AllowSetForegroundWindow", lambda who: calls.append("allow") or True)
    assert launcher.launch(AppInfo("Calculator", "shell:AppsFolder\\c", "uwp"))
    assert calls == ["nudge", "allow", "open shell:AppsFolder\\c"]


@pytest.mark.skipif(not os.environ.get("ProgramData"), reason="needs a Windows Start menu")
def test_real_start_menu_scan_is_quick():
    started = time.perf_counter()
    found = apps.scan_shortcuts()
    assert (time.perf_counter() - started) < 2.0
    assert all(not apps.is_junk_app_name(a.name) for a in found)


def test_osk_docked_rect_and_settings():
    left, top, width, height = accessibility.docked_rect((0, 0, 1920, 1020))
    assert (left, width) == (0, 1920)
    assert top + height == 1020 and 260 <= height <= 520
    values = accessibility.osk_settings(800, (0, 653, 1920, 367))
    assert values["Mode"] == accessibility.OSK_MODE_HOVER == 1
    assert values["HoverPeriod"] == 800
    assert values["WindowTop"] == 653 and values["WindowHeight"] == 367
    assert accessibility.osk_settings(50, (0, 0, 1, 1))["HoverPeriod"] == 100  # clamped to what OSK accepts


def test_osk_registry_round_trip_in_a_scratch_key(monkeypatch):
    import winreg

    scratch = r"Software\SightlineDesktopTests\Osk"
    monkeypatch.setattr(accessibility, "OSK_KEY", scratch)
    try:
        accessibility.write_osk_settings({"Mode": 1, "HoverPeriod": 900})
        assert accessibility.read_osk_settings() == {"Mode": 1, "HoverPeriod": 900}
    finally:
        for key in (scratch, r"Software\SightlineDesktopTests"):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            except OSError:
                pass


# ------------------------------------------------------------------ programs by full path (review S-10)


def test_windows_programs_are_started_by_full_path(tmp_path):
    windows = tmp_path / "Windows"
    (windows / "System32").mkdir(parents=True)
    assert apps.system_exe("osk.exe", windows=windows) == str(windows / "System32" / "osk.exe")
    (windows / "Sysnative").mkdir()  # what a 32-bit Python sees on 64-bit Windows
    assert apps.system_exe("osk.exe", windows=windows) == str(windows / "Sysnative" / "osk.exe")
    for path in (apps.system_exe("osk.exe"), apps.powershell_exe()):
        assert os.path.isabs(path), path
    assert apps.powershell_exe().lower().endswith(os.path.join("windowspowershell", "v1.0", "powershell.exe"))


def test_get_start_apps_runs_powershell_by_its_full_path(monkeypatch):
    import subprocess
    from types import SimpleNamespace

    seen: dict[str, list[str]] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return SimpleNamespace(stdout=b"[]")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert apps.scan_get_start_apps() == []
    assert seen["command"][0] == apps.powershell_exe() and os.path.isabs(seen["command"][0])


def test_the_on_screen_keyboard_is_started_by_full_path(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(accessibility, "shell_open", lambda target, *a, **k: opened.append(target) or True)
    assert accessibility.start_osk()
    assert opened == [apps.system_exe("osk.exe")] and os.path.isabs(opened[0])


# ------------------------------------------------------------------ the user's own keyboard settings (review I-01)


class FakeOskRegistry:
    """The OSK key as a dict of name -> (value, type)."""

    def __init__(self, **values: int) -> None:
        import winreg

        self.dword = winreg.REG_DWORD
        self.values = {k: (v, self.dword) for k, v in values.items()}

    def read(self, names):
        return {n: self.values.get(n) for n in names}

    def write(self, values):
        self.values.update({k: (int(v), self.dword) for k, v in values.items()})

    def put_back(self, saved):
        for name, old in saved.items():
            if old is None:
                self.values.pop(name, None)
            else:
                self.values[name] = old


@pytest.fixture
def no_window_calls(monkeypatch):
    """The keyboard's window is a made-up handle: no Win32 call may reach a real window with it."""
    calls: list[tuple] = []
    monkeypatch.setattr(accessibility.w, "ShowWindowAsync", lambda hwnd, cmd: calls.append(("show", hwnd, cmd)) or True)
    monkeypatch.setattr(accessibility.w, "IsWindowVisible", lambda hwnd: False)
    monkeypatch.setattr(accessibility.w, "IsIconic", lambda hwnd: False)
    monkeypatch.setattr(accessibility.w, "PostMessageW", lambda *a: pytest.fail("no real close request"))
    return calls


def _keyboard(reg: FakeOskRegistry, window: dict, close):
    from sightline.core.fakes import FakeDisplay

    def start():
        window["hwnd"] = 4242
        return True

    return accessibility.OnScreenKeyboard(
        FakeDisplay().primary, lambda: 800, find=lambda: window["hwnd"], start=start, close=close,
        read=reg.read, write=reg.write, put_back=reg.put_back,
    )  # fmt: skip


def test_the_users_keyboard_settings_come_back_when_sightline_closes_it(no_window_calls):
    reg = FakeOskRegistry(Mode=0, HoverPeriod=1500)  # the user's own: click mode
    window = {"hwnd": 0}

    def close(hwnd, timeout):
        reg.write({"Mode": 1})  # osk.exe writes its settings back as it closes
        window["hwnd"] = 0
        return True

    kb = _keyboard(reg, window, close)
    assert kb.show()
    assert reg.values["Mode"][0] == accessibility.OSK_MODE_HOVER and reg.values["HoverPeriod"][0] == 800 and "Dock" in reg.values
    assert kb.settings_changed
    assert kb.hide()
    assert reg.values == FakeOskRegistry(Mode=0, HoverPeriod=1500).values, "values the user never had are removed again"
    assert not kb.settings_changed
    kb.show()  # a second time keeps the user's values from before the first
    kb.close_and_put_back()
    assert reg.values == FakeOskRegistry(Mode=0, HoverPeriod=1500).values


def test_a_keyboard_that_will_not_close_gets_the_settings_back_as_sightline_quits(no_window_calls, monkeypatch):
    monkeypatch.setattr(accessibility.time, "sleep", lambda s: None)
    reg = FakeOskRegistry(Mode=2)
    window = {"hwnd": 0}
    refuse = {"on": True}

    def close(hwnd, timeout):
        if refuse["on"]:
            return False
        window["hwnd"] = 0
        return True

    kb = _keyboard(reg, window, close)
    kb.show()
    assert kb.hide(), "hidden instead"
    assert reg.values["Mode"][0] == 1 and kb.settings_changed, "not put back while it still runs: it would write over them"
    kb.close_and_put_back()  # still refuses: left as it is
    assert reg.values["Mode"][0] == 1
    refuse["on"] = False
    kb.close_and_put_back()
    assert reg.values == {"Mode": (2, reg.dword)}


def test_a_keyboard_sightline_did_not_set_up_is_left_alone(no_window_calls):
    reg = FakeOskRegistry(Mode=0)
    kb = _keyboard(reg, {"hwnd": 0}, lambda hwnd, timeout: pytest.fail("nothing to close"))
    kb.close_and_put_back()
    assert kb.hide() and reg.values == {"Mode": (0, reg.dword)}


def test_osk_values_are_put_back_exactly_in_a_scratch_key(monkeypatch):
    import winreg

    scratch = r"Software\SightlineDesktopTests\Osk"
    monkeypatch.setattr(accessibility, "OSK_KEY", scratch)
    try:
        accessibility.write_osk_settings({"Mode": 0})
        saved = accessibility.read_osk_values(["Mode", "HoverPeriod"])
        assert saved == {"Mode": (0, winreg.REG_DWORD), "HoverPeriod": None}
        accessibility.write_osk_settings({"Mode": 1, "HoverPeriod": 900})
        accessibility.put_back_osk_values(saved)
        assert accessibility.read_osk_settings() == {"Mode": 0}
    finally:
        for key in (scratch, r"Software\SightlineDesktopTests"):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            except OSError:
                pass

