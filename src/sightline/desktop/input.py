"""Synthetic mouse and keyboard input through SendInput.

Measured on the development machine (125 % scaling, per-monitor DPI aware v2):

* ``SetCursorPos`` lands on the exact pixel and costs about 8 us.
* ``SendInput`` with ``MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK`` is
  exact only when the normalised coordinate points at the pixel centre:
  ``((x - left) * 65536 + 32768) // width``.  The common
  ``x * 65536 / width`` lands one pixel short on two thirds of the screen.
  It costs about 0.25 ms.

Moves go through SendInput (a real mouse event: hover effects, raw-input
apps, the idle timer and a hidden cursor all react) and are checked with
GetCursorPos; when the cursor did not arrive (Windows drops injected input
over administrator windows, or a clip rectangle is active) SetCursorPos
finishes the move.

Keys are sent as virtual key plus scan code plus the extended flag, without
``KEYEVENTF_SCANCODE``: hooks, remote desktop clients and games see the
scan code, while the virtual key stays fixed whatever the NumLock state
(scan code 0x47 alone means Home or numpad 7 depending on NumLock).

Everything held down is tracked so ``release_all`` can always undo it.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from collections.abc import Iterable
from typing import Literal

from . import win32 as w
from .keys import (
    MODIFIER_VKS,
    VK_LCONTROL,
    VK_LMENU,
    VK_LSHIFT,
    VK_LWIN,
    VK_RCONTROL,
    VK_RETURN,
    VK_RMENU,
    VK_RSHIFT,
    VK_RWIN,
    VK_TAB,
    KeyStroke,
    parse_combo,
    parse_key,
    resolve_chord,
    stroke_for_key,
)

log = logging.getLogger(__name__)

MouseButton = Literal["left", "right", "middle"]

_BUTTON_FLAGS: dict[str, tuple[int, int]] = {
    "left": (w.MOUSEEVENTF_LEFTDOWN, w.MOUSEEVENTF_LEFTUP),
    "right": (w.MOUSEEVENTF_RIGHTDOWN, w.MOUSEEVENTF_RIGHTUP),
    "middle": (w.MOUSEEVENTF_MIDDLEDOWN, w.MOUSEEVENTF_MIDDLEUP),
}

# Physical buttons as GetAsyncKeyState reports them (it ignores the swap setting).
_PHYSICAL_BUTTON_VKS = {"left": 0x01, "right": 0x02, "middle": 0x04}

_STROKES_FOR_STUCK_MODIFIERS = {
    VK_LSHIFT: KeyStroke(VK_LSHIFT, 0x2A, False, "shift"),
    VK_RSHIFT: KeyStroke(VK_RSHIFT, 0x36, False, "rshift"),
    VK_LCONTROL: KeyStroke(VK_LCONTROL, 0x1D, False, "ctrl"),
    VK_RCONTROL: KeyStroke(VK_RCONTROL, 0x1D, True, "rctrl"),
    VK_LMENU: KeyStroke(VK_LMENU, 0x38, False, "alt"),
    VK_RMENU: KeyStroke(VK_RMENU, 0x38, True, "ralt"),
    VK_LWIN: KeyStroke(VK_LWIN, 0x5B, True, "win"),
    VK_RWIN: KeyStroke(VK_RWIN, 0x5C, True, "rwin"),
}


def normalized_coordinate(value: float, origin: int, extent: int) -> int:
    """SendInput absolute coordinate (0..65535 over the virtual desktop) for a physical pixel.

    Windows maps a normalised value n back to ``floor(n * extent / 65536)``,
    so aiming at the pixel centre is exact for every pixel.
    """
    pixel = int(round(value)) - origin
    pixel = min(max(pixel, 0), extent - 1)
    return (pixel * 65536 + 32768) // extent


class Win32KeyResolver:
    """Maps keys through a real Windows keyboard layout (HKL)."""

    def __init__(self, hkl: int | None = None) -> None:
        self._hkl = hkl if hkl is not None else w.foreground_thread_layout()

    def scan_for_vk(self, vk: int) -> tuple[int, bool]:
        value = w.MapVirtualKeyExW(vk, w.MAPVK_VK_TO_VSC_EX, self._hkl)
        if not value:
            return (0, False)
        return (value & 0xFF, (value >> 8) in (0xE0, 0xE1))

    def vk_for_char(self, ch: str) -> tuple[int, bool, bool, bool] | None:
        if len(ch) != 1:
            return None
        result = w.VkKeyScanExW(ch, self._hkl)
        if result == -1:
            return None
        vk = result & 0xFF
        state = (result >> 8) & 0xFF
        return (vk, bool(state & 1), bool(state & 2), bool(state & 4))


class SendInputInjector:
    """The ``InputInjector`` implementation.  Thread safe; every method may be called from any thread."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._held_keys: dict[int, KeyStroke] = {}
        self._held_buttons: set[str] = set()
        self._last_injected = 0.0
        self._last_click = 0.0
        self._wheel_acc = [0.0, 0.0]  # vertical, horizontal, in wheel delta units
        # Timings.  A real click holds the button 60-120 ms; 30 ms is still seen by every app tested,
        # including ones that poll button state each frame.
        self.press_s = 0.03
        self.pre_click_settle_s = 0.012
        self.key_hold_s = 0.015
        self.key_gap_s = 0.008
        self.type_chunk = 48  # UTF-16 code units per SendInput call
        self.type_chunk_pause_s = 0.004
        self.fallback_moves = 0  # how often SetCursorPos had to finish a move (diagnostics)

    # ------------------------------------------------------------------ low level

    def _send(self, inputs: list[w.INPUT]) -> int:
        if not inputs:
            return 0
        array = (w.INPUT * len(inputs))(*inputs)
        sent = w.SendInput(len(inputs), array, ctypes.sizeof(w.INPUT))
        if sent != len(inputs):
            log.debug("SendInput sent %d of %d events (error %d)", sent, len(inputs), w.last_error())
        return sent

    @staticmethod
    def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> w.INPUT:
        event = w.INPUT()
        event.type = w.INPUT_MOUSE
        event.u.mi = w.MOUSEINPUT(dx, dy, ctypes.c_uint32(data).value, flags, 0, w.SIGHTLINE_INPUT_TAG)
        return event

    @staticmethod
    def _key(stroke: KeyStroke, up: bool) -> w.INPUT:
        flags = (w.KEYEVENTF_KEYUP if up else 0) | (w.KEYEVENTF_EXTENDEDKEY if stroke.extended else 0)
        event = w.INPUT()
        event.type = w.INPUT_KEYBOARD
        event.u.ki = w.KEYBDINPUT(stroke.vk, stroke.scan, flags, 0, w.SIGHTLINE_INPUT_TAG)
        return event

    @staticmethod
    def _unicode(unit: int, up: bool) -> w.INPUT:
        event = w.INPUT()
        event.type = w.INPUT_KEYBOARD
        event.u.ki = w.KEYBDINPUT(0, unit, w.KEYEVENTF_UNICODE | (w.KEYEVENTF_KEYUP if up else 0), 0, w.SIGHTLINE_INPUT_TAG)
        return event

    @staticmethod
    def _logical_button(button: str) -> str:
        """The physical button that performs ``button``'s role when the user has swapped buttons."""
        if button in ("left", "right") and w.GetSystemMetrics(w.SM_SWAPBUTTON):
            return "right" if button == "left" else "left"
        return button

    # ------------------------------------------------------------------ mouse

    def cursor_pos(self) -> tuple[int, int]:
        return w.cursor_pos()

    def move_cursor(self, x: float, y: float) -> None:
        left, top, width, height = w.virtual_screen()
        tx = min(max(int(round(x)), left), left + width - 1)
        ty = min(max(int(round(y)), top), top + height - 1)
        with self._lock:
            nx = normalized_coordinate(tx, left, width)
            ny = normalized_coordinate(ty, top, height)
            self._send([self._mouse(w.MOUSEEVENTF_MOVE | w.MOUSEEVENTF_ABSOLUTE | w.MOUSEEVENTF_VIRTUALDESK, nx, ny)])
            self._last_injected = time.perf_counter()
            if w.cursor_pos() != (tx, ty):
                # Blocked by UIPI, clipped, or between monitors: SetCursorPos is never blocked and clamps
                # to the nearest monitor itself.
                w.SetCursorPos(tx, ty)
                self.fallback_moves += 1

    def click(self, button: MouseButton = "left", count: int = 1, x: float | None = None, y: float | None = None) -> None:
        if button not in _BUTTON_FLAGS:
            raise ValueError(f"unknown mouse button {button!r}")
        count = max(1, min(int(count), 3))
        down, up = _BUTTON_FLAGS[self._logical_button(button)]
        # Clicks of a double click must fall inside the system double-click time.
        gap = min(0.06, w.GetDoubleClickTime() / 1000.0 * 0.3)
        with self._lock:
            if x is not None and y is not None:
                self.move_cursor(x, y)
                time.sleep(self.pre_click_settle_s)  # lets the target see the move (hover) before the press
            for i in range(count):
                self._send([self._mouse(down)])
                self._held_buttons.add(button)
                try:
                    time.sleep(self.press_s)
                finally:  # whatever happens, the button comes back up
                    self._send([self._mouse(up)])
                    self._held_buttons.discard(button)
                if i + 1 < count:
                    time.sleep(gap)
            self._last_injected = self._last_click = time.perf_counter()

    def button_down(self, button: MouseButton = "left") -> None:
        if button not in _BUTTON_FLAGS:
            raise ValueError(f"unknown mouse button {button!r}")
        with self._lock:
            self._send([self._mouse(_BUTTON_FLAGS[self._logical_button(button)][0])])
            self._held_buttons.add(button)
            self._last_injected = time.perf_counter()

    def button_up(self, button: MouseButton = "left") -> None:
        if button not in _BUTTON_FLAGS:
            raise ValueError(f"unknown mouse button {button!r}")
        with self._lock:
            self._send([self._mouse(_BUTTON_FLAGS[self._logical_button(button)][1])])
            self._held_buttons.discard(button)
            self._last_injected = time.perf_counter()

    def buttons_held(self) -> set[str]:
        with self._lock:
            return set(self._held_buttons)

    def scroll(self, dy: float = 0.0, dx: float = 0.0, *, smooth: bool = False) -> None:
        """Wheel scroll in notches; positive ``dy`` moves the content down (wheel toward the user).

        Fractions accumulate, so ten calls of 0.1 make one notch.  With
        ``smooth`` the wheel turns in thirds of a notch, which modern apps
        animate smoothly; otherwise only whole notches are sent, one event per
        notch like a real wheel, which every app understands.
        """
        unit = 40 if smooth else w.WHEEL_DELTA
        with self._lock:
            events: list[w.INPUT] = []
            for axis, amount, flag, sign in ((0, dy, w.MOUSEEVENTF_WHEEL, -1), (1, dx, w.MOUSEEVENTF_HWHEEL, 1)):
                if not amount:
                    continue
                self._wheel_acc[axis] += sign * amount * w.WHEEL_DELTA
                whole = int(self._wheel_acc[axis] / unit) * unit
                if whole == 0:
                    continue
                self._wheel_acc[axis] -= whole
                step = w.WHEEL_DELTA if abs(whole) >= w.WHEEL_DELTA else abs(whole)
                remaining = abs(whole)
                direction = 1 if whole > 0 else -1
                while remaining > 0:
                    delta = min(step, remaining)
                    events.append(self._mouse(flag, data=direction * delta))
                    remaining -= delta
            for i, event in enumerate(events):
                self._send([event])
                if i + 1 < len(events):
                    time.sleep(0.008)  # a real wheel spaces its notches; some apps coalesce otherwise
            if events:
                self._last_injected = time.perf_counter()

    def reset_scroll_remainder(self) -> None:
        with self._lock:
            self._wheel_acc = [0.0, 0.0]

    # ------------------------------------------------------------------ keyboard

    def key_combo(self, combo: str, repeat: int = 1) -> None:
        chord = parse_combo(combo)
        mods, mains = resolve_chord(chord, Win32KeyResolver())
        repeat = max(1, min(int(repeat), 100))
        with self._lock:
            pressed: list[KeyStroke] = []
            try:
                for m in mods:
                    if m.vk in self._held_keys:
                        continue  # the user is holding it ("hold shift"); leave it held afterwards
                    self._send([self._key(m, up=False)])
                    pressed.append(m)
                if pressed and mains:
                    time.sleep(self.key_gap_s)
                for r in range(repeat):
                    if mains:
                        self._send([self._key(k, up=False) for k in mains])
                        try:
                            time.sleep(self.key_hold_s)
                        finally:
                            self._send([self._key(k, up=True) for k in reversed(mains)])
                    else:
                        time.sleep(self.key_hold_s)
                    if r + 1 < repeat:
                        time.sleep(self.key_gap_s)
                        if not mains:
                            # Bare modifiers ("press win" twice): release and press again.
                            self._send([self._key(m, up=True) for m in reversed(pressed)])
                            time.sleep(self.key_gap_s)
                            self._send([self._key(m, up=False) for m in pressed])
            finally:
                if pressed:
                    self._send([self._key(m, up=True) for m in reversed(pressed)])

    def key_down(self, key: str) -> None:
        stroke = stroke_for_key(parse_key(key), Win32KeyResolver())
        with self._lock:
            self._send([self._key(stroke, up=False)])
            self._held_keys[stroke.vk] = stroke

    def key_up(self, key: str) -> None:
        stroke = stroke_for_key(parse_key(key), Win32KeyResolver())
        with self._lock:
            held = self._held_keys.pop(stroke.vk, stroke)
            self._send([self._key(held, up=True)])

    def keys_held(self) -> list[str]:
        with self._lock:
            return [s.name for s in self._held_keys.values()]

    def type_text(self, text: str) -> None:
        if not text:
            return
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        enter = KeyStroke(VK_RETURN, 0x1C, False, "enter")
        tab = KeyStroke(VK_TAB, 0x0F, False, "tab")
        with self._lock:
            batch: list[w.INPUT] = []
            units_in_batch = 0

            def flush() -> None:
                nonlocal batch, units_in_batch
                if batch:
                    self._send(batch)
                    time.sleep(self.type_chunk_pause_s)
                batch = []
                units_in_batch = 0

            for ch in text:
                if ch == "\n" or ch == "\t":
                    # Real Enter and Tab keys: a Unicode line feed is ignored or misread by many apps.
                    flush()
                    stroke = enter if ch == "\n" else tab
                    self._send([self._key(stroke, up=False), self._key(stroke, up=True)])
                    time.sleep(self.type_chunk_pause_s)
                    continue
                for unit in _utf16_units(ch):
                    # Each code unit is a down/up pair; a surrogate pair is two pairs in order, which
                    # TranslateMessage turns into two WM_CHARs the app joins.
                    batch.append(self._unicode(unit, up=False))
                    batch.append(self._unicode(unit, up=True))
                    units_in_batch += 1
                if units_in_batch >= self.type_chunk:
                    flush()
            flush()

    # ------------------------------------------------------------------ safety

    def release_all(self) -> None:
        """Release every key and button Sightline holds, plus any modifier or button Windows reports stuck."""
        with self._lock:
            events: list[w.INPUT] = []
            for stroke in reversed(list(self._held_keys.values())):
                events.append(self._key(stroke, up=True))
            released_vks = set(self._held_keys)
            self._held_keys.clear()
            for vk in sorted(MODIFIER_VKS):
                if vk not in released_vks and w.GetAsyncKeyState(vk) & 0x8000:
                    events.append(self._key(_STROKES_FOR_STUCK_MODIFIERS[vk], up=True))
            self._send(events)
            for button in list(self._held_buttons):
                self._send([self._mouse(_BUTTON_FLAGS[self._logical_button(button)][1])])
            self._held_buttons.clear()
            for physical, vk in _PHYSICAL_BUTTON_VKS.items():
                if w.GetAsyncKeyState(vk) & 0x8000:
                    self._send([self._mouse(_BUTTON_FLAGS[physical][1])])
            self.reset_scroll_remainder()

    def last_injected(self) -> float:
        return self._last_injected

    def last_click(self) -> float:
        return self._last_click

    def nudge_activity(self) -> None:
        """A zero-length mouse event.  Counts as input for Windows' foreground rules and idle timer."""
        self._send([self._mouse(w.MOUSEEVENTF_MOVE)])


def _utf16_units(ch: str) -> Iterable[int]:
    data = ch.encode("utf-16-le", "surrogatepass")
    for i in range(0, len(data), 2):
        yield data[i] | (data[i + 1] << 8)
