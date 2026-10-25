"""Plain data types shared by every subsystem.

Coordinates are always physical screen pixels in the Windows virtual desktop
(the process is per-monitor DPI aware v2).  Only the Qt layer converts to
logical pixels, at the last moment, when it draws.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Literal, Mapping

# --------------------------------------------------------------------------- geometry


@dataclass(frozen=True, slots=True)
class Rect:
    """Axis-aligned rectangle in physical pixels.  ``w`` and ``h`` are never negative."""

    x: float
    y: float
    w: float
    h: float

    @classmethod
    def from_ltrb(cls, left: float, top: float, right: float, bottom: float) -> Rect:
        return cls(left, top, max(0.0, right - left), max(0.0, bottom - top))

    @property
    def left(self) -> float:
        return self.x

    @property
    def top(self) -> float:
        return self.y

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2.0, self.y + self.h / 2.0)

    @property
    def area(self) -> float:
        return self.w * self.h

    def is_empty(self) -> bool:
        return self.w <= 0 or self.h <= 0

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.right and self.y <= py < self.bottom

    def distance_to(self, px: float, py: float) -> float:
        """Distance from a point to the nearest edge; 0 when the point is inside."""
        dx = max(self.x - px, 0.0, px - self.right)
        dy = max(self.y - py, 0.0, py - self.bottom)
        return math.hypot(dx, dy)

    def inflate(self, d: float) -> Rect:
        return Rect(self.x - d, self.y - d, self.w + 2 * d, self.h + 2 * d)

    def intersects(self, other: Rect) -> bool:
        return not (
            other.x >= self.right
            or other.right <= self.x
            or other.y >= self.bottom
            or other.bottom <= self.y
        )

    def intersection(self, other: Rect) -> Rect:
        return Rect.from_ltrb(
            max(self.x, other.x),
            max(self.y, other.y),
            min(self.right, other.right),
            min(self.bottom, other.bottom),
        )

    def clamp_point(self, px: float, py: float) -> tuple[float, float]:
        """Nearest point inside the rectangle (the right and bottom edges are exclusive)."""
        return (
            min(max(px, self.x), self.right - 1),
            min(max(py, self.y), self.bottom - 1),
        )


@dataclass(frozen=True, slots=True)
class Monitor:
    """One display.  ``rect`` and ``work_rect`` are physical pixels in the virtual desktop."""

    id: str
    name: str
    rect: Rect
    work_rect: Rect
    scale: float  # DPI / 96
    primary: bool
    size_mm: tuple[float, float] | None = None  # physical panel size when the EDID reports it


# --------------------------------------------------------------------------- modes


class SpeechMode(StrEnum):
    AUTO = "auto"  # commands win, dictation when a text field has focus
    COMMAND = "command"  # never types free text unless told to ("type ...")
    DICTATION = "dictation"  # types everything except a few escape commands
    SLEEP = "sleep"  # ignores everything except the wake phrase


class PointerMode(StrEnum):
    HYBRID = "hybrid"  # gaze jumps the pointer, small head movements fine-tune it
    HEAD = "head"  # head movement only, like a head mouse
    GAZE = "gaze"  # pointer follows gaze directly


class ClickType(StrEnum):
    """What a dwell (or the generic "click" gesture) does next."""

    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"
    DOUBLE = "double"
    DRAG = "drag"  # first dwell presses, second dwell releases


# --------------------------------------------------------------------------- actions

ActionSource = Literal["voice", "eye", "dwell", "ui", "keyboard", "system", "test"]

# Arguments that carry what the user said or typed, or what is on their screen: logs show only their length.
PRIVATE_ARGS = frozenset({"text", "query", "url", "phrase", "start", "end", "name"})
# Argument marker on an action made in a password field: nothing about it is logged.  Handlers ignore it.
PRIVATE = "private"


@dataclass(frozen=True, slots=True)
class Action:
    """A request to do something, routed by name through the ActionRegistry.

    ``args`` is treated as read-only.  The full list of action names and their
    arguments is in ARCHITECTURE.md ("Action catalogue").
    """

    name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    source: ActionSource = "voice"
    utterance: str | None = None  # what the user said, when it came from speech
    # time.perf_counter() when the user began to aim: the speech onset for spoken commands.
    # Handlers that act "here" use VisionAPI.aim_point(aim_t), not the cursor at dispatch,
    # because people look at a target before they speak and the pointer moves on meanwhile.
    aim_t: float | None = None

    def arg(self, key: str, default: Any = None) -> Any:
        return self.args.get(key, default)

    def with_args(self, **changes: Any) -> Action:
        merged = dict(self.args)
        merged.update(changes)
        return replace(self, args=merged)

    def describe(self) -> str:
        """For logs.  Words the user said, typed or looked at (``PRIVATE_ARGS``) are reduced to their
        length, and an action from a password field (``PRIVATE``) shows no arguments at all."""
        if not self.args:
            return self.name
        if self.args.get(PRIVATE):
            return f"{self.name}(<private>)"
        inner = ", ".join(_describe_arg(k, v) for k, v in self.args.items())
        return f"{self.name}({inner})"


def _describe_arg(key: str, value: Any) -> str:
    if key not in PRIVATE_ARGS:
        return f"{key}={value!r}"
    if isinstance(value, str):
        return f"{key}=<{len(value)} chars>"
    return f"{key}=<{type(value).__name__}>"


# --------------------------------------------------------------------------- vision data


@dataclass(frozen=True, slots=True)
class Attention:
    """How the face looked over a time window, for deciding whether speech was meant for the computer.

    Each value is the fraction of camera frames in the window, 0..1.
    """

    face: float  # a face was found
    facing: float  # the head was turned toward the screen (within about 25 degrees of the user's neutral pose)
    mouth: float | None = None  # the mouth was moving; None when it cannot be measured (no blendshapes, mask)
    frames: int = 0  # camera frames in the window; 0 means vision had nothing to go on


# --------------------------------------------------------------------------- desktop data


@dataclass(frozen=True, slots=True)
class ElementInfo:
    """A UI Automation element as seen by Sightline."""

    rect: Rect
    name: str = ""
    control_type: str = ""  # UIA control type name, e.g. "Button", "Hyperlink", "Edit"
    automation_id: str = ""
    clickable: bool = False
    editable: bool = False
    process: str = ""
    handle: Any = field(default=None, compare=False, repr=False)  # opaque, owned by the desktop module


@dataclass(frozen=True, slots=True)
class FocusInfo:
    """What currently has keyboard focus.  Produced from a cache, so it may be up to ~200 ms old."""

    hwnd: int = 0
    process: str = ""
    window_title: str = ""
    control_type: str = ""
    name: str = ""
    editable: bool = False  # caret is in a text field that accepts typing
    password: bool = False  # focused field is a password box (never echo or log its text)
    t: float = 0.0  # time.perf_counter() when this was sampled


@dataclass(frozen=True, slots=True)
class WindowInfo:
    hwnd: int
    title: str
    process: str
    rect: Rect
    minimized: bool = False
    maximized: bool = False


@dataclass(frozen=True, slots=True)
class AppInfo:
    """A launchable application from the Start menu or the UWP app list."""

    name: str
    target: str  # .lnk path, exe path or "shell:AppsFolder\\<AppUserModelID>"
    kind: Literal["lnk", "uwp", "exe"] = "lnk"


@dataclass(frozen=True, slots=True)
class SystemCheck:
    """One Windows setting Sightline depends on, for the set-up check page."""

    id: str  # stable key, e.g. "camera_privacy"
    label: str  # plain English, e.g. "Apps may use the camera"
    ok: bool | None  # None when it could not be read
    detail: str = ""  # what is wrong and what to do about it, in plain words
    fix_uri: str = ""  # e.g. "ms-settings:privacy-webcam", opened for a helper


@dataclass(frozen=True, slots=True)
class CameraInfo:
    index: int
    name: str


@dataclass(frozen=True, slots=True)
class MicInfo:
    id: str
    name: str
    default: bool = False


@dataclass(frozen=True, slots=True)
class CommandHelp:
    """One row of the "what can I say" sheet."""

    category: str  # e.g. "Mouse", "Keys", "Text editing", "Windows and apps"
    phrase: str  # the spoken form with placeholders, e.g. "scroll down <number>"
    description: str
    example: str = ""
