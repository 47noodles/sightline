"""Monitors: physical-pixel rectangles, work areas, scale and panel size.

The list is read lazily on first use (vision and the UI ask for it while they
are being constructed) and refreshed when Windows reports a display or work
area change.  Physical size comes from the monitor's EDID in the registry;
remote and virtual displays have none, so ``size_mm`` is None there.
"""

from __future__ import annotations

import ctypes
import logging
import re
import threading
import winreg
from ctypes import wintypes as wt

from sightline.core.types import Monitor, Rect

from . import win32 as w

log = logging.getLogger(__name__)

_EDD_GET_DEVICE_INTERFACE_NAME = 0x00000001


def parse_edid_size_mm(edid: bytes) -> tuple[float, float] | None:
    """Panel size in millimetres from an EDID block.

    The first detailed timing descriptor carries millimetres (bytes 66-68);
    bytes 21-22 carry whole centimetres, or an aspect ratio when one of them
    is zero (projectors and TVs).  Prefer millimetres when they agree with
    the centimetre figure.
    """
    if len(edid) < 128 or edid[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None
    cm_w, cm_h = edid[21], edid[22]
    mm_w = edid[66] | ((edid[68] & 0xF0) << 4)
    mm_h = edid[67] | ((edid[68] & 0x0F) << 8)
    pixel_clock = edid[54] | (edid[55] << 8)
    dtd_ok = pixel_clock != 0 and mm_w > 0 and mm_h > 0
    if dtd_ok and cm_w and cm_h:
        # Some panels report mm in the DTD that are really cm, or nonsense; trust the DTD only near the cm values.
        if abs(mm_w - cm_w * 10) <= 15 and abs(mm_h - cm_h * 10) <= 15:
            return (float(mm_w), float(mm_h))
        return (float(cm_w * 10), float(cm_h * 10))
    if dtd_ok:
        return (float(mm_w), float(mm_h))
    if cm_w and cm_h:
        return (float(cm_w * 10), float(cm_h * 10))
    return None


def _edid_for_device(device_name: str) -> bytes | None:
    """EDID bytes for a monitor device such as ``\\\\.\\DISPLAY1``, read from the registry."""
    dd = w.DISPLAY_DEVICEW()
    dd.cb = ctypes.sizeof(dd)
    if not w.EnumDisplayDevicesW(device_name, 0, ctypes.byref(dd), _EDD_GET_DEVICE_INTERFACE_NAME):
        return None
    # \\?\DISPLAY#GSM5B09#5&2b...&UID4352#{e6f07b5f-...}
    match = re.match(r"\\\\\?\\DISPLAY#([^#]+)#([^#]+)#", dd.DeviceID)
    if not match:
        return None
    path = rf"SYSTEM\CurrentControlSet\Enum\DISPLAY\{match.group(1)}\{match.group(2)}\Device Parameters"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            value, kind = winreg.QueryValueEx(key, "EDID")
    except OSError:
        return None
    return bytes(value) if kind == winreg.REG_BINARY else None


def _enumerate() -> list[Monitor]:
    handles: list[int] = []

    def collect(hmon, _hdc, _rect, _lp) -> bool:
        handles.append(hmon)
        return True

    w.EnumDisplayMonitors(None, None, w.MONITORENUMPROC(collect), 0)
    monitors: list[Monitor] = []
    for hmon in handles:
        info = w.MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if not w.GetMonitorInfoW(hmon, ctypes.byref(info)):
            continue
        r, wk = info.rcMonitor, info.rcWork
        scale = 1.0
        if w.GetDpiForMonitor is not None:
            dx, dy = wt.UINT(), wt.UINT()
            if w.GetDpiForMonitor(hmon, w.MDT_EFFECTIVE_DPI, ctypes.byref(dx), ctypes.byref(dy)) == 0 and dx.value:
                scale = dx.value / 96.0
        size = None
        edid = _edid_for_device(info.szDevice)
        if edid:
            size = parse_edid_size_mm(edid)
            width_px, height_px = r.right - r.left, r.bottom - r.top
            if size and (width_px < height_px) != (size[0] < size[1]):
                size = (size[1], size[0])  # rotated to portrait
        name = info.szDevice
        dd = w.DISPLAY_DEVICEW()
        dd.cb = ctypes.sizeof(dd)
        if w.EnumDisplayDevicesW(info.szDevice, 0, ctypes.byref(dd), 0) and dd.DeviceString:
            name = dd.DeviceString
        monitors.append(
            Monitor(
                id=info.szDevice,
                name=name,
                rect=Rect.from_ltrb(r.left, r.top, r.right, r.bottom),
                work_rect=Rect.from_ltrb(wk.left, wk.top, wk.right, wk.bottom),
                scale=scale,
                primary=bool(info.dwFlags & w.MONITORINFOF_PRIMARY),
                size_mm=size,
            )
        )
    monitors.sort(key=lambda m: (not m.primary, m.rect.x, m.rect.y))
    return monitors


def nearest_monitor(monitors: list[Monitor], x: float, y: float) -> Monitor:
    """The monitor containing the point, else the one with the nearest edge."""
    for m in monitors:
        if m.rect.contains(x, y):
            return m
    return min(monitors, key=lambda m: m.rect.distance_to(x, y))


class WindowsDisplay:
    """The ``DisplayInfo`` implementation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._monitors: list[Monitor] | None = None
        self._listeners: list = []

    def on_change(self, callback) -> None:
        """Call ``callback()`` after the monitor list changes (from the desktop event thread)."""
        self._listeners.append(callback)

    def refresh(self) -> bool:
        """Re-read the monitors.  Returns True when anything changed."""
        try:
            fresh = _enumerate()
        except OSError:
            log.exception("Could not enumerate monitors")
            return False
        if not fresh:
            return False
        with self._lock:
            changed = fresh != self._monitors
            self._monitors = fresh
        if changed:
            log.info(
                "Monitors: %s",
                "; ".join(
                    f"{m.name} {int(m.rect.w)}x{int(m.rect.h)} at {int(m.rect.x)},{int(m.rect.y)} scale {m.scale:g}"
                    + (" primary" if m.primary else "")
                    for m in fresh
                ),
            )
            for callback in list(self._listeners):
                try:
                    callback()
                except Exception:
                    log.exception("Display change listener failed")
        return changed

    def monitors(self) -> list[Monitor]:
        monitors = self._monitors
        if monitors is None:
            self.refresh()
            monitors = self._monitors
        if not monitors:
            left, top, width, height = w.virtual_screen()
            rect = Rect(left, top, width, height)
            return [Monitor("fallback", "Display", rect, rect, 1.0, True, None)]
        return list(monitors)

    def primary(self) -> Monitor:
        monitors = self.monitors()
        return next((m for m in monitors if m.primary), monitors[0])

    def virtual_rect(self) -> Rect:
        left, top, width, height = w.virtual_screen()
        return Rect(left, top, width, height)

    def monitor_at(self, x: float, y: float) -> Monitor:
        return nearest_monitor(self.monitors(), x, y)
