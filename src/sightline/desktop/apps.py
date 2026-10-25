"""Installed apps from the Start menu, and launching them through the shell.

Two sources, merged by name:

* Start menu shortcuts (all users and the current user).  A file scan, fast
  enough to run on the first call when there is no cache yet.
* The shell's AppsFolder, enumerated in-process through ``Shell.Application``
  (what ``Get-StartApps`` does, in under a second instead of 1.4 s for
  PowerShell).  It covers UWP apps and gives every app an ID that
  ``shell:AppsFolder\\<id>`` launches.  ``Get-StartApps`` is the fallback.

The merged list is cached in ``<data_dir>/desktop/apps.json`` and refreshed
in the background at most hourly.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from sightline.core import paths
from sightline.core.types import AppInfo

from . import win32 as w
from .matching import is_junk_app_name, normalize

log = logging.getLogger(__name__)

CACHE_VERSION = 1
REFRESH_INTERVAL_S = 3600.0
_CREATE_NO_WINDOW = 0x08000000
_SHORTCUT_SUFFIXES = (".lnk", ".appref-ms")


def start_menu_dirs() -> list[Path]:
    dirs = []
    for env in ("ProgramData", "APPDATA"):
        base = os.environ.get(env)
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return dirs


def scan_shortcuts(roots: list[Path] | None = None) -> list[AppInfo]:
    found: dict[str, AppInfo] = {}
    for root in roots if roots is not None else start_menu_dirs():
        if not root.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(root):
            for file in files:
                if not file.lower().endswith(_SHORTCUT_SUFFIXES):
                    continue
                name = file.rsplit(".", 1)[0].strip()
                if not name or is_junk_app_name(name):
                    continue
                found.setdefault(normalize(name), AppInfo(name, str(Path(dirpath) / file), "lnk"))
    return list(found.values())


def scan_apps_folder() -> list[AppInfo]:
    """Every app in the shell's AppsFolder, launchable as ``shell:AppsFolder\\<id>``.  Needs COM (STA)."""
    import comtypes
    import comtypes.client

    comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
    try:
        shell = comtypes.client.CreateObject("Shell.Application", dynamic=True)
        folder = shell.NameSpace("shell:AppsFolder")
        if folder is None:
            return []
        items = folder.Items()
        out: list[AppInfo] = []
        for i in range(items.Count):
            item = items.Item(i)
            name, app_id = str(item.Name or "").strip(), str(item.Path or "").strip()
            if name and app_id and not is_junk_app_name(name):
                out.append(AppInfo(name, "shell:AppsFolder\\" + app_id, "uwp"))
        del items, folder, shell
        return out
    finally:
        comtypes.CoUninitialize()


def windows_dir() -> Path:
    """The Windows folder (usually ``C:\\Windows``), from Windows itself, else from ``%SystemRoot%``."""
    import ctypes

    try:
        buffer = ctypes.create_unicode_buffer(260)
        length = int(w.GetSystemWindowsDirectoryW(buffer, 260))
        if 0 < length < 260:
            return Path(buffer.value)
    except (AttributeError, OSError):
        pass
    return Path(os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows")


def system_exe(*parts: str, windows: Path | None = None) -> str:
    """The full path of a Windows program (``system_exe("osk.exe")``), so no other folder is searched for it.

    A bare name is looked for in the working directory first (review S-10).  A 32-bit Python on 64-bit
    Windows sees SysWOW64 under the name System32; there ``Sysnative`` is the real System32.
    """
    root = windows if windows is not None else windows_dir()
    native = root / "Sysnative"
    base = native if native.is_dir() else root / "System32"
    return str(base.joinpath(*parts))


def powershell_exe() -> str:
    return system_exe("WindowsPowerShell", "v1.0", "powershell.exe")


def scan_get_start_apps(timeout: float = 20.0) -> list[AppInfo]:
    """The same list through PowerShell's Get-StartApps, without a console window."""
    import subprocess

    command = [
        powershell_exe(),
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress",
    ]
    result = subprocess.run(
        command, capture_output=True, timeout=timeout, creationflags=_CREATE_NO_WINDOW, check=False
    )
    data = json.loads(result.stdout.decode("utf-8", "replace") or "[]")
    if isinstance(data, dict):
        data = [data]
    out = []
    for row in data:
        name, app_id = str(row.get("Name", "")).strip(), str(row.get("AppID", "")).strip()
        if name and app_id and not is_junk_app_name(name):
            out.append(AppInfo(name, "shell:AppsFolder\\" + app_id, "uwp"))
    return out


def merge(shortcuts: list[AppInfo], folder_apps: list[AppInfo]) -> list[AppInfo]:
    """One entry per name.  Shortcuts win for desktop apps (they carry arguments and working folders)."""
    merged: dict[str, AppInfo] = {}
    for app in shortcuts:
        merged.setdefault(normalize(app.name), app)
    for app in folder_apps:
        merged.setdefault(normalize(app.name), app)
    return sorted(merged.values(), key=lambda a: a.name.lower())


class StartMenuApps:
    """The ``AppLauncher`` implementation."""

    def __init__(self, nudge: Callable[[], None] | None = None, cache_path: Path | None = None) -> None:
        self._nudge = nudge
        self._cache_path = cache_path
        self._lock = threading.Lock()
        self._apps: list[AppInfo] | None = None
        self._loaded_at = 0.0  # wall-clock time of the last full scan
        self._refreshing = False
        self._listeners: list[Callable[[], None]] = []

    @property
    def cache_path(self) -> Path:
        return self._cache_path or (paths.data_dir() / "desktop" / "apps.json")

    def on_change(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    # ------------------------------------------------------------------ list

    def apps(self) -> list[AppInfo]:
        apps = self._apps
        if apps is None:
            with self._lock:
                if self._apps is None:
                    self._apps = self._load_cache()
                    if self._apps is None:
                        self._apps = scan_shortcuts()  # quick first answer; the full scan follows
                        self._loaded_at = 0.0
                apps = self._apps
        return list(apps)

    def stale(self) -> bool:
        return time.time() - self._loaded_at > REFRESH_INTERVAL_S

    def refresh(self) -> None:
        """Rescan in the background."""
        self.refresh_async(force=True)

    def refresh_async(self, force: bool = False) -> threading.Thread | None:
        if self._apps is None:
            self.apps()
        if not force and not self.stale():
            return None
        with self._lock:
            if self._refreshing:
                return None
            self._refreshing = True
        thread = threading.Thread(target=self._refresh_now, name="sightline-apps", daemon=True)
        thread.start()
        return thread

    def _refresh_now(self) -> None:
        try:
            started = time.perf_counter()
            shortcuts = scan_shortcuts()
            try:
                folder_apps = scan_apps_folder()
            except Exception as e:  # COM trouble: PowerShell gives the same list, slower
                log.info("AppsFolder scan failed (%s); trying Get-StartApps", e)
                try:
                    folder_apps = scan_get_start_apps()
                except Exception:
                    log.exception("Get-StartApps failed")
                    folder_apps = []
            apps = merge(shortcuts, folder_apps)
            changed = apps != self._apps
            with self._lock:
                self._apps = apps
                self._loaded_at = time.time()
            self._save_cache(apps)
            log.info("Found %d apps in %.0f ms", len(apps), (time.perf_counter() - started) * 1000)
            if changed:
                for callback in list(self._listeners):
                    try:
                        callback()
                    except Exception:
                        log.exception("App list listener failed")
        finally:
            with self._lock:
                self._refreshing = False

    def _load_cache(self) -> list[AppInfo] | None:
        try:
            data = json.loads(self.cache_path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return None
        try:
            apps = [AppInfo(a["name"], a["target"], a.get("kind", "lnk")) for a in data.get("apps", [])]
        except (KeyError, TypeError):
            return None
        self._loaded_at = float(data.get("time", 0.0))
        return apps or None

    def _save_cache(self, apps: list[AppInfo]) -> None:
        path = self.cache_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            payload = {
                "version": CACHE_VERSION,
                "time": time.time(),
                "apps": [{"name": a.name, "target": a.target, "kind": a.kind} for a in apps],
            }
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=0), "utf-8")
            os.replace(tmp, path)
        except OSError:
            log.exception("Could not save the app list")

    # ------------------------------------------------------------------ launch

    def launch(self, app: AppInfo) -> bool:
        # A zero-length mouse event makes this process the last to receive input, so it may pass the
        # right to take the foreground to the app it starts; otherwise new windows open behind.
        if self._nudge is not None:
            self._nudge()
        w.AllowSetForegroundWindow(w.ASFW_ANY)
        ok = shell_open(app.target)
        if not ok:
            log.warning("Could not launch %s", app.name)
        return ok


def shell_open(target: str, arguments: str | None = None, timeout: float = 10.0) -> bool:
    """ShellExecute on a short-lived STA thread (shell handlers expect COM), waiting for the result."""
    result: dict[str, int] = {}

    def run() -> None:
        import comtypes

        try:
            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        except OSError:
            pass
        try:
            result["code"] = int(w.ShellExecuteW(None, "open", target, arguments, None, w.SW_SHOWNORMAL) or 0)
        finally:
            try:
                comtypes.CoUninitialize()
            except OSError:
                pass

    thread = threading.Thread(target=run, name="sightline-shell", daemon=True)
    thread.start()
    thread.join(timeout)
    code = result.get("code")
    if code is None:
        log.warning("A shell launch is taking longer than %.0f s", timeout)  # not the target: it can be a web address
        return True  # still starting; the shell owns it now
    return code > 32
