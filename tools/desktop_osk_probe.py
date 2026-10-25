"""Check the On-Screen Keyboard registry values Sightline writes against what osk.exe shows.

Opens the On-Screen Keyboard the way ``keyboard.show`` does (hover mode,
docked at the bottom), then reads its Options dialog through UI Automation:
which of "Click on keys", "Hover over keys" and "Scan through keys" is
selected, and the hover duration.  Closes everything again.  Holds the
desktop lock; refuses to run if the keyboard is already open.

    .venv\\Scripts\\python.exe tools\\desktop_osk_probe.py            automatic check
    .venv\\Scripts\\python.exe tools\\desktop_osk_probe.py --manual   registry diff while you change an option by hand
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tools.desktop_lock import desktop_lock  # noqa: E402


def _wait(predicate, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return predicate()


def _on_mta(fn):
    """Run fn(client) on a fresh MTA thread with its own UI Automation client."""
    box: dict = {}

    def run() -> None:
        import comtypes

        from sightline.desktop.uia import create_client

        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            box["result"] = fn(create_client())
        except Exception as e:  # reported, not raised: UIPI may refuse access to osk.exe
            box["error"] = f"{type(e).__name__}: {e}"
        finally:
            import gc

            gc.collect()
            comtypes.CoUninitialize()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(30)
    return box


def _read_options(osk_hwnd: int) -> dict:
    from sightline.desktop import win32 as w
    from sightline.desktop.uia import PAT_INVOKE, PAT_SELECTION_ITEM, P_CONTROL_TYPE, P_NAME

    def body(client):
        mod = client.mod
        uia = client.uia
        root = uia.ElementFromHandle(osk_hwnd)
        options = root.FindFirst(4, uia.CreatePropertyCondition(P_NAME, "Options"))
        if not options:
            return {"error": "no Options button visible through UI Automation"}
        client.pattern(options, PAT_INVOKE, mod.IUIAutomationInvokePattern).Invoke()
        pid = w.window_pid(osk_hwnd)
        dialog = _wait(lambda: next((h for h in _top_windows() if w.window_pid(h) == pid and h != osk_hwnd and w.IsWindowVisible(h)), 0), 5)
        if not dialog:
            return {"error": "the Options dialog did not open"}
        element = uia.ElementFromHandle(dialog)
        report: dict = {"dialog_title": w.window_text(dialog), "radios": {}, "values": {}}
        radios = element.FindAll(4, uia.CreatePropertyCondition(P_CONTROL_TYPE, 50013))
        for i in range(radios.Length):
            radio = radios.GetElement(i)
            selection = client.pattern(radio, PAT_SELECTION_ITEM, mod.IUIAutomationSelectionItemPattern)
            report["radios"][radio.CurrentName] = bool(selection.CurrentIsSelected) if selection else None
        for type_id in (50003, 50015, 50004):  # ComboBox, Slider, Edit: the hover duration control
            found = element.FindAll(4, uia.CreatePropertyCondition(P_CONTROL_TYPE, type_id))
            for i in range(found.Length):
                control = found.GetElement(i)
                value = client.pattern(control, 10002, mod.IUIAutomationValuePattern)
                rng = client.pattern(control, 10003, mod.IUIAutomationRangeValuePattern)
                report["values"][f"{control.CurrentName or control.CurrentAutomationId}"] = (
                    value.CurrentValue if value else (rng.CurrentValue if rng else None)
                )
        cancel = element.FindFirst(4, uia.CreatePropertyCondition(P_NAME, "Cancel"))
        if cancel:
            client.pattern(cancel, PAT_INVOKE, mod.IUIAutomationInvokePattern).Invoke()
        return report

    return _on_mta(body)


def _top_windows() -> list[int]:
    from sightline.desktop import win32 as w

    found: list[int] = []
    w.EnumWindows(w.WNDENUMPROC(lambda h, _l: found.append(int(h)) or True), 0)
    return found


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manual", action="store_true", help="diff the registry while a person changes an option")
    parser.add_argument("--hover-ms", type=int, default=800)
    args = parser.parse_args(argv)

    from sightline.desktop import accessibility as acc
    from sightline.desktop import elevation
    from sightline.desktop import win32 as w
    from sightline.desktop.display import WindowsDisplay

    w.ensure_dpi_awareness()
    if acc.osk_window():
        print("The On-Screen Keyboard is already open; close it first.")
        return 2
    with desktop_lock("desktop-osk-probe"):
        before = acc.read_osk_settings()
        print("registry before:", json.dumps(before))
        keyboard = acc.OnScreenKeyboard(WindowsDisplay().primary, lambda: args.hover_ms)
        if args.manual:
            acc.start_osk()  # by full path, as Sightline does
            input("Change one option in the keyboard's Options dialog, press OK, close the keyboard, then press Enter here.")
            after = acc.read_osk_settings()
            print("changed:", json.dumps({k: (before.get(k), v) for k, v in after.items() if before.get(k) != v}))
            return 0
        if not keyboard.show():
            print("osk.exe did not open")
            return 1
        hwnd = acc.osk_window()
        time.sleep(1.0)
        pid = w.window_pid(hwnd)
        level = elevation.process_integrity(pid)
        print("osk window", hwnd, "rect", w.frame_rect(hwnd), "integrity", hex(level) if level is not None else "unknown")
        print("blocked for our input:", elevation.ElevationGuard().window_blocked(hwnd))
        print("options dialog:", json.dumps(_read_options(hwnd), indent=2))
        closed = keyboard.hide()
        print("hide():", closed, "| window still exists:", bool(acc.osk_window()))
        time.sleep(0.5)
        print("registry after:", json.dumps(acc.read_osk_settings()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
