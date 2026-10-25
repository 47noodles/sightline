"""In-memory stand-ins for the subsystem contracts, for unit tests and UI work without hardware.

They record what was asked of them so tests can assert on it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .interfaces import CalibrationReport, MouseButton
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


@dataclass
class FakeInput:
    pos: tuple[float, float] = (960.0, 540.0)
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    typed: list[str] = field(default_factory=list)
    _last: float = 0.0

    def cursor_pos(self) -> tuple[int, int]:
        return (int(self.pos[0]), int(self.pos[1]))

    def move_cursor(self, x: float, y: float) -> None:
        self.pos = (x, y)
        self._last = time.perf_counter()
        self.calls.append(("move_cursor", (x, y)))

    def click(self, button: MouseButton = "left", count: int = 1, x: float | None = None, y: float | None = None) -> None:
        if x is not None and y is not None:
            self.pos = (x, y)
        self.calls.append(("click", (button, count, self.pos[0], self.pos[1])))

    def button_down(self, button: MouseButton = "left") -> None:
        self.calls.append(("button_down", (button,)))

    def button_up(self, button: MouseButton = "left") -> None:
        self.calls.append(("button_up", (button,)))

    def scroll(self, dy: float = 0.0, dx: float = 0.0, *, smooth: bool = False) -> None:
        self.calls.append(("scroll", (dy, dx)))

    def key_combo(self, combo: str, repeat: int = 1) -> None:
        self.calls.append(("key_combo", (combo, repeat)))

    def key_down(self, key: str) -> None:
        self.calls.append(("key_down", (key,)))

    def key_up(self, key: str) -> None:
        self.calls.append(("key_up", (key,)))

    def release_all(self) -> None:
        self.calls.append(("release_all", ()))

    def type_text(self, text: str) -> None:
        self.typed.append(text)
        self.calls.append(("type_text", (text,)))

    def last_injected(self) -> float:
        return self._last

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeDisplay:
    def __init__(self, width: int = 1920, height: int = 1080, scale: float = 1.0) -> None:
        rect = Rect(0, 0, width, height)
        self._monitor = Monitor("fake0", "Fake display", rect, Rect(0, 0, width, height - 48), scale, True, (527.0, 296.0))

    def monitors(self) -> list[Monitor]:
        return [self._monitor]

    def primary(self) -> Monitor:
        return self._monitor

    def virtual_rect(self) -> Rect:
        return self._monitor.rect

    def monitor_at(self, x: float, y: float) -> Monitor:
        return self._monitor


@dataclass
class FakeUIA:
    elements: list[ElementInfo] = field(default_factory=list)
    focus: FocusInfo = field(default_factory=FocusInfo)
    caret_text: str | None = ""
    invoked: list[ElementInfo] = field(default_factory=list)

    def focus_info(self) -> FocusInfo:
        return self.focus

    def element_at(self, x: float, y: float, timeout: float = 0.05) -> ElementInfo | None:
        hits = [e for e in self.elements if e.rect.contains(x, y)]
        return min(hits, key=lambda e: e.rect.area) if hits else None

    def nearest_clickable(self, x: float, y: float, radius: float) -> ElementInfo | None:
        best: tuple[float, ElementInfo] | None = None
        for e in self.elements:
            if not e.clickable:
                continue
            d = e.rect.distance_to(x, y)
            if d <= radius and (best is None or d < best[0]):
                best = (d, e)
        return best[1] if best else None

    def clickables_near(self, x: float, y: float, radius: float) -> list[ElementInfo]:
        near = [(e.rect.distance_to(x, y), e) for e in self.elements if e.clickable]
        return [e for d, e in sorted(near, key=lambda p: p[0]) if d <= radius]

    def clickables(self, timeout: float = 1.5) -> list[ElementInfo]:
        return [e for e in self.elements if e.clickable]

    def find_by_name(self, name: str, timeout: float = 1.0) -> list[ElementInfo]:
        wanted = name.lower()
        return [e for e in self.elements if e.clickable and wanted in e.name.lower()]

    def visible_names(self) -> list[str]:
        return [e.name for e in self.elements if e.clickable and e.name]

    def text_before_caret(self, max_chars: int = 200, timeout: float = 0.05) -> str | None:
        if self.caret_text is None:
            return None
        return self.caret_text[-max_chars:]

    def invoke(self, element: ElementInfo, timeout: float = 0.5) -> bool:
        self.invoked.append(element)
        return True


@dataclass
class FakeWindows:
    windows: list[WindowInfo] = field(default_factory=list)
    activated: list[int] = field(default_factory=list)
    calls: list[tuple[str, int | None]] = field(default_factory=list)

    def foreground(self) -> WindowInfo | None:
        return self.windows[0] if self.windows else None

    def list_windows(self) -> list[WindowInfo]:
        return list(self.windows)

    def window_at(self, x: float, y: float) -> WindowInfo | None:
        """The front-most of ``windows`` that contains the point and is not minimised."""
        return next((w for w in self.windows if not w.minimized and w.rect.contains(x, y)), None)

    def activate(self, hwnd: int) -> bool:
        self.activated.append(hwnd)
        return True

    def close(self, hwnd: int | None = None) -> None:
        self.calls.append(("close", hwnd))

    def minimize(self, hwnd: int | None = None) -> None:
        self.calls.append(("minimize", hwnd))

    def maximize(self, hwnd: int | None = None) -> None:
        self.calls.append(("maximize", hwnd))

    def restore(self, hwnd: int | None = None) -> None:
        self.calls.append(("restore", hwnd))


@dataclass
class FakeApps:
    items: list[AppInfo] = field(
        default_factory=lambda: [
            AppInfo("Google Chrome", r"C:\fake\Google Chrome.lnk"),
            AppInfo("Notepad", r"shell:AppsFolder\Microsoft.WindowsNotepad_8wekyb3d8bbwe!App", "uwp"),
            AppInfo("File Explorer", r"C:\fake\File Explorer.lnk"),
        ]
    )
    launched: list[AppInfo] = field(default_factory=list)

    def apps(self) -> list[AppInfo]:
        return list(self.items)

    def launch(self, app: AppInfo) -> bool:
        self.launched.append(app)
        return True

    def refresh(self) -> None:
        pass


class FakeDesktop:
    def __init__(self) -> None:
        self.input = FakeInput()
        self.display = FakeDisplay()
        self.uia = FakeUIA()
        self.windows = FakeWindows()
        self.apps = FakeApps()
        self.checks: list[SystemCheck] = [
            SystemCheck("camera_privacy", "Apps may use the camera", True, fix_uri="ms-settings:privacy-webcam"),
            SystemCheck(
                "camera_sharing",
                "Other apps can use the camera at the same time",
                None,  # Windows doesn't document where this is stored, so the real check can't read it either
                "A video call could take the camera away from Sightline.",
                "ms-settings:camera",
            ),
        ]

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def system_checks(self) -> list[SystemCheck]:
        return list(self.checks)


class FakeCalibration:
    """Pretends to calibrate; ``live_estimate`` returns whatever ``estimate`` is set to."""

    def __init__(self) -> None:
        self.kind: str | None = None
        self.targets: list[tuple[float, float, bool]] = []
        self.labels: list[str | None] = []
        self.estimate: tuple[float, float] | None = None
        self._count = 0

    def begin(self, kind: str) -> None:
        self.kind = kind
        self.targets.clear()
        self.labels.clear()
        self._count = 0

    def set_target(self, x: float, y: float, collecting: bool) -> None:
        self.targets.append((x, y, collecting))
        if collecting:
            self._count += 1
        else:
            self._count = 0

    def set_eye_label(self, label: str | None) -> None:
        self.labels.append(label)

    def sample_count(self) -> int:
        return self._count * 3

    def live_estimate(self) -> tuple[float, float] | None:
        return self.estimate

    def finish(self) -> CalibrationReport:
        return CalibrationReport(True, self.kind or "gaze", "Average error 40 px (about 1.2 degrees)", 40.0, 1.2, (), 270)

    def cancel(self) -> None:
        self.kind = None


class FakeVision:
    def __init__(self) -> None:
        self.calibration = FakeCalibration()
        self.preview = False
        self.recentered = 0
        self.aim: tuple[float, float] | None = None  # what aim_point returns
        self.aim_calls: list[float] = []
        self.attention_value: Attention | None = None  # what attention returns

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def cameras(self) -> list[CameraInfo]:
        return [CameraInfo(0, "Fake camera")]

    def set_camera(self, index: int) -> None:
        pass

    def set_preview(self, enabled: bool) -> None:
        self.preview = enabled

    def recenter(self) -> None:
        self.recentered += 1

    def aim_point(self, t: float) -> tuple[float, float] | None:
        self.aim_calls.append(t)
        return self.aim

    def attention(self, t0: float, t1: float) -> Attention | None:
        return self.attention_value


class FakeSpeech:
    def __init__(self) -> None:
        self.fed: list[str] = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def microphones(self) -> list[MicInfo]:
        return [MicInfo("default", "Fake microphone", True)]

    def set_microphone(self, mic_id: str) -> None:
        pass

    def commands_help(self) -> list[CommandHelp]:
        return [
            CommandHelp("Mouse", "click", "Left click where the pointer is"),
            CommandHelp("Mouse", "scroll down <number>", "Scroll down", "scroll down three"),
        ]

    def feed_text(self, text: str) -> None:
        self.fed.append(text)

    def lists_changed(self) -> None:
        pass
