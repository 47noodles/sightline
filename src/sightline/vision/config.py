"""Vision settings.  Owned by the vision subsystem; extend freely, keep labels plain English."""

from __future__ import annotations

from dataclasses import dataclass

from sightline.core.settings_base import setting
from sightline.core.types import PointerMode


@dataclass
class VisionConfig:
    # camera
    camera_index: int = setting(0, label="Camera", group="Camera", hidden=True)
    camera_name: str = setting("", label="Camera name", group="Camera", hidden=True)
    camera_width: int = setting(1280, label="Camera width", group="Camera", advanced=True, unit="px")
    camera_height: int = setting(720, label="Camera height", group="Camera", advanced=True, unit="px")
    camera_fps: int = setting(30, label="Camera frame rate", group="Camera", advanced=True, minimum=15, maximum=60)
    camera_fov_deg: float = setting(
        62.0,
        label="Camera field of view",
        help="Used to estimate how far you sit from the screen. Most webcams are between 55 and 80 degrees.",
        group="Camera",
        advanced=True,
        minimum=40.0,
        maximum=120.0,
        step=1.0,
        unit="degrees",
    )
    camera_mirrored: bool = setting(
        False,
        label="My camera shows a mirror image",
        help="Turn this on if turning your head left moves the pointer right, or a left wink counts as a right wink.",
        group="Camera",
        advanced=True,
    )

    # pointer
    pointer_mode: PointerMode = setting(
        PointerMode.HYBRID,
        label="Pointer control",
        group="Pointer",
        choices={
            "hybrid": "Eyes jump, head fine-tunes (recommended)",
            "head": "Head only",
            "gaze": "Eyes only",
        },
    )
    warp_distance_px: int = setting(
        180,
        label="Jump when you look this far away",
        help="If you look further than this from the pointer, it jumps to where you are looking.",
        group="Pointer",
        minimum=60,
        maximum=600,
        unit="px",
    )
    warp_dwell_ms: int = setting(
        130,
        label="Look this long before the pointer jumps",
        help="Shorter feels quicker; longer stops the pointer jumping while your eyes flick around.",
        group="Pointer",
        advanced=True,
        minimum=60,
        maximum=600,
        unit="ms",
    )
    head_speed: float = setting(1.0, label="Head pointer speed", group="Pointer", minimum=0.2, maximum=4.0, step=0.1)
    head_dead_zone: float = setting(
        0.3,
        label="Ignore small head movements",
        help="Higher ignores more shaking; lower reacts to tinier movements.",
        group="Pointer",
        advanced=True,
        minimum=0.0,
        maximum=1.0,
        step=0.05,
    )
    smoothing: float = setting(
        0.5,
        label="Pointer smoothing",
        help="Higher is steadier but slightly slower to follow.",
        group="Pointer",
        minimum=0.0,
        maximum=1.0,
        step=0.05,
    )
    reading_hold: bool = setting(
        True,
        label="Hold the pointer still while I read",
        help="Moving your head lets it go again.",
        group="Pointer",
    )
    warp_on_intent: bool = setting(
        False,
        label="Jump only when I move my head, click or speak",
        help="The pointer waits where it is until you show you want to use it.",
        group="Pointer",
    )
    yield_to_mouse: bool = setting(
        True,
        label="Let a real mouse take over",
        help="When someone moves the mouse, eye control waits until they stop.",
        group="Pointer",
    )
    yield_ms: int = setting(
        1500,
        label="Wait this long after the mouse stops",
        group="Pointer",
        advanced=True,
        minimum=300,
        maximum=10000,
        unit="ms",
    )

    # clicking with the eyes
    blink_clicks: bool = setting(True, label="Click by closing both eyes", group="Eye clicks")
    blink_click_ms: int = setting(
        350,
        label="Close both eyes this long to click",
        help="Normal blinks are shorter than this, so they are ignored.",
        group="Eye clicks",
        minimum=300,
        maximum=1500,
        unit="ms",
    )
    blink_double_ms: int = setting(
        1000,
        label="Keep them closed this long to double-click",
        group="Eye clicks",
        minimum=600,
        maximum=3000,
        unit="ms",
    )
    blink_cancel_ms: int = setting(
        2500,
        label="Eyes closed longer than this cancels",
        help="So resting your eyes never clicks anything.",
        group="Eye clicks",
        minimum=1500,
        maximum=10000,
        unit="ms",
    )
    winks: bool = setting(
        True,
        label="Click by winking",
        help="Wink your right eye to right-click and your left eye to middle-click.",
        group="Eye clicks",
    )
    wink_min_ms: int = setting(180, label="Shortest wink that counts", group="Eye clicks", minimum=80, maximum=800, unit="ms")
    gesture_actions: dict[str, str] = setting(
        {
            "long_blink": "click",
            "hold_blink": "double_click",
            "wink_right": "right_click",
            "wink_left": "middle_click",
            # Face gestures stay off until the face gesture trainer has run and one is chosen.
            "brow_raise": "none",
            "smile": "none",
            "mouth_open": "none",
            "cheek_puff": "none",
        },
        label="What each eye and face gesture does",
        help="click follows the chosen click type; also double_click, right_click, middle_click, drag or none.",
        group="Eye clicks",
        advanced=True,
    )
    face_hold_ms: int = setting(
        150,
        label="Hold a face gesture this long",
        help="Raised eyebrows, a smile, an open mouth or puffed cheeks count after this long.",
        group="Eye clicks",
        advanced=True,
        minimum=80,
        maximum=600,
        unit="ms",
    )

    # dwell
    dwell_ms: int = setting(900, label="Dwell time", group="Dwell", minimum=300, maximum=3000, unit="ms")
    dwell_radius_px: int = setting(40, label="Dwell tolerance", group="Dwell", minimum=10, maximum=150, unit="px")

    # pausing
    resume_zone: str = setting(
        "top",
        label="Look here to turn eye control back on",
        help="While eye control is paused, look at this spot for a moment to resume.",
        group="Pausing",
        choices={
            "top": "Top middle of the screen",
            "top_left": "Top left corner",
            "top_right": "Top right corner",
            "off": "Off",
        },
    )
    resume_ms: int = setting(
        1500,
        label="Look at it this long",
        group="Pausing",
        advanced=True,
        minimum=800,
        maximum=5000,
        unit="ms",
    )

    # robustness
    learn_from_clicks: bool = setting(
        True,
        label="Keep improving accuracy from my clicks",
        help="Each click tells Sightline where you were really looking, so it corrects drift over time.",
        group="Accuracy",
    )
    pause_when_face_lost: bool = setting(True, label="Freeze the pointer when my face is not visible", group="Accuracy")
