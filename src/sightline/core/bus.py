"""A small thread-safe publish/subscribe bus.

``publish`` calls subscribers synchronously, in the publisher's thread.
Subscribers must return quickly (well under a millisecond): anything slow
belongs on the subscriber's own queue or thread.  Qt code subscribes through
``sightline.ui.bridge`` so its callbacks run on the GUI thread.

Subscribing to a base class receives every subclass, so ``subscribe(object, fn)``
sees every event (used by diagnostics and the event recorder).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, TypeVar

log = logging.getLogger(__name__)

E = TypeVar("E")


class Subscription:
    __slots__ = ("_bus", "_event_type", "_callback", "_active")

    def __init__(self, bus: EventBus, event_type: type, callback: Callable[[Any], None]) -> None:
        self._bus = bus
        self._event_type = event_type
        self._callback = callback
        self._active = True

    @property
    def active(self) -> bool:
        return self._active

    def cancel(self) -> None:
        if self._active:
            self._active = False
            self._bus._remove(self)


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[type, list[Subscription]] = {}
        # Per concrete event type, the flattened subscriber tuple.  Rebuilt lazily after changes.
        self._dispatch_cache: dict[type, tuple[Subscription, ...]] = {}

    def subscribe(self, event_type: type[E], callback: Callable[[E], None]) -> Subscription:
        sub = Subscription(self, event_type, callback)
        with self._lock:
            self._subs.setdefault(event_type, []).append(sub)
            self._dispatch_cache.clear()
        return sub

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            lst = self._subs.get(sub._event_type)
            if lst and sub in lst:
                lst.remove(sub)
            self._dispatch_cache.clear()

    def _targets(self, event_type: type) -> tuple[Subscription, ...]:
        cached = self._dispatch_cache.get(event_type)
        if cached is not None:
            return cached
        with self._lock:
            subs: list[Subscription] = []
            for klass in event_type.__mro__:
                subs.extend(self._subs.get(klass, ()))
            result = tuple(subs)
            self._dispatch_cache[event_type] = result
            return result

    def publish(self, event: object) -> None:
        for sub in self._targets(type(event)):
            if not sub._active:
                continue
            try:
                sub._callback(event)
            except Exception:
                log.exception("Subscriber %r failed on %s", sub._callback, type(event).__name__)

    def subscriber_count(self, event_type: type) -> int:
        """How many subscribers would receive this event type (used to skip costly events nobody reads)."""
        return sum(1 for s in self._targets(event_type) if s._active)
