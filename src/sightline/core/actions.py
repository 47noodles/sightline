"""Routing of Actions to the subsystem that performs them.

Producers (speech, eye gestures, dwell, the UI) call ``registry.dispatch``,
which queues the action and returns at once.  One worker thread runs actions
in order, so a spoken "select all" followed by "copy" can never interleave.

Handlers declare where they run:

* ``"worker"`` (default): on the action thread.  Input injection and UI
  Automation calls belong here.
* ``"ui"``: on the Qt GUI thread, through the invoker the UI installs with
  ``set_ui_invoker``.  The action thread waits for it to finish (up to
  ``ui_timeout``) so ordering still holds.

Interceptors see every action before its handler and may swallow it (return
None) or replace it (return another Action).  The zoom lens, for example,
turns the next click into a click at the magnified position.  An interceptor
that fails is logged and skipped, unless it was added with ``fail_closed``:
a guard such as the countdown before risky actions must never let an action
through because the guard itself broke, so then the action is dropped and the
user is told plainly.

Undo: a handler may register an ``undo`` function that, given the action and
the handler's return value, returns the Action that reverses it.  Every
action the user caused (voice, eyes, dwell, UI) that ran from the queue is
remembered, so "undo that" (``action.undo``) reverses the last one, or says
plainly that it cannot.  Register with ``history=False`` for actions that
should not count as "the last thing done" (showing an overlay, pausing).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .bus import EventBus
from .events import ActionExecuted
from .types import Action

log = logging.getLogger(__name__)

Handler = Callable[[Action], Any]
Interceptor = Callable[[Action], "Action | None"]
UiInvoker = Callable[[Callable[[], None]], None]
HandlerThread = Literal["worker", "ui"]
UndoFn = Callable[[Action, Any], "Action | None"]  # (action that ran, handler's return value) -> its reverse

_UNDO_SOURCES = frozenset({"voice", "eye", "dwell", "ui", "keyboard"})
_UNDO_DEPTH = 20
GUARD_FAILED_MESSAGE = "Sightline stopped that because a safety check failed. Please try again."


class ActionError(Exception):
    """Raised by a handler for an expected failure.  The message is shown to the user as is."""


@dataclass(slots=True)
class _Spec:
    name: str
    handler: Handler
    thread: HandlerThread
    description: str
    undo: UndoFn | None = None
    history: bool = True


@dataclass(slots=True)
class _Done:
    action: Action
    inverse: Action | None
    label: str  # what to call it in "can't undo" messages


@dataclass(slots=True)
class _Interceptor:
    fn: Interceptor
    priority: int
    owner: str
    fail_closed: bool = False  # when it raises, drop the action instead of letting it through


_STOP = object()


class ActionRegistry:
    def __init__(self, bus: EventBus, *, ui_timeout: float = 5.0) -> None:
        self._bus = bus
        self._specs: dict[str, _Spec] = {}
        self._interceptors: list[_Interceptor] = []
        self._lock = threading.Lock()
        self._queue: queue.Queue[object] = queue.Queue()
        self._ui_invoker: UiInvoker | None = None
        self._ui_timeout = ui_timeout
        self._thread: threading.Thread | None = None
        self._worker_ident: int | None = None
        self._done: list[_Done] = []
        self.register("action.undo", self._undo, description="Undo the last action", history=False)

    # ------------------------------------------------------------------ registration

    def register(
        self,
        name: str,
        handler: Handler,
        *,
        thread: HandlerThread = "worker",
        description: str = "",
        replace: bool = False,
        undo: UndoFn | None = None,
        history: bool = True,
    ) -> None:
        with self._lock:
            if name in self._specs and not replace:
                raise ValueError(f"action {name!r} is already registered")
            self._specs[name] = _Spec(name, handler, thread, description, undo, history)

    def unregister(self, name: str) -> None:
        with self._lock:
            self._specs.pop(name, None)

    def has(self, name: str) -> bool:
        return name in self._specs

    def names(self) -> list[str]:
        return sorted(self._specs)

    def describe(self) -> dict[str, str]:
        return {name: spec.description for name, spec in sorted(self._specs.items())}

    def add_interceptor(self, fn: Interceptor, *, priority: int = 0, owner: str = "", fail_closed: bool = False) -> Callable[[], None]:
        """Install an interceptor.  Higher priority runs first.  Returns a function that removes it.

        With ``fail_closed`` an exception in ``fn`` drops the action with a plain error
        (``GUARD_FAILED_MESSAGE``) instead of passing it on unchanged: for guards.
        """
        entry = _Interceptor(fn, priority, owner, fail_closed)
        with self._lock:
            self._interceptors.append(entry)
            self._interceptors.sort(key=lambda i: -i.priority)

        def remove() -> None:
            with self._lock:
                if entry in self._interceptors:
                    self._interceptors.remove(entry)

        return remove

    def set_ui_invoker(self, invoker: UiInvoker | None) -> None:
        """Install the function that runs a callable on the GUI thread (set by the UI at startup)."""
        self._ui_invoker = invoker

    # ------------------------------------------------------------------ running

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="sightline-actions", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        if self._thread is None:
            return
        self._queue.put(_STOP)
        self._thread.join(timeout)
        self._thread = None

    def dispatch(self, action: Action) -> None:
        """Queue an action.  Never blocks.  Safe from any thread."""
        self._queue.put(action)

    def execute_now(self, action: Action) -> Any:
        """Run an action synchronously in the calling thread, interceptors included.

        For handlers that chain other actions (the grid's "click" moves then
        clicks) and for tests.  Returns the handler's return value.  Raises
        KeyError for an unknown action and re-raises handler errors.
        """
        final = self._intercept(action)
        if final is None:
            return None
        return self._invoke(final)

    def on_worker_thread(self) -> bool:
        return threading.get_ident() == self._worker_ident

    def _run(self) -> None:
        self._worker_ident = threading.get_ident()
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            assert isinstance(item, Action)
            self._execute(item)

    def _execute(self, action: Action) -> None:
        started = time.perf_counter()
        try:
            final = self._intercept(action)
            if final is None:
                return
            result = self._invoke(final)
            self._remember(final, result)
        except ActionError as e:
            self._bus.publish(ActionExecuted(action, False, str(e), _ms_since(started)))
            return
        except KeyError as e:
            log.warning("Unknown action %s", action.describe())
            self._bus.publish(ActionExecuted(action, False, f"unknown action {e}", _ms_since(started)))
            return
        except Exception as e:
            log.exception("Action %s failed", action.describe())
            self._bus.publish(ActionExecuted(action, False, f"{type(e).__name__}: {e}", _ms_since(started)))
            return
        self._bus.publish(ActionExecuted(final, True, None, _ms_since(started)))

    def _intercept(self, action: Action) -> Action | None:
        with self._lock:
            chain = list(self._interceptors)
        current: Action | None = action
        for entry in chain:
            try:
                current = entry.fn(current)
            except Exception:
                log.exception("Interceptor %s failed on %s", entry.owner or entry.fn, action.describe())
                if entry.fail_closed:
                    # A guard that broke must not let the action it guards slip through.
                    raise ActionError(GUARD_FAILED_MESSAGE) from None
            if current is None:
                log.debug("Action %s consumed by interceptor %s", action.describe(), entry.owner)
                return None
        return current

    def _invoke(self, action: Action) -> Any:
        spec = self._specs.get(action.name)
        if spec is None:
            raise KeyError(action.name)
        if spec.thread == "ui":
            if self._ui_invoker is not None:
                return self._invoke_on_ui(spec, action)
            if threading.current_thread() is not threading.main_thread():
                # Qt widgets must never be touched from another thread; without an invoker there is no safe route.
                raise ActionError("The Sightline window isn't ready yet.")
        return spec.handler(action)

    def _invoke_on_ui(self, spec: _Spec, action: Action) -> Any:
        assert self._ui_invoker is not None
        done = threading.Event()
        box: dict[str, Any] = {}

        def call() -> None:
            try:
                box["result"] = spec.handler(action)
            except BaseException as e:  # carried back to the action thread
                box["error"] = e
            finally:
                done.set()

        self._ui_invoker(call)
        if not done.wait(self._ui_timeout):
            raise ActionError(f"{action.name} did not finish on the UI thread in time")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    # ------------------------------------------------------------------ undo

    def _remember(self, action: Action, result: Any) -> None:
        spec = self._specs.get(action.name)
        if spec is None or not spec.history or action.source not in _UNDO_SOURCES:
            return
        inverse = None
        if spec.undo is not None:
            try:
                inverse = spec.undo(action, result)
            except Exception:
                log.exception("Could not prepare the undo for %s", action.describe())
        label = action.utterance or spec.description or action.name
        with self._lock:
            self._done.append(_Done(action, inverse, label))
            del self._done[:-_UNDO_DEPTH]

    def _undo(self, action: Action) -> None:
        with self._lock:
            done = self._done.pop() if self._done else None
        if done is None:
            raise ActionError("There is nothing to undo.")
        if done.inverse is None:
            raise ActionError(f"Sightline can't undo “{done.label}”.")
        self.execute_now(done.inverse)


def _ms_since(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0
