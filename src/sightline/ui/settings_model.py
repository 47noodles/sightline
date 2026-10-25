"""What the settings window shows and does, without Qt: fields from the ``setting()`` metadata,
values in words, steps, spoken values, presets and the five front-page settings.

The window is generated from the metadata each subsystem declares in its
``config.py``, so a new setting appears without UI work.  Nothing here
needs typing: numbers move in steps, choices are buttons, and free text
(the wake phrase, camera sizes) is left to a helper and the settings file.
Values Sightline learns by itself (``readonly``) are shown in words, with
nothing to change.  Safety settings (``protected``: the countdown before
closing or deleting, ignoring speech while you look away, the mouth check)
may be turned on or made stricter by voice, but only the eyes or dwell turn
them off (review S-01): a voice from a video must not be able to switch off
the checks that stand between it and the computer.
"""

from __future__ import annotations

import dataclasses
import enum
import math
import typing
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

SECTION_ORDER = ("vision", "speech", "desktop", "ui", "general")

# The pages after "Most used" and "Presets", each a list of (section, group) pairs.
PAGES: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Eyes and pointer", (("vision", "Pointer"), ("vision", "Eye clicks"), ("vision", "Dwell"), ("vision", "Pausing"), ("vision", "Accuracy"), ("vision", "Camera"))),
    ("Voice", (("speech", "Listening"), ("speech", "Recognition"), ("speech", "Dictation"), ("speech", "Microphone"))),
    ("Clicking and scrolling", (("desktop", "Clicking"), ("desktop", "Scrolling"), ("desktop", "On-screen keyboard"), ("desktop", "Power"))),
    ("Look and sound", (("ui", "Appearance"), ("ui", "Feedback"), ("ui", "Dock"), ("ui", "Precision"))),
    ("Start-up", (("general", "Startup"),)),
)

# What each eye gesture can do (the vision config's help text lists the same).
GESTURE_CHOICES: dict[str, str] = {
    "click": "Click (the chosen click type)",
    "double_click": "Double click",
    "right_click": "Right click",
    "middle_click": "Middle click",
    "drag": "Drag",
    "none": "Nothing",
}
GESTURE_NAMES: dict[str, str] = {
    "long_blink": "Close both eyes",
    "hold_blink": "Keep both eyes closed longer",
    "wink_right": "Wink your right eye",
    "wink_left": "Wink your left eye",
}


@dataclass(frozen=True)
class FieldInfo:
    section: str
    name: str
    label: str
    help: str
    group: str
    kind: str  # "number", "bool", "choice", "gestures" or "text" (left to a helper)
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    unit: str = ""
    choices: tuple[tuple[Any, str], ...] = ()
    advanced: bool = False
    restart: bool = False
    integer: bool = False
    readonly: bool = False  # Sightline measures it and shows it; nobody sets it (``setting(readonly=True)``)
    protected: bool = False  # a safety setting: voice may turn it on, never off (``setting(protected=True)``)

    @property
    def key(self) -> str:
        return f"{self.section}.{self.name}"


def fields(settings: Any) -> list[FieldInfo]:
    """Every visible setting, in declaration order, sections in ``SECTION_ORDER``."""
    out: list[FieldInfo] = []
    for section in SECTION_ORDER:
        cfg = getattr(settings, section, None)
        if cfg is None or not dataclasses.is_dataclass(cfg):
            continue
        hints = typing.get_type_hints(type(cfg))
        for f in dataclasses.fields(cfg):
            meta = f.metadata or {}
            if not meta or meta.get("hidden"):
                continue
            out.append(_info(section, f, hints.get(f.name, Any), meta))
    return out


def _info(section: str, f: dataclasses.Field, hint: Any, meta: Any) -> FieldInfo:
    choices: tuple[tuple[Any, str], ...] = ()
    raw_choices = meta.get("choices")
    is_enum = isinstance(hint, type) and issubclass(hint, enum.Enum)
    if hint is bool:
        kind = "bool"
    elif raw_choices:
        kind = "choice"
        choices = tuple((hint(k) if is_enum else k, v) for k, v in raw_choices.items())
    elif typing.get_origin(hint) is dict and f.name == "gesture_actions":
        kind = "gestures"
    elif hint in (int, float) and meta.get("minimum") is not None and meta.get("maximum") is not None:
        kind = "number"
    else:
        kind = "text"
    integer = hint is int
    step = meta.get("step")
    if kind == "number" and step is None:
        step = _default_step(meta.get("unit", ""), float(meta["minimum"]), float(meta["maximum"]), integer)
    return FieldInfo(
        section=section,
        name=f.name,
        label=meta.get("label", f.name),
        help=meta.get("help", ""),
        group=meta.get("group", ""),
        kind=kind,
        minimum=meta.get("minimum"),
        maximum=meta.get("maximum"),
        step=step,
        unit=meta.get("unit", ""),
        choices=choices,
        advanced=bool(meta.get("advanced")),
        restart=bool(meta.get("restart")),
        integer=integer,
        readonly=bool(meta.get("readonly")),
        protected=bool(meta.get("protected")),
    )


def _default_step(unit: str, lo: float, hi: float, integer: bool) -> float:
    span = hi - lo
    if unit == "ms":
        return 50.0 if span <= 3000 else 250.0
    if unit == "px":
        return 5.0 if span <= 160 else 10.0
    if integer:
        return 1.0 if span <= 40 else float(10 ** max(0, int(math.log10(span)) - 1))
    return span / 20.0


def find(settings: Any, key: str) -> FieldInfo:
    for info in fields(settings):
        if info.key == key:
            return info
    raise KeyError(key)


def get_value(settings: Any, info: FieldInfo) -> Any:
    return getattr(getattr(settings, info.section), info.name)


def set_value(settings: Any, info: FieldInfo, value: Any) -> bool:
    """Store a value (clamped and rounded).  True when it changed; a read-only value never does."""
    if info.readonly:
        return False
    value = normalise(info, value)
    cfg = getattr(settings, info.section)
    if getattr(cfg, info.name) == value:
        return False
    setattr(cfg, info.name, value)
    return True


def weakens(info: FieldInfo, old: Any, new: Any) -> bool:
    """True when ``new`` turns a protected setting off or otherwise away from what it was.

    A protected on-or-off setting is weakened only by turning it off; any other change to a
    protected value counts, since which way is safer is not known here.
    """
    if not info.protected:
        return False
    if info.kind == "bool":
        return bool(old) and not bool(new)
    return normalise(info, new) != old


def voice_may_set(info: FieldInfo, old: Any, new: Any, source: str) -> bool:
    """Whether an action from ``source`` may make this change (voice may not weaken a safety setting)."""
    return not (source == "voice" and weakens(info, old, new))


def normalise(info: FieldInfo, value: Any) -> Any:
    if info.kind != "number":
        return value
    v = float(value)
    if info.minimum is not None:
        v = max(float(info.minimum), v)
    if info.maximum is not None:
        v = min(float(info.maximum), v)
    if info.integer:
        return int(round(v))
    decimals = max(2, _decimals(info.step or 0.1))  # presets may sit between steps (125%)
    return round(v, decimals)


def _decimals(step: float) -> int:
    text = f"{step:.6f}".rstrip("0")
    return max(0, len(text.split(".")[1]) if "." in text else 0)


# --------------------------------------------------------------------------- words


def describe(info: FieldInfo, value: Any) -> str:
    """The value as a person would say it: "0.35 seconds", "60%", "On", "Top left corner"."""
    if info.kind == "bool":
        return "On" if value else "Off"
    if info.kind == "choice":
        for key, label in info.choices:
            if key == value or str(getattr(key, "value", key)) == str(getattr(value, "value", value)):
                return label
        return str(getattr(value, "value", value))
    if info.kind == "gestures":
        return f"{len(value)} gestures"
    if info.kind != "number":
        return str(value) if value not in ("", None) else "Not set"
    v = float(value)
    unit = info.unit
    if unit == "ms":
        seconds = v / 1000.0
        return "1 second" if abs(seconds - 1.0) < 1e-9 else f"{_trim(seconds, 2)} seconds"
    if unit == "s":
        return "1 second" if abs(v - 1.0) < 1e-9 else f"{_trim(v, 1)} seconds"
    if unit == "px":
        return f"{int(round(v))} pixels"
    if unit == "x":
        return f"{_trim(v, 1)} times"
    if unit in ("degrees", "notches"):
        return f"{_trim(v, 1)} {unit}"
    if unit:
        return f"{_trim(v, 2)} {unit}"
    if info.maximum is not None and float(info.maximum) <= 1.0 and not info.integer:
        return f"{int(round(v * 100))}%"
    if info.name == "scale":
        return f"{int(round(v * 100))}%"
    if not info.integer:
        return f"{_trim(v, 1)} times"
    return str(int(v))


def _trim(v: float, places: int) -> str:
    text = f"{v:.{places}f}".rstrip("0").rstrip(".")
    return text or "0"


def step_value(info: FieldInfo, value: Any, direction: int) -> Any:
    """One step up (+1) or down (-1); choices move to the next or previous choice; bools flip."""
    if info.kind == "number":
        step = float(info.step or 1.0)
        base = float(info.minimum or 0.0)
        k = (float(value) - base) / step
        # Off the step grid (set elsewhere): go to the next round value in that direction.
        if abs(k - round(k)) < 1e-6:
            k = round(k) + direction
        else:
            k = math.floor(k) + (1 if direction > 0 else 0)
        return normalise(info, base + k * step)
    if info.kind == "bool":
        return direction > 0 if direction else not value
    if info.kind == "choice":
        keys = [k for k, _ in info.choices]
        try:
            i = keys.index(value)
        except ValueError:
            i = -1 if direction > 0 else len(keys)
        return keys[max(0, min(len(keys) - 1, i + direction))]
    return value


_TRUE = {"on", "yes", "true", "1", "turn on", "enable", "enabled"}
_FALSE = {"off", "no", "false", "0", "turn off", "disable", "disabled"}


def parse_spoken(info: FieldInfo, spoken: Any) -> Any:
    """A value said aloud ("set to three", "set to top left", "set to off") for this field, or None."""
    if info.kind == "bool":
        word = str(spoken).strip().lower()
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
        return None
    if info.kind == "choice":
        return _match_choice(info, spoken)
    if info.kind != "number":
        return None
    try:
        v = float(spoken)
    except (TypeError, ValueError):
        return None
    # People say seconds and percentages; the settings store milliseconds and fractions.
    if info.unit == "ms" and v < 20:
        v *= 1000.0
    elif info.unit == "" and info.maximum is not None and float(info.maximum) <= 1.0 and v > 1.0:
        v /= 100.0
    elif info.name == "scale" and v > 4.0:
        v /= 100.0
    return normalise(info, v)


def _match_choice(info: FieldInfo, spoken: Any) -> Any:
    keys = [k for k, _ in info.choices]
    if isinstance(spoken, (int, float)) and not isinstance(spoken, bool):
        index = int(spoken) - 1
        return keys[index] if 0 <= index < len(keys) else None
    word = " ".join(str(spoken).lower().replace("_", " ").split())
    if not word:
        return None
    if word.isdigit():
        index = int(word) - 1
        return keys[index] if 0 <= index < len(keys) else None
    for key, label in info.choices:
        if word in (str(getattr(key, "value", key)).lower().replace("_", " "), label.lower()):
            return key
    for key, label in info.choices:
        if word in label.lower():
            return key
    return None


# --------------------------------------------------------------------------- the five on the front page


@dataclass(frozen=True)
class FrontItem:
    key: str  # "section.name"
    title: str
    words: tuple[str, str, str, str, str]  # from much less to much more; the middle one is "As is"
    amounts: tuple[float, float, float, float, float]  # multipliers, or offsets when ``offset``
    offset: bool = False
    preview: str = ""  # "speed", "duration", "ring", "hearing"


FRONT: tuple[FrontItem, ...] = (
    FrontItem("vision.head_speed", "Pointer speed", ("Slower", "A bit slower", "As is", "A bit faster", "Faster"), (0.6, 0.8, 1.0, 1.25, 1.6), preview="speed"),
    FrontItem("vision.blink_click_ms", "Blink to click", ("Shorter", "A bit shorter", "As is", "A bit longer", "Longer"), (0.75, 0.9, 1.0, 1.25, 1.6), preview="duration"),
    FrontItem("speech.speech_threshold", "Voice sensitivity", ("Less", "A bit less", "As is", "A bit more", "More"), (0.2, 0.1, 0.0, -0.1, -0.2), offset=True, preview="hearing"),
    FrontItem("speech.command_endpoint_ms", "Pause that ends a command", ("Shorter", "A bit shorter", "As is", "A bit longer", "Longer"), (0.6, 0.8, 1.0, 1.3, 1.7), preview="duration"),
    FrontItem("vision.dwell_ms", "Dwell time", ("Shorter", "A bit shorter", "As is", "A bit longer", "Longer"), (0.6, 0.8, 1.0, 1.3, 1.7), preview="ring"),
)


def front_levels(info: FieldInfo, item: FrontItem, base: Any) -> list[tuple[str, Any]]:
    """The five choices for a front-page setting, relative to its value when the window opened."""
    out = []
    for word, amount in zip(item.words, item.amounts, strict=True):
        raw = float(base) + amount if item.offset else float(base) * amount
        out.append((word, normalise(info, raw)))
    return out


def front_level_index(levels: list[tuple[str, Any]], value: Any) -> int | None:
    for i, (_word, v) in enumerate(levels):
        if v == value:
            return i
    return None


# --------------------------------------------------------------------------- presets


@dataclass(frozen=True)
class Preset:
    name: str
    summary: str
    values: tuple[tuple[str, Any], ...]  # ("section.name", value)


PRESETS: tuple[Preset, ...] = (
    Preset(
        "Steady hands",
        "Quicker and more direct: less smoothing and a shorter dwell.",
        (("vision.smoothing", 0.35), ("vision.head_dead_zone", 0.2), ("vision.warp_dwell_ms", 110), ("vision.dwell_ms", 700), ("desktop.snap_radius_px", 40)),
    ),
    Preset(
        "Tremor",
        "A steadier pointer and more forgiving targets.",
        (
            ("vision.smoothing", 0.75),
            ("vision.head_dead_zone", 0.55),
            ("vision.warp_dwell_ms", 220),
            ("vision.dwell_ms", 1200),
            ("vision.dwell_radius_px", 80),
            ("desktop.snap_radius_px", 80),
        ),
    ),
    Preset(
        "Tired eyes",
        "Fewer accidental eye clicks, more time, and bigger text.",
        (
            ("vision.blink_click_ms", 550),
            ("vision.blink_double_ms", 1400),
            ("vision.dwell_ms", 1300),
            ("speech.command_endpoint_ms", 400),
            ("ui.scale", 1.25),
            ("ui.caption_seconds", 6.0),
        ),
    ),
)


def default_preset(settings: Any) -> Preset:
    """Puts back the defaults of everything the named presets touch."""
    keys: list[str] = []
    for preset in PRESETS:
        keys.extend(k for k, _ in preset.values if k not in keys)
    values = []
    for key in keys:
        section, name = key.split(".", 1)
        cfg_type = type(getattr(settings, section))
        f = next(f for f in dataclasses.fields(cfg_type) if f.name == name)
        values.append((key, f.default))
    return Preset("Sightline defaults", "Puts back the values the presets change.", tuple(values))


def all_presets(settings: Any) -> list[Preset]:
    return [*PRESETS, default_preset(settings)]


def find_preset(settings: Any, name: str) -> Preset | None:
    wanted = " ".join(str(name).lower().replace("_", " ").split())
    for preset in all_presets(settings):
        if preset.name.lower() == wanted or wanted in preset.name.lower():
            return preset
    return None


def preset_lines(settings: Any, preset: Preset) -> list[str]:
    """What a preset changes, in words: "Dwell time: 1.2 seconds (now 0.9 seconds)"."""
    lines = []
    for key, value in preset.values:
        info = find(settings, key)
        now = get_value(settings, info)
        new = normalise(info, value)
        if new == now:
            lines.append(f"{info.label}: {describe(info, new)} (already)")
        else:
            lines.append(f"{info.label}: {describe(info, new)} (now {describe(info, now)})")
    return lines


def apply_preset(settings: Any, preset: Preset, *, source: str = "") -> set[str]:
    """Set the preset's values.  Returns the sections that changed.

    From voice, a value that would turn a safety setting off is left as it is.
    """
    changed: set[str] = set()
    for key, value in preset.values:
        info = find(settings, key)
        if not voice_may_set(info, get_value(settings, info), value, source):
            continue
        if set_value(settings, info, value):
            changed.add(info.section)
    return changed


def page_fields(all_fields: Iterable[FieldInfo], groups: tuple[tuple[str, str], ...], *, advanced: bool) -> list[FieldInfo]:
    """Fields of the given (section, group) pairs, ordinary or advanced, groups in the given order."""
    pool = list(all_fields)
    out: list[FieldInfo] = []
    for section, group in groups:
        out.extend(f for f in pool if f.section == section and f.group == group and f.advanced == advanced)
    return out
