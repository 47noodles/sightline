"""Pointer engine (where the pointer should be) and cursor pump (moving the real cursor there smoothly).

Hybrid mode (the default): when the user fixates further than the jump
distance from the pointer for ``warp_dwell_s``, the pointer jumps to the
fixation; closer than that, only head movement moves it.  Webcam gaze is
accurate to a few degrees, the head is precise, and this splits the work
between them.  After a jump the fixation that caused it becomes an anchor:
while the eyes stay on that spot the pointer will not jump back to it, so
the head can steer the pointer away from an estimate that is a little off
without the gaze pulling it back.  For ``head_quiet_s`` after a jump, head
movement is ignored: people turn the head part of the way during a large
eye movement, and that turn would otherwise push the pointer past the
target it just reached.  Once the head has moved the pointer after a jump
(``steered``), it is where the user put it rather than where gaze guessed,
and the UI's number chooser stops second-guessing clicks there.

The engine keeps a few seconds of targets, so a spoken "click" can act
where the pointer came to rest when the user began to speak
(``rest_point``) rather than where it is when the command arrives.

The cursor pump runs at about 120 Hz and eases the real cursor towards the
engine's target with a critically damped spring (no overshoot), fast for
jumps so the eye can follow, gentler otherwise.  If the cursor moves in a
way Sightline did not cause, a person is using the mouse: the pump stops
for ``yield_s`` after the last such movement, so a carer is never fought.
If another Sightline action moves the cursor (a spoken click at its aim
point, a snapped click, the grid), a press may follow a few milliseconds
later: the pump keeps still for ``hold_s`` and then carries on from where
that action left the cursor, so the press lands where it was aimed.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from sightline.core.events import PointerPhase
from sightline.core.interfaces import DisplayInfo, InputInjector
from sightline.core.types import PointerMode, Rect

from .filters import GazeState
from .head import HeadSample

log = logging.getLogger(__name__)

# Another action moved the cursor by more than this from the pump's last command (a pixel of slack
# for rounding at screen edges).
MOVED_PX = 1.5


@dataclass
class PointerConfig:
    warp_distance: float = 180.0
    warp_dwell_s: float = 0.13
    warp_anim_s: float = 0.08
    fine_scale: float = 0.35  # hybrid mode: gaze makes the long moves, so head movement is finer than in head mode
    pause_when_face_lost: bool = True
    reacquire_s: float = 0.3  # after the face returns, hold still this long while tracking settles
    head_quiet_s: float = 0.2  # after a jump, ignore head movement this long (the head turn that came with the saccade)
    history_s: float = 3.0  # spoken commands look back about a second; keep spare
    rest_radius_px: float = 12.0  # "at rest": the target moved less than this ...
    rest_s: float = 0.1  # ... over this long
    # Conservative jumping (MAGIC's conservative variant): a far fixation only becomes a candidate, and the
    # pointer jumps there on the first sign of intent: a head movement, a click gesture or a spoken command.
    warp_on_intent: bool = False
    intent_head_px: float = 0.5  # head movement past the deadband, in pixels this frame, that counts as intent
    # Pointer movement since gaze last placed it, summed, before the pointer counts as steered.  The head's
    # deadband has already removed tremor; this only ignores sub-pixel changes nobody could see.
    steer_px: float = 2.0


@dataclass(frozen=True, slots=True)
class PumpTarget:
    x: float
    y: float
    active: bool  # False: leave the real cursor alone (paused, face lost, calibrating)
    fast: bool  # a jump is animating
    seq: int  # increases on every jump and external re-anchor


class PointerEngine:
    """Decides the pointer target each camera frame.  ``update`` runs on the vision thread; the rest is thread-safe."""

    def __init__(self, display: DisplayInfo | None, start: tuple[float, float] | None = None) -> None:
        self.cfg = PointerConfig()
        self._display = display
        self._lock = threading.Lock()
        if start is None:
            start = display.primary().rect.center if display is not None else (960.0, 540.0)
        self._x, self._y = float(start[0]), float(start[1])
        self._phase: PointerPhase = "head"
        self._active = True
        self._seq = 0
        self._warp_until = 0.0
        self._head_quiet_until = 0.0
        self._anchor: tuple[float, float] | None = None
        self._history: deque[tuple[float, float, float]] = deque()
        self._candidates: deque[tuple[float, float, float]] = deque()  # (t, x, y), nan when there was none
        self._jump: tuple[float, float, float] | None = None  # (t, x, y) of the last jump
        self._t_last = 0.0
        self._lost_since: float | None = None
        self._reacquire_until = 0.0
        self._steered = False
        self._moved_px = 0.0  # movement since gaze last placed the pointer, other than gaze's own

    # ------------------------------------------------------------------ thread-safe accessors

    @property
    def position(self) -> tuple[float, float]:
        with self._lock:
            return self._x, self._y

    @property
    def phase(self) -> PointerPhase:
        return self._phase

    @property
    def steered(self) -> bool:
        """The head (or a nudge, or the mouse) has moved the pointer since gaze last placed it.

        Then the pointer is where the user put it, not where gaze guessed, and a click there
        needs no second guess (``PointerSample.steered``).  Gaze placing the pointer, by a
        jump or by following in gaze-only mode, clears it.
        """
        return self._steered

    def pump_target(self, now: float | None = None) -> PumpTarget:
        now = time.perf_counter() if now is None else now
        with self._lock:
            return PumpTarget(self._x, self._y, self._active, now < self._warp_until, self._seq)

    def position_at(self, t: float) -> tuple[float, float]:
        """Pointer target at time ``t`` (the last one at or before it), from the recent history."""
        with self._lock:
            best = None
            for ht, hx, hy in self._history:
                if ht <= t:
                    best = (hx, hy)
                else:
                    break
            if best is None:
                if self._history:
                    _, hx, hy = self._history[0]
                    return hx, hy
                return self._x, self._y
            return best

    def rest_point(self, t: float) -> tuple[float, float] | None:
        """Where the target came to rest at or just after ``t``.  Thread-safe and never waits long.

        At rest means it moved less than ``rest_radius_px`` over the ``rest_s`` before.  If the
        target was at rest at ``t``, that point; if it was moving (the head steering), the first
        point where it came to rest after ``t``; if it has not rested since, the latest target.
        None when the pointer is not active (paused, face lost, calibrating), before there is
        any history, or when ``t`` is older than the history.
        """
        if not self._lock.acquire(timeout=0.05):
            return None
        try:
            if not self._active or not self._history:
                return None
            hist = list(self._history)
        finally:
            self._lock.release()
        return find_rest(hist, t, self.cfg.rest_radius_px, self.cfg.rest_s)

    def take_intent(self, t: float) -> tuple[float, float] | None:
        """Conservative jumping: the user acted (a click gesture, a spoken command) aiming at time ``t``.

        If they were then looking somewhere the pointer would have jumped to, jump there now and
        return that point.  None otherwise (and always when jumping is not conservative).
        """
        if not self.cfg.warp_on_intent or not self._lock.acquire(timeout=0.05):
            return None
        try:
            if not self._active:
                return None
            cand = self._candidate_at(t)
            if cand is None or math.hypot(cand[0] - self._x, cand[1] - self._y) <= self.cfg.warp_distance:
                return None
            self._intent_jump(cand)
            return self._x, self._y
        finally:
            self._lock.release()

    def aim_point(self, t: float) -> tuple[float, float] | None:
        """Where a command aimed at time ``t`` acts.  Thread-safe and never waits long.

        Usually ``rest_point(t)``.  With conservative jumping, when the user was looking somewhere
        the pointer would have jumped to at ``t``, the pointer jumps there now; if it went there
        already since ``t`` (a head movement, or an earlier command with the same aim), the answer
        is where it came to rest after that jump.
        """
        if not self._lock.acquire(timeout=0.05):
            return None
        try:
            if not self._active or not self._history:
                return None
            if self.cfg.warp_on_intent:
                cand = self._candidate_at(t)
                if cand is not None:
                    jump = self._jump
                    if jump is not None and jump[0] >= t and math.hypot(jump[1] - cand[0], jump[2] - cand[1]) <= self.cfg.warp_distance:
                        t = jump[0]
                    elif math.hypot(cand[0] - self._x, cand[1] - self._y) > self.cfg.warp_distance:
                        self._intent_jump(cand)
                        return self._x, self._y
            hist = list(self._history)
        finally:
            self._lock.release()
        return find_rest(hist, t, self.cfg.rest_radius_px, self.cfg.rest_s)

    def _candidate_at(self, t: float) -> tuple[float, float] | None:
        """The far fixation the user was looking at at ``t``, if any (lock held)."""
        for ht, hx, hy in reversed(self._candidates):
            if ht <= t + 0.05:  # half a frame or so of slack: the command's time is not a frame time
                return None if math.isnan(hx) else (hx, hy)
        return None

    def _intent_jump(self, cand: tuple[float, float]) -> None:
        """Jump between frames (lock held).  The history gets the new point at once, so a second
        command aimed at the same moment finds the pointer where the first one put it."""
        t = self._t_last + 1e-6
        self._warp(t, cand[0], cand[1])
        self._history.append((t, self._x, self._y))

    def adopt(self, x: float, y: float) -> None:
        """The cursor was moved by something else (a person, or another Sightline action): continue from there."""
        with self._lock:
            nx, ny = self._clamp(x, y)
            moved = math.hypot(nx - self._x, ny - self._y)
            if moved > 1.0:  # not the cursor's own rounding, which coming back from a pause adopts every time
                self._note_moved(moved)  # a person's mouse, a click that snapped, "move mouse"
            self._x, self._y = nx, ny
            self._anchor = None
            self._seq += 1

    def nudge(self, dx: float, dy: float) -> tuple[float, float]:
        with self._lock:
            self._x, self._y = self._clamp(self._x + dx, self._y + dy)
            self._seq += 1
            self._steered = True  # "move left 10": the user corrected the pointer on purpose
            return self._x, self._y

    def move_to(self, x: float, y: float) -> None:
        with self._lock:
            self._x, self._y = self._clamp(x, y)
            self._anchor = None
            self._seq += 1

    def _clamp(self, x: float, y: float) -> tuple[float, float]:
        if self._display is None:
            return x, y
        try:
            rect: Rect = self._display.monitor_at(x, y).rect
        except Exception:  # display changing under us: keep the point as is
            return x, y
        return rect.clamp_point(x, y)

    # ------------------------------------------------------------------ per frame

    def update(
        self,
        t: float,
        *,
        present: bool,
        mode: PointerMode,
        gaze: GazeState | None,
        head: HeadSample | None,
        frozen: bool,
        paused: bool,
        inactive: bool = False,
        reading: bool = False,
    ) -> PointerPhase:
        """One camera frame.  ``gaze`` is None when there is no calibrated estimate this frame.

        ``reading``: the user is reading (``reading.ReadingDetector``); the pointer holds still
        instead of following the eyes along the lines.
        """
        cfg = self.cfg
        phase: PointerPhase
        with self._lock:
            self._t_last = t
            candidate: tuple[float, float] | None = None
            if not present:
                if self._lost_since is None:
                    self._lost_since = t
                self._active = not cfg.pause_when_face_lost and not paused and not inactive
                phase = "lost"
            else:
                if self._lost_since is not None:
                    self._lost_since = None
                    if cfg.pause_when_face_lost:
                        self._reacquire_until = t + cfg.reacquire_s
                self._active = not paused and not inactive
                if paused or inactive:
                    phase = "paused"
                    self._anchor = None
                elif frozen or t < self._reacquire_until:
                    phase = "frozen"
                else:
                    phase, candidate = self._move(t, mode, gaze, head, reading)
            self._phase = phase
            self._history.append((t, self._x, self._y))
            while self._history and t - self._history[0][0] > cfg.history_s:
                self._history.popleft()
            if cfg.warp_on_intent:
                self._candidates.append((t, *candidate) if candidate is not None else (t, math.nan, math.nan))
                while self._candidates and t - self._candidates[0][0] > cfg.history_s:
                    self._candidates.popleft()
            elif self._candidates:
                self._candidates.clear()
        return phase

    def _warp(self, t: float, x: float, y: float) -> None:
        """Jump to a fixation (lock held)."""
        cfg = self.cfg
        self._x, self._y = self._clamp(x, y)
        self._anchor = (x, y)
        self._seq += 1
        self._warp_until = t + cfg.warp_anim_s
        self._head_quiet_until = t + max(cfg.head_quiet_s, cfg.warp_anim_s)
        self._jump = (t, self._x, self._y)
        self._placed_by_gaze()

    def _placed_by_gaze(self) -> None:
        """Gaze has just put the pointer where it is: nothing has corrected it yet (lock held)."""
        self._steered = False
        self._moved_px = 0.0

    def _note_moved(self, distance: float) -> None:
        """Something other than gaze moved the pointer this far (lock held)."""
        self._moved_px += distance
        if self._moved_px >= self.cfg.steer_px:
            self._steered = True

    def _move(
        self, t: float, mode: PointerMode, gaze: GazeState | None, head: HeadSample | None, reading: bool
    ) -> tuple[PointerPhase, tuple[float, float] | None]:
        """The new target.  Also returns the jump candidate, for conservative jumping."""
        cfg = self.cfg
        if mode == PointerMode.GAZE and gaze is not None:
            if not reading:
                self._x, self._y = self._clamp(gaze.x, gaze.y)
                self._placed_by_gaze()
            return "gaze", None
        if mode == PointerMode.HYBRID and gaze is not None:
            candidate = None
            if not reading and gaze.fixating and gaze.fixation_s >= cfg.warp_dwell_s:
                fx, fy = gaze.fixation_x, gaze.fixation_y
                d_pointer = math.hypot(fx - self._x, fy - self._y)
                d_anchor = math.inf if self._anchor is None else math.hypot(fx - self._anchor[0], fy - self._anchor[1])
                # The anchor refreshes whenever the eyes rest near the pointer, so a new place to look is
                # always far from it; the same place is not, however far the head has steered the pointer.
                if d_pointer > cfg.warp_distance and d_anchor > 0.75 * cfg.warp_distance:
                    if not cfg.warp_on_intent:
                        self._warp(t, fx, fy)
                        return "warp", None
                    candidate = (fx, fy)
                elif d_pointer <= cfg.warp_distance:
                    # Looking near the pointer: that is where the eyes rest now.
                    self._anchor = (fx, fy)
            if t < self._warp_until:
                return "warp", candidate
            if t < self._head_quiet_until:
                return "fine", candidate  # the head turn that came with the jump: do not let it push the pointer on
            if candidate is not None and head is not None and math.hypot(head.dx, head.dy) > cfg.intent_head_px:
                # Looking somewhere new and then moving the head: that is the intent to go there.
                self._warp(t, candidate[0], candidate[1])
                return "warp", None
            if reading:
                return "fine", None  # held; a deliberate head movement ends reading before it gets here
            self._apply_head(head, cfg.fine_scale)
            return "fine", candidate
        if mode == PointerMode.GAZE:
            return "gaze", None  # no estimate this frame: hold
        # Head mode, or hybrid before gaze calibration.
        self._apply_head(head, 1.0)
        return "head", None

    def _apply_head(self, head: HeadSample | None, scale: float) -> None:
        if head is None or (head.dx == 0.0 and head.dy == 0.0):
            return
        x, y = self._clamp(self._x + head.dx * scale, self._y + head.dy * scale)
        self._note_moved(math.hypot(x - self._x, y - self._y))  # measured after clamping: an edge stops it
        self._x, self._y = x, y


class CursorPump:
    """Moves the real cursor toward the engine's target about 120 times a second."""

    def __init__(
        self,
        injector: InputInjector,
        engine: PointerEngine,
        *,
        rate: float = 120.0,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.input = injector
        self.engine = engine
        self.rate = rate
        self.clock = clock
        self.yield_enabled = True
        self.yield_s = 1.5
        # After another Sightline action moves the cursor, keep still this long: its press comes 12 to
        # 40 ms after the move, and easing in that gap would put the press somewhere else.
        self.hold_s = 0.15
        self.eps_px = 6.0
        self.omega_follow = 40.0  # rad/s: 95% of a step in about 120 ms
        self.omega_jump = 60.0  # 95% of a jump in about 80 ms
        self._pos: tuple[float, float] | None = None
        self._vel = (0.0, 0.0)
        self._t: float | None = None
        self._last_cmd: tuple[int, int] | None = None
        self._last_cmd_t = 0.0  # last_injected() as of the last input the pump has accounted for
        self._was_active = False
        self._yield_until: float | None = None
        self._yield_pos: tuple[int, int] | None = None
        self._hold_until: float | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.on_yield: Callable[[bool], None] | None = None

    def set_smoothing(self, smoothing: float) -> None:
        s = min(max(smoothing, 0.0), 1.0)
        self.omega_follow = 55.0 - 30.0 * s

    @property
    def yielding(self) -> bool:
        return self._yield_until is not None

    @property
    def holding(self) -> bool:
        """Keeping still after another Sightline action moved the cursor."""
        return self._hold_until is not None

    def reset(self) -> None:
        """The engine was replaced: start again from wherever the real cursor is now."""
        self._was_active = False
        self._last_cmd = None
        self._hold_until = None

    # ------------------------------------------------------------------ one step

    def tick(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        target = self.engine.pump_target(now)
        try:
            # The order of these reads matters: the injection time read before a cursor position can be
            # marked as accounted for with that position; one read after it may belong to a move the
            # position does not show yet.
            seen = self.input.last_injected()
            actual = self._cursor()
            injected = self.input.last_injected()
        except Exception:  # pragma: no cover - the desktop module may be restarting
            log.debug("cursor_pos failed", exc_info=True)
            return

        if self._yield_until is not None:
            if actual != self._yield_pos:
                self._yield_pos = actual
                self._yield_until = now + self.yield_s
            if now < self._yield_until:
                self._t = now
                return
            # The person has let go of the mouse: carry on from where they left the cursor.
            self._yield_until = None
            self.engine.adopt(float(actual[0]), float(actual[1]))
            self._reset_at(actual, now, seen)
            self._notify(False)
            return

        if not target.active:
            self._was_active = False
            self._last_cmd = None
            self._pos = None
            self._hold_until = None
            self._t = now
            return
        if not self._was_active:
            # Becoming active (resumed, face back): start from the real cursor, wherever it is now.
            self._was_active = True
            self.engine.adopt(float(actual[0]), float(actual[1]))
            self._reset_at(actual, now, seen)
            return

        if injected > self._last_cmd_t + 1e-6:
            # Another Sightline action sent input since the pump last looked.  Read the cursor again: a
            # move sent between the reads above shows up now, and must not be taken for a person's hand.
            try:
                actual = self._cursor()
            except Exception:  # pragma: no cover
                return
            if self._last_cmd is None or math.hypot(actual[0] - self._last_cmd[0], actual[1] - self._last_cmd[1]) > MOVED_PX:
                # It moved the cursor (a spoken click at its aim point, a click that snapped, the grid,
                # "move mouse") and may press next: keep still for a moment, then carry on from here
                # rather than easing back to where the pointer was.
                self.engine.adopt(float(actual[0]), float(actual[1]))
                self._reset_at(actual, now, injected)
                self._hold_until = now + self.hold_s
                return
            # A wheel turn or a button: the cursor stayed where it was.  Holding here would freeze the
            # pointer for as long as a scroll runs.
            self._last_cmd_t = injected
        elif self._last_cmd is not None and math.hypot(actual[0] - self._last_cmd[0], actual[1] - self._last_cmd[1]) > self.eps_px:
            if self.yield_enabled:
                self._yield_until = now + self.yield_s
                self._yield_pos = actual
                self._last_cmd = None
                self._hold_until = None
                log.info("Physical mouse movement: eye control waits until it stops")
                self._notify(True)
                return
            self._reset_at(actual, now, self._last_cmd_t)

        if self._hold_until is not None:
            if now < self._hold_until:
                self._t = now
                return
            self._hold_until = None  # ease on from the point the action chose: _pos is still there, at rest

        if self._pos is None:
            self._reset_at(actual, now, self._last_cmd_t)
        assert self._pos is not None
        dt = 1.0 / self.rate if self._t is None else min(max(now - self._t, 0.0), 0.05)
        self._t = now
        omega = self.omega_jump if target.fast else self.omega_follow
        nx, vx = _spring(self._pos[0] - target.x, self._vel[0], omega, dt)
        ny, vy = _spring(self._pos[1] - target.y, self._vel[1], omega, dt)
        self._pos = (target.x + nx, target.y + ny)
        self._vel = (vx, vy)
        cmd = (int(round(self._pos[0])), int(round(self._pos[1])))
        if cmd != self._last_cmd:
            if self.input.last_injected() > self._last_cmd_t + 1e-6:
                return  # another action sent input while this step was worked out: look again next tick
            self.input.move_cursor(self._pos[0], self._pos[1])
            self._last_cmd = cmd
            self._last_cmd_t = self.input.last_injected()

    def _cursor(self) -> tuple[int, int]:
        x, y = self.input.cursor_pos()
        return int(x), int(y)

    def _reset_at(self, actual: tuple[int, int], now: float, injected: float) -> None:
        """Continue from the cursor at ``actual``, with input up to ``injected`` accounted for."""
        self._pos = (float(actual[0]), float(actual[1]))
        self._vel = (0.0, 0.0)
        self._t = now
        self._last_cmd = actual
        self._last_cmd_t = injected

    def _notify(self, yielding: bool) -> None:
        if self.on_yield is not None:
            try:
                self.on_yield(yielding)
            except Exception:
                log.exception("yield callback failed")

    # ------------------------------------------------------------------ thread

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sightline-cursor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1.0)
            self._thread = None

    def _run(self) -> None:
        period = 1.0 / self.rate
        while not self._stop.is_set():
            started = self.clock()
            try:
                self.tick(started)
            except Exception:
                log.exception("cursor pump step failed")
            spare = period - (self.clock() - started)
            if spare > 0:
                self._stop.wait(spare)


def find_rest(
    history: list[tuple[float, float, float]], t: float, radius: float, window: float
) -> tuple[float, float] | None:
    """The resting point at or after ``t`` in a (t, x, y) history sorted by time (see ``rest_point``)."""
    if not history or t < history[0][0]:
        return None
    last = history[-1]
    if t >= last[0]:
        return last[1], last[2]
    # Allow half a frame of timing jitter at the start of the window.
    slack = 0.25 * window
    start = 0
    begin = next(i for i in range(len(history) - 1, -1, -1) if history[i][0] <= t)
    for i in range(begin, len(history)):
        ti, xi, yi = history[i]
        while history[start][0] < ti - window - slack:
            start += 1
        if ti - history[start][0] < 0.6 * window:
            continue  # not enough of the window in the history to judge
        if all(math.hypot(x - xi, y - yi) < radius for _, x, y in history[start : i + 1]):
            return xi, yi
    return last[1], last[2]


def _spring(x0: float, v0: float, omega: float, dt: float) -> tuple[float, float]:
    """Exact step of a critically damped spring toward 0: stable at any time step, never overshoots a step."""
    b = v0 + omega * x0
    e = math.exp(-omega * dt)
    x1 = (x0 + b * dt) * e
    v1 = (v0 - omega * b * dt) * e
    if abs(x1) < 0.25 and abs(v1) < 2.0:
        return 0.0, 0.0
    return x1, v1
