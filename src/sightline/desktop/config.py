"""Desktop integration settings.  Owned by the desktop subsystem; extend freely, keep labels plain English."""

from __future__ import annotations

from dataclasses import dataclass

from sightline.core.settings_base import setting


@dataclass
class DesktopConfig:
    snap_to_targets: bool = setting(
        True,
        label="Snap clicks to the nearest button or link",
        help="When the pointer is just outside something clickable, the click lands on it.",
        group="Clicking",
    )
    snap_radius_px: int = setting(48, label="Snap distance", group="Clicking", minimum=0, maximum=200, unit="px")
    highlight_target: bool = setting(True, label="Outline what a click will hit", group="Clicking")
    scroll_step: int = setting(3, label="Scroll amount per command", group="Scrolling", minimum=1, maximum=20, unit="notches")
    scroll_speed: float = setting(
        1.0,
        label="Continuous scrolling speed",
        help="How fast 'start scrolling' moves.  It starts slowly and speeds up over a couple of seconds.",
        group="Scrolling",
        minimum=0.25,
        maximum=4.0,
        step=0.25,
    )
    smooth_scrolling: bool = setting(
        True,
        label="Smooth continuous scrolling",
        help="Turn this off if an older app does not move when Sightline scrolls it.",
        group="Scrolling",
        advanced=True,
    )
    keep_awake: bool = setting(
        True,
        label="Keep the screen on while you are at the computer",
        help="Stops the screen turning off and the computer sleeping while you read or listen, which is when Windows would otherwise lock it.  "
        "Once the camera has not seen you for a while, Windows may lock it as usual.  Without a camera the screen stays on.",
        group="Power",
    )
    keep_awake_minutes: int = setting(
        5,
        label="Let the computer lock when it has not seen you for",
        help="How long the camera must see nobody before the screen may turn off and the computer lock.",
        group="Power",
        minimum=1,
        maximum=60,
        unit="minutes",
    )
    keyboard_hover_ms: int = setting(
        800,
        label="On-screen keyboard: look at a key this long to type it",
        group="On-screen keyboard",
        minimum=300,
        maximum=3000,
        unit="ms",
    )
    password_manager: str = setting(
        "",
        label="Password manager",
        help="Saying 'fill login' presses its shortcut for filling in a sign-in, so you do not have to type passwords.",
        group="Sign-ins",
        choices={
            "": "None",
            "bitwarden": "Bitwarden",
            "1password": "1Password",
            "lastpass": "LastPass",
            "keeper": "Keeper (opens its list of logins)",
            "dashlane": "Dashlane (opens its list of logins)",
            "protonpass": "Proton Pass (give it a shortcut in your browser first)",
            "custom": "Another one, using the keys below",
        },
    )
    password_manager_keys: str = setting(
        "",
        label="Keys that fill in a sign-in",
        help="Leave empty to use your password manager's usual keys.  Fill this in if you changed them, for example ctrl+shift+l.",
        group="Sign-ins",
    )
