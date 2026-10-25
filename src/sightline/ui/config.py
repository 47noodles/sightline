"""Interface settings.  Owned by the UI subsystem; extend freely, keep labels plain English."""

from __future__ import annotations

from dataclasses import dataclass

from sightline.core.settings_base import setting


@dataclass
class UIConfig:
    theme: str = setting("system", label="Theme", group="Appearance", choices={"system": "Match Windows", "dark": "Dark", "light": "Light"})
    scale: float = setting(1.0, label="Text and control size", group="Appearance", minimum=0.8, maximum=2.0, step=0.1)
    show_captions: bool = setting(True, label="Show what I said", group="Feedback")
    caption_seconds: float = setting(3.5, label="Keep captions on screen for", group="Feedback", minimum=1.5, maximum=15.0, step=0.5, unit="s")
    show_pointer_ring: bool = setting(True, label="Show a ring around the pointer", group="Feedback")
    show_gaze_dot: bool = setting(False, label="Show where my eyes are looking", group="Feedback", advanced=True)
    sounds: bool = setting(True, label="Play sounds", group="Feedback")
    sound_volume: float = setting(0.6, label="Sound volume", group="Feedback", minimum=0.0, maximum=1.0, step=0.05)
    show_dock: bool = setting(True, label="Show the eye-control dock", help="Large buttons you can look at to pause, scroll, open the keyboard and more.", group="Dock")
    dock_edge: str = setting("right", label="Dock position", group="Dock", choices={"right": "Right edge", "left": "Left edge", "top": "Top edge"})
    dock_reserve_space: bool = setting(
        True,
        label="Keep windows clear of the dock",
        help="Maximised windows stop at the dock instead of going under it.",
        group="Dock",
    )
    zoom_factor: float = setting(3.0, label="Zoom lens magnification", group="Precision", minimum=2.0, maximum=6.0, step=0.5, unit="x")
    max_numbers: int = setting(150, label="Most numbers to show at once", group="Precision", minimum=20, maximum=400, advanced=True)
    confirm_risky: bool = setting(
        True,
        label="Count down before closing, deleting or sending",
        help="Closing a window or tab, quitting, Delete, send keys, opening a command window and clicking Delete, Send or Pay wait a moment first. Say cancel, or blink on the countdown, to stop them.",
        group="Feedback",
        protected=True,
    )
    confirm_delay_ms: int = setting(1200, label="Countdown before closing or deleting", help="How long you have to stop it.", group="Feedback", minimum=600, maximum=3000, step=100, unit="ms")
