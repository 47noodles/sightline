"""Events published on the EventBus.

Every event is a small frozen dataclass.  Subsystems may define private
events in their own ``events.py``; anything another subsystem reads lives
here.  Coordinates are physical screen pixels.  ``t`` fields are
``time.perf_counter()`` seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .types import Action, ElementInfo, FocusInfo, Rect

# --------------------------------------------------------------------------- vision


@dataclass(frozen=True, slots=True)
class FaceStatus:
    """Tracking health, published a few times a second and on every change of ``present``."""

    present: bool
    fps: float = 0.0
    distance_cm: float | None = None
    brightness: float | None = None  # mean luma of the face region, 0..255
    yaw_deg: float = 0.0
    pitch_deg: float = 0.0
    hint: str | None = None  # short plain-English advice, e.g. "Move closer to the camera"


@dataclass(frozen=True, slots=True)
class CameraFrame:
    """Downscaled BGR preview frame, only published while a preview is requested."""

    image: Any  # numpy uint8 array (h, w, 3), BGR
    landmarks: Any = None  # numpy float array (n, 2) in preview pixel coordinates, or None
    t: float = 0.0


PointerPhase = Literal["warp", "fine", "gaze", "head", "frozen", "paused", "lost"]


@dataclass(frozen=True, slots=True)
class PointerSample:
    """Pointer engine output, published once per processed camera frame."""

    t: float
    x: float  # where the engine wants the cursor (the cursor pump eases toward it)
    y: float
    gaze_x: float | None = None  # filtered gaze estimate, None before calibration or when invalid
    gaze_y: float | None = None
    phase: PointerPhase = "fine"
    confidence: float = 1.0
    # Expected gaze error around here in px, from the calibration's accuracy map.  None before calibration.
    # Snapping, the number chooser and target sizes use it instead of fixed constants.
    error_px: float | None = None
    # The head has moved the pointer since gaze last placed it (a jump, or gaze-only following); a nudge or
    # the mouse counts too.  Then the pointer is where the user steered it, not where gaze guessed, so the
    # number chooser lets its clicks through.
    steered: bool = False


EyeStateName = Literal["open", "closed", "left_wink", "right_wink", "unknown"]


@dataclass(frozen=True, slots=True)
class EyeState:
    """Per-frame eye openness, for diagnostics and the calibration wizard."""

    t: float
    left_closed: float  # 0 open .. 1 closed, the user's own left eye
    right_closed: float
    state: EyeStateName


EyeGestureKind = Literal["long_blink", "hold_blink", "wink_left", "wink_right"]


@dataclass(frozen=True, slots=True)
class EyeGesture:
    """A completed deliberate eye gesture.  Natural blinks never produce one."""

    t: float
    kind: EyeGestureKind
    duration: float
    x: float  # pointer position from just before the eyes started to close
    y: float


FaceGestureKind = Literal["brow_raise", "smile", "mouth_open", "cheek_puff"]


@dataclass(frozen=True, slots=True)
class FaceGesture:
    """A deliberate face gesture, made with the eyes open.  Mapped to actions like eye gestures."""

    t: float
    kind: FaceGestureKind
    x: float  # pointer position when the gesture began
    y: float


@dataclass(frozen=True, slots=True)
class CalibrationActive:
    """A calibration started (True) or ended (False).  Gestures and dwell are off meanwhile.

    The wizard must also end when it sees False it did not ask for: a pipeline rebuild cancels calibration.
    """

    active: bool
    kind: str | None = None


@dataclass(frozen=True, slots=True)
class GazeModelChanged:
    """A gaze model was activated (calibration, learning from clicks, or the saved profile)."""

    source: str  # "calibration", "clicks", "drift" or "profile"
    accuracy_px: float | None
    targets: int
    samples: int


@dataclass(frozen=True, slots=True)
class EyeHold:
    """Progress while both eyes are held closed: stage 1 arms a click, stage 2 a double click.

    Stage 0 means released or cancelled.  With the eyes shut, the user hears these as ticks.
    """

    t: float
    stage: int
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class DwellProgress:
    """Dwell timer around the pointer.  ``progress`` 0 clears the indicator, 1 fires."""

    x: float
    y: float
    progress: float
    radius: float = 0.0  # tolerance radius in pixels, for drawing


@dataclass(frozen=True, slots=True)
class GazeZoneProgress:
    """Looking at a special screen zone that works even while the pointer is paused.

    ``zone`` "resume": while eye control is paused, holding your gaze on the
    resume zone (vision decides where; ``rect`` says) for long enough resumes it.
    ``progress`` 0 clears the indicator, 1 means it fired.
    """

    zone: str
    progress: float
    rect: Rect | None = None


# --------------------------------------------------------------------------- speech


@dataclass(frozen=True, slots=True)
class MicLevel:
    rms: float  # 0..1
    speaking: bool


@dataclass(frozen=True, slots=True)
class TranscriptPartial:
    utterance_id: int
    text: str


@dataclass(frozen=True, slots=True)
class TranscriptFinal:
    utterance_id: int
    text: str
    audio_ms: float = 0.0  # length of the speech segment
    endpoint_ms: float = 0.0  # silence waited before deciding the utterance ended
    asr_ms: float = 0.0  # recogniser time after the endpoint


IntentKind = Literal["command", "dictation", "ignored", "rejected"]


@dataclass(frozen=True, slots=True)
class IntentResult:
    """What Sightline decided to do with one utterance.  The UI shows ``display`` as a caption."""

    utterance_id: int
    text: str
    kind: IntentKind
    display: str  # e.g. "Scroll down 3", "Typed: hello world", "Not understood"
    actions: tuple[Action, ...] = ()
    confidence: float = 1.0
    latency_ms: float = 0.0  # end of speech to dispatch
    # "Did you mean" choices for a rejected utterance, best first, e.g. ("Scroll down", "Scroll up").
    # The UI shows them numbered; saying the number, or Action("speech.choose", {"index": i}), runs one.
    alternatives: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SpeechStatus:
    ready: bool
    engine: str = ""
    microphone: str = ""
    error: str | None = None


# --------------------------------------------------------------------------- desktop


@dataclass(frozen=True, slots=True)
class FocusChanged:
    focus: FocusInfo


@dataclass(frozen=True, slots=True)
class HoverTarget:
    """The element a click would hit right now (after snapping), for the highlight box.

    ``rect`` None clears the highlight.
    """

    rect: Rect | None
    name: str = ""
    control_type: str = ""
    snapped: bool = False  # True when the pointer is outside the element and snapping pulled it in


@dataclass(frozen=True, slots=True)
class ClickPerformed:
    x: float
    y: float
    button: Literal["left", "right", "middle"]
    count: int = 1
    source: str = "voice"
    aim_t: float | None = None  # the action's aim_t: when the user aimed, so click learning anchors there


# --------------------------------------------------------------------------- core


@dataclass(frozen=True, slots=True)
class ActionExecuted:
    action: Action
    ok: bool
    error: str | None = None
    ms: float = 0.0


@dataclass(frozen=True, slots=True)
class StateChanged:
    field: str
    old: Any
    new: Any


@dataclass(frozen=True, slots=True)
class SettingsChanged:
    section: str  # "general", "vision", "speech", "desktop" or "ui"


@dataclass(frozen=True, slots=True)
class Notice:
    """A short message for the user, shown as a toast."""

    text: str
    level: Literal["info", "warn", "error"] = "info"
    key: str | None = None  # same key replaces an existing toast instead of stacking


@dataclass(frozen=True, slots=True)
class ModelProgress:
    model_id: str
    status: Literal["queued", "downloading", "verifying", "extracting", "ready", "error"]
    done_bytes: int = 0
    total_bytes: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AppLifecycle:
    phase: Literal["starting", "ready", "stopping"]
    detail: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "ActionExecuted",
    "AppLifecycle",
    "CameraFrame",
    "ClickPerformed",
    "DwellProgress",
    "ElementInfo",
    "EyeGesture",
    "EyeGestureKind",
    "EyeHold",
    "EyeState",
    "EyeStateName",
    "FaceStatus",
    "FocusChanged",
    "GazeZoneProgress",
    "HoverTarget",
    "IntentKind",
    "IntentResult",
    "MicLevel",
    "ModelProgress",
    "Notice",
    "PointerPhase",
    "PointerSample",
    "SettingsChanged",
    "SpeechStatus",
    "StateChanged",
    "TranscriptFinal",
    "TranscriptPartial",
]
