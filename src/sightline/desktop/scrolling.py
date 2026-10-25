"""Continuous scrolling on its own thread, with a gentle speed ramp.

It starts slowly so the user can stop on what they are reading, then
speeds up over a couple of seconds.  Any click, ``mouse.scroll_stop`` or
``key.release_all`` stops it.
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from collections.abc import Callable
from typing import Protocol

log = logging.getLogger(__name__)

DIRECTIONS: dict[str, tuple[float, float]] = {
    "down": (1.0, 0.0),
    "up": (-1.0, 0.0),
    "right": (0.0, 1.0),
    "left": (0.0, -1.0),
}


class _Scrolls(Protocol):
    def scroll(self, dy: float = 0.0, dx: float = 0.0, *, smooth: bool = False) -> None: ...


def ramp_rate(elapsed: float, speed: float, start: float = 2.0, top: float = 8.0, ramp_s: float = 2.5) -> float:
    """Notches per second after ``elapsed`` seconds: eases from ``start`` to ``top``, scaled by ``speed``."""
    progress = min(1.0, max(0.0, elapsed / ramp_s))
    eased = progress * progress * (3.0 - 2.0 * progress)  # smoothstep: no jolt at either end
    return max(0.0, speed) * (start + (top - start) * eased)


class ContinuousScroller:
    def __init__(
        self,
        injector: _Scrolls,
        *,
        smooth: Callable[[], bool] = lambda: True,
        base_speed: Callable[[], float] = lambda: 1.0,
        on_stop: Callable[[], None] | None = None,
        tick_s: float = 1.0 / 40.0,
    ) -> None:
        self._injector = injector
        try:  # the protocol's scroll() has no ``smooth``; only ours does
            self._takes_smooth = "smooth" in inspect.signature(injector.scroll).parameters
        except (TypeError, ValueError):
            self._takes_smooth = False
        self._smooth = smooth
        self._base_speed = base_speed
        self._on_stop = on_stop
        self._tick = tick_s
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._direction = (0.0, 0.0)
        self._speed = 1.0
        self._started = 0.0

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._stop.is_set()

    @property
    def direction(self) -> str | None:
        if not self.running:
            return None
        for name, vector in DIRECTIONS.items():
            if vector == self._direction:
                return name
        return None

    def start(self, direction: str, speed: float = 1.0) -> None:
        if direction not in DIRECTIONS:
            raise ValueError(direction)
        with self._lock:
            vector = DIRECTIONS[direction]
            if vector != self._direction or not self.running:
                self._started = time.perf_counter()  # new direction: ramp again from slow
            self._direction = vector
            self._speed = max(0.1, min(float(speed), 5.0))
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="sightline-scroll", daemon=True)
            self._thread.start()

    def set_speed(self, speed: float) -> None:
        with self._lock:
            self._speed = max(0.1, min(float(speed), 5.0))

    def stop(self) -> bool:
        """Stop scrolling.  Returns True if it was running."""
        thread = self._thread
        was_running = self.running
        self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(0.5)
        self._thread = None
        return was_running

    def _run(self) -> None:
        last = time.perf_counter()
        try:
            while not self._stop.wait(self._tick):
                now = time.perf_counter()
                dt = min(now - last, 0.1)
                last = now
                with self._lock:
                    dy, dx = self._direction
                    rate = ramp_rate(now - self._started, self._speed * self._base_speed())
                notches = rate * dt
                if self._takes_smooth:
                    self._injector.scroll(dy * notches, dx * notches, smooth=self._smooth())
                else:
                    self._injector.scroll(dy * notches, dx * notches)
        except Exception:
            log.exception("Continuous scrolling failed")
        finally:
            if self._on_stop is not None:
                try:
                    self._on_stop()
                except Exception:
                    log.exception("Scroll stop callback failed")
