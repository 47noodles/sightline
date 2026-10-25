"""Read-only timings for the desktop module's UI Automation work, on whatever windows are open.

Nothing is clicked, typed or moved.  Measures, per visible top-level window:
the raw ``FindAllBuildCache`` clickable query, the full scan (conversion and
covering checks included), and ``element_at`` at the window centre; then
focus polling, ``text_before_caret``, ``nearest_clickable`` and window
listing.  Holds the cpu and desktop locks so other agents' benchmarks and
desktop tests do not skew or disturb it.

    .venv\\Scripts\\python.exe tools\\desktop_bench.py [--json out\\desktop-bench.json] [--title Notepad]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tools.desktop_lock import desktop_lock  # noqa: E402


def _median(samples: list[float]) -> float:
    return round(statistics.median(samples), 2) if samples else float("nan")


def bench(title_filter: str | None, repeats: int) -> dict:
    from sightline.desktop import uia as uia_mod
    from sightline.desktop import win32 as w
    from sightline.desktop.windows import Win32WindowManager

    w.ensure_dpi_awareness()
    import comtypes  # noqa: F401  (main thread first, so the workers can join the MTA)

    provider = uia_mod.UIAProvider(uia_mod.UIAConfig())
    provider.start()
    provider.interactive.wait_ready(5)
    provider.scanner.wait_ready(5)
    results: dict = {"windows": []}
    try:
        manager = Win32WindowManager()
        t0 = time.perf_counter()
        windows = manager.list_windows()
        results["list_windows_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        taskbar = int(w.FindWindowW("Shell_TrayWnd", None) or 0)
        targets = [(x.hwnd, x.title, x.process) for x in windows if not x.minimized]
        if taskbar:
            targets.append((taskbar, "Taskbar", "explorer.exe"))
        if title_filter:
            targets = [t for t in targets if title_filter.lower() in t[1].lower()]

        scanner = provider.scanner
        for hwnd, title, process in targets:

            def raw(client, hwnd=hwnd):
                element = client.element_from_handle(hwnd)
                t = time.perf_counter()
                found = element.FindAllBuildCache(uia_mod.TREE_SCOPE_DESCENDANTS, client.clickable_condition, client.clickable_cache)
                return (time.perf_counter() - t) * 1000, (found.Length if found is not None else 0)

            def full(client, hwnd=hwnd):
                items: list = []
                infos: list = []
                arrays: list = []
                t = time.perf_counter()
                scanner._prime_chromium(client, hwnd)
                count = scanner._scan_root(client, hwnd, 0, items, infos, arrays)
                return (time.perf_counter() - t) * 1000, count

            scanner.call(full, 10.0)  # first call primes Chromium and warms caches
            raw_ms, counts, full_ms, kept = [], [], [], []
            for _ in range(repeats):
                ms, n = scanner.call(raw, 10.0, (float("nan"), 0))
                raw_ms.append(ms)
                counts.append(n)
                ms, n = scanner.call(full, 10.0, (float("nan"), 0))
                full_ms.append(ms)
                kept.append(n)
            rect = w.frame_rect(hwnd) or (0, 0, 0, 0)
            cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
            point_ms = []
            for _ in range(repeats * 4):
                t = time.perf_counter()
                provider.element_at(cx, cy, timeout=2.0)
                point_ms.append((time.perf_counter() - t) * 1000)
            row = {
                "title": title[:60],
                "process": process,
                "class": w.class_name(hwnd),
                "findall_ms": _median(raw_ms),
                "candidates": counts[-1] if counts else 0,
                "scan_ms": _median(full_ms),
                "kept_visible": kept[-1] if kept else 0,
                "element_at_ms": _median(point_ms),
            }
            results["windows"].append(row)
            print(json.dumps(row))

        snapshot = provider.scanner.call(lambda c: provider.scanner.scan_now(), 10.0)
        if snapshot is not None:
            t = time.perf_counter()
            for i in range(1000):
                provider.nearest_clickable((i * 37) % 1900, (i * 53) % 1000, 48)
            results["nearest_clickable_us"] = round((time.perf_counter() - t) * 1000, 2)  # per call: ms/1000 calls
            results["snapshot_elements"] = len(snapshot.elements)

        time.sleep(2.0)
        polls = list(provider.interactive.focus_poll_ms)
        results["focus_poll_ms_mean"] = round(sum(polls) / len(polls), 3) if polls else None
        results["focus"] = {
            "process": provider.focus_info().process,
            "control_type": provider.focus_info().control_type,
            "editable": provider.focus_info().editable,
        }
        text_ms = []
        for _ in range(repeats * 4):
            t = time.perf_counter()
            text = provider.text_before_caret(200, timeout=1.0)
            text_ms.append((time.perf_counter() - t) * 1000)
        results["text_before_caret_ms"] = _median(text_ms)
        results["text_before_caret_available"] = text is not None  # the text itself is never printed
    finally:
        provider.stop()
    return results


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", type=Path, help="also write the results here")
    parser.add_argument("--title", help="only windows whose title contains this")
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args(argv)
    with desktop_lock("desktop-bench", name="cpu"), desktop_lock("desktop-bench"):
        results = bench(args.title, args.repeats)
    print(json.dumps({k: v for k, v in results.items() if k != "windows"}, indent=2))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(results, indent=2), "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
