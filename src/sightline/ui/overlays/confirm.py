"""A short countdown before actions that are hard to reverse: closing a window or tab, quitting, deleting, sending.

Concept review item 14c.  An error costs more for someone who cannot fix it by hand, and
a misheard "close this", a side conversation or a mis-aimed blink should never close or
delete anything at once.  An interceptor holds these actions from voice, the eyes or
dwell for ``confirm_delay_ms`` (1.2 s by default) while a chip above the captions says
what is about to happen and how to stop it.  What counts is the shared risk policy in
``core.risk`` (ARCHITECTURE.md section 8), the same one speech uses to keep paraphrases
from running such actions:

* ``window.close`` and ``app.quit``;
* ``key.press`` of a shortcut that closes, deletes, sends or quits (Alt+F4, Ctrl+W,
  Ctrl+Shift+W, Ctrl+F4, Delete, Shift+Delete, Ctrl+Shift+Delete, Ctrl+Enter, Alt+S,
  Ctrl+Q) or opens the Run box (Windows+R), however it is spelled and with any extra keys
  pressed with it, and Enter into a command window;
* ``text.type`` of a line ending in Enter into a command window;
* ``app.open`` of a command window, PowerShell, Terminal, the registry editor or Run;
* ``element.click``, or a click from the eyes or dwell, on a control whose name means
  delete, remove, discard, send, submit, pay, buy or empty the recycle bin.

``ui.cancel`` ("cancel"), ``key.release_all`` ("stop"), ``action.undo`` ("undo that") or
a blink on the chip cancels it, and a plain caption says so.  Otherwise the action is
dispatched again with ``confirmed=True``, aimed at the window that was in front when it
was asked for (a click at the point it was aimed at), so nothing closes a window the user
switched to meanwhile.  Actions that came in the same breath ("select all, delete, type
hello") wait with it, so they still run in the order they were said.

The interceptor is installed fail-closed (``ActionRegistry.add_interceptor``): if it
breaks, the action is dropped with a plain message rather than let through (review
S-05).  The log says what kind of action was held, never a window title or a name.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QFontMetricsF, QPainter
from PySide6.QtWidgets import QApplication

from sightline.core.risk import RISKY_ACTIONS, RISKY_KEYS, Risk, assess, normalize_combo, risk_of
from sightline.core.types import Action, FocusInfo, Rect

from ..coords import ScreenGeometry
from ..services import UiServices
from ..theme import frame_rect
from .base import OverlayWindow, solid_panel
from .captions import Captions
from .shared import USER_SOURCES, OverlaySlot, OwnWindows, Teardown, gaze_tolerance, point_of

if TYPE_CHECKING:
    from sightline.core.context import AppContext

log = logging.getLogger(__name__)

NAME = "confirm"
CONFIRMED = "confirmed"  # set on an action the countdown releases, so the interceptor lets it through
PRIORITY = 120  # before every other interceptor: "cancel" must always reach a running countdown
DEFAULT_DELAY_MS = 1200
MIN_DELAY_MS, MAX_DELAY_MS = 600, 3000
FOLLOW_UP_S = 0.3  # later actions of the same utterance arrive within this and wait with it
TICK_MS = 33
NOTICE_SECONDS = 2.5
HINT = "Say cancel or blink here to stop."


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def describe(action: Action, title: str = "", focus: FocusInfo | None = None, target: object = None, *, risk: Risk | None = None) -> tuple[str, str]:
    """(what is about to happen, the caption if it is cancelled), in plain words."""
    if risk is None:
        risk = assess(action, focus, target=target)
    times = 1
    if action.name == "key.press":
        try:
            times = max(1, int(action.arg("repeat", 1) or 1))
        except (TypeError, ValueError):
            times = 1
    if risk is None:
        doing, cancelled = f"Running {action.name}", "Cancelled."
    elif risk.kind == "close-window" and title:
        doing, cancelled = f"Closing “{_clip(title, 40)}”", risk.cancelled
    else:
        doing, cancelled = risk.doing, risk.cancelled
    if times > 1:
        doing += f" {times} times"
    return doing, cancelled


def delay_ms(value: object) -> int:
    """The countdown length setting, kept inside its range."""
    try:
        ms = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        ms = DEFAULT_DELAY_MS
    return max(MIN_DELAY_MS, min(MAX_DELAY_MS, ms))


def _utterance_key(action: Action) -> tuple[object, ...] | None:
    """What ties the actions of one utterance together; None for eye and dwell actions, which come one by one."""
    if action.utterance is None and action.aim_t is None:
        return None
    return (action.source, action.utterance, action.aim_t)


# =========================================================================== windows


class _Chip(OverlayWindow):
    """What is about to happen, how to stop it, and a bar running down.  Takes clicks, never focus."""

    cancel_requested = Signal()

    def __init__(self, services: UiServices) -> None:
        super().__init__(services, click_through=False, name="confirm-countdown")
        self.title = ""
        self.hint = HINT
        self.fraction = 1.0
        self.hover = False

    def fonts(self):  # noqa: ANN201 - (title font, hint font)
        t = self.svc.theme
        return t.font("label", size=19, weight=600), t.font("label")

    def measure(self, longest_title: str) -> tuple[float, float]:
        """Logical size for a title this long, so the chip does not change width as the seconds tick."""
        t = self.svc.theme
        title_font, hint_font = self.fonts()
        tf, hf = QFontMetricsF(title_font), QFontMetricsF(hint_font)
        pad = t.pxf(18)
        # A little spare, because digits differ in width as the seconds count down.
        width = max(tf.horizontalAdvance(longest_title) + tf.horizontalAdvance("00"), hf.horizontalAdvance(self.hint)) + 2 * pad
        height = pad + tf.height() + t.pxf(4) + hf.height() + t.pxf(12) + t.pxf(10) + pad
        return max(width, t.pxf(380)), height

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.cancel_requested.emit()

    def enterEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        super().enterEvent(event)
        self.set_hover(True)

    def leaveEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        super().leaveEvent(event)
        self.set_hover(False)

    def set_hover(self, hover: bool) -> None:
        if hover != self.hover:
            self.hover = hover
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: ANN001 - Qt signature
        t = self.svc.theme
        pal = t.palette
        p = QPainter(self)
        full = QRectF(0, 0, self.width(), self.height())
        solid_panel(p, full, pal.surface, pal.ink)
        if self.hover:
            frame_rect(p, full.adjusted(1, 1, -1, -1), pal.ink, t.pxf(2.5))
        title_font, hint_font = self.fonts()
        tf, hf = QFontMetricsF(title_font), QFontMetricsF(hint_font)
        pad = t.pxf(18)
        y = pad
        p.setFont(title_font)
        p.setPen(pal.ink)
        p.drawText(QRectF(pad, y, full.width() - 2 * pad, tf.height()), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.title)
        y += tf.height() + t.pxf(4)
        p.setFont(hint_font)
        p.setPen(pal.ink_2)
        p.drawText(QRectF(pad, y, full.width() - 2 * pad, hf.height()), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.hint)
        y += hf.height() + t.pxf(12)
        # The time left, draining: amber, because something is armed and about to happen.
        track = QRectF(pad, y, full.width() - 2 * pad, t.pxf(10))
        p.fillRect(track, pal.ground)
        frame_rect(p, track, pal.ink)
        inner = track.adjusted(2, 2, -2, -2)
        left = QRectF(inner.x(), inner.y(), inner.width() * max(0.0, min(1.0, self.fraction)), inner.height())
        if left.width() > 0.5:
            p.fillRect(left, pal.on_accent)
            p.fillRect(left.adjusted(1, 1, -1, -1), pal.accent)
        p.end()


class _Notice(OverlayWindow):
    """Click-through: one plain line, such as "Cancelled. The window stays open.", that fades away."""

    def __init__(self, services: UiServices) -> None:
        super().__init__(services, name="confirm-notice")
        self.text = ""
        self._opacity = 1.0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(450)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.valueChanged.connect(self._on_fade)
        self._anim.finished.connect(self.hide)

    def show_text(self, text: str, center_x: float, bottom: float, screen: ScreenGeometry, seconds: float = NOTICE_SECONDS) -> None:
        """Centre a line on ``center_x`` with its bottom on ``bottom`` (logical pixels on ``screen``)."""
        t = self.svc.theme
        fm = QFontMetricsF(t.font("label", size=17))
        w = fm.horizontalAdvance(text) + 2 * t.pxf(20)
        h = fm.height() + 2 * t.pxf(12)
        work = self.svc.screens.map.rect_to_logical(screen.work_rect, screen)
        x = max(work.x, min(center_x - w / 2.0, work.right - w))
        y = max(work.y, min(bottom - h, work.bottom - h))
        self.text = text
        self.screen_geometry = screen
        self.setGeometry(round(x), round(y), round(w), round(h))
        self._anim.stop()
        self._opacity = 1.0
        self.show()
        self.raise_()
        self.update()
        self._timer.start(int(seconds * 1000))

    def dismiss(self) -> None:
        self._timer.stop()
        self._anim.stop()
        self.hide()

    def _fade(self) -> None:
        self._anim.start()

    def _on_fade(self, value: object) -> None:
        self._opacity = float(value)  # type: ignore[arg-type]
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: ANN001 - Qt signature
        t = self.svc.theme
        pal = t.palette
        p = QPainter(self)
        p.setOpacity(self._opacity)
        full = QRectF(0, 0, self.width(), self.height())
        solid_panel(p, full, pal.surface, pal.ink)
        p.setFont(t.font("label", size=17))
        p.setPen(pal.ink)
        p.drawText(full, int(Qt.AlignmentFlag.AlignCenter), self.text)
        p.end()


# =========================================================================== the countdown


@dataclass(frozen=True, slots=True)
class _Held:
    action: Action  # as it will be released (aimed at the window that was in front)
    doing: str  # "" for an action that only waits with a risky one
    cancelled: str


class ConfirmCountdown(QObject):
    def __init__(self, services: UiServices) -> None:
        super().__init__()
        self.svc = services
        self.slot = OverlaySlot(services, NAME, self._closed_by_state)
        self.chip = _Chip(services)
        self.chip.cancel_requested.connect(lambda: self.cancel("blink"))
        self.notice = _Notice(services)
        self._own = OwnWindows()  # a click on the dock or a chip is Sightline's own, whatever lies beneath
        self._lock = threading.Lock()
        self._armed = False  # set on the action thread the moment it holds an action, before the chip shows
        self._batch: tuple[tuple[object, ...], float] | None = None  # (utterance, until) whose followers wait too
        self._chip_rect: Rect | None = None  # physical, for blinks on the chip (action thread)
        self._held: list[_Held] = []
        self._delay = DEFAULT_DELAY_MS / 1000.0
        self._deadline = 0.0
        self._longest = ""
        self._alive = True
        self._tick = QTimer(self)
        self._tick.setInterval(TICK_MS)
        self._tick.timeout.connect(self._on_tick)
        self._end = QTimer(self)
        self._end.setSingleShot(True)
        self._end.timeout.connect(self._release)
        services.cursor.moved.connect(self._on_cursor)
        services.theme.changed.connect(self._relayout)

    # ------------------------------------------------------------------ for tests and tools

    @property
    def is_counting(self) -> bool:
        return bool(self._held)

    @property
    def held(self) -> list[Action]:
        return [h.action for h in self._held]

    @property
    def remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic()) if self._held else 0.0

    # ------------------------------------------------------------------ the interceptor (action thread)

    def intercept(self, action: Action) -> Action | None:
        name = action.name
        with self._lock:
            armed, batch = self._armed, self._batch
        if armed:
            if name in ("ui.cancel", "action.undo"):
                self.svc.bridge.post(lambda: self.cancel(name))
                return None  # this cancel is spent on the countdown, not on whatever else is open
            if name == "key.release_all":
                self.svc.bridge.post(lambda: self.cancel(name))
                return action  # still let go of every key and button
            if name == "mouse.click" and self._on_chip(action):
                self.svc.bridge.post(lambda: self.cancel("blink"))
                return None
        if action.arg(CONFIRMED):
            return action
        risk = self._risk(action) if action.source in USER_SOURCES and self._enabled() else None
        if risk is None:
            if armed and batch is not None and batch[0] == _utterance_key(action) and time.monotonic() <= batch[1]:
                self.svc.bridge.post(lambda: self._hold(action, ""))
                return None  # said in the same breath as the risky one: it waits so the order holds
            return action
        pinned, title = self._aim(action)
        key = _utterance_key(action)
        with self._lock:
            self._armed = True
            self._batch = (key, time.monotonic() + FOLLOW_UP_S) if key is not None else None
        self.svc.bridge.post(lambda: self._hold(pinned, title, risk=risk))
        return None

    def _enabled(self) -> bool:
        return bool(getattr(self.svc.ctx.settings.ui, "confirm_risky", True))

    def _risk(self, action: Action) -> Risk | None:
        """The shared policy's verdict, with what has focus (Enter into a console) and what a click would hit."""
        return assess(action, self._focus(), target=self._click_target(action))

    def _focus(self) -> FocusInfo | None:
        desktop = self.svc.ctx.desktop
        try:
            return desktop.uia.focus_info() if desktop is not None else None
        except Exception:  # without it only the console rules are lost; the rest still count
            log.debug("Could not read the focus", exc_info=True)
            return None

    def _click_target(self, action: Action) -> str | None:
        """The name of the control a left click would land on (from the cache: this never blocks)."""
        if action.name != "mouse.click" or str(action.arg("button") or "left").lower() != "left":
            return None  # a right or middle click opens a menu; it does not press the button
        desktop = self.svc.ctx.desktop
        if desktop is None:
            return None
        point = point_of(self.svc, action)
        if point is None or self._own.at(*point):
            return None
        try:
            cfg = self.svc.ctx.settings.desktop
            snap = bool(action.arg("snap", True)) and bool(cfg.snap_to_targets)
            element = desktop.uia.nearest_clickable(point[0], point[1], float(cfg.snap_radius_px) if snap else 0.0)
        except Exception:
            log.debug("Could not look up the click target", exc_info=True)
            return None
        return element.name if element is not None and element.name else None

    def _aim(self, action: Action) -> tuple[Action, str]:
        """Pin a close or a key press to the window in front now (with its title for the chip), and a click to its point."""
        if action.name == "mouse.click":
            if action.arg("x") is not None and action.arg("y") is not None:
                return action, ""
            point = point_of(self.svc, action)
            # Where the user aimed, not wherever the pointer has drifted to when the countdown ends.
            return (action.with_args(x=point[0], y=point[1]) if point is not None else action), ""
        if action.name not in ("window.close", "key.press") or action.arg("hwnd"):
            return action, ""
        desktop = self.svc.ctx.desktop
        try:
            front = desktop.windows.foreground() if desktop is not None else None
        except Exception:  # the window can be read later by the action itself; the countdown matters more
            log.debug("Could not read the foreground window", exc_info=True)
            front = None
        if front is None or not front.hwnd:
            return action, ""
        return action.with_args(hwnd=front.hwnd), front.title

    def _on_chip(self, action: Action) -> bool:
        rect = self._chip_rect
        if rect is None:
            return False
        point = point_of(self.svc, action)
        return point is not None and rect.distance_to(*point) <= gaze_tolerance(self.svc, *point)

    # ------------------------------------------------------------------ holding and releasing (GUI thread)

    def _hold(self, action: Action, title: str, *, risk: Risk | None = None) -> None:
        if not self._alive:
            return
        if risk is None:
            if self._held:
                self._held.append(_Held(action, "", ""))
            else:
                # Its countdown has already ended (it cannot have been cancelled this fast): run it in order.
                self.svc.ctx.actions.dispatch(action.with_args(**{CONFIRMED: True}))
            return
        doing, cancelled = describe(action, title, risk=risk)
        first = not self._held
        self._held.append(_Held(action, doing, cancelled))
        # The kind only: the chip's words can carry a window title or a button's name.
        log.info("Confirm: holding %s (%s) for %.1f s", action.name, risk.kind, self._delay_s())
        if first:
            self.slot.opened()  # closes the numbers, grid, zoom, help, menu or chooser if one is up
        self._start()

    def _delay_s(self) -> float:
        return delay_ms(getattr(self.svc.ctx.settings.ui, "confirm_delay_ms", DEFAULT_DELAY_MS)) / 1000.0

    def _start(self) -> None:
        """(Re)start the full countdown; a second risky request gets the whole time again."""
        with self._lock:
            self._armed = True  # again: a countdown that just ended may have disarmed it in between
        self._delay = self._delay_s()
        self._deadline = time.monotonic() + self._delay
        self._present()
        self._end.start(int(self._delay * 1000))
        self._tick.start()

    def _risky(self) -> list[_Held]:
        return [h for h in self._held if h.doing]

    def _title(self, seconds: float) -> str:
        risky = self._risky()
        doing = risky[0].doing if risky else ""
        more = len(risky) - 1
        if more > 0:
            doing += f" and {more} more"
        return f"{doing} in {seconds:.1f} s"

    def _present(self) -> None:
        if not self._held:
            return
        t = self.svc.theme
        smap = self.svc.screens.map
        screen, center_x, bottom = self._place_above_captions()
        self._longest = self._title(self._delay)
        w, h = self.chip.measure(self._longest)
        work = smap.rect_to_logical(screen.work_rect, screen)
        w = min(w, work.w - t.pxf(32))
        x = max(work.x, min(center_x - w / 2.0, work.right - w))
        y = max(work.y, min(bottom - h, work.bottom - h))
        self.chip.screen_geometry = screen
        self.chip.setGeometry(round(x), round(y), round(w), round(h))
        self._chip_rect = smap.rect_to_physical(Rect(round(x), round(y), round(w), round(h)), screen)
        self.notice.dismiss()
        self._on_tick()
        self.chip.show()
        self.chip.raise_()
        self._on_cursor(*self.svc.cursor.pos)

    def _place_above_captions(self) -> tuple[ScreenGeometry, float, float]:
        """The screen the pointer is on, and the logical point the chip's bottom centre goes: just above the captions."""
        t = self.svc.theme
        smap = self.svc.screens.map
        screen = smap.screen_at(*self.svc.cursor.pos)
        work = smap.rect_to_logical(screen.work_rect, screen)
        # The same rule as the "did you mean" choices: above the tallest caption, or near the bottom without captions.
        captions = next((w for w in QApplication.topLevelWidgets() if isinstance(w, Captions)), None)
        if captions is None:
            below = t.pxf(150)
        else:
            below = captions.reserved_height() if captions.enabled else t.pxf(28)
        bottom = work.bottom - below - t.pxf(10)
        for widget in QApplication.topLevelWidgets():
            # The "did you mean" choices live in the same place; stand clear of them while they show.
            if widget.objectName() == "did-you-mean" and widget.isVisible():
                bottom = min(bottom, widget.geometry().top() - t.pxf(10))
        return screen, work.x + work.w / 2.0, bottom

    def _relayout(self) -> None:
        if self._held:
            self._present()
        self.chip.update()

    def _on_tick(self) -> None:
        if not self._held:
            return
        left = self.remaining
        self.chip.title = self._title(left)
        self.chip.fraction = left / self._delay if self._delay > 0 else 0.0
        self.chip.update()

    def _release(self) -> None:
        held = self._finish()
        if not held:
            return
        log.info("Confirm: running %s", ", ".join(h.action.name for h in held))
        for h in held:
            self.svc.ctx.actions.dispatch(h.action.with_args(**{CONFIRMED: True}))

    def cancel(self, why: str = "") -> None:
        """Drop what is held, say so in a plain caption, and close the chip."""
        geometry, screen = self.chip.geometry(), self.chip.screen_geometry
        held = self._finish()
        if not held:
            return
        risky = [h for h in held if h.doing]
        log.info("Confirm: cancelled %s (%s)", ", ".join(h.action.name for h in held), why or "asked")
        if risky and screen is not None and self._alive:
            self.notice.show_text(risky[0].cancelled, geometry.center().x(), geometry.bottom() + 1, screen)

    def _finish(self) -> list[_Held]:
        """Stop the countdown and hand back what it held (the GUI thread's half of disarming)."""
        held, self._held = self._held, []
        self._tick.stop()
        self._end.stop()
        with self._lock:
            self._armed = False
            self._batch = None
        self._chip_rect = None
        self.chip.hide()
        self.chip.set_hover(False)
        if held or self.slot.is_current:
            self.slot.closed()
        return held

    def _closed_by_state(self) -> None:
        # Another overlay opened, or the shell is stopping: a countdown the user has moved on from is cancelled.
        self.cancel("another overlay opened")
        self.slot.closed()

    def _on_cursor(self, x: float, y: float) -> None:
        rect = self._chip_rect
        if rect is not None:
            self.chip.set_hover(rect.contains(x, y))

    # ------------------------------------------------------------------ uninstall

    def shutdown(self) -> None:
        self._alive = False
        if self._held:
            log.info("Confirm: dropped %d held action(s) on shutdown", len(self._held))
        self._finish()
        self.notice.dismiss()
        self.slot.release()
        for signal, slot in ((self.svc.cursor.moved, self._on_cursor), (self.svc.theme.changed, self._relayout)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        for window in (self.chip, self.notice):
            window.hide()
            window.deleteLater()


def install(ctx: AppContext, services: UiServices) -> Callable[[], None]:
    """Hold hard-to-reverse actions for a short, cancellable countdown.  Returns a function that undoes it."""
    countdown = ConfirmCountdown(services)
    teardown = Teardown()
    teardown.overlay = countdown
    teardown.add(countdown.shutdown)
    # Fail-closed: if the countdown breaks, what it guards is dropped with a plain message, never let through.
    teardown.add(ctx.actions.add_interceptor(countdown.intercept, priority=PRIORITY, owner="ui.confirm", fail_closed=True))
    return teardown


__all__ = ["CONFIRMED", "RISKY_ACTIONS", "RISKY_KEYS", "ConfirmCountdown", "delay_ms", "describe", "install", "normalize_combo", "risk_of"]
