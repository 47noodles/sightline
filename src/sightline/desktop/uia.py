"""UI Automation: focus, clickable elements, hover target, text before the caret, invoking.

Threads.  Callers never touch COM.  Two desktop-owned threads, both in the
multithreaded apartment with their own ``IUIAutomation``, do all UI
Automation work from request queues, and callers wait with a timeout:

* ``sightline-uia`` answers quick questions (focus, element at a point, text
  before the caret, invoke) and runs the focus poll and the hover loop.
* ``sightline-uia-scan`` runs the slow clickable scans (20-300 ms per
  window, more on a large web page), so a scan never delays the hover
  highlight or speech's question about the caret.

``ElementInfo.handle`` is an ``ElementRef``: an ID, never a COM pointer, so
COM objects stay on the thread that made them and are released there.

Speed.  One ``FindAllBuildCache`` call with a condition (enabled, on screen,
and either a clickable control type or an Invoke, Toggle, SelectionItem or
ExpandCollapse pattern) fetches every candidate and its properties in one
cross-process round trip.  The idea (enabled, on-screen elements with
Invoke, Toggle, SelectionItem or ExpandCollapse, invoked in that order)
follows hunt-and-peck (github.com/zsims/hunt-and-peck), a Vimium-style UIA
tool; it publishes no licence, so no code was taken from it.

Chromium (Chrome, Edge, Electron apps) only builds the web-content tree
after a UI Automation client asks a ``Chrome_RenderWidgetHostHWND`` for its
root.  ``ElementFromHandle`` on each render widget does exactly that; on this
machine it took the Claude desktop app from 3 to 183 clickables.  The first
scan after priming can be thin, so a follow-up scan is queued.
"""

from __future__ import annotations

import ctypes
import gc
import itertools
import logging
import queue
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from sightline.core.types import ElementInfo, FocusInfo, Rect

from . import win32 as w
from .matching import match_elements
from .spatial import ClickableSnapshot, SpatialIndex, Target, dedupe, element_root, nearest_first, resolve_target
from .textedit import Span, TextEditResult, TextQuery, locate, same_text, walk_plan
from .windows import ProcessNames, popup_windows_of

log = logging.getLogger(__name__)

# UIA control type IDs and the names ElementInfo uses.
CONTROL_TYPES: dict[int, str] = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox", 50004: "Edit",
    50005: "Hyperlink", 50006: "Image", 50007: "ListItem", 50008: "List", 50009: "Menu",
    50010: "MenuBar", 50011: "MenuItem", 50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab", 50019: "TabItem",
    50020: "Text", 50021: "ToolBar", 50022: "ToolTip", 50023: "Tree", 50024: "TreeItem",
    50025: "Custom", 50026: "Group", 50027: "Thumb", 50028: "DataGrid", 50029: "DataItem",
    50030: "Document", 50031: "SplitButton", 50032: "Window", 50033: "Pane", 50034: "Header",
    50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator", 50039: "SemanticZoom",
    50040: "AppBar",
}  # fmt: skip
_TYPE_ID = {name: tid for tid, name in CONTROL_TYPES.items()}

CLICKABLE_TYPES = (
    "Button", "Hyperlink", "MenuItem", "ListItem", "TabItem", "TreeItem", "CheckBox", "RadioButton",
    "ComboBox", "SplitButton", "Edit", "DataItem", "HeaderItem", "Document", "Slider",
)  # fmt: skip
# Types that are targets in their own right; an invokable Text or Image inside one of them is a duplicate.
STRONG_TYPES = frozenset(
    {"Button", "Hyperlink", "MenuItem", "ListItem", "TabItem", "TreeItem", "CheckBox", "RadioButton", "SplitButton"}
)
WEAK_TYPES = frozenset({"Text", "Image", "Group", "Custom", "Pane"})
# Parts and containers that carry patterns but are not something a person means to click.
NEVER_TARGETS = frozenset(
    {"Thumb", "ScrollBar", "Separator", "TitleBar", "Window", "StatusBar", "ToolBar", "Tab", "List", "Tree",
     "Table", "DataGrid", "Header", "Menu", "MenuBar", "ProgressBar", "ToolTip", "AppBar", "SemanticZoom"}
)  # fmt: skip
TEXT_TYPES = frozenset({"Edit", "Document"})

# Window classes of editors that use a Win32 caret, so a focused Document there is a text field.
_CHROMIUM_CLASS_PREFIX = "Chrome_WidgetWin_"
_CHROMIUM_RENDER_CLASS = "Chrome_RenderWidgetHostHWND"

# Property IDs (uiautomationclient.h).
P_RUNTIME_ID = 30000
P_BOUNDING_RECT = 30001
P_PROCESS_ID = 30002
P_CONTROL_TYPE = 30003
P_HAS_KEYBOARD_FOCUS = 30008
P_IS_KEYBOARD_FOCUSABLE = 30009
P_IS_ENABLED = 30010
P_AUTOMATION_ID = 30011
P_CLASS_NAME = 30012
P_NAME = 30005
P_IS_PASSWORD = 30019
P_NATIVE_HWND = 30020
P_IS_OFFSCREEN = 30022
P_IS_EXPAND_COLLAPSE_AVAILABLE = 30028
P_IS_INVOKE_AVAILABLE = 30031
P_IS_SELECTION_ITEM_AVAILABLE = 30036
P_IS_TEXT_AVAILABLE = 30040
P_IS_TOGGLE_AVAILABLE = 30041
P_IS_VALUE_AVAILABLE = 30043
P_VALUE_IS_READ_ONLY = 30046
P_IS_TEXT2_AVAILABLE = 30119

# Pattern IDs.
PAT_INVOKE = 10000
PAT_VALUE = 10002
PAT_EXPAND_COLLAPSE = 10005
PAT_SELECTION_ITEM = 10010
PAT_TEXT = 10014
PAT_TOGGLE = 10015
PAT_LEGACY = 10018
PAT_TEXT2 = 10024

TREE_SCOPE_ELEMENT = 1
TREE_SCOPE_DESCENDANTS = 4
TEXT_UNIT_CHARACTER = 0
ENDPOINT_START = 0
ENDPOINT_END = 1
TEXT_WINDOW = 3000  # characters read each side of the caret when looking for a phrase


@dataclass(frozen=True, slots=True)
class ElementRef:
    """What ``ElementInfo.handle`` holds: which thread owns the COM element, and how to find it there."""

    owner: str
    key: tuple[int, int]
    root: int = 0


@dataclass
class UIAConfig:
    """Live settings, read through callables so a settings change applies on the next use."""

    snap_enabled: Callable[[], bool] = lambda: True
    snap_radius: Callable[[], float] = lambda: 48.0
    highlight: Callable[[], bool] = lambda: True
    element_cap: int = 2500
    scan_budget_s: float = 1.5
    focus_poll_s: float = 0.2
    hover_poll_s: float = 0.1
    rescan_while_moving_s: float = 1.5
    taskbar_rescan_s: float = 5.0


# =========================================================================== COM client (one per thread)


_CLIENT_LOCK = threading.Lock()


def create_client(attempts: int = 3) -> UIAClient:
    """Build a UIAClient.  Two threads creating their first clients at the same moment made one of
    them fail with E_FAIL on this machine, so creation is serialised and retried."""
    last: Exception | None = None
    for attempt in range(attempts):
        with _CLIENT_LOCK:
            try:
                return UIAClient()
            except Exception as e:
                last = e
                log.debug("UI Automation client creation failed (attempt %d): %s", attempt + 1, e)
        time.sleep(0.05 * (attempt + 1))
    assert last is not None
    raise last


class UIAClient:
    """IUIAutomation plus prebuilt conditions and cache requests.  Use only on the creating thread."""

    def __init__(self) -> None:
        import comtypes.client

        mod = comtypes.client.GetModule("UIAutomationCore.dll")
        self.mod = mod
        try:
            self.uia = comtypes.client.CreateObject(mod.CUIAutomation8._reg_clsid_, interface=mod.IUIAutomation2)
            # A hung app must not hang the thread for the default 20 s.
            self.uia.ConnectionTimeout = 1500
            self.uia.TransactionTimeout = 3000
        except Exception:  # pragma: no cover - Windows 7
            self.uia = comtypes.client.CreateObject(mod.CUIAutomation._reg_clsid_, interface=mod.IUIAutomation)
        uia = self.uia

        def cache(props: list[int]) -> Any:
            request = uia.CreateCacheRequest()
            for prop in props:
                request.AddProperty(prop)
            return request

        base = [P_BOUNDING_RECT, P_NAME, P_CONTROL_TYPE, P_AUTOMATION_ID, P_PROCESS_ID, P_IS_ENABLED]
        self.clickable_cache = cache(base + [P_IS_VALUE_AVAILABLE, P_VALUE_IS_READ_ONLY, P_IS_PASSWORD])
        self.point_cache = cache(
            base + [P_IS_VALUE_AVAILABLE, P_VALUE_IS_READ_ONLY, P_IS_PASSWORD, P_IS_INVOKE_AVAILABLE,
                    P_IS_TOGGLE_AVAILABLE, P_IS_SELECTION_ITEM_AVAILABLE, P_IS_EXPAND_COLLAPSE_AVAILABLE]
        )  # fmt: skip
        self.focus_cache = cache(
            base + [P_IS_PASSWORD, P_IS_VALUE_AVAILABLE, P_VALUE_IS_READ_ONLY, P_IS_TEXT_AVAILABLE,
                    P_IS_TEXT2_AVAILABLE, P_HAS_KEYBOARD_FOCUS, P_IS_KEYBOARD_FOCUSABLE, P_CLASS_NAME, P_NATIVE_HWND]
        )  # fmt: skip

        type_conditions = [uia.CreatePropertyCondition(P_CONTROL_TYPE, _TYPE_ID[t]) for t in CLICKABLE_TYPES]
        pattern_conditions = [
            uia.CreatePropertyCondition(p, True)
            for p in (P_IS_INVOKE_AVAILABLE, P_IS_TOGGLE_AVAILABLE, P_IS_SELECTION_ITEM_AVAILABLE, P_IS_EXPAND_COLLAPSE_AVAILABLE)
        ]
        any_clickable = type_conditions[0]
        for condition in type_conditions[1:] + pattern_conditions:
            any_clickable = uia.CreateOrCondition(any_clickable, condition)
        visible = uia.CreateAndCondition(
            uia.CreatePropertyCondition(P_IS_OFFSCREEN, False), uia.CreatePropertyCondition(P_IS_ENABLED, True)
        )
        self.clickable_condition = uia.CreateAndCondition(visible, any_clickable)
        self.walker = uia.ControlViewWalker

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def cached(element: Any, prop: int, default: Any = None) -> Any:
        try:
            value = element.GetCachedPropertyValue(prop)
        except Exception:
            return default
        return default if value is None else value

    def element_from_handle(self, hwnd: int) -> Any | None:
        try:
            return self.uia.ElementFromHandle(hwnd)
        except Exception:
            return None

    def pattern(self, element: Any, pattern_id: int, interface: Any) -> Any | None:
        try:
            unknown = element.GetCurrentPattern(pattern_id)
        except Exception:
            return None
        if not unknown:
            return None
        try:
            return unknown.QueryInterface(interface)
        except Exception:
            return None


def _clean_name(raw: Any) -> str:
    if not raw:
        return ""
    text = " ".join(str(raw).split())
    return text[:200]


def _rect_of(element: Any) -> Rect:
    r = element.CachedBoundingRectangle
    return Rect.from_ltrb(r.left, r.top, r.right, r.bottom)


def _is_editable(control_type: str, value_available: bool, read_only: bool, text_available: bool = False) -> bool:
    if control_type == "Edit":
        return not (value_available and read_only)
    if control_type == "Document":
        return value_available and not read_only
    if control_type == "ComboBox":
        return value_available and not read_only
    return False


# =========================================================================== worker threads


_STOP = object()
_WAKE = object()  # makes a worker re-read its schedule at once


class _Request:
    __slots__ = ("fn", "done", "result", "error", "cancelled")

    def __init__(self, fn: Callable[[UIAClient], Any]) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None
        self.cancelled = False


class ComWorker:
    """A thread in the MTA that owns a UIAClient and runs queued requests, plus periodic work."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._thread: threading.Thread | None = None
        self._ident = 0
        self.client: UIAClient | None = None
        self._ready = threading.Event()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def on_thread(self) -> bool:
        return threading.get_ident() == self._ident

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def wait_ready(self, timeout: float) -> bool:
        return self._ready.wait(timeout)

    def stop(self, timeout: float = 3.0) -> None:
        if self._thread is None:
            return
        self._running = False
        self._queue.put(_Wrapped(-1, next(self._seq), _STOP))
        self._thread.join(timeout)
        if self._thread.is_alive():
            log.warning("%s did not stop in time (a UI Automation call is still running)", self.name)
        self._thread = None

    def call(self, fn: Callable[[UIAClient], Any], timeout: float, default: Any = None, priority: int = 5) -> Any:
        """Run ``fn(client)`` on this thread; give up after ``timeout`` seconds and return ``default``."""
        if self.on_thread():
            return fn(self.client) if self.client is not None else default
        if not self._running:
            return default
        request = _Request(fn)
        self._queue.put(_Wrapped(priority, next(self._seq), request))
        if not request.done.wait(max(0.0, timeout)):
            request.cancelled = True
            return default
        if request.error is not None:
            log.debug("%s request failed: %s", self.name, request.error)
            return default
        return request.result

    def post(self, fn: Callable[[UIAClient], Any], priority: int = 5) -> None:
        """Queue work without waiting for it."""
        if self._running:
            self._queue.put(_Wrapped(priority, next(self._seq), _Request(fn)))

    def wake(self) -> None:
        """Interrupt the wait for the next request so a new schedule takes effect now."""
        if self._running:
            self._queue.put(_Wrapped(9, next(self._seq), _WAKE))

    # ------------------------------------------------------------------ subclass hooks

    def next_wait(self) -> float:
        return 0.25

    def tick(self) -> None:
        """Periodic work, run between requests."""

    def setup(self) -> None:
        """Runs on the thread after COM and the client are ready."""

    def teardown(self) -> None:
        """Runs on the thread before COM shuts down: drop every COM reference here."""

    # ------------------------------------------------------------------ loop

    def _run(self) -> None:
        import comtypes

        self._ident = threading.get_ident()
        w.set_thread_dpi_awareness()
        initialized = False
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            initialized = True
        except OSError as e:  # the thread was already initialised differently; UIA still works there
            log.warning("%s: COM already initialised on this thread (%s)", self.name, e)
        try:
            try:
                self.client = create_client()
                self.setup()
            except Exception:
                log.exception("%s could not start UI Automation", self.name)
                self.client = None
            self._ready.set()
            while True:
                try:
                    item = self._queue.get(timeout=max(0.005, self.next_wait()))
                except queue.Empty:
                    item = None
                if item is not None and item.payload is not _WAKE:
                    if item.payload is _STOP:
                        break
                    request: _Request = item.payload
                    if not request.cancelled:
                        try:
                            request.result = request.fn(self.client) if self.client is not None else None
                        except Exception as e:
                            request.error = e
                        finally:
                            request.done.set()
                if self.client is not None and self._running:
                    try:
                        self.tick()
                    except Exception:
                        log.exception("%s periodic work failed", self.name)
        finally:
            self._running = False
            try:
                self.teardown()
            except Exception:
                log.exception("%s teardown failed", self.name)
            self.client = None
            # Pending callers get their default instead of waiting out their timeout.
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if isinstance(item.payload, _Request):
                    item.payload.done.set()
            gc.collect()  # release COM wrappers while COM is still initialised on this thread
            if initialized:
                comtypes.CoUninitialize()
            self._ready.set()


@dataclass(order=True)
class _Wrapped:
    priority: int
    seq: int
    payload: Any = field(compare=False)


# =========================================================================== the scanner


class ScanWorker(ComWorker):
    """Builds ClickableSnapshots for the foreground window, its popups, the taskbar and the hovered window."""

    def __init__(self, owner: UIAProvider) -> None:
        super().__init__("sightline-uia-scan")
        self._owner = owner
        self._due: float | None = None
        self._force_roots: set[int] = set()
        self._lock = threading.Lock()
        self._generation = 0
        self._elements: deque[tuple[int, Any, list[Any]]] = deque(maxlen=3)  # (generation, array, list)
        self._primed: dict[int, float] = {}  # Chromium render widgets already asked for their tree
        self._taskbar: tuple[float, list[ElementInfo], list[Any], int] | None = None
        self._extra_requested: dict[int, float] = {}
        self._followups: dict[int, int] = {}  # root -> follow-up scans left after priming
        self.last_scan_ms: dict[str, float] = {}

    def request(self, delay: float = 0.0, extra_root: int = 0) -> None:
        due = time.perf_counter() + max(0.0, delay)
        with self._lock:
            earlier = self._due is None or due < self._due
            if earlier:
                self._due = due
            if extra_root:
                self._force_roots.add(extra_root)
        if earlier and not self.on_thread():
            self.wake()

    def next_wait(self) -> float:
        with self._lock:
            due = self._due
        if due is None:
            return 0.25
        return max(0.0, due - time.perf_counter())

    def tick(self) -> None:
        with self._lock:
            due = self._due
            if due is None or time.perf_counter() < due:
                return
            self._due = None
            extra = set(self._force_roots)
            self._force_roots.clear()
        self.scan_now(extra)

    def teardown(self) -> None:
        self._elements.clear()
        self._taskbar = None

    # ------------------------------------------------------------------ scanning

    def element(self, key: tuple[int, int]) -> Any | None:
        generation, index = key
        taskbar = self._taskbar
        if taskbar is not None and taskbar[3] == generation:
            return taskbar[2][index] if 0 <= index < len(taskbar[2]) else None
        for gen, _array, items in self._elements:
            if gen == generation and 0 <= index < len(items):
                return items[index]
        return None

    def scan_now(self, extra_roots: set[int] | None = None) -> ClickableSnapshot | None:
        client = self.client
        if client is None:
            return None
        owner = self._owner
        started = time.perf_counter()
        own_pid = owner.own_pid
        fg = w.GetForegroundWindow()
        roots: list[int] = []
        if fg and w.window_pid(fg) != own_pid and not w.IsHungAppWindow(fg):
            roots.append(int(fg))
            roots.extend(popup_windows_of(fg, own_pid))
        for extra in extra_roots or ():
            if extra and extra not in roots and w.IsWindow(extra) and w.window_pid(extra) != own_pid:
                roots.append(int(extra))
        cursor = w.cursor_pos()
        hover_root = w.window_at(cursor[0], cursor[1], own_pid)
        if hover_root and hover_root not in roots and w.window_pid(hover_root) != own_pid and not _is_taskbar(hover_root):
            roots.append(int(hover_root))

        self._generation += 1
        generation = self._generation
        com_items: list[Any] = []
        infos: list[ElementInfo] = []
        arrays: list[Any] = []
        covered: set[int] = set()
        for root in roots:
            if time.perf_counter() - started > owner.config.scan_budget_s:
                log.debug("Clickable scan budget used up; skipped window %s", root)
                break
            t0 = time.perf_counter()
            got = self._scan_root(client, root, generation, com_items, infos, arrays)
            ms = (time.perf_counter() - t0) * 1000
            if got >= 0:
                covered.add(root)
                self.last_scan_ms[w.class_name(root)] = ms
                log.debug("Scanned %s (%s): %d clickables in %.0f ms", root, w.class_name(root), got, ms)

        taskbar_infos = self._taskbar_elements(client)
        for tb in _taskbars():
            covered.add(tb)
        all_infos = _drop_inner_duplicates(dedupe(infos)) + taskbar_infos
        all_infos = nearest_first(all_infos, cursor[0], cursor[1], owner.config.element_cap)
        self._elements.append((generation, arrays, com_items))
        snapshot = ClickableSnapshot(
            elements=tuple(all_infos),
            roots=frozenset(covered),
            foreground=int(fg or 0),
            t=time.perf_counter(),
            generation=generation,
        )
        owner._set_snapshot(snapshot)
        self.last_scan_ms["total"] = (time.perf_counter() - started) * 1000
        return snapshot

    def _scan_root(self, client: UIAClient, root: int, generation: int, com_items: list, infos: list, arrays: list) -> int:
        self._prime_chromium(client, root)
        element = client.element_from_handle(root)
        if element is None:
            return -1
        try:
            found = element.FindAllBuildCache(TREE_SCOPE_DESCENDANTS, client.clickable_condition, client.clickable_cache)
        except Exception as e:
            log.debug("FindAll failed on %s: %s", root, e)
            return -1
        if found is None:
            return 0
        arrays.append(found)
        frame = w.frame_rect(root)
        bounds = Rect.from_ltrb(*frame) if frame else None
        count = found.Length
        added = 0
        window_area = bounds.area if bounds else 0.0
        process_names = self._owner.process_names
        own_pid = self._owner.own_pid
        for i in range(count):
            try:
                el = found.GetElement(i)
                rect = _rect_of(el)
                if bounds is not None:
                    rect = rect.intersection(bounds)
                if rect.w < 3 or rect.h < 3:
                    continue
                type_name = CONTROL_TYPES.get(el.CachedControlType, "Custom")
                if type_name in NEVER_TARGETS:
                    continue
                editable = False
                if type_name in TEXT_TYPES or type_name == "ComboBox":
                    value_available = bool(client.cached(el, P_IS_VALUE_AVAILABLE, False))
                    read_only = bool(client.cached(el, P_VALUE_IS_READ_ONLY, False))
                    editable = _is_editable(type_name, value_available, read_only)
                    if type_name == "Document" and not editable:
                        continue  # a web page or a read-only viewer: clicking inside it is not a target
                elif window_area and rect.area > 0.5 * window_area and type_name not in STRONG_TYPES:
                    continue  # an invokable pane covering the window would swallow every click
                cx, cy = rect.center
                at = w.window_at(cx, cy)
                # Covered by another app's window.  Sightline's own windows never count: its overlays
                # sit over everything, and the numbered labels need the elements underneath.
                if at and at != root and w.window_pid(at) != own_pid and not _same_app_popup(at, root):
                    continue
                pid = int(el.CachedProcessId or 0)
                if pid == own_pid:
                    continue
                index = len(com_items)
                com_items.append(el)
                infos.append(
                    ElementInfo(
                        rect=rect,
                        name=_clean_name(el.CachedName),
                        control_type=type_name,
                        automation_id=str(el.CachedAutomationId or ""),
                        clickable=True,
                        editable=editable,
                        process=process_names.name(pid),
                        handle=ElementRef("scan", (generation, index), root),
                    )
                )
                added += 1
            except Exception:
                continue
        left = self._followups.get(root, 0)
        if left > 0:
            # Chromium fills its web-content tree asynchronously after it is first asked: look again twice.
            self._followups[root] = left - 1
            self.request(0.4 if left == 2 else 1.0)
        elif root in self._followups:
            del self._followups[root]
        return added

    def _prime_chromium(self, client: UIAClient, root: int) -> bool:
        if not w.class_name(root).startswith(_CHROMIUM_CLASS_PREFIX):
            return False
        renderers: list[int] = []

        def visit(child, _lp) -> bool:
            if w.class_name(child) == _CHROMIUM_RENDER_CLASS and w.IsWindowVisible(child):
                renderers.append(int(child))
            return True

        w.EnumChildWindows(root, w.WNDENUMPROC(visit), 0)
        now = time.monotonic()
        primed = False
        for hwnd in renderers:
            last = self._primed.get(hwnd)
            # Chrome turns accessibility off again after a while without clients; re-ask every minute.
            if last is None or now - last > 60.0:
                element = client.element_from_handle(hwnd)
                del element
                self._primed[hwnd] = now
                primed = primed or last is None
        if len(self._primed) > 64:
            self._primed = {h: t for h, t in self._primed.items() if w.IsWindow(h)}
        if primed:
            self._followups[root] = 2
        return primed

    def request_root(self, root: int) -> None:
        """Scan a window the pointer is over, at most every two seconds per window."""
        now = time.perf_counter()
        if now - self._extra_requested.get(root, 0.0) < 2.0:
            return
        if len(self._extra_requested) > 64:
            self._extra_requested.clear()
        self._extra_requested[root] = now
        self.request(0.05, extra_root=root)

    def _taskbar_elements(self, client: UIAClient) -> list[ElementInfo]:
        now = time.perf_counter()
        cached = self._taskbar
        if cached is not None and now - cached[0] < self._owner.config.taskbar_rescan_s:
            return cached[1]
        self._generation += 1
        generation = self._generation
        infos: list[ElementInfo] = []
        items: list[Any] = []
        for tb in _taskbars():
            element = client.element_from_handle(tb)
            if element is None:
                continue
            try:
                found = element.FindAllBuildCache(TREE_SCOPE_DESCENDANTS, client.clickable_condition, client.clickable_cache)
            except Exception:
                continue
            frame = w.frame_rect(tb)
            bounds = Rect.from_ltrb(*frame) if frame else None
            for i in range(found.Length):
                try:
                    el = found.GetElement(i)
                    rect = _rect_of(el)
                    if bounds is not None:
                        rect = rect.intersection(bounds)
                    if rect.w < 3 or rect.h < 3:
                        continue
                    items.append(el)
                    infos.append(
                        ElementInfo(
                            rect=rect,
                            name=_clean_name(el.CachedName),
                            control_type=CONTROL_TYPES.get(el.CachedControlType, "Custom"),
                            automation_id=str(el.CachedAutomationId or ""),
                            clickable=True,
                            process="explorer.exe",
                            handle=ElementRef("scan", (generation, len(items) - 1), int(tb)),
                        )
                    )
                except Exception:
                    continue
        self._taskbar = (now, infos, items, generation)
        return infos


def _taskbars() -> list[int]:
    found: list[int] = []
    main = w.FindWindowW("Shell_TrayWnd", None)
    if main:
        found.append(int(main))
    other = 0
    while True:
        other = w.FindWindowExW(None, other, "Shell_SecondaryTrayWnd", None)
        if not other:
            break
        found.append(int(other))
    return found


def _is_taskbar(hwnd: int) -> bool:
    return w.class_name(hwnd) in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd")


def _same_app_popup(hwnd: int, root: int) -> bool:
    """A popup of the same process over the element (a tooltip, a menu) does not hide it from clicks for long."""
    return w.window_pid(hwnd) == w.window_pid(root) and bool(w.GetWindowLongPtrW(hwnd, w.GWL_STYLE) & w.WS_POPUP)


def _drop_inner_duplicates(elements: list[ElementInfo]) -> list[ElementInfo]:
    """Drop an invokable Text, Image or Group sitting inside a button or link: it is the same target."""
    strong = [e for e in elements if e.control_type in STRONG_TYPES]
    if not strong:
        return elements
    index = SpatialIndex(strong)
    out: list[ElementInfo] = []
    for el in elements:
        if el.control_type in WEAK_TYPES:
            cx, cy = el.rect.center
            outer = index.containing(cx, cy, lambda o: element_root(o) == element_root(el))
            if outer is not None and outer.rect.area <= 6 * max(el.rect.area, 1.0):
                continue
        out.append(el)
    return out


# =========================================================================== the interactive worker


class InteractiveWorker(ComWorker):
    """Focus polling, hover target, and the quick queries."""

    def __init__(self, owner: UIAProvider) -> None:
        super().__init__("sightline-uia")
        self._owner = owner
        self._focus_due = 0.0
        self._last_hover = 0.0
        self._last_cursor: tuple[int, int] | None = None
        self._last_snapshot_gen = -1
        self._last_live: tuple[float, float, float, int, ElementInfo | None] | None = None  # t, x, y, gen, result
        self._focus_element: Any = None
        self._refs: OrderedDict[int, Any] = OrderedDict()
        self._ref_seq = itertools.count(1)
        self.focus_poll_ms: deque[float] = deque(maxlen=50)

    def next_wait(self) -> float:
        now = time.perf_counter()
        return max(0.0, min(self._focus_due - now, self._last_hover + self._owner.config.hover_poll_s - now, 0.1))

    def focus_soon(self, delay: float = 0.03) -> None:
        due = time.perf_counter() + delay
        if due < self._focus_due:
            self._focus_due = due
            if not self.on_thread():
                self.wake()

    def teardown(self) -> None:
        self._focus_element = None
        self._refs.clear()

    def remember(self, element: Any) -> tuple[int, int]:
        key = next(self._ref_seq)
        self._refs[key] = element
        while len(self._refs) > 128:
            self._refs.popitem(last=False)
        return (0, key)

    def element(self, key: tuple[int, int]) -> Any | None:
        return self._refs.get(key[1])

    def tick(self) -> None:
        now = time.perf_counter()
        if now >= self._focus_due:
            self._focus_due = now + self._owner.config.focus_poll_s
            t0 = time.perf_counter()
            self._poll_focus()
            self.focus_poll_ms.append((time.perf_counter() - t0) * 1000)
        if now - self._last_hover >= self._owner.config.hover_poll_s:
            self._last_hover = now
            self._hover()

    # ------------------------------------------------------------------ focus

    def _poll_focus(self) -> None:
        owner = self._owner
        client = self.client
        fg = w.GetForegroundWindow()
        if not fg:
            return
        pid = w.window_pid(fg)
        title = w.window_text(fg)
        info = w.GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        w.GetGUIThreadInfo(w.window_thread(fg), ctypes.byref(info))
        focus_hwnd = int(info.hwndFocus or fg)
        if pid == owner.own_pid or client is None or w.IsHungAppWindow(fg):
            # Never ask UI Automation about our own windows: Qt answers on the GUI thread and could deadlock.
            self._focus_element = None
            owner._set_focus(FocusInfo(int(focus_hwnd), owner.process_names.name(pid), title, "", "", False, False, time.perf_counter()))
            return
        try:
            element = client.uia.GetFocusedElementBuildCache(client.focus_cache)
        except Exception:
            element = None
        if element is None:
            return
        el_pid = int(client.cached(element, P_PROCESS_ID, 0) or 0)
        if el_pid == owner.own_pid:
            self._focus_element = None
            return
        type_name = CONTROL_TYPES.get(int(client.cached(element, P_CONTROL_TYPE, 0) or 0), "")
        password = bool(client.cached(element, P_IS_PASSWORD, False))
        value_available = bool(client.cached(element, P_IS_VALUE_AVAILABLE, False))
        read_only = bool(client.cached(element, P_VALUE_IS_READ_ONLY, False))
        text_available = bool(client.cached(element, P_IS_TEXT_AVAILABLE, False))
        enabled = bool(client.cached(element, P_IS_ENABLED, True))
        editable = enabled and _is_editable(type_name, value_available, read_only)
        rich_editor_candidate = type_name in ("Document", "Edit", "Custom", "Pane", "Group") and text_available
        if enabled and not editable and not value_available and rich_editor_candidate:
            # Rich editors (Word, Notepad's RichEdit, terminals) offer text but no value at all.  A live
            # caret settles it: the Win32 caret, or an active TextPattern2 caret.  A read-only value wins.
            if info.hwndCaret:
                editable = True
            elif bool(client.cached(element, P_IS_TEXT2_AVAILABLE, False)):
                editable = self._caret_active(element)
        self._focus_element = element
        native = int(client.cached(element, P_NATIVE_HWND, 0) or 0)
        process = owner.process_names.name(el_pid or pid)
        owner._set_focus(
            FocusInfo(
                hwnd=native or focus_hwnd,
                process=process,
                window_title=title,
                control_type=type_name,
                name=_clean_name(client.cached(element, P_NAME, "")),
                editable=editable,
                password=password,
                t=time.perf_counter(),
            )
        )

    def _caret_active(self, element: Any) -> bool:
        client = self.client
        if client is None:
            return False
        tp2 = client.pattern(element, PAT_TEXT2, client.mod.IUIAutomationTextPattern2)
        if tp2 is None:
            return False
        try:
            active, _range = tp2.GetCaretRange()
            return bool(active)
        except Exception:
            return False

    # ------------------------------------------------------------------ hover

    def _hover(self) -> None:
        owner = self._owner
        cursor = w.cursor_pos()
        snapshot = owner.snapshot
        moved = cursor != self._last_cursor
        if not moved and snapshot.generation == self._last_snapshot_gen:
            return
        if moved:
            owner._note_pointer_moved()
        self._last_cursor = cursor
        self._last_snapshot_gen = snapshot.generation
        if not owner.config.highlight():
            owner._set_hover(Target(cursor[0], cursor[1], None, False))
            return
        target = self.target_at(cursor[0], cursor[1], owner.config.snap_enabled(), owner.config.snap_radius(), reuse_live=True)
        owner._set_hover(target)

    def target_at(self, x: float, y: float, snap: bool, radius: float, reuse_live: bool = False) -> Target:
        owner = self._owner
        root = w.window_at(x, y, owner.own_pid)
        if not root or w.window_pid(root) == owner.own_pid:
            return Target(x, y, None, False)  # the click would land on Sightline itself (the dock)
        snapshot = owner.snapshot
        if not snapshot.covers(root) and not _is_taskbar(root):
            owner.scanner.request_root(root)
        live = None
        if not (snapshot.covers(root) and snapshot.containing(x, y, root) is not None):
            live = self._live_clickable(x, y, root, snapshot.generation, reuse_live)
        return resolve_target(x, y, snapshot, root=root, snap=snap, radius=radius, under_point=live)

    def _live_clickable(self, x: float, y: float, root: int, generation: int, reuse: bool) -> ElementInfo | None:
        """ElementFromPoint for content the snapshot misses.  The hover loop reuses a lookup made
        within 0.2 s and 6 px, since an eye pointer moves a little on almost every tick and each lookup
        costs 2-7 ms; a click always looks again."""
        now = time.perf_counter()
        last = self._last_live
        if reuse and last is not None:
            t, lx, ly, gen, result = last
            if now - t < 0.2 and gen == generation and abs(x - lx) < 6 and abs(y - ly) < 6:
                return result if result is None or result.rect.contains(x, y) else None
        result = self._clickable_at(x, y, root)
        self._last_live = (now, x, y, generation, result)
        return result

    # ------------------------------------------------------------------ point queries

    def element_at(self, x: float, y: float) -> ElementInfo | None:
        client = self.client
        owner = self._owner
        if client is None:
            return None
        root = w.window_at(x, y)
        if not root or w.window_pid(root) == owner.own_pid or w.IsHungAppWindow(root):
            return None
        try:
            point = client.mod.tagPOINT(int(x), int(y))
            element = client.uia.ElementFromPointBuildCache(point, client.point_cache)
        except Exception:
            return None
        if element is None:
            return None
        return self._info(element, root)

    def _clickable_at(self, x: float, y: float, root: int) -> ElementInfo | None:
        """The clickable element under a point: the element itself or its nearest clickable ancestor."""
        client = self.client
        if client is None or w.IsHungAppWindow(root):
            return None
        try:
            element = client.uia.ElementFromPointBuildCache(client.mod.tagPOINT(int(x), int(y)), client.point_cache)
        except Exception:
            return None
        window_area = max(1.0, _area(root))
        for _ in range(5):
            if element is None:
                return None
            info = self._info(element, root)
            if info is None:
                return None
            big = info.rect.area > 0.5 * window_area
            if info.control_type in ("Window", "Document") and not info.editable:
                return None
            if big and info.control_type not in STRONG_TYPES and not info.editable:
                return None  # a page-sized invokable group would swallow every click
            if info.clickable and info.control_type not in NEVER_TARGETS:
                return info
            try:
                element = client.walker.GetParentElementBuildCache(element, client.point_cache)
            except Exception:
                return None
        return None

    def _info(self, element: Any, root: int) -> ElementInfo | None:
        client = self.client
        if client is None:
            return None
        try:
            pid = int(client.cached(element, P_PROCESS_ID, 0) or 0)
            if pid == self._owner.own_pid:
                return None
            type_name = CONTROL_TYPES.get(int(client.cached(element, P_CONTROL_TYPE, 0) or 0), "Custom")
            value_available = bool(client.cached(element, P_IS_VALUE_AVAILABLE, False))
            read_only = bool(client.cached(element, P_VALUE_IS_READ_ONLY, False))
            editable = _is_editable(type_name, value_available, read_only)
            patterns = any(
                bool(client.cached(element, p, False))
                for p in (P_IS_INVOKE_AVAILABLE, P_IS_TOGGLE_AVAILABLE, P_IS_SELECTION_ITEM_AVAILABLE, P_IS_EXPAND_COLLAPSE_AVAILABLE)
            )
            enabled = bool(client.cached(element, P_IS_ENABLED, True))
            clickable = enabled and (type_name in CLICKABLE_TYPES or patterns)
            if type_name == "Document" and not editable:
                clickable = False
            return ElementInfo(
                rect=_rect_of(element),
                name=_clean_name(client.cached(element, P_NAME, "")),
                control_type=type_name,
                automation_id=str(client.cached(element, P_AUTOMATION_ID, "") or ""),
                clickable=clickable,
                editable=editable,
                process=self._owner.process_names.name(pid),
                handle=ElementRef("interactive", self.remember(element), root),
            )
        except Exception:
            return None

    # ------------------------------------------------------------------ text

    def text_before_caret(self, max_chars: int) -> str | None:
        client = self.client
        owner = self._owner
        if client is None:
            return None
        fg = w.GetForegroundWindow()
        if not fg or w.window_pid(fg) == owner.own_pid or w.IsHungAppWindow(fg):
            return None
        try:
            element = client.uia.GetFocusedElementBuildCache(client.focus_cache)
        except Exception:
            return None
        if element is None or bool(client.cached(element, P_IS_PASSWORD, False)):
            return None
        if int(client.cached(element, P_PROCESS_ID, 0) or 0) == owner.own_pid:
            return None
        mod = client.mod
        caret = None
        if bool(client.cached(element, P_IS_TEXT2_AVAILABLE, False)):
            tp2 = client.pattern(element, PAT_TEXT2, mod.IUIAutomationTextPattern2)
            if tp2 is not None:
                try:
                    _active, caret = tp2.GetCaretRange()
                except Exception:
                    caret = None
        if caret is None and bool(client.cached(element, P_IS_TEXT_AVAILABLE, False)):
            tp = client.pattern(element, PAT_TEXT, mod.IUIAutomationTextPattern)
            if tp is not None:
                try:
                    selection = tp.GetSelection()
                    if selection is not None and selection.Length > 0:
                        caret = selection.GetElement(0)
                except Exception:
                    caret = None
        if caret is None:
            return None
        try:
            before = caret.Clone()
            before.MoveEndpointByRange(ENDPOINT_END, caret, ENDPOINT_START)
            before.MoveEndpointByUnit(ENDPOINT_START, TEXT_UNIT_CHARACTER, -int(max_chars))
            text = before.GetText(int(max_chars) + 8)
        except Exception:
            return None
        if text is None:
            return None
        text = str(text).replace("\r\n", "\n").replace("\r", "\n")
        return text[-int(max_chars) :] if max_chars > 0 else ""

    def edit_text(self, query: TextQuery) -> TextEditResult:
        """Find a phrase near the caret of the focused field, then select it or put the caret by it.

        The search reads up to ``TEXT_WINDOW`` characters each side of the caret in one call.
        Offsets found in that text become character moves on a text range; the range is read
        back before it is selected, because some providers count a CR LF pair as one
        character and others as two.  When the read-back disagrees, or the provider cannot
        select, the result is an arrow-key walk for the action thread to press instead.
        """
        client = self.client
        owner = self._owner
        if client is None:
            return TextEditResult("unsupported")
        fg = w.GetForegroundWindow()
        if not fg or w.window_pid(fg) == owner.own_pid or w.IsHungAppWindow(fg):
            return TextEditResult("unsupported")
        try:
            element = client.uia.GetFocusedElementBuildCache(client.focus_cache)
        except Exception:
            return TextEditResult("unsupported")
        if element is None or int(client.cached(element, P_PROCESS_ID, 0) or 0) == owner.own_pid:
            return TextEditResult("unsupported")
        if bool(client.cached(element, P_IS_PASSWORD, False)):
            return TextEditResult("password")
        if not bool(client.cached(element, P_IS_TEXT_AVAILABLE, False)):
            return TextEditResult("unsupported")
        tp = client.pattern(element, PAT_TEXT, client.mod.IUIAutomationTextPattern)
        if tp is None:
            return TextEditResult("unsupported")
        anchor, has_selection = self._anchor(element, tp)
        if anchor is None:
            return TextEditResult("unsupported")
        try:
            window = anchor.Clone()
            window.MoveEndpointByUnit(ENDPOINT_START, TEXT_UNIT_CHARACTER, -TEXT_WINDOW)
            window.MoveEndpointByUnit(ENDPOINT_END, TEXT_UNIT_CHARACTER, TEXT_WINDOW)
            text = str(window.GetText(2 * TEXT_WINDOW + 64) or "")
            head = window.Clone()
            head.MoveEndpointByRange(ENDPOINT_END, anchor, ENDPOINT_START)
            caret = len(str(head.GetText(TEXT_WINDOW + 64) or ""))
        except Exception:
            return TextEditResult("unsupported")
        found = locate(text, caret, query)
        if isinstance(found, str):
            return TextEditResult("not_found", missing=found)
        if self._select_span(window, text, found):
            return TextEditResult("done")
        plan = walk_plan(text, caret, found, select=query.kind != "caret", collapse=has_selection)
        return TextEditResult("walk", keys=plan) if plan is not None else TextEditResult("too_far")

    def _anchor(self, element: Any, tp: Any) -> tuple[Any | None, bool]:
        """An empty range at the start of the selection (or at the caret), and whether text is selected."""
        client = self.client
        assert client is not None
        rng = None
        try:
            selection = tp.GetSelection()
            if selection is not None and selection.Length > 0:
                rng = selection.GetElement(0)
        except Exception:
            rng = None
        if rng is None and bool(client.cached(element, P_IS_TEXT2_AVAILABLE, False)):
            tp2 = client.pattern(element, PAT_TEXT2, client.mod.IUIAutomationTextPattern2)
            if tp2 is not None:
                try:
                    _active, rng = tp2.GetCaretRange()
                except Exception:
                    rng = None
        if rng is None:
            return None, False
        try:
            has_selection = rng.CompareEndpoints(ENDPOINT_START, rng, ENDPOINT_END) != 0
            anchor = rng.Clone()
            anchor.MoveEndpointByRange(ENDPOINT_END, anchor, ENDPOINT_START)
        except Exception:
            return None, False
        return anchor, has_selection

    @staticmethod
    def _select_span(window: Any, text: str, span: Span) -> bool:
        try:
            target = window.Clone()
            target.MoveEndpointByRange(ENDPOINT_END, window, ENDPOINT_START)
            if span.end:
                target.MoveEndpointByUnit(ENDPOINT_END, TEXT_UNIT_CHARACTER, span.end)
            if span.start:
                target.MoveEndpointByUnit(ENDPOINT_START, TEXT_UNIT_CHARACTER, span.start)
            if span.end > span.start:
                expected, check = text[span.start : span.end], target
            else:
                # An empty range has no text to compare: compare what lies just before it.
                back = min(span.start, 12)
                expected, check = text[span.start - back : span.start], target.Clone()
                if back:
                    check.MoveEndpointByUnit(ENDPOINT_START, TEXT_UNIT_CHARACTER, -back)
            if not same_text(str(check.GetText(len(expected) + 16) or ""), expected):
                return False
            target.Select()
            return True
        except Exception:
            return False


def _area(hwnd: int) -> float:
    rect = w.frame_rect(hwnd)
    if not rect:
        return 0.0
    return float(max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1]))


# =========================================================================== the provider


class UIAProvider:
    """The ``UIAProvider`` implementation.  Every public method is safe from any thread."""

    def __init__(
        self,
        config: UIAConfig | None = None,
        *,
        own_pid: int | None = None,
        on_focus: Callable[[FocusInfo], None] | None = None,
        on_hover: Callable[[Target], None] | None = None,
        on_snapshot: Callable[[ClickableSnapshot], None] | None = None,
    ) -> None:
        self.config = config or UIAConfig()
        self.own_pid = own_pid if own_pid is not None else w.GetCurrentProcessId()
        self.process_names = ProcessNames()
        self._on_focus = on_focus
        self._on_hover = on_hover
        self._on_snapshot = on_snapshot
        self._focus = FocusInfo()
        self._snapshot = ClickableSnapshot()
        self._hover: Target | None = None
        self._last_rescan_for_motion = 0.0
        self._hover_listeners = 0
        self.interactive = InteractiveWorker(self)
        self.scanner = ScanWorker(self)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self.interactive.start()
        self.scanner.start()
        self.scanner.request(0.0)

    def stop(self) -> None:
        self.scanner.stop()
        self.interactive.stop()

    @property
    def running(self) -> bool:
        return self.interactive.running

    # ------------------------------------------------------------------ signals from other threads

    def foreground_changed(self, hwnd: int) -> None:
        self.interactive.focus_soon(0.02)
        self.scanner.request(0.15)

    def focus_changed(self, hwnd: int) -> None:
        self.interactive.focus_soon(0.03)

    def menu_changed(self, opened: bool) -> None:
        self.scanner.request(0.12 if opened else 0.2)

    def content_changed(self, delay: float = 0.4) -> None:
        """After clicks, scrolling and key presses the screen changes: rescan soon."""
        self.scanner.request(delay)

    def _note_pointer_moved(self) -> None:
        # Slow windows (a big web page) are rescanned less often, so scanning stays a small share of a core.
        last_ms = self.scanner.last_scan_ms.get("total", 0.0)
        interval = max(self.config.rescan_while_moving_s, 6.0 * last_ms / 1000.0)
        now = time.perf_counter()
        if now - self._snapshot.t > interval and now - self._last_rescan_for_motion > interval:
            self._last_rescan_for_motion = now
            self.scanner.request(0.0)

    # ------------------------------------------------------------------ state updates (worker threads)

    def _set_focus(self, focus: FocusInfo) -> None:
        old = self._focus
        self._focus = focus
        if replace(old, t=0.0) != replace(focus, t=0.0) and self._on_focus is not None:
            self._on_focus(focus)

    def _set_snapshot(self, snapshot: ClickableSnapshot) -> None:
        self._snapshot = snapshot
        if self._on_snapshot is not None:
            self._on_snapshot(snapshot)

    def _set_hover(self, target: Target) -> None:
        old = self._hover
        self._hover = target
        if self._on_hover is None:
            return
        old_key = _hover_key(old)
        if old_key != _hover_key(target):
            self._on_hover(target)

    # ------------------------------------------------------------------ UIAProvider protocol

    @property
    def snapshot(self) -> ClickableSnapshot:
        return self._snapshot

    def focus_info(self) -> FocusInfo:
        return self._focus

    def element_at(self, x: float, y: float, timeout: float = 0.05) -> ElementInfo | None:
        return self.interactive.call(lambda _c: self.interactive.element_at(x, y), timeout, None, priority=1)

    def nearest_clickable(self, x: float, y: float, radius: float) -> ElementInfo | None:
        root = w.window_at(x, y, self.own_pid)
        snapshot = self._snapshot
        if snapshot.covers(root):
            inside = snapshot.containing(x, y, root)
            if inside is not None:
                return inside
            return snapshot.nearest(x, y, radius, root)
        return snapshot.nearest(x, y, radius)

    def clickables_near(self, x: float, y: float, radius: float) -> list[ElementInfo]:
        """Every cached clickable within ``radius`` px of the point, nearest first.  Never blocks.

        Only the last snapshot is read.  Between a change of foreground window and the rescan
        it triggers (about 0.2 s), the snapshot still lists the old window, which may now be
        hidden, so until then only the taskbar and the new window's elements are offered.
        """
        snapshot = self._snapshot
        fg = int(w.GetForegroundWindow() or 0)
        roots: frozenset[int] | None = None
        if snapshot.foreground and fg and fg != snapshot.foreground:
            roots = frozenset(r for r in snapshot.roots if _is_taskbar(r)) | {fg}
        return snapshot.within(x, y, radius, roots)

    def clickables(self, timeout: float = 1.5) -> list[ElementInfo]:
        fresh = self.scanner.call(lambda _c: self.scanner.scan_now(), timeout, None, priority=1)
        if isinstance(fresh, ClickableSnapshot):
            return list(fresh.elements)
        cached = self._snapshot
        if cached.foreground == int(w.GetForegroundWindow() or 0) and time.perf_counter() - cached.t < 3.0:
            return list(cached.elements)
        return []

    def find_by_name(self, name: str, timeout: float = 1.0) -> list[ElementInfo]:
        elements = self.clickables(timeout)
        return [m.item for m in match_elements(name, elements)]

    def visible_names(self) -> list[str]:
        return self._snapshot.names()

    def text_before_caret(self, max_chars: int = 200, timeout: float = 0.05) -> str | None:
        return self.interactive.call(lambda _c: self.interactive.text_before_caret(max_chars), timeout, None, priority=0)

    def edit_text(self, query: TextQuery, timeout: float = 1.0) -> TextEditResult | None:
        """Select a phrase, or put the caret by it, in the focused field.  None when UI Automation did not answer."""
        return self.interactive.call(lambda _c: self.interactive.edit_text(query), timeout, None, priority=0)

    def invoke(self, element: ElementInfo, timeout: float = 0.5) -> bool:
        ref = element.handle
        if not isinstance(ref, ElementRef):
            return False
        worker: ComWorker = self.scanner if ref.owner == "scan" else self.interactive

        def run(client: UIAClient) -> bool:
            com = worker.element(ref.key)  # type: ignore[attr-defined]
            if com is None:
                com = _refind(client, element)
            if com is None:
                return False
            return _invoke(client, com)

        return bool(worker.call(run, timeout, False, priority=1))

    # ------------------------------------------------------------------ extras used by the action handlers

    def click_target(self, x: float, y: float, snap: bool, radius: float, timeout: float = 0.08) -> Target:
        """Where a click at (x, y) lands after snapping; falls back to the cache if UIA is busy."""
        result = self.interactive.call(lambda _c: self.interactive.target_at(x, y, snap, radius), timeout, None, priority=0)
        if isinstance(result, Target):
            return result
        root = w.window_at(x, y, self.own_pid)
        return resolve_target(x, y, self._snapshot, root=root, snap=snap, radius=radius)

    def hover_target(self) -> Target | None:
        return self._hover

    def stats(self) -> dict[str, Any]:
        polls = list(self.interactive.focus_poll_ms)
        return {
            "scan_ms": dict(self.scanner.last_scan_ms),
            "focus_poll_ms": (sum(polls) / len(polls)) if polls else None,
            "elements": len(self._snapshot.elements),
        }


def _hover_key(target: Target | None) -> tuple:
    if target is None or target.element is None:
        return (None,)
    el = target.element
    return (el.rect, el.name, el.control_type, target.snapped)


def _refind(client: UIAClient, element: ElementInfo) -> Any | None:
    """Find an element again from its centre when its COM object has been dropped."""
    cx, cy = element.rect.center
    try:
        found = client.uia.ElementFromPointBuildCache(client.mod.tagPOINT(int(cx), int(cy)), client.point_cache)
    except Exception:
        return None
    for _ in range(4):
        if found is None:
            return None
        try:
            name = _clean_name(client.cached(found, P_NAME, ""))
            type_name = CONTROL_TYPES.get(int(client.cached(found, P_CONTROL_TYPE, 0) or 0), "")
            if name == element.name and type_name == element.control_type:
                return found
            found = client.walker.GetParentElementBuildCache(found, client.point_cache)
        except Exception:
            return None
    return None


def _invoke(client: UIAClient, element: Any) -> bool:
    mod = client.mod
    invoke = client.pattern(element, PAT_INVOKE, mod.IUIAutomationInvokePattern)
    if invoke is not None:
        invoke.Invoke()
        return True
    toggle = client.pattern(element, PAT_TOGGLE, mod.IUIAutomationTogglePattern)
    if toggle is not None:
        toggle.Toggle()
        return True
    select = client.pattern(element, PAT_SELECTION_ITEM, mod.IUIAutomationSelectionItemPattern)
    if select is not None:
        select.Select()
        return True
    expand = client.pattern(element, PAT_EXPAND_COLLAPSE, mod.IUIAutomationExpandCollapsePattern)
    if expand is not None:
        state = expand.CurrentExpandCollapseState
        if state == 1:  # expanded
            expand.Collapse()
        else:
            expand.Expand()
        return True
    legacy = client.pattern(element, PAT_LEGACY, mod.IUIAutomationLegacyIAccessiblePattern)
    if legacy is not None:
        legacy.DoDefaultAction()
        return True
    return False
