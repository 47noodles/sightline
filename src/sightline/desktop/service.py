"""``WindowsDesktop``: the ``Desktop`` facade, its threads and its events.

Construction is cheap and starts nothing: vision and speech use
``ctx.desktop`` from their constructors (display info, input, the focus
cache), so everything they touch works before ``start()``.  ``start()`` opens
the event hooks and the two UI Automation threads; ``stop()`` releases every
key and button, then closes them in reverse order.

Threads owned here:

* ``sightline-winevents``: WinEvent hooks and a hidden window for display changes.
* ``sightline-uia``: focus polling (5 Hz plus focus events), the hover loop
  (10 Hz while the pointer moves) and quick UI Automation queries.
* ``sightline-uia-scan``: clickable scans.
* ``sightline-scroll``: continuous scrolling, only while it runs.
* ``sightline-apps``: the Start menu scan, at most hourly.

``start()`` first lets go of any key or mouse button Windows reports held: a
crash while "hold shift" or a drag was active leaves them down, and the
watchdog's restart must not inherit that (review S-08).  The display is kept
on only while someone is there (``AwakePolicy``, review S-13).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from sightline.core.events import FaceStatus, FocusChanged, HoverTarget, SettingsChanged
from sightline.core.types import FocusInfo, SystemCheck

from . import win32 as w
from .accessibility import OSK_CLASS, Magnifier, OnScreenKeyboard
from .apps import StartMenuApps, shell_open
from .checks import WinRegistry, run_checks
from .display import WindowsDisplay
from .elevation import ElevationGuard, account_kind
from .handlers import DesktopActions
from .input import SendInputInjector
from .scrolling import ContinuousScroller
from .spatial import ClickableSnapshot, Target
from .uia import UIAConfig, UIAProvider
from .windows import Win32WindowManager
from .winevents import WinEventCallbacks, WinEventThread

log = logging.getLogger(__name__)


class _ListsNotifier:
    """Tells speech its dynamic vocabulary changed, coalesced and off the desktop threads."""

    def __init__(self, ctx: Any, min_interval: float = 2.0) -> None:
        self._ctx = ctx
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._pending = False
        self._last = 0.0

    def poke(self) -> None:
        with self._lock:
            if self._pending:
                return
            self._pending = True
        delay = max(0.3, self._min_interval - (time.monotonic() - self._last))
        timer = threading.Timer(delay, self._fire)
        timer.daemon = True
        timer.name = "sightline-lists"
        timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._pending = False
            self._last = time.monotonic()
        speech = getattr(self._ctx, "speech", None)
        if speech is None:
            return
        try:
            speech.lists_changed()
        except Exception:
            log.exception("speech.lists_changed failed")


class AwakePolicy:
    """Whether to keep the display on: the setting is on, and someone was seen lately (review S-13).

    Keeping the screen on stops Windows locking a PC whose owner cannot type a password, but an
    empty room should lock as usual.  Once the camera has seen a face, the request lasts until it
    has seen none for ``keep_awake_minutes``.  Before any face is seen (no camera, voice only, or a
    camera that cannot track) there is nothing to go by and the setting alone decides, like the
    attention gate, which also lets everything through without a camera.
    """

    def __init__(
        self,
        apply: Callable[[bool], None],
        cfg: Callable[[], Any],
        *,
        clock: Callable[[], float] = time.monotonic,
        timer: Callable[[float, Callable[[], None]], Any] = threading.Timer,
    ) -> None:
        self._apply = apply
        self._cfg = cfg
        self._clock = clock
        self._timer_factory = timer
        self._lock = threading.Lock()
        self._last_face: float | None = None
        self._applied: bool | None = None
        self._timer: Any = None
        self._stopped = False

    def _window_s(self) -> float:
        try:
            minutes = float(self._cfg().keep_awake_minutes)
        except (AttributeError, TypeError, ValueError):
            minutes = 5.0
        return max(1.0, min(minutes, 60.0)) * 60.0

    def wanted(self) -> bool:
        if not bool(self._cfg().keep_awake):
            return False
        if self._last_face is None:
            return True
        return self._clock() - self._last_face <= self._window_s()

    def face(self, present: bool) -> None:
        """A face status from vision (several a second while the camera runs)."""
        if present:
            with self._lock:
                self._last_face = self._clock()
        self.update()

    def update(self) -> None:
        """Apply the wanted state when it changed; while it rests on a face, look again once that face is too old."""
        with self._lock:
            if self._stopped:
                return
            want = self.wanted()
            changed = want != self._applied
            self._applied = want
            if want and self._last_face is not None and self._timer is None:
                delay = max(1.0, self._last_face + self._window_s() - self._clock() + 1.0)
                timer = self._timer_factory(delay, self._expired)
                timer.daemon = True
                self._timer = timer
                timer.start()
        if changed:
            self._apply(want)

    def _expired(self) -> None:
        with self._lock:
            self._timer = None
        self.update()

    def start(self) -> None:
        with self._lock:
            self._stopped = False
            self._applied = None
        self.update()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()


class WindowsDesktop:
    """The ``Desktop`` implementation for Windows 10 and 11."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        w.ensure_dpi_awareness()

        def cfg() -> Any:
            return ctx.settings.desktop

        self.guard = ElevationGuard()
        self.input = SendInputInjector()
        self.display = WindowsDisplay()
        self.windows = Win32WindowManager(nudge=self.input.nudge_activity)
        self.apps = StartMenuApps(nudge=self.input.nudge_activity)
        self.uia = UIAProvider(
            UIAConfig(
                snap_enabled=lambda: bool(cfg().snap_to_targets),
                snap_radius=lambda: float(cfg().snap_radius_px),
                highlight=lambda: bool(cfg().highlight_target),
            ),
            on_focus=self._on_focus,
            on_hover=self._on_hover,
            on_snapshot=self._on_snapshot,
        )
        self.keyboard = OnScreenKeyboard(self.display.primary, lambda: int(cfg().keyboard_hover_ms))
        self.magnifier = Magnifier(lambda combo: self.input.key_combo(combo, 1))
        self.scroller = ContinuousScroller(
            self.input,
            smooth=lambda: bool(cfg().smooth_scrolling),
            base_speed=lambda: float(cfg().scroll_speed),
            on_stop=lambda: self.uia.content_changed(0.3),
        )
        self.actions = DesktopActions(
            ctx,
            input=self.input,
            uia=self.uia,
            windows=self.windows,
            apps=self.apps,
            display=self.display,
            guard=self.guard,
            keyboard=self.keyboard,
            magnifier=self.magnifier,
            scroller=self.scroller,
            open_url=shell_open,
            window_at=lambda x, y: w.window_at(x, y, self.uia.own_pid),
            is_osk_window=lambda hwnd: bool(hwnd) and w.class_name(hwnd) == OSK_CLASS,
        )
        self.actions.register(ctx.actions)
        self._lists = _ListsNotifier(ctx)
        self.apps.on_change(self._lists.poke)
        self._events = WinEventThread(
            WinEventCallbacks(
                foreground=self._on_foreground,
                focus=self.uia.focus_changed,
                menu=self.uia.menu_changed,
                display=self._on_display,
                work_area=self._on_display,
            )
        )
        self.awake = AwakePolicy(self._events.keep_awake, cfg)
        self._names: frozenset[str] = frozenset()
        self._started = False
        ctx.bus.subscribe(SettingsChanged, self._on_settings)
        ctx.bus.subscribe(FaceStatus, self._on_face)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._started:
            return
        # Before anything else: a crash while a key or button was held leaves it down in Windows, and a
        # restarted Sightline would click with shift held.  Key-ups and button-ups only (review S-08).
        try:
            self.input.release_all()
        except Exception:
            log.exception("Could not release keys and buttons at start")
        # comtypes initialises COM on the thread that first imports it.  Importing it here, on the
        # caller's (GUI) thread, leaves the desktop's own threads free to join the multithreaded apartment.
        import comtypes  # noqa: F401

        self.display.refresh()
        self.awake.start()
        self._events.start()
        self.uia.start()
        self.apps.refresh_async(force=False)
        self._started = True
        log.info("Desktop services started")

    def stop(self) -> None:
        if not self._started:
            return
        self.scroller.stop()
        try:
            self.input.release_all()
        except Exception:
            log.exception("Could not release keys and buttons")
        try:
            self._ctx.state.update(drag_active=False)
        except Exception:
            pass
        try:
            self.keyboard.close_and_put_back()  # the user's own On-Screen Keyboard settings (review I-01)
        except Exception:
            log.exception("Could not put back the On-Screen Keyboard settings")
        self.awake.stop()
        self.uia.stop()
        self._events.stop()
        self._started = False
        log.info("Desktop services stopped")

    def system_checks(self) -> list[SystemCheck]:
        """Read-only checks of the Windows settings Sightline depends on.  Registry reads only: a few ms."""
        return run_checks(WinRegistry(), keep_awake_enabled=bool(self._ctx.settings.desktop.keep_awake), account=account_kind)

    # ------------------------------------------------------------------ events from the desktop threads

    def _on_focus(self, focus: FocusInfo) -> None:
        self._ctx.bus.publish(FocusChanged(focus))

    def _on_hover(self, target: Target) -> None:
        el = target.element
        if el is None:
            self._ctx.bus.publish(HoverTarget(None))
        else:
            self._ctx.bus.publish(HoverTarget(el.rect, el.name, el.control_type, target.snapped))

    def _on_snapshot(self, snapshot: ClickableSnapshot) -> None:
        names = frozenset(n for n in (e.name for e in snapshot.elements) if n)
        if names != self._names:
            self._names = names
            self._lists.poke()

    def _on_foreground(self, hwnd: int) -> None:
        self.uia.foreground_changed(hwnd)
        self._lists.poke()  # window titles are part of speech's vocabulary
        if self.apps.stale():
            self.apps.refresh_async(force=False)

    def _on_display(self) -> None:
        self.display.refresh()

    def _on_settings(self, event: SettingsChanged) -> None:
        # Everything else reads the settings when it is used; only the power request needs a nudge.
        if event.section == "desktop" and self._started:
            self.awake.update()

    def _on_face(self, event: FaceStatus) -> None:
        if self._started:
            self.awake.face(bool(event.present))
