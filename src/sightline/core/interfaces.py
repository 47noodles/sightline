"""The contracts between subsystems.

Each subsystem implements its side of these Protocols and codes only
against the other sides, never against another subsystem's internals.
``sightline.core.fakes`` has in-memory versions for tests.

Thread rules: every method here is safe to call from any thread unless it
says otherwise.  Methods documented as "never blocks" read a cache and must
return in well under a millisecond.  Methods with a ``timeout`` may block up
to that long and return None / [] when it expires.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from .types import (
    AppInfo,
    Attention,
    CameraInfo,
    CommandHelp,
    ElementInfo,
    FocusInfo,
    MicInfo,
    Monitor,
    Rect,
    SystemCheck,
    WindowInfo,
)

MouseButton = Literal["left", "right", "middle"]

# =========================================================================== desktop


@runtime_checkable
class InputInjector(Protocol):
    """Synthetic mouse and keyboard input (Windows SendInput)."""

    def cursor_pos(self) -> tuple[int, int]: ...

    def move_cursor(self, x: float, y: float) -> None:
        """Absolute move in physical pixels; clamps to the virtual desktop."""

    def click(self, button: MouseButton = "left", count: int = 1, x: float | None = None, y: float | None = None) -> None:
        """Click at (x, y), or at the cursor when x/y are None.  ``count`` 2 is a double click."""

    def button_down(self, button: MouseButton = "left") -> None: ...

    def button_up(self, button: MouseButton = "left") -> None: ...

    def scroll(self, dy: float = 0.0, dx: float = 0.0, *, smooth: bool = False) -> None:
        """Wheel scroll in notches (positive dy scrolls content down, i.e. wheel toward the user).  Fractions allowed.

        ``smooth`` sends partial notches that modern apps animate; without it only whole
        notches go out, which every app understands.  Implementations may ignore it.
        """

    def key_combo(self, combo: str, repeat: int = 1) -> None:
        """Press a key or chord, e.g. "enter", "ctrl+shift+t", "win+d", "alt+f4", "f5"."""

    def key_down(self, key: str) -> None: ...

    def key_up(self, key: str) -> None: ...

    def release_all(self) -> None:
        """Release any held keys and mouse buttons (the panic button)."""

    def type_text(self, text: str) -> None:
        """Type arbitrary Unicode text into the focused control."""

    def last_injected(self) -> float:
        """time.perf_counter() of the last synthetic mouse move, to tell user mouse motion from ours."""


@runtime_checkable
class DisplayInfo(Protocol):
    def monitors(self) -> list[Monitor]: ...

    def primary(self) -> Monitor: ...

    def virtual_rect(self) -> Rect: ...

    def monitor_at(self, x: float, y: float) -> Monitor:
        """The monitor containing the point, or the nearest one."""


@runtime_checkable
class UIAProvider(Protocol):
    """UI Automation queries.  All COM work happens on the desktop module's own thread."""

    def focus_info(self) -> FocusInfo:
        """Current keyboard focus.  Never blocks."""

    def element_at(self, x: float, y: float, timeout: float = 0.05) -> ElementInfo | None: ...

    def nearest_clickable(self, x: float, y: float, radius: float) -> ElementInfo | None:
        """Closest clickable element within ``radius`` px of the point, from the cache.  Never blocks."""

    def clickables_near(self, x: float, y: float, radius: float) -> list[ElementInfo]:
        """Every cached clickable whose rectangle comes within ``radius`` px of the point, nearest first.

        Never blocks.  The UI's number chooser uses it when a gaze click is ambiguous.
        """

    def clickables(self, timeout: float = 1.5) -> list[ElementInfo]:
        """Visible, on-screen clickable elements of the foreground window (and the taskbar)."""

    def find_by_name(self, name: str, timeout: float = 1.0) -> list[ElementInfo]:
        """Clickable elements in the foreground window whose name matches (fuzzy), best first."""

    def visible_names(self) -> list[str]:
        """Names of the cached clickables, for voice "click <name>".  Never blocks."""

    def text_before_caret(self, max_chars: int = 200, timeout: float = 0.05) -> str | None:
        """Text just before the caret in the focused field, or None when unavailable or a password box."""

    def invoke(self, element: ElementInfo, timeout: float = 0.5) -> bool:
        """Activate an element through its UIA pattern (Invoke, Toggle, SelectionItem, ExpandCollapse)."""


@runtime_checkable
class WindowManager(Protocol):
    def foreground(self) -> WindowInfo | None: ...

    def list_windows(self) -> list[WindowInfo]:
        """Visible top-level windows with a title, front to back."""

    def window_at(self, x: float, y: float) -> WindowInfo | None:
        """The top-level window under a physical point, as its root owner: a dialog or popup an app owns stands for the app's window.

        Sightline's own windows are looked through.  None over the desktop, the taskbar or
        nothing.  Cheap (one lookup, no window list) and safe from any thread.
        """

    def activate(self, hwnd: int) -> bool: ...

    def close(self, hwnd: int | None = None) -> None: ...

    def minimize(self, hwnd: int | None = None) -> None: ...

    def maximize(self, hwnd: int | None = None) -> None: ...

    def restore(self, hwnd: int | None = None) -> None: ...


@runtime_checkable
class AppLauncher(Protocol):
    def apps(self) -> list[AppInfo]:
        """Installed apps from the Start menu and the UWP list.  Cached; never blocks after the first load."""

    def launch(self, app: AppInfo) -> bool: ...

    def refresh(self) -> None: ...


@runtime_checkable
class Desktop(Protocol):
    """Facade over the Windows integration, built by ``sightline.desktop``."""

    input: InputInjector
    display: DisplayInfo
    uia: UIAProvider
    windows: WindowManager
    apps: AppLauncher

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def system_checks(self) -> list[SystemCheck]:
        """Read-only checks of the Windows settings Sightline depends on, for the set-up check page.

        Camera and microphone privacy, sharing the camera between apps, the UAC prompt level,
        Voice Access before sign-in, keep-awake.  May take up to a second; call off the GUI thread.
        """


# =========================================================================== vision


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    ok: bool
    kind: str
    message: str  # plain-English result for the wizard, e.g. "Average error 38 px (about 1.1 degrees)"
    mean_error_px: float | None = None
    mean_error_deg: float | None = None
    per_point_error_px: tuple[float, ...] = ()
    samples: int = 0
    details: dict = field(default_factory=dict)


# "gestures" trains the face-gesture triggers; "drift" is the one-dot "fix pointer": finish() applies an offset, no refit.
CalibrationKind = Literal["eyes", "gaze", "validate", "head", "gestures", "drift"]
# Eye labels for kind "eyes"; face-gesture labels plus "talking" and "reading" (for false-trigger rates) for "gestures".
EyeLabel = Literal[
    "open",
    "closed",
    "wink_left",
    "wink_right",
    "neutral",
    "brow_raise",
    "smile",
    "mouth_open",
    "cheek_puff",
    "talking",
    "reading",
]


@runtime_checkable
class CalibrationAPI(Protocol):
    """Driven by the calibration wizard (GUI thread).  Sample collection happens on the vision thread.

    Typical gaze run: ``begin("gaze")``; for each target: ``set_target(x, y, collecting=False)``,
    animate, wait ~400 ms for the eyes to settle, ``set_target(x, y, collecting=True)``, wait ~800 ms,
    ``set_target(x, y, collecting=False)``; then ``finish()``.  Moving (smooth pursuit) targets
    call ``set_target`` every animation frame with ``collecting=True``.
    """

    def begin(self, kind: CalibrationKind) -> None: ...

    def set_target(self, x: float, y: float, collecting: bool) -> None: ...

    def set_eye_label(self, label: EyeLabel | None) -> None:
        """For kind "eyes": what the user is being asked to do right now (None pauses collection)."""

    def sample_count(self) -> int:
        """Samples collected for the current target or eye label."""

    def live_estimate(self) -> tuple[float, float] | None:
        """Current gaze estimate in screen pixels, for validation and live feedback."""

    def finish(self) -> CalibrationReport:
        """Fit, save to the profile and activate.  Blocks for up to a second or two."""

    def cancel(self) -> None: ...


@runtime_checkable
class VisionAPI(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def cameras(self) -> list[CameraInfo]: ...

    def set_camera(self, index: int) -> None: ...

    def set_preview(self, enabled: bool) -> None:
        """Publish CameraFrame events (about 15 per second) while enabled."""

    @property
    def calibration(self) -> CalibrationAPI: ...

    def recenter(self) -> None:
        """Treat the current head pose as neutral."""

    def aim_point(self, t: float) -> tuple[float, float] | None:
        """Where the pointer came to rest at or just after ``t`` (perf_counter seconds).  Never blocks.

        If the pointer was moving at ``t`` (the user steering with the head), the first resting
        point after it; if it has not rested since, where it is now.  None when vision is not
        driving the pointer (paused, yielding to a real mouse, no history), so callers fall back
        to the cursor.  Spoken "click", "scroll here", "zoom here" and the like act on this point.
        """

    def attention(self, t0: float, t1: float) -> Attention | None:
        """How the face looked between ``t0`` and ``t1`` (perf_counter seconds).  Never blocks.

        None when vision is not running.  Speech uses it to tell talk meant for the computer
        from a side conversation, a carer or a television.
        """


# =========================================================================== speech


@runtime_checkable
class SpeechAPI(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def microphones(self) -> list[MicInfo]: ...

    def set_microphone(self, mic_id: str) -> None: ...

    def commands_help(self) -> list[CommandHelp]:
        """Every command in the current grammar, for the "what can I say" sheet."""

    def feed_text(self, text: str) -> None:
        """Handle text as if it had been spoken (testing, and the typed command box)."""

    def lists_changed(self) -> None:
        """Dynamic vocab (apps, windows, element names) changed; refresh it."""


__all__: Sequence[str] = [
    "AppLauncher",
    "CalibrationAPI",
    "CalibrationKind",
    "CalibrationReport",
    "Desktop",
    "DisplayInfo",
    "ElementInfo",
    "EyeLabel",
    "InputInjector",
    "MouseButton",
    "SpeechAPI",
    "UIAProvider",
    "VisionAPI",
    "WindowManager",
]
