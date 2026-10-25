"""The desktop actions from ARCHITECTURE.md section 7.

Handlers run on the ``sightline-actions`` thread.  They check what Windows
will refuse before acting (administrator windows, the secure desktop), so
the user hears why instead of seeing nothing happen, and raise
``ActionError`` with a plain-English message for anything expected.

Aim.  A spoken "click" or "scroll here" acts where the user was looking when
they began to speak (``Action.aim_t``), from ``VisionAPI.aim_point``, not
where the pointer has drifted to since.  Vision is looked up when needed,
because it is created after the desktop.

Undo.  Actions with a clean reversal register an ``undo`` that builds the
reversing Action from what the handler returned (the window it acted on, the
placement it had, the point it scrolled at).  Actions that change nothing
the user would call "the last thing done" (moving the pointer, showing the
keyboard) register ``history=False``.

Every dependency is passed in, so the handlers run against the fakes in
``sightline.core.fakes`` in unit tests.
"""

from __future__ import annotations

import logging
import math
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sightline.core.actions import ActionError, ActionRegistry
from sightline.core.events import ClickPerformed
from sightline.core.risk import key_risk
from sightline.core.types import Action, ElementInfo, WindowInfo
from sightline.core.urls import REFUSED_MESSAGE as URL_REFUSED_MESSAGE
from sightline.core.urls import web_address

from .elevation import ELEVATED_MESSAGE, SECURE_DESKTOP_MESSAGE
from .keys import KeyComboError, parse_combo
from .matching import BROWSER_PROCESSES, match_apps, match_windows, normalize
from .scrolling import DIRECTIONS
from .spatial import Target, element_root, snap_point
from .textedit import TextEditResult, TextQuery, locate, walk_plan

log = logging.getLogger(__name__)

BUTTONS = ("left", "right", "middle")
OSK_BLOCKED_MESSAGE = "The on-screen keyboard types when you rest the pointer on a key, so there is no need to click."
SEARCH_URL = "https://www.google.com/search?q="

PASSWORD_TEXT_MESSAGE = "Sightline does not read password boxes, so it can't find words in one."
TEXT_UNREADABLE_MESSAGE = "I can't read the text in this box, so I can't find words in it."
TEXT_TOO_FAR_MESSAGE = "That is too far from the cursor for this box.  Move the cursor nearer to it first."
TEXT_FALLBACK_CHARS = 1000  # text before the caret read for an arrow-key walk; further than a walk can reach anyway

LOGIN_NO_MANAGER_MESSAGE = "Choose your password manager in Sightline's settings, under Sign-ins, so I know which keys fill in a sign-in."
LOGIN_NO_FIELD_MESSAGE = "Click in the user name or password box first."
# Default keys that fill in a sign-in on Windows, checked against the vendors' pages in October 2026.
PASSWORD_MANAGER_KEYS: dict[str, str] = {
    "bitwarden": "ctrl+shift+l",  # bitwarden.com/help/keyboard-shortcuts: autofill the last-used login
    "1password": "ctrl+\\",  # support.1password.com/keyboard-shortcuts: "Fill in Browser"; needs the 1Password app
    "lastpass": "alt+page up",  # "fill in next login": from third-party lists, no LastPass page found
    "keeper": "alt+k",  # docs.keeper.io: opens KeeperFill, where Shift+Enter fills the chosen login
    "dashlane": "ctrl+shift+l",  # support.dashlane.com: opens the Dashlane pop-up
}
PASSWORD_MANAGER_NAMES = {"protonpass": "Proton Pass"}  # no default keys: the user assigns them in the browser

OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}


class Guard(Protocol):
    def point_blocked(self, x: float, y: float) -> bool: ...

    def foreground_blocked(self) -> bool: ...

    def window_blocked(self, hwnd: int) -> bool: ...

    def secure_desktop_active(self) -> bool: ...


class NoGuard:
    """Nothing is ever blocked (tests, and platforms without UIPI)."""

    def point_blocked(self, x: float, y: float) -> bool:
        return False

    def foreground_blocked(self) -> bool:
        return False

    def window_blocked(self, hwnd: int) -> bool:
        return False

    def secure_desktop_active(self) -> bool:
        return False


@dataclass
class _Cycle:
    hwnds: list[int] = field(default_factory=list)
    index: int = 0
    t: float = 0.0


def _title(name: str) -> str:
    name = " ".join(str(name).split())
    return name.title() if name.islower() else name


def _num(value: Any, what: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ActionError(f"{what} must be a number") from None


def _state(info: WindowInfo) -> str:
    return "minimized" if info.minimized else "maximized" if info.maximized else "normal"


def _closes_tab(combo: str) -> bool:
    """Ctrl+W or Ctrl+F4: what closes a browser tab."""
    try:
        chord = parse_combo(combo)
    except KeyComboError:
        return False
    modifiers = {key.name.removeprefix("r") for key in chord.modifiers}
    return modifiers == {"ctrl"} and len(chord.keys) == 1 and chord.keys[0].name in ("w", "f4")


class DesktopActions:
    def __init__(
        self,
        ctx: Any,
        *,
        input: Any,
        uia: Any,
        windows: Any,
        apps: Any,
        display: Any,
        guard: Guard | None = None,
        keyboard: Any = None,
        magnifier: Any = None,
        scroller: Any = None,
        open_url: Callable[[str], bool] | None = None,
        window_at: Callable[[float, float], int] | None = None,
        is_osk_window: Callable[[int], bool] | None = None,
    ) -> None:
        self.ctx = ctx
        self.input = input
        self.uia = uia
        self.windows = windows
        self.apps = apps
        self.display = display
        self.guard: Guard = guard or NoGuard()
        self.keyboard = keyboard
        self.magnifier = magnifier
        self.scroller = scroller
        self.open_url = open_url
        self.window_at = window_at
        self.is_osk_window = is_osk_window or (lambda _hwnd: False)
        self._cycle = _Cycle()

    @property
    def cfg(self) -> Any:
        return self.ctx.settings.desktop

    # ================================================================== registration

    def register(self, registry: ActionRegistry) -> None:
        # (name, handler, description, undo, history)
        table: list[tuple[str, Callable[[Action], Any], str, Callable[[Action, Any], Action | None] | None, bool]] = [
            ("mouse.click", self.mouse_click, "Click, snapping to the nearest button or link", None, True),
            ("mouse.down", self.mouse_down, "Press and hold a mouse button", None, True),
            ("mouse.up", self.mouse_up, "Release a held mouse button", None, True),
            ("mouse.drag_toggle", self.mouse_drag_toggle, "Pick up or drop for a drag", None, True),
            ("mouse.scroll", self.mouse_scroll, "Scroll a few notches", self._undo_scroll, True),
            ("mouse.scroll_start", self.mouse_scroll_start, "Keep scrolling until told to stop", None, True),
            ("mouse.scroll_stop", self.mouse_scroll_stop, "Stop scrolling", None, False),
            ("mouse.move", self.mouse_move, "Move the pointer", None, False),
            ("key.press", self.key_press, "Press a key or shortcut", self._undo_key_press, True),
            ("key.hold", self.key_hold, "Hold a key down", self._undo_key_hold, True),
            ("key.release", self.key_release, "Release a held key", None, True),
            ("key.release_all", self.key_release_all, "Release every key and button and stop scrolling", None, False),
            ("text.type", self.text_type, "Type text", self._undo_text_type, True),
            # Kept in history with no reversal: if "undo that" skipped a selection, the dictation before it
            # would be scratched with backspaces, and the first one would delete the selected words.
            ("text.select", self.text_select, "Select words in the text you are editing", None, True),
            ("text.select_range", self.text_select_range, "Select from one phrase to another", None, True),
            ("text.caret", self.text_caret, "Move the cursor before or after a phrase", None, True),
            ("login.fill", self.login_fill, "Fill in a sign-in with your password manager", None, True),
            ("element.click", self.element_click, "Click something by its name", None, True),
            ("window.switch", self.window_switch, "Switch to a window by name", self._undo_switch, True),
            ("window.close", self.window_close, "Close a window", None, True),
            ("window.minimize", self.window_minimize, "Minimise a window", self._undo_window_state, True),
            ("window.maximize", self.window_maximize, "Maximise a window", self._undo_window_state, True),
            ("window.restore", self.window_restore, "Restore a window", self._undo_window_state, True),
            ("window.snap", self.window_snap, "Snap a window to a side of the screen", self._undo_snap, True),
            ("window.place", self.window_place, "Put a window back where it was", None, False),
            ("window.next", self.window_next, "Switch to the next window", self._undo_switch, True),
            ("window.previous", self.window_previous, "Switch to the previous window", self._undo_switch, True),
            ("desktop.show", self.desktop_show, "Show the desktop", self._undo_same, True),
            ("app.open", self.app_open, "Open an app", None, True),
            ("system.volume", self.system_volume, "Change the volume", self._undo_volume, True),
            ("system.media", self.system_media, "Play, pause or skip media", self._undo_media, True),
            ("web.go", self.web_go, "Go to a web address", None, True),
            ("web.search", self.web_search, "Search the web", None, True),
            ("keyboard.show", self.keyboard_show, "Show the on-screen keyboard", None, False),
            ("keyboard.hide", self.keyboard_hide, "Hide the on-screen keyboard", None, False),
            ("keyboard.toggle", self.keyboard_toggle, "Show or hide the on-screen keyboard", None, False),
            ("magnifier.toggle", self.magnifier_toggle, "Turn Magnifier on or off", self._undo_same, True),
            ("magnifier.zoom", self.magnifier_zoom, "Zoom Magnifier in or out", self._undo_zoom, True),
        ]  # fmt: skip
        for name, handler, description, undo, history in table:
            registry.register(name, handler, description=description, undo=undo, history=history)

    # ================================================================== shared checks

    def _aim(self, action: Action) -> tuple[float, float] | None:
        """Where the pointer rested when the user began to speak, or None to use the cursor."""
        if action.aim_t is None:
            return None
        aim_point = getattr(getattr(self.ctx, "vision", None), "aim_point", None)
        if aim_point is None:
            return None
        try:
            point = aim_point(float(action.aim_t))
            if point is None:
                return None
            x, y = float(point[0]), float(point[1])
        except Exception:  # a vision fault must not cost the click: the cursor is a fine fallback
            log.debug("aim_point failed; using the cursor", exc_info=True)
            return None
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        return (float(round(x)), float(round(y)))

    def _point(self, action: Action) -> tuple[float, float]:
        x, y = action.arg("x"), action.arg("y")
        if x is None and y is None:
            aim = self._aim(action)
            if aim is not None:
                # Put the pointer where the user looked, so the highlight shows what is about to be hit.
                if tuple(self.input.cursor_pos()) != (int(aim[0]), int(aim[1])):
                    self.input.move_cursor(*aim)
                return aim
        cx, cy = self.input.cursor_pos()
        return (_num(x, "x") if x is not None else float(cx), _num(y, "y") if y is not None else float(cy))

    def _check_secure(self) -> None:
        if self.guard.secure_desktop_active():
            raise ActionError(SECURE_DESKTOP_MESSAGE)

    def _check_point(self, x: float, y: float) -> None:
        self._check_secure()
        if self.guard.point_blocked(x, y):
            if self.window_at is not None and self.is_osk_window(self.window_at(x, y)):
                raise ActionError(OSK_BLOCKED_MESSAGE)
            raise ActionError(ELEVATED_MESSAGE)

    def _check_keyboard_target(self) -> None:
        self._check_secure()
        if self.guard.foreground_blocked():
            raise ActionError(ELEVATED_MESSAGE)

    def _button(self, action: Action) -> str:
        button = str(action.arg("button", "left") or "left").lower()
        if button not in BUTTONS:
            raise ActionError("The mouse button must be left, right or middle")
        return button

    def _content_changed(self, delay: float) -> None:
        notify = getattr(self.uia, "content_changed", None)
        if notify is not None:
            notify(delay)

    def _stop_scrolling(self) -> None:
        if self.scroller is not None:
            self.scroller.stop()

    def _set_drag(self, active: bool) -> None:
        self.ctx.state.update(drag_active=active)

    def _foreground_hwnd(self) -> int:
        foreground = self.windows.foreground()
        return foreground.hwnd if foreground is not None else 0

    # ================================================================== mouse

    def resolve_click(self, x: float, y: float, snap: bool) -> Target:
        """Where a click at (x, y) lands, using the same rule as the hover highlight."""
        enabled = bool(snap) and bool(self.cfg.snap_to_targets)
        radius = float(self.cfg.snap_radius_px) if enabled else 0.0
        fast = getattr(self.uia, "click_target", None)
        if fast is not None:
            return fast(x, y, enabled, radius)
        under = self.uia.element_at(x, y, timeout=0.05)
        if under is not None and under.clickable:
            return Target(x, y, under, False)
        if radius > 0:
            near = self.uia.nearest_clickable(x, y, radius)
            if near is not None:
                sx, sy = snap_point(near.rect, x, y, radius)
                return Target(sx, sy, near, True)
        return Target(x, y, None, False)

    def mouse_click(self, action: Action) -> dict[str, Any]:
        self._stop_scrolling()
        button = self._button(action)
        try:
            count = max(1, min(int(action.arg("count", 1) or 1), 3))
        except (TypeError, ValueError):
            raise ActionError("The click count must be 1, 2 or 3") from None
        x, y = self._point(action)
        self._check_secure()
        if self.ctx.state.drag_active and button == "left" and count == 1:
            return self._drop(x, y, action)
        target = self.resolve_click(x, y, bool(action.arg("snap", True)))
        self._check_point(target.x, target.y)
        self.input.click(button, count, target.x, target.y)  # type: ignore[arg-type]
        self.ctx.bus.publish(ClickPerformed(target.x, target.y, button, count, action.source, aim_t=action.aim_t))  # type: ignore[arg-type]
        self._content_changed(0.5)
        el = target.element
        return {
            "x": target.x,
            "y": target.y,
            "snapped": target.snapped,
            "element": el.name if el is not None else None,
            "control_type": el.control_type if el is not None else None,
        }

    def _drop(self, x: float, y: float, action: Action) -> dict[str, Any]:
        """A click while a drag is held means "drop it here"."""
        self._check_point(x, y)
        self.input.move_cursor(x, y)
        time.sleep(0.03)
        self.input.button_up("left")
        self._set_drag(False)
        self._content_changed(0.5)
        return {"x": x, "y": y, "dropped": True}

    def mouse_down(self, action: Action) -> None:
        button = self._button(action)
        x, y = self._point(action)
        self._check_point(x, y)
        if action.arg("x") is not None and action.arg("y") is not None:
            self.input.move_cursor(x, y)
        self.input.button_down(button)  # type: ignore[arg-type]
        self._set_drag(True)

    def mouse_up(self, action: Action) -> None:
        button = self._button(action)
        x, y = self._point(action)
        if action.arg("x") is not None and action.arg("y") is not None:
            self.input.move_cursor(x, y)
            time.sleep(0.02)
        self.input.button_up(button)  # type: ignore[arg-type]
        self._set_drag(False)
        self._content_changed(0.5)

    def mouse_drag_toggle(self, action: Action) -> dict[str, Any]:
        self._stop_scrolling()
        x, y = self._point(action)
        if self.ctx.state.drag_active:
            return self._drop(x, y, action)
        self._check_point(x, y)
        self.input.move_cursor(x, y)
        time.sleep(0.02)
        self.input.button_down("left")
        self._set_drag(True)
        return {"x": x, "y": y, "dragging": True}

    def _direction(self, action: Action, default: str | None = None) -> str:
        direction = str(action.arg("direction", default) or "").lower().strip()
        if direction not in DIRECTIONS:
            raise ActionError("Scroll direction must be up, down, left or right")
        return direction

    def mouse_scroll(self, action: Action) -> dict[str, Any]:
        direction = self._direction(action)
        amount = action.arg("amount")
        notches = float(self.cfg.scroll_step) if amount is None else _num(amount, "The scroll amount")
        notches = max(0.0, min(notches, 100.0))
        self._stop_scrolling()
        x, y = self._point(action)
        self._check_point(x, y)
        if action.arg("x") is not None and action.arg("y") is not None:
            self.input.move_cursor(x, y)  # the wheel scrolls whatever is under the pointer
        dy, dx = DIRECTIONS[direction]
        self.input.scroll(dy * notches, dx * notches)
        self._content_changed(0.35)
        return {"direction": direction, "amount": notches, "x": x, "y": y}

    def mouse_scroll_start(self, action: Action) -> None:
        if self.scroller is None:
            raise ActionError("Continuous scrolling is not available")
        direction = self._direction(action, "down")
        speed = _num(action.arg("speed", 1.0) or 1.0, "The scroll speed")
        x, y = self._point(action)
        self._check_point(x, y)
        self.scroller.start(direction, speed)

    def mouse_scroll_stop(self, action: Action) -> None:
        self._stop_scrolling()

    def mouse_move(self, action: Action) -> dict[str, float]:
        cx, cy = self.input.cursor_pos()
        to = action.arg("to")
        if action.arg("x") is not None or action.arg("y") is not None:
            x, y = self._point(action)
        elif action.arg("dx") is not None or action.arg("dy") is not None:
            x = cx + _num(action.arg("dx", 0) or 0, "dx")
            y = cy + _num(action.arg("dy", 0) or 0, "dy")
        elif to:
            x, y = self._named_spot(str(to), cx, cy)
        else:
            raise ActionError("Say where to move the pointer")
        self.input.move_cursor(x, y)
        return {"x": x, "y": y}

    def _named_spot(self, to: str, cx: float, cy: float) -> tuple[float, float]:
        monitor = self.display.monitor_at(cx, cy)
        r = monitor.rect
        words = set(to.lower().replace("-", " ").replace("_", " ").replace("centre", "center").split())
        inset_x, inset_y = r.w * 0.1, r.h * 0.1
        x, y = r.center
        known = {"center", "middle", "top", "bottom", "left", "right"}
        if not words or not words <= known:
            raise ActionError("I can move the pointer to the centre, top, bottom, left or right")
        if "top" in words:
            y = r.y + inset_y
        if "bottom" in words:
            y = r.bottom - inset_y
        if "left" in words:
            x = r.x + inset_x
        if "right" in words:
            x = r.right - inset_x
        return (float(int(x)), float(int(y)))

    # ================================================================== keyboard

    def key_press(self, action: Action) -> dict[str, Any]:
        combo = action.arg("combo") or action.arg("key")
        if not combo:
            raise ActionError("Say which key to press")
        try:
            repeat = max(1, min(int(action.arg("repeat", 1) or 1), 100))
        except (TypeError, ValueError):
            raise ActionError("The repeat count must be a number") from None
        hwnd = action.arg("hwnd")
        if hwnd:
            # Keys meant for one window (an undo reopening a browser tab) must not land in another.
            if not self.windows.activate(int(hwnd)):
                raise ActionError("That window is no longer open")
            time.sleep(0.1)
        self._check_keyboard_target()
        browser = 0
        if _closes_tab(str(combo)):
            foreground = self.windows.foreground()
            if foreground is not None and foreground.process.lower() in BROWSER_PROCESSES:
                browser = foreground.hwnd
        try:
            self.input.key_combo(str(combo), repeat)
        except KeyComboError as e:
            raise ActionError(str(e)) from None
        self._content_changed(0.5)
        return {"combo": str(combo), "repeat": repeat, "browser": browser}

    def key_hold(self, action: Action) -> dict[str, str]:
        key = action.arg("key") or action.arg("combo")
        if not key:
            raise ActionError("Say which key to hold")
        self._check_keyboard_target()
        try:
            self.input.key_down(str(key))
        except KeyComboError as e:
            raise ActionError(str(e)) from None
        return {"key": str(key)}

    def key_release(self, action: Action) -> None:
        key = action.arg("key") or action.arg("combo")
        if not key:
            raise ActionError("Say which key to release")
        try:
            self.input.key_up(str(key))
        except KeyComboError as e:
            raise ActionError(str(e)) from None

    def key_release_all(self, action: Action) -> None:
        self._stop_scrolling()
        self.input.release_all()
        self._set_drag(False)

    def text_type(self, action: Action) -> dict[str, int] | None:
        text = action.arg("text")
        if text is None or text == "":
            return None
        self._check_keyboard_target()
        self.input.type_text(str(text))
        self._content_changed(0.8)
        return {"chars": len(str(text))}

    # ================================================================== editing text by content

    def text_select(self, action: Action) -> dict[str, str]:
        phrase = self._phrase(action, "phrase", "Say which words to select")
        return self._edit_text(TextQuery("select", phrase))

    def text_select_range(self, action: Action) -> dict[str, str]:
        start = self._phrase(action, "start", "Say where the selection starts")
        end = self._phrase(action, "end", "Say where the selection ends")
        return self._edit_text(TextQuery("range", start, end_phrase=end))

    def text_caret(self, action: Action) -> dict[str, str]:
        where = str(action.arg("where") or "after").lower().strip()
        if where not in ("before", "after"):
            raise ActionError("The cursor can go before or after the words")
        phrase = self._phrase(action, "phrase", "Say which words to move the cursor to")
        return self._edit_text(TextQuery("caret", phrase, where=where))  # type: ignore[arg-type]

    @staticmethod
    def _phrase(action: Action, key: str, message: str) -> str:
        phrase = " ".join(str(action.arg(key) or "").split())
        if not phrase:
            raise ActionError(message)
        return phrase

    def _edit_text(self, query: TextQuery) -> dict[str, str]:
        self._check_keyboard_target()
        if self.uia.focus_info().password:
            raise ActionError(PASSWORD_TEXT_MESSAGE)
        result: TextEditResult | None = None
        edit = getattr(self.uia, "edit_text", None)
        if edit is not None:
            result = edit(query, timeout=1.0)
        if result is None or result.status == "unsupported":
            result = self._edit_by_keys(query)
        if result.status == "done":
            return {"method": "text range"}
        if result.status == "walk":
            for combo, count in result.keys:
                self.input.key_combo(combo, count)
            return {"method": "arrow keys"}
        if result.status == "password":
            raise ActionError(PASSWORD_TEXT_MESSAGE)
        if result.status == "too_far":
            raise ActionError(TEXT_TOO_FAR_MESSAGE)
        if result.status == "not_found":
            if query.kind == "range" and result.missing == query.end_phrase and query.end_phrase != query.phrase:
                raise ActionError(f"I could not find “{query.end_phrase}” near “{query.phrase}”")
            raise ActionError(f"I could not find “{result.missing or query.phrase}” in this text")
        raise ActionError(TEXT_UNREADABLE_MESSAGE)

    def _edit_by_keys(self, query: TextQuery) -> TextEditResult:
        """The field cannot select by itself: search the text before the caret and walk there with arrow keys."""
        before = self.uia.text_before_caret(max_chars=TEXT_FALLBACK_CHARS, timeout=0.3)
        if before is None:
            return TextEditResult("unsupported")
        caret = len(before)
        found = locate(before, caret, query)
        if isinstance(found, str):
            return TextEditResult("not_found", missing=found)
        plan = walk_plan(before, caret, found, select=query.kind != "caret", collapse=False)
        return TextEditResult("walk", keys=plan) if plan is not None else TextEditResult("too_far")

    # ================================================================== sign-ins

    def login_fill(self, action: Action) -> dict[str, str]:
        manager = str(self.cfg.password_manager or "").strip().lower()
        custom = " ".join(str(self.cfg.password_manager_keys or "").split())
        if not manager and not custom:
            raise ActionError(LOGIN_NO_MANAGER_MESSAGE)
        combo = custom or PASSWORD_MANAGER_KEYS.get(manager, "")
        if not combo:
            if manager in PASSWORD_MANAGER_NAMES:
                raise ActionError(
                    f"{PASSWORD_MANAGER_NAMES[manager]} has no keys for filling in a sign-in until you give it some.  "
                    "Set them in your browser's extension shortcuts, then enter the same keys in Sightline's settings, under Sign-ins."
                )
            raise ActionError("Enter the keys that fill in a sign-in in Sightline's settings, under Sign-ins.")
        focus = self.uia.focus_info()
        if not focus.editable:
            raise ActionError(LOGIN_NO_FIELD_MESSAGE)
        if key_risk(combo, focus) is not None:
            # The setting is free text, and these keys are pressed here, not through key.press, so the
            # countdown never sees them (review S-04): keys that close or delete are refused instead.
            raise ActionError(f"The keys for filling in a sign-in, “{combo}”, would close or delete something, so I did not press them.  Check them in Sightline's settings.")
        self._check_keyboard_target()
        try:
            self.input.key_combo(combo, 1)
        except KeyComboError:
            raise ActionError(f"The keys for filling in a sign-in, “{combo}”, are not keys I know.  Check them in Sightline's settings.") from None
        self._content_changed(0.8)
        return {"manager": manager or "custom"}

    # ================================================================== elements

    def element_click(self, action: Action) -> dict[str, Any]:
        name = str(action.arg("name") or "").strip()
        if not name:
            raise ActionError("Say what to click")
        button = self._button(action)
        try:
            count = max(1, min(int(action.arg("count", 1) or 1), 3))
        except (TypeError, ValueError):
            raise ActionError("The click count must be 1, 2 or 3") from None
        self._stop_scrolling()
        self._check_secure()
        matches: list[ElementInfo] = self.uia.find_by_name(name, timeout=1.0)
        if not matches:
            raise ActionError(f"I could not find “{name}” on the screen")
        element = self._nearest_of_same_name(matches, action)
        x, y = self._visible_point(element)
        if x is None or y is None:
            # Covered or scrolled away: its UI Automation pattern still works.
            if button == "left" and count == 1 and self.uia.invoke(element):
                self._content_changed(0.5)
                return {"element": element.name, "invoked": True}
            raise ActionError(f"I found “{element.name or name}” but could not click it")
        self._check_point(x, y)
        self.input.click(button, count, x, y)  # type: ignore[arg-type]
        self.ctx.bus.publish(ClickPerformed(x, y, button, count, action.source, aim_t=action.aim_t))  # type: ignore[arg-type]
        self._content_changed(0.5)
        return {"element": element.name, "x": x, "y": y}

    def _nearest_of_same_name(self, matches: list[ElementInfo], action: Action) -> ElementInfo:
        """Of several elements with the best match's name ("Save" twice), the one nearest where the user looked."""
        best = matches[0]
        key = normalize(best.name)
        same = [m for m in matches if normalize(m.name) == key]
        if len(same) < 2:
            return best
        point = self._aim(action)
        if point is None:
            cx, cy = self.input.cursor_pos()
            point = (float(cx), float(cy))
        px, py = point
        return min(same, key=lambda e: (e.rect.distance_to(px, py), math.hypot(e.rect.center[0] - px, e.rect.center[1] - py)))

    def _visible_point(self, element: ElementInfo) -> tuple[float | None, float | None]:
        """A point that hits the element: its centre, unless another window covers it."""
        cx, cy = element.rect.center
        cx, cy = float(int(cx)), float(int(cy))
        if self.window_at is None:
            return (cx, cy)
        root = element_root(element)
        if not root or self.window_at(cx, cy) == root:
            return (cx, cy)
        return (None, None)

    # ================================================================== windows

    def window_switch(self, action: Action) -> dict[str, Any]:
        previous = self._foreground_hwnd()
        hwnd = action.arg("hwnd")
        if hwnd:
            if not self.windows.activate(int(hwnd)):
                raise ActionError("Windows would not bring that window to the front")
            return {"hwnd": int(hwnd), "previous": previous}
        name = str(action.arg("name") or "").strip()
        if not name:
            raise ActionError("Say which window to switch to")
        windows: list[WindowInfo] = self.windows.list_windows()
        matches = match_windows(name, windows)
        if not matches:
            raise ActionError(f"I could not find a window called {_title(name)}")
        chosen = matches[0].item
        if chosen.hwnd == previous:
            # Already there: a second "switch to chrome" moves on to the next Chrome window.
            others = [m.item for m in matches[1:] if m.score >= matches[0].score - 10]
            if not others:
                return {"hwnd": chosen.hwnd, "title": chosen.title, "previous": previous}
            chosen = others[0]
        if not self.windows.activate(chosen.hwnd):
            raise ActionError(f"Windows would not bring {chosen.title or _title(name)} to the front")
        self._content_changed(0.2)
        return {"hwnd": chosen.hwnd, "title": chosen.title, "previous": previous}

    def _hwnd_arg(self, action: Action) -> int | None:
        hwnd = action.arg("hwnd")
        return int(hwnd) if hwnd else None

    def _window_before(self, hwnd: int | None) -> WindowInfo | None:
        """The window an action is about to change, as it is now (None when it cannot be found)."""
        try:
            if not hwnd:
                return self.windows.foreground()
            info = getattr(self.windows, "info", None)
            if info is not None:
                return info(hwnd)
            return next((win for win in self.windows.list_windows() if win.hwnd == hwnd), None)
        except Exception:
            log.debug("Could not read window %s before changing it", hwnd, exc_info=True)
            return None

    def window_close(self, action: Action) -> None:
        self._check_secure()
        self.windows.close(self._hwnd_arg(action))
        self._content_changed(0.4)

    def _change_window(self, action: Action, change: Callable[[int | None], None]) -> dict[str, Any]:
        self._check_secure()
        hwnd = self._hwnd_arg(action)
        before = self._window_before(hwnd)
        change(hwnd)
        self._content_changed(0.4)
        return {"hwnd": before.hwnd, "before": _state(before)} if before is not None else {}

    def window_minimize(self, action: Action) -> dict[str, Any]:
        return self._change_window(action, self.windows.minimize)

    def window_maximize(self, action: Action) -> dict[str, Any]:
        return self._change_window(action, self.windows.maximize)

    def window_restore(self, action: Action) -> dict[str, Any]:
        return self._change_window(action, self.windows.restore)

    def window_snap(self, action: Action) -> dict[str, Any]:
        side = str(action.arg("side") or action.arg("direction") or "").lower().strip()
        if side not in ("left", "right", "up", "down"):
            raise ActionError("A window can snap left, right, up or down")
        hwnd = self._hwnd_arg(action)
        if hwnd:
            self.windows.activate(hwnd)
        self._check_keyboard_target()
        before = self._window_before(hwnd)
        placement = None
        get_placement = getattr(self.windows, "placement", None)
        if before is not None and get_placement is not None:
            placement = get_placement(before.hwnd)
        self.input.key_combo(f"win+{side}", 1)
        self._content_changed(0.5)
        if before is None:
            return {"side": side}
        return {"side": side, "hwnd": before.hwnd, "before": _state(before), "placement": placement}

    def window_place(self, action: Action) -> None:
        hwnd = self._hwnd_arg(action)
        placement = action.arg("placement")
        place = getattr(self.windows, "place", None)
        if not hwnd or not placement or place is None:
            raise ActionError("That window cannot be put back")
        self._check_secure()
        place(hwnd, placement)
        self._content_changed(0.4)

    def _step_window(self, step: int) -> dict[str, Any]:
        now = time.monotonic()
        cycle = self._cycle
        previous = self._foreground_hwnd()
        if now - cycle.t > 4.0 or not cycle.hwnds:
            cycle.hwnds = [w.hwnd for w in self.windows.list_windows()]
            cycle.index = cycle.hwnds.index(previous) if previous in cycle.hwnds else 0
        if len(cycle.hwnds) < 2:
            raise ActionError("There is no other window to switch to")
        for _ in range(len(cycle.hwnds)):
            cycle.index = (cycle.index + step) % len(cycle.hwnds)
            hwnd = cycle.hwnds[cycle.index]
            if self.windows.activate(hwnd):
                cycle.t = time.monotonic()
                self._content_changed(0.2)
                return {"hwnd": hwnd, "previous": previous}
        raise ActionError("Windows would not switch windows")

    def window_next(self, action: Action) -> dict[str, Any]:
        return self._step_window(+1)

    def window_previous(self, action: Action) -> dict[str, Any]:
        return self._step_window(-1)

    def desktop_show(self, action: Action) -> None:
        self._check_secure()
        self.input.key_combo("win+d", 1)
        self._content_changed(0.5)

    # ================================================================== apps

    def app_open(self, action: Action) -> dict[str, Any]:
        name = str(action.arg("name") or "").strip()
        if not name:
            raise ActionError("Say which app to open")
        matches = match_apps(name, self.apps.apps())
        if not matches:
            raise ActionError(f"I could not find an app called {_title(name)}")
        app = matches[0].item
        if not self.apps.launch(app):
            raise ActionError(f"{app.name} did not start")
        self._content_changed(1.0)
        return {"app": app.name}

    # ================================================================== system

    def system_volume(self, action: Action) -> dict[str, Any]:
        direction = str(action.arg("direction") or "").lower().strip()
        if direction == "mute":
            self.input.key_combo("volume mute", 1)
            return {"direction": "mute"}
        if direction not in ("up", "down"):
            raise ActionError("Volume can go up, down or mute")
        amount = _num(action.arg("amount", 2) or 2, "The volume amount")
        # Each volume key press moves Windows' volume by 2 points, so ``amount`` reads as percent.
        presses = max(1, min(int(round(amount / 2.0)), 50))
        self.input.key_combo(f"volume {direction}", presses)
        return {"direction": direction, "amount": presses * 2}

    def system_media(self, action: Action) -> dict[str, str]:
        key = str(action.arg("key") or "").lower().strip().replace(" ", "_")
        keys = {"play_pause": "play pause", "play": "play pause", "pause": "play pause", "next": "next track",
                "previous": "previous track", "prev": "previous track", "stop": "media stop"}  # fmt: skip
        if key not in keys:
            raise ActionError("Media keys are play or pause, next, previous and stop")
        self.input.key_combo(keys[key], 1)
        return {"key": keys[key]}

    # ================================================================== web

    def _browser(self) -> WindowInfo | None:
        foreground = self.windows.foreground()
        if foreground is not None and foreground.process.lower() in BROWSER_PROCESSES:
            return foreground
        for win in self.windows.list_windows():
            if win.process.lower() in BROWSER_PROCESSES:
                return win
        return None

    def _type_in_address_bar(self, text: str) -> None:
        browser = self._browser()
        foreground = self.windows.foreground()
        if browser is not None and (foreground is None or foreground.hwnd != browser.hwnd):
            if not self.windows.activate(browser.hwnd):
                raise ActionError(f"Windows would not bring {browser.title or 'the browser'} to the front")
            time.sleep(0.15)
        self._check_keyboard_target()
        self.input.key_combo("ctrl+l", 1)  # focuses and selects the address bar in every major browser
        time.sleep(0.12)
        self.input.type_text(text)
        time.sleep(0.05)
        self.input.key_combo("enter", 1)
        self._content_changed(1.0)

    def web_go(self, action: Action) -> dict[str, Any]:
        url = str(action.arg("url") or "").strip()
        if not url:
            raise ActionError("Say which web address to go to")
        target = web_address(url)
        if target is None:  # file:, ms-settings:, search-ms: and the like go to whatever claims them (review S-09)
            raise ActionError(URL_REFUSED_MESSAGE)
        if self._browser() is None:
            if self.open_url is None or not self.open_url(target):
                raise ActionError("I could not open the web browser")
            return {"opened": target}
        self._type_in_address_bar(url)
        return {"url": url}

    def web_search(self, action: Action) -> dict[str, Any]:
        query = str(action.arg("query") or "").strip()
        if not query:
            raise ActionError("Say what to search for")
        if self._browser() is None:
            target = SEARCH_URL + urllib.parse.quote_plus(query)
            if self.open_url is None or not self.open_url(target):
                raise ActionError("I could not open the web browser")
            return {"opened": target}
        # A query that looks like an address ("python.org") would be visited; "? " forces a search
        # in Chrome, Edge and Firefox.
        text = f"? {query}" if (" " not in query and "." in query) else query
        self._type_in_address_bar(text)
        return {"query": query}

    # ================================================================== Windows accessibility tools

    def keyboard_show(self, action: Action) -> None:
        if self.keyboard is None or not self.keyboard.show():
            raise ActionError("The on-screen keyboard did not open")

    def keyboard_hide(self, action: Action) -> None:
        if self.keyboard is None:
            return
        if not self.keyboard.hide():
            raise ActionError("The on-screen keyboard did not close")

    def keyboard_toggle(self, action: Action) -> dict[str, bool]:
        if self.keyboard is None:
            raise ActionError("The on-screen keyboard is not available")
        return {"showing": bool(self.keyboard.toggle())}

    def magnifier_toggle(self, action: Action) -> dict[str, bool]:
        if self.magnifier is None:
            raise ActionError("Magnifier is not available")
        self._check_secure()
        return {"on": bool(self.magnifier.toggle())}

    def magnifier_zoom(self, action: Action) -> dict[str, str]:
        if self.magnifier is None:
            raise ActionError("Magnifier is not available")
        direction = str(action.arg("direction") or "in").lower().strip()
        if direction not in ("in", "out"):
            raise ActionError("Magnifier can zoom in or out")
        self._check_secure()
        self.magnifier.zoom(direction)
        return {"direction": direction}

    # ================================================================== undo (core calls these after a handler succeeds)

    @staticmethod
    def _undo_same(action: Action, result: Any) -> Action:
        """Actions that undo themselves: show the desktop again, Magnifier on or off again."""
        return Action(action.name, dict(action.args), action.source)

    @staticmethod
    def _undo_scroll(action: Action, result: Any) -> Action | None:
        if not isinstance(result, dict) or not result.get("amount"):
            return None
        args = {"direction": OPPOSITE[result["direction"]], "amount": result["amount"], "x": result["x"], "y": result["y"]}
        return Action("mouse.scroll", args, action.source)

    @staticmethod
    def _undo_key_press(action: Action, result: Any) -> Action | None:
        # Closing a browser tab is the one key press with a reliable reverse: Ctrl+Shift+T reopens it.
        if not isinstance(result, dict) or not result.get("browser"):
            return None
        args = {"combo": "ctrl+shift+t", "repeat": result.get("repeat", 1), "hwnd": result["browser"]}
        return Action("key.press", args, action.source)

    @staticmethod
    def _undo_key_hold(action: Action, result: Any) -> Action | None:
        if not isinstance(result, dict) or not result.get("key"):
            return None
        return Action("key.release", {"key": result["key"]}, action.source)

    def _undo_text_type(self, action: Action, result: Any) -> Action | None:
        # Speech keeps the record of what it dictated (and checks the caret has not moved), so it undoes it.
        if action.source != "voice" or not result or not self.ctx.actions.has("speech.scratch"):
            return None
        return Action("speech.scratch", {}, action.source)

    @staticmethod
    def _undo_switch(action: Action, result: Any) -> Action | None:
        if not isinstance(result, dict):
            return None
        previous, now = result.get("previous"), result.get("hwnd")
        if not previous or previous == now:
            return None
        return Action("window.switch", {"hwnd": previous}, action.source)

    @staticmethod
    def _undo_window_state(action: Action, result: Any) -> Action | None:
        if not isinstance(result, dict) or not result.get("hwnd"):
            return None
        hwnd, before = result["hwnd"], result.get("before")
        if action.name == "window.minimize":
            # Bringing a minimised window to the front restores it to how it was, maximised or not.
            return None if before == "minimized" else Action("window.switch", {"hwnd": hwnd}, action.source)
        if action.name == "window.maximize":
            undo = {"normal": "window.restore", "minimized": "window.minimize"}.get(before or "")
        else:  # window.restore
            undo = {"maximized": "window.maximize", "minimized": "window.minimize"}.get(before or "")
        return Action(undo, {"hwnd": hwnd}, action.source) if undo else None

    @staticmethod
    def _undo_snap(action: Action, result: Any) -> Action | None:
        if not isinstance(result, dict) or not result.get("hwnd"):
            return None
        hwnd, before, side = result["hwnd"], result.get("before"), result.get("side")
        if before == "maximized":
            return None if side == "up" else Action("window.maximize", {"hwnd": hwnd}, action.source)
        if before != "normal":
            return None
        if result.get("placement"):
            return Action("window.place", {"hwnd": hwnd, "placement": result["placement"]}, action.source)
        if side == "up":
            return Action("window.restore", {"hwnd": hwnd}, action.source)
        if side == "down":
            return Action("window.switch", {"hwnd": hwnd}, action.source)
        return None

    @staticmethod
    def _undo_volume(action: Action, result: Any) -> Action | None:
        if not isinstance(result, dict):
            return None
        direction = result.get("direction")
        if direction == "mute":
            return Action("system.volume", {"direction": "mute"}, action.source)
        if direction in ("up", "down"):
            return Action("system.volume", {"direction": OPPOSITE[direction], "amount": result["amount"]}, action.source)
        return None

    @staticmethod
    def _undo_media(action: Action, result: Any) -> Action | None:
        if isinstance(result, dict) and result.get("key") == "play pause":
            return Action("system.media", {"key": "play_pause"}, action.source)
        return None

    @staticmethod
    def _undo_zoom(action: Action, result: Any) -> Action | None:
        if not isinstance(result, dict) or result.get("direction") not in ("in", "out"):
            return None
        return Action("magnifier.zoom", {"direction": "out" if result["direction"] == "in" else "in"}, action.source)
