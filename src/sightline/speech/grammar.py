"""The voice command grammar: hassil sentence templates, slot lists, help rows and action builders.

Each ``CommandSpec`` lists hassil templates (https://github.com/OHF-Voice/hassil,
Apache-2.0) in normalised form: lowercase, digits for numbers, no
punctuation (``normalize.normalize_command`` is applied to what the user
said before matching).  A builder turns the matched slots into Actions.

Templates are grouped by context: ``global`` (normal use), ``labels`` and
``grid`` (while those overlays are open), ``wizard`` (setup and
calibration), ``settings`` (the settings window), ``chooser`` and ``menu``
(numbered targets and the action menu), ``confirm`` (the countdown before a
hard-to-reverse action), ``dictation`` (the escape commands that work in
dictation mode) and ``sleep`` (only the wake phrase).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sightline.core.types import Action, CommandHelp
from sightline.core.urls import web_address

from . import vocab
from .formatting import apply_formatters, spell
from .normalize import normalize_command

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- outcome and build context


@dataclass(frozen=True, slots=True)
class TypeRequest:
    """Text to type.  ``prose`` text gets smart spacing and capitals; code-formatted text is typed as is."""

    text: str
    prose: bool = True
    raw_words: str = ""  # what was said, for captions
    formatter: str = ""  # prose formatter ids applied after dictation formatting ("CAPITALIZE", "ALL_CAPS")


@dataclass(frozen=True, slots=True)
class Outcome:
    actions: tuple[Action, ...] = ()
    display: str = ""
    type_request: TypeRequest | None = None
    confidence: float = 1.0  # below 1.0 when a spoken name could not be matched exactly
    reject: str | None = None  # the template matched but the words did not make sense (plain-English reason)
    state: tuple[tuple[str, Any], ...] = ()  # changes to apply to the BuildContext if this outcome is chosen


class BuildContext:
    """What builders may look at.  The service fills it in; tests use the defaults."""

    def __init__(self) -> None:
        self.scroll_step: int = 3
        self.wake_phrase: str = "wake up"
        self.sleep_phrase: str = "go to sleep"
        self.resolve_app: Callable[[str], tuple[str, float] | None] = lambda text: None
        self.resolve_window: Callable[[str], tuple[str, int | None, float] | None] = lambda text: None
        self.resolve_element: Callable[[str], tuple[str, float] | None] = lambda text: None
        self.continuous_scroll: tuple[str, float] | None = None  # (direction, speed) while scrolling
        self.last_command: tuple[Action, ...] = ()
        self.previous_mode: str = "auto"
        self.current_mode: str = "auto"

    def apply(self, outcome: Outcome) -> None:
        """Builders are pure; the chosen outcome's state changes are applied here."""
        for key, value in outcome.state:
            setattr(self, key, value)


Builder = Callable[[Mapping[str, Any], BuildContext], Outcome]


@dataclass(frozen=True)
class HelpRow:
    phrase: str
    description: str
    example: str = ""


@dataclass(frozen=True)
class CommandSpec:
    id: str
    category: str
    sentences: tuple[str | tuple[str, dict[str, Any]], ...]
    build: Builder
    help: tuple[HelpRow, ...] = ()
    contexts: frozenset[str] = frozenset({"global"})
    fuzzy: bool = True  # may be matched by the fuzzy pass
    semantic: tuple[Any, ...] = ()  # paraphrases (text, or (text, slots)) for the semantic index; empty means never
    exact_only: bool = False  # only an exact template match runs it (quit Sightline)
    chainable: bool = True  # may be combined with other commands in one utterance
    free_text: bool = False  # captures dictated words ("type ...", "find ..."): yields to dictation in text fields
    wait_after: bool = False  # a longer phrase often follows, so do not end the utterance early
    priority: int = 0  # higher wins when two commands match equally well (user commands are 10)
    risky: bool = False  # hard to reverse: never run from a paraphrase, only offered as "did you mean"


def _a(action: str, /, **args: Any) -> Action:
    return Action(action, args, source="voice")


def _one(action: Action, display: str) -> Outcome:
    return Outcome((action,), display)


def _keys(display: str, *combos: str, repeat: int = 1) -> Outcome:
    actions = tuple(_a("key.press", combo=c, repeat=repeat) if repeat > 1 else _a("key.press", combo=c) for c in combos)
    return Outcome(actions, display)


def _count(slots: Mapping[str, Any], name: str = "count", default: int = 1) -> int:
    value = slots.get(name)
    if value is None:
        return default
    try:
        return max(1, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def _times(n: int) -> str:
    return f" {n} times" if n > 1 else ""


def _pretty_combo(combo: str) -> str:
    names = {
        "ctrl": "Ctrl",
        "shift": "Shift",
        "alt": "Alt",
        "win": "Windows",
        "pageup": "Page Up",
        "pagedown": "Page Down",
        "printscreen": "Print Screen",
        "capslock": "Caps Lock",
        "backspace": "Backspace",
        "escape": "Escape",
        "enter": "Enter",
        "plus": "+",
    }
    parts = []
    for part in combo.split("+"):
        if part in names:
            parts.append(names[part])
        elif len(part) == 1:
            parts.append(part.upper())
        elif part.startswith("f") and part[1:].isdigit():
            parts.append(part.upper())
        else:
            parts.append(part.capitalize())
    return "+".join(parts)


# --------------------------------------------------------------------------- slot lists


def _count_values() -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = [{"in": str(n), "out": n} for n in range(1, 101)]
    for word, n in vocab.NUMBER_HOMOPHONES.items():
        values.append({"in": word, "out": n})
    return values


def _small_numbers(top: int) -> list[dict[str, Any]]:
    """1 to ``top``, and the words heard for them ("to", "for"), for an overlay with that many numbered items."""
    return [{"in": str(n), "out": n} for n in range(1, top + 1)] + [{"in": w, "out": n} for w, n in vocab.NUMBER_HOMOPHONES.items() if n <= top]


def _mapping_values(mapping: Mapping[str, str]) -> list[dict[str, Any]]:
    return [{"in": normalize_command(k) or k, "out": v} for k, v in mapping.items()]


def _key_values() -> list[dict[str, Any]]:
    keys: dict[str, str] = {}
    keys.update(vocab.all_letter_words())
    keys.update(vocab.DIGIT_KEYS)
    keys.update(vocab.SPECIAL_KEYS)
    keys.update(vocab.ARROW_KEYS)
    keys.update(vocab.FUNCTION_KEYS)
    keys.update({k: vocab.key_name(v) for k, v in vocab.SYMBOL_KEYS.items()})
    return _mapping_values(keys)


def _bare_key_values() -> list[dict[str, Any]]:
    keys = {k: v for k, v in vocab.SPECIAL_KEYS.items() if v not in ("insert", "menu", "capslock")}
    keys.update(vocab.ARROW_KEYS)
    return _mapping_values(keys)


STATIC_LISTS: dict[str, Any] = {
    "count": {"values": _count_values()},
    "times_word": {"values": [{"in": "once", "out": 1}, {"in": "twice", "out": 2}, {"in": "thrice", "out": 3}]},
    "cell": {"values": _small_numbers(9)},
    "chooser_number": {"values": _small_numbers(6)},  # the number chooser shows two to six targets
    "menu_number": {"values": _small_numbers(8)},  # the action menu has eight tiles
    "direction": {"values": ["up", "down", "left", "right"]},
    "vertical": {"values": ["up", "down"]},
    "modifier": {"values": _mapping_values(vocab.MODIFIER_KEYS)},
    "key": {"values": _key_values()},
    "bare_key": {"values": _bare_key_values()},
    "formatter": {"values": _mapping_values(vocab.FORMATTERS)},
    "click_type": {"values": ["left", "right", "middle", "double", "drag"]},
    "place": {
        "values": [
            {"in": "center", "out": "center"},
            {"in": "centre", "out": "center"},
            {"in": "middle", "out": "center"},
            {"in": "top", "out": "top"},
            {"in": "bottom", "out": "bottom"},
            {"in": "left", "out": "left"},
            {"in": "right", "out": "right"},
        ]
    },
    "side": {
        "values": [
            {"in": "left", "out": "left"},
            {"in": "right", "out": "right"},
            {"in": "up", "out": "up"},
            {"in": "top", "out": "up"},
            {"in": "down", "out": "down"},
            {"in": "bottom", "out": "down"},
        ]
    },
    # "Did you mean" choices: at most three, said as a bare number or an ordinal.
    "choice": {"values": [{"in": "1", "out": 1}, {"in": "won", "out": 1}, {"in": "2", "out": 2}, {"in": "to", "out": 2}, {"in": "too", "out": 2}, {"in": "3", "out": 3}, {"in": "tree", "out": 3}]},
    "choice_ord": {"values": [{"in": "1st", "out": 1}, {"in": "2nd", "out": 2}, {"in": "3rd", "out": 3}]},
    "text": {"wildcard": True},
    "app_name": {"wildcard": True},
    "window_name": {"wildcard": True},
    "element_name": {"wildcard": True},
    "url": {"wildcard": True},
    "query": {"wildcard": True},
    "letters": {"wildcard": True},
    "key_words": {"wildcard": True},
    "phrase": {"wildcard": True},  # words already in the focused text field ("select quick brown")
    "caret_where": {"values": ["before", "after"]},
    # While the settings window is open ("set to three", "set to a bit faster", "set to off").
    "setting_value": {"wildcard": True},
    # Settings presets, by the names the settings window shows (ui/settings_model.py finds them by these words).
    "preset": {
        "values": [
            {"in": "steady hands", "out": "steady hands"},
            {"in": "steady hand", "out": "steady hands"},
            {"in": "tremor", "out": "tremor"},
            {"in": "tremors", "out": "tremor"},
            {"in": "tired eyes", "out": "tired eyes"},
            {"in": "tired eye", "out": "tired eyes"},
            {"in": "defaults", "out": "defaults"},
            {"in": "default", "out": "defaults"},
            {"in": "default settings", "out": "defaults"},
            {"in": "sightline defaults", "out": "defaults"},
        ]
    },
    # Items of the action menu (ARCHITECTURE.md section 7, ``ui.menu.select item=...``): the words as said,
    # which the menu matches to its tiles.  The last six are for its scroll controls.
    "menu_item": {
        "values": [
            {"in": words, "out": words}
            for words in (
                "double click",
                "right click",
                "context menu",
                "middle click",
                "drag",
                "drag here",
                "drag from here",
                "drop",
                "drop here",
                "scroll",
                "scroll here",
                "zoom",
                "zoom here",
                "numbers",
                "show numbers",
                "keyboard",
                "show keyboard",
                "stop scrolling",
                "up",
                "scroll up",
                "down",
                "scroll down",
            )
        ]
    },
}

EXPANSION_RULES: dict[str, str] = {
    "times": "[{count} [times]|{times_word:count}]",
    "the": "[the|this|that]",
    "it": "[it|that|this|here|there]",
    "minimise": "(minimise|minimize)",
    "maximise": "(maximise|maximize)",
    "window": "[the|this] window",
}

# --------------------------------------------------------------------------- key sequences ("press ...")

_MODIFIER_WORDS = {normalize_command(k): v for k, v in vocab.MODIFIER_KEYS.items()}
_KEY_WORDS: dict[str, str] = {}
for _source in (
    vocab.all_letter_words(),
    vocab.DIGIT_KEYS,
    vocab.SPECIAL_KEYS,
    vocab.ARROW_KEYS,
    vocab.FUNCTION_KEYS,
    {k: vocab.key_name(v) for k, v in vocab.SYMBOL_KEYS.items()},
):
    for _k, _v in _source.items():
        _KEY_WORDS[normalize_command(_k) or _k] = _v
_LETTER_KEYS = set(vocab.all_letter_words().values())
_UPPER_WORDS = {"capital", "upper", "uppercase", "ship"}
_CHARACTER_KEYS = set(vocab.all_letter_words().values()) | set(vocab.DIGIT_KEYS.values()) | {vocab.key_name(v) for v in vocab.SYMBOL_KEYS.values()}
_MAX_PHRASE = max(len(k.split()) for k in list(_KEY_WORDS) + list(_MODIFIER_WORDS))


def _longest(words: list[str], i: int, table: Mapping[str, str]) -> tuple[str, int] | None:
    for n in range(min(_MAX_PHRASE, len(words) - i), 0, -1):
        phrase = " ".join(words[i : i + n])
        if phrase in table:
            return table[phrase], i + n
    return None


def parse_key_sequence(text: str) -> list[tuple[str, int]] | None:
    """"control shift t" -> [("ctrl+shift+t", 1)]; "tab 3 times enter" -> [("tab", 3), ("enter", 1)].

    Returns None unless every word is understood.  A number after a character
    key is another key ("press 1 2 3"); after other keys it is a repeat count.
    """
    words = text.split()
    out: list[tuple[str, int]] = []
    i = 0
    while i < len(words):
        mods: list[str] = []
        while True:
            hit = _longest(words, i, _MODIFIER_WORDS)
            if hit is None:
                break
            if hit[0] not in mods:
                mods.append(hit[0])
            i = hit[1]
        upper = i < len(words) - 1 and words[i] in _UPPER_WORDS
        if upper:
            i += 1  # "press capital a" is shift+a
        key_hit = _longest(words, i, _KEY_WORDS) if i < len(words) else None
        if key_hit is None:
            if len(mods) == 1 and i == len(words):
                out.append((mods[0], 1))  # "press windows"; "press control shift" lost its key, so no
                break
            return None
        key, i = key_hit
        if upper:
            if key not in _LETTER_KEYS:
                return None
            if "shift" not in mods:
                mods.append("shift")
        repeat = 1
        if i < len(words):
            nxt = words[i]
            count = int(nxt) if nxt.isdigit() else vocab.NUMBER_HOMOPHONES.get(nxt)
            followed_by_times = i + 1 < len(words) and words[i + 1] == "times"
            if count is not None and (followed_by_times or (key not in _CHARACTER_KEYS and nxt.isdigit())):
                repeat = max(1, min(100, count))
                i += 2 if followed_by_times else 1
        combo = "+".join(mods + [key])
        if out and out[-1][0] == combo:
            out[-1] = (combo, min(100, out[-1][1] + repeat))  # "down down down"
        else:
            out.append((combo, repeat))
    return out or None


def _key_outcome(sequence: list[tuple[str, int]]) -> Outcome:
    actions: list[Action] = []
    shown: list[str] = []
    for combo, repeat in sequence:
        if "+" not in combo and combo in vocab.SHIFTED_SYMBOLS | {"plus"}:
            char = "+" if combo == "plus" else combo
            actions.append(_a("text.type", text=char * repeat))
        elif repeat > 1:
            actions.append(_a("key.press", combo=combo, repeat=repeat))
        else:
            actions.append(_a("key.press", combo=combo))
        shown.append(_pretty_combo(combo) + _times(repeat))
    return Outcome(tuple(actions), "Press " + ", ".join(shown))


# --------------------------------------------------------------------------- builders


def _b_press(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    seq = parse_key_sequence(str(slots.get("key_words", "")))
    if seq is None:
        return Outcome(reject="Unknown key")
    return _key_outcome(seq)


def _b_combo(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    mods: list[str] = []
    for name in ("mod1", "mod2", "mod3"):
        value = slots.get(name)
        if value and value not in mods:
            mods.append(value)
    key = slots.get("key")
    combo = "+".join(mods + ([key] if key else []))
    return _key_outcome([(combo, _count(slots))])


def _b_bare_key(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    return _key_outcome([(slots["bare_key"], _count(slots))])


def _b_scroll(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    direction = slots["direction"]
    n = _count(slots, default=0)
    if slots.get("small"):
        return _one(_a("mouse.scroll", direction=direction, amount=1), f"Scroll {direction} a little")
    if n:
        return _one(_a("mouse.scroll", direction=direction, amount=n * ctx.scroll_step), f"Scroll {direction} {n}")
    return _one(_a("mouse.scroll", direction=direction), f"Scroll {direction}")


def _b_scroll_start(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    direction = slots["direction"]
    speed = float(slots.get("speed", 1.0))
    outcome = _one(_a("mouse.scroll_start", direction=direction, speed=speed), f"Scrolling {direction}")
    return Outcome(outcome.actions, outcome.display, state=(("continuous_scroll", (direction, speed)),))


def _b_scroll_speed(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    if ctx.continuous_scroll is None:
        return Outcome(reject="Not scrolling")
    direction, speed = ctx.continuous_scroll
    factor = 1.5 if slots.get("faster") else 1 / 1.5
    speed = round(max(0.25, min(8.0, speed * factor)), 2)
    word = "faster" if slots.get("faster") else "slower"
    return Outcome((_a("mouse.scroll_start", direction=direction, speed=speed),), f"Scrolling {word}", state=(("continuous_scroll", (direction, speed)),))


def _b_scroll_stop(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    return Outcome((_a("mouse.scroll_stop"),), "Stop scrolling", state=(("continuous_scroll", None),))


def _b_click(button: str, count: int, display: str) -> Builder:
    def build(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
        args: dict[str, Any] = {"button": button}
        if count != 1:
            args["count"] = count
        return _one(_a("mouse.click", **args), display)

    return build


def _b_click_element(button: str, count: int, verb: str) -> Builder:
    def build(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
        spoken = str(slots.get("element_name", "")).strip()
        if not spoken:
            return Outcome(reject="No name")
        resolved = ctx.resolve_element(spoken)
        name, confidence = (resolved[0], resolved[1]) if resolved else (spoken, 0.5)
        args: dict[str, Any] = {"name": name}
        if button != "left":
            args["button"] = button
        if count != 1:
            args["count"] = count
        return Outcome((_a("element.click", **args),), f"{verb} {name}", confidence=confidence)

    return build


def _b_move_to(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    place = slots["place"]
    return _one(_a("mouse.move", to=place), f"Move pointer to the {place}")


def _b_move_by(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    direction = slots["direction"]
    px = _count(slots, default=100)
    dx = {"left": -px, "right": px}.get(direction, 0)
    dy = {"up": -px, "down": px}.get(direction, 0)
    return _one(_a("mouse.move", dx=dx, dy=dy), f"Move pointer {direction} {px}")


def _b_nudge(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    direction = slots["direction"]
    n = _count(slots)
    return _one(_a("pointer.nudge", direction=direction, amount=10 * n), f"Nudge {direction}" + (f" {n}" if n > 1 else ""))


def _b_spell(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    text = spell(str(slots.get("letters", "")))
    if text is None:
        return Outcome(reject="Not letters")
    return Outcome(type_request=TypeRequest(text, prose=False, raw_words=text), display=f"Typed: {text}")


def _b_type(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    words = str(slots.get("text", "")).strip()
    if not words:
        return Outcome(reject="Nothing to type")
    return Outcome(type_request=TypeRequest(words, prose=True, raw_words=words))


def _b_format(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    words = str(slots.get("text", "")).strip()
    formatter = slots.get("formatter")
    if not words or not formatter:
        return Outcome(reject="Nothing to type")
    prose = all(part in vocab.PROSE_FORMATTERS for part in formatter.split(","))
    if prose:
        # Prose formatters still get spacing; dictation formatting runs first, then the formatter.
        return Outcome(type_request=TypeRequest(words, prose=True, raw_words=words, formatter=formatter))
    text = apply_formatters(words, formatter)
    return Outcome(type_request=TypeRequest(text, prose=False, raw_words=words), display=f"Typed: {text}")


def _b_text(text: str, display: str) -> Builder:
    def build(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
        return Outcome(type_request=TypeRequest(text, prose=False, raw_words=display), display=display)

    return build


def _b_app(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    spoken = str(slots.get("app_name", "")).strip()
    if not spoken:
        return Outcome(reject="No app name")
    resolved = ctx.resolve_app(spoken)
    if resolved:
        name, confidence = resolved
    else:
        name, confidence = spoken, 0.5
    return Outcome((_a("app.open", name=name),), f"Open {name}", confidence=confidence)


def _b_window(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    spoken = str(slots.get("window_name", "")).strip()
    if not spoken:
        return Outcome(reject="No window name")
    resolved = ctx.resolve_window(spoken)
    if resolved:
        title, hwnd, confidence = resolved
        args: dict[str, Any] = {"name": title}
        if hwnd:
            args["hwnd"] = hwnd
        short = title if len(title) <= 40 else title[:37] + "..."
        return Outcome((_a("window.switch", **args),), f"Switch to {short}", confidence=confidence)
    return Outcome((_a("window.switch", name=spoken),), f"Switch to {spoken}", confidence=0.5)


_TLDS = ("com", "org", "net", "edu", "gov", "io", "co", "uk", "au", "nz", "ca", "de", "fr", "app", "dev", "ai", "info", "me", "tv", "us")


def spoken_url(words: str) -> str | None:
    """"google dot com" -> "google.com"; returns None when the words do not look like a web address."""
    tokens = words.split()
    out: list[str] = []
    symbols = {"dot": ".", "point": ".", "slash": "/", "forward slash": "/", "colon": ":", "dash": "-", "hyphen": "-", "underscore": "_", "at": "@"}
    i = 0
    while i < len(tokens):
        two = " ".join(tokens[i : i + 2])
        if two in symbols:
            out.append(symbols[two])
            i += 2
            continue
        tok = tokens[i]
        out.append(symbols.get(tok, tok))
        i += 1
    url = "".join(out)
    if "." not in url and len(tokens) >= 2 and tokens[-1] in _TLDS and all(t.isalnum() for t in tokens):
        url = ".".join(tokens)  # recognisers write "google dot com" as "google. com"
    if web_address(url) is None:  # only web pages: "file colon ..." or "ms-settings colon ..." never (review S-09)
        return None
    if url.startswith(("www.", "http://", "https://")):
        return url
    host = url.split("/")[0]
    if "." in host:
        tld = host.rsplit(".", 1)[-1]
        if tld in _TLDS or (len(tld) >= 2 and tld.isalpha() and len(host) > len(tld) + 1):
            return url
    return None


def _b_url(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    url = spoken_url(str(slots.get("url", "")))
    if url is None:
        return Outcome(reject="Not a web address")
    return _one(_a("web.go", url=url), f"Go to {url}")


def _b_search(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    query = str(slots.get("query", "")).strip()
    if not query:
        return Outcome(reject="Nothing to search for")
    return _one(_a("web.search", query=query), f"Search for {query}")


def _b_find(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    query = str(slots.get("query", "")).strip()
    if not query:
        return _keys("Find", "ctrl+f")
    return Outcome((_a("key.press", combo="ctrl+f"), _a("text.type", text=query)), f"Find {query}")


# system.volume takes percent; one Windows volume key press is 2 percent.  A bare "volume up"
# moves 10 percent so the change can be heard; "volume up 3" is three key presses.
VOLUME_STEP = 10


def _b_volume(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    direction = slots["vertical"]
    n = _count(slots, default=0)
    if slots.get("percent") and n:
        return _one(_a("system.volume", direction=direction, amount=min(100, n)), f"Volume {direction} {n} percent")
    if n:
        return _one(_a("system.volume", direction=direction, amount=min(100, 2 * n)), f"Volume {direction} {n}")
    return _one(_a("system.volume", direction=direction, amount=VOLUME_STEP), f"Volume {direction}")


def _b_repeat(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    if not ctx.last_command:
        return Outcome(reject="Nothing to repeat")
    n = _count(slots)
    return Outcome(tuple(ctx.last_command) * n, "Repeat" + _times(n))


def _b_mode(mode: str, display: str) -> Builder:
    return lambda slots, ctx: _one(_a("speech.mode", mode=mode), display)


def _b_wake(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    if ctx.current_mode != "sleep":
        return Outcome(display="Already listening")
    mode = ctx.previous_mode if ctx.previous_mode in ("auto", "command", "dictation") else "auto"
    return _one(_a("speech.mode", mode=mode), "Awake")


def _b_labels_select(button: str, count: int) -> Builder:
    def build(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
        number = _count(slots, "number")
        args: dict[str, Any] = {"number": number}
        if button != "left":
            args["button"] = button
        if count != 1:
            args["count"] = count
        verb = {("left", 1): "Click", ("left", 2): "Double-click", ("right", 1): "Right-click", ("middle", 1): "Middle-click"}.get((button, count), "Click")
        return _one(_a("ui.labels.select", **args), f"{verb} {number}")

    return build


def _b_chooser(button: str, count: int) -> Builder:
    """A numbered target after an unsure gaze click; the button and count replace the original click's."""

    def build(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
        number = _count(slots, "number")
        args: dict[str, Any] = {"number": number}
        if button != "left":
            args["button"] = button
        if count != 1:
            args["count"] = count
        verb = {("left", 1): "Choose", ("left", 2): "Double-click", ("right", 1): "Right-click", ("middle", 1): "Middle-click"}[(button, count)]
        return _one(_a("ui.chooser.select", **args), f"{verb} {number}")

    return build


# "2nd": text2num reads "one second" as "1 2nd".
_UNIT_WORDS = frozenset({"second", "2nd", "seconds", "sec", "secs", "millisecond", "milliseconds", "ms", "percent", "cent", "pixel", "pixels", "px", "times", "notch", "notches", "degree", "degrees"})


def spoken_setting_value(words: str) -> int | float | str:
    """"3" -> 3, "0.5 seconds" -> 0.5, "60 percent" -> 60, "half a second" -> 0.5; other words stay words ("off", "a bit faster").

    Only the number goes across: the settings window knows whether the current setting
    is in milliseconds or a fraction and reads "0.5" or "60" the way a person means it.
    """
    tokens = words.split()
    while len(tokens) > 1 and tokens[-1] in _UNIT_WORDS:
        tokens.pop()
        if tokens and tokens[-1] == "per":  # "per cent"
            tokens.pop()
    phrase = " ".join(tokens).rstrip("%")
    if phrase in ("half", "a half", "half a"):
        return 0.5
    if phrase == "a" and len(words.split()) > 1:  # "a second"
        return 1
    whole, _, rest = phrase.partition(" and a half")
    if rest == "" and whole != phrase and _number(whole) is not None:
        return _number(whole) + 0.5  # type: ignore[operator]
    if (n := _number(phrase)) is not None:
        return n
    if phrase in vocab.NUMBER_HOMOPHONES:  # "set it to for": a lone "for" can only be the number here
        return vocab.NUMBER_HOMOPHONES[phrase]
    return words


def _number(text: str) -> int | float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return int(value) if value.is_integer() and "." not in text else value


def _b_settings_set(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    words = str(slots.get("setting_value", "")).strip()
    if not words:
        return Outcome(reject="Set it to what?")
    value = spoken_setting_value(words)
    shown = words if isinstance(value, str) else f"{value:g}"  # not "half a 2nd": text2num's reading of "second"
    return _one(_a("ui.settings.set", value=value), f"Set to {shown}")


def _b_settings_adjust(direction: str) -> Builder:
    def build(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
        n = min(20, _count(slots))
        return Outcome(tuple(_a("ui.settings.adjust", direction=direction) for _ in range(n)), ("More" if direction == "up" else "Less") + _times(n))

    return build


def _b_menu_item(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
    item = str(slots["item"])
    return _one(_a("ui.menu.select", item=item), item[:1].upper() + item[1:])


def _b_grid_click(button: str, count: int, display: str) -> Builder:
    def build(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
        args: dict[str, Any] = {}
        if button != "left":
            args["button"] = button
        if count != 1:
            args["count"] = count
        return _one(_a("ui.grid.click", **args), display)

    return build


def _simple(name: str, display: str, **args: Any) -> Builder:
    return lambda slots, ctx: _one(_a(name, **args), display)


def _seq(display: str, *combos: str) -> Builder:
    return lambda slots, ctx: _keys(display, *combos)


def _repeated(display: str, combo: str) -> Builder:
    def build(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
        n = _count(slots)
        return _keys(display + _times(n), combo, repeat=n)

    return build


# --------------------------------------------------------------------------- the commands

G = frozenset({"global"})


def _c(
    id: str,
    category: str,
    sentences: list[Any] | tuple[Any, ...],
    build: Builder,
    help: list[tuple[str, str] | tuple[str, str, str]] | None = None,
    **kw: Any,
) -> CommandSpec:
    rows = tuple(HelpRow(*row) for row in (help or []))
    return CommandSpec(id=id, category=category, sentences=tuple(sentences), build=build, help=rows, **kw)


def build_commands() -> list[CommandSpec]:
    """Every built-in command.  Order does not matter; the matcher ranks matches."""
    M, K, E, D, W, B, S, L, AM, SW = (
        "Mouse",
        "Keys",
        "Text editing",
        "Dictation",
        "Windows and apps",
        "Web browser",
        "Sound and media",
        "Sightline",
        "Action menu",
        "Settings window",
    )
    cmds: list[CommandSpec] = [
        # ------------------------------------------------------------------ mouse
        # A name may follow a bare "click" ("click save"), so it waits a little longer for the end of
        # speech.  "tap" and "click it" cannot grow, so they end as soon as the pause starts.
        _c(
            "mouse.click",
            M,
            ["[left] click"],
            _b_click("left", 1, "Click"),
            [("click", "Left click where you were looking", "click")],
            semantic=("press the mouse button", "click on this", "click the mouse"),
            wait_after=True,
            chainable=False,
        ),
        _c(
            "mouse.tap",
            M,
            ["tap", "[left] click (it|that|this|here|there)"],
            _b_click("left", 1, "Click"),
            [("tap", "Click straight away, without waiting to hear a name", "tap")],
            chainable=False,
        ),
        _c(
            "mouse.double_click",
            M,
            ["double click"],
            _b_click("left", 2, "Double-click"),
            [("double click", "Double-click where you were looking")],
            semantic=("click twice", "open this item"),
            wait_after=True,
            chainable=False,
        ),
        _c("mouse.double_tap", M, ["double tap", "double click (it|that|this|here|there)"], _b_click("left", 2, "Double-click"), [("double tap", "Double-click straight away")], chainable=False),
        _c("mouse.triple_click", M, ["triple click <it>"], _b_click("left", 3, "Triple-click"), [("triple click", "Triple-click, usually selects a whole line")]),
        _c(
            "mouse.right_click",
            M,
            ["right click"],
            _b_click("right", 1, "Right-click"),
            [("right click", "Right-click where you were looking")],
            semantic=("open the context menu", "show the right click menu"),
            wait_after=True,
            chainable=False,
        ),
        _c("mouse.right_tap", M, ["right tap", "right click (it|that|this|here|there)"], _b_click("right", 1, "Right-click"), chainable=False),
        _c("mouse.middle_click", M, ["middle click <it>"], _b_click("middle", 1, "Middle-click"), [("middle click", "Middle-click where the pointer is")], wait_after=True, chainable=False),
        _c(
            "element.click",
            M,
            ["[left] click [on] [the] {element_name}"],
            _b_click_element("left", 1, "Click"),
            [("click <name>", "Click the button or link with that name", "click save")],
            chainable=False,
        ),
        _c("element.double_click", M, ["double click [on] [the] {element_name}"], _b_click_element("left", 2, "Double-click"), [("double click <name>", "Double-click something by name")], chainable=False),
        _c("element.right_click", M, ["right click [on] [the] {element_name}"], _b_click_element("right", 1, "Right-click"), [("right click <name>", "Right-click something by name")], chainable=False),
        _c(
            "mouse.drag",
            M,
            ["drag <it>", "start [a] drag", "start dragging", "hold [the] (click|mouse [button])", "mouse down"],
            _simple("mouse.down", "Drag started", button="left"),
            [("drag", "Hold the left button down to drag; say \"drop\" to let go")],
            semantic=("pick this up", "grab this and move it"),
        ),
        _c(
            "mouse.drop",
            M,
            ["drop <it>", "release [the] (click|mouse [button])", "let go", "mouse up", "stop dragging"],
            _simple("mouse.up", "Dropped", button="left"),
            [("drop", "Let go of the mouse button after a drag")],
            semantic=("put it down here", "let go of the mouse"),
        ),
        _c(
            "mouse.scroll",
            M,
            ["scroll [the page] {direction} <times>", "(wheel|page scroll) {direction} <times>", ("scroll {direction} (a bit|a little|slightly)", {"small": True})],
            _b_scroll,
            [("scroll <up|down|left|right> [number]", "Scroll where the pointer is", "scroll down 3")],
            semantic=(
                ("move the page downwards", {"direction": "down"}),
                ("further down please", {"direction": "down"}),
                ("see what is below", {"direction": "down"}),
                ("lower down the page", {"direction": "down"}),
                ("move the page upwards", {"direction": "up"}),
                ("further up please", {"direction": "up"}),
                ("see what is above", {"direction": "up"}),
                ("go up the page", {"direction": "up"}),
                ("go down the page", {"direction": "down"}),
                ("higher up the page", {"direction": "up"}),
                ("down a little", {"direction": "down", "small": True}),
                ("up a little", {"direction": "up", "small": True}),
            ),
        ),
        _c(
            "mouse.scroll_start",
            M,
            [
                "(keep|start|auto) scrolling {direction}",
                "auto scroll {direction}",
                "scroll {direction} (continuously|continually|non stop|all the way)",
                ("scroll {direction} slowly", {"speed": 0.5}),
                ("scroll {direction} (fast|quickly)", {"speed": 2.0}),
            ],
            _b_scroll_start,
            [("keep scrolling <up|down>", "Scroll until you say \"stop\"", "keep scrolling down")],
        ),
        _c("mouse.scroll_speed", M, [("scroll faster", {"faster": True}), ("scroll slower", {"faster": False}), ("(faster|speed up)", {"faster": True}), ("(slower|slow down)", {"faster": False})], _b_scroll_speed, [("scroll faster / scroll slower", "Change the speed of continuous scrolling")]),
        _c("mouse.scroll_stop", M, ["stop scrolling", "stop scroll", "scroll stop"], _b_scroll_stop, [("stop scrolling", "Stop continuous scrolling")]),
        _c(
            "mouse.move_to",
            M,
            ["(move|put|send) [the] (mouse|pointer|cursor) [to] [the] {place} [of the screen]", "(mouse|pointer) [to] [the] {place}", ("center [the] (mouse|pointer)", {"place": "center"})],
            _b_move_to,
            [("move mouse to the <center|top|bottom|left|right>", "Jump the pointer to part of the screen")],
            semantic=("put the mouse in the middle of the screen",),
        ),
        _c("mouse.move_by", M, ["(move|shift) [the] (mouse|pointer|cursor) {direction} {count} [pixels]"], _b_move_by, [("move mouse <direction> <number>", "Move the pointer by that many pixels", "move mouse left 50")]),
        _c(
            "pointer.nudge",
            M,
            ["nudge [the] [(mouse|pointer)] {direction} <times>", "nudge {direction} <times>"],
            _b_nudge,
            [("nudge <direction> [number]", "Move the pointer a tiny step", "nudge left")],
            semantic=("move the pointer a tiny bit",),
        ),
        # ------------------------------------------------------------------ keys
        _c(
            "key.press",
            K,
            ["(press|hit) {key_words}"],
            _b_press,
            [("press <keys>", "Press keys and shortcuts; letters can be said plainly, with NATO words or the Talon alphabet", "press control shift t")],
        ),
        _c(
            "key.combo",
            K,
            ["{modifier:mod1} [{modifier:mod2}] [{modifier:mod3}] {key} <times>"],
            _b_combo,
            [("<modifiers> <key>", "Shortcuts without saying press", "control c")],
        ),
        _c(
            "key.bare",
            K,
            ["{bare_key} <times>"],
            _b_bare_key,
            [("<key> [number] [times]", "Enter, tab, escape, backspace, delete, space, home, end, page up, page down and the arrow keys", "tab 3 times")],
        ),
        _c("key.hold", K, ["hold [down] {modifier}", "hold [down] [the] key {key}"], lambda s, c: _one(_a("key.hold", key=s.get("modifier") or s.get("key")), f"Holding {_pretty_combo(s.get('modifier') or s.get('key'))}"), [("hold <key>", "Keep a key held down", "hold shift")]),
        _c("key.release", K, ["release {modifier}", "release [the] key {key}", "let go of {modifier}"], lambda s, c: _one(_a("key.release", key=s.get("modifier") or s.get("key")), f"Released {_pretty_combo(s.get('modifier') or s.get('key'))}"), [("release <key>", "Let go of a held key")]),
        _c(
            "key.release_all",
            K,
            ["release (all|everything|all keys)", "let go of everything"],
            _simple("key.release_all", "Released everything"),
            [("release all", "Let go of every held key and button")],
        ),
        # ------------------------------------------------------------------ text editing
        _c("edit.select_all", E, ["select all", "select everything", "highlight everything"], _seq("Select all", "ctrl+a"), [("select all", "Select everything")], semantic=("mark all the text",)),
        _c("edit.select_word", E, ["select [the|this] word"], _seq("Select word", "ctrl+left", "ctrl+shift+right"), [("select word", "Select the word at the cursor")]),
        _c("edit.select_line", E, ["select [the|this] line"], _seq("Select line", "home", "shift+end"), [("select line", "Select the line the cursor is on")], semantic=("highlight this line",)),
        _c("edit.select_word_left", E, ["select [the] (word left|previous word|last word) <times>"], _repeated("Select word left", "ctrl+shift+left"), [("select word left/right [number]", "Extend the selection by words")]),
        _c("edit.select_word_right", E, ["select [the] (word right|next word) <times>"], _repeated("Select word right", "ctrl+shift+right")),
        _c("edit.select_left", E, ["select left <times>"], _repeated("Select left", "shift+left"), [("select <left|right|up|down> [number]", "Extend the selection")]),
        _c("edit.select_right", E, ["select right <times>"], _repeated("Select right", "shift+right")),
        _c("edit.select_up", E, ["select up <times>"], _repeated("Select up", "shift+up")),
        _c("edit.select_down", E, ["select down <times>"], _repeated("Select down", "shift+down")),
        _c("edit.select_line_end", E, ["select to [the] end of [the] line"], _seq("Select to end of line", "shift+end"), [("select to end of line", "Select from the cursor to the end of the line")]),
        _c("edit.select_line_start", E, ["select to [the] (start|beginning) of [the] line"], _seq("Select to start of line", "shift+home")),
        _c("edit.select_doc_start", E, ["select to [the] (top|start|beginning) of [the] (document|page|file)"], _seq("Select to top", "ctrl+shift+home")),
        _c("edit.select_doc_end", E, ["select to [the] (bottom|end) of [the] (document|page|file)"], _seq("Select to bottom", "ctrl+shift+end")),
        _c(
            "edit.delete_word",
            E,
            ["delete [the] (word|previous word|last word) [left] <times>", "delete {count} words", "(clear|remove) [the] (word|last word)"],
            _repeated("Delete word", "ctrl+backspace"),
            [("delete word [number]", "Delete the word before the cursor", "delete word")],
            semantic=("get rid of the word before the cursor",),
            risky=True,
        ),
        _c("edit.delete_word_right", E, ["delete [the] (next word|word right) <times>"], _repeated("Delete next word", "ctrl+delete"), [("delete next word", "Delete the word after the cursor")]),
        _c("edit.delete_line", E, ["delete [the|this] line", "(clear|remove) [the|this] line"], _seq("Delete line", "home", "shift+end", "backspace"), [("delete line", "Delete the text on this line")], semantic=("erase this whole line",), risky=True),
        _c("edit.delete_selection", E, ["delete (that|this|the selection|selection|it)"], _seq("Delete", "delete"), [("delete that", "Delete the selected text")]),
        _c(
            "edit.line_end",
            E,
            ["[go to] [the] end of [the] line", "line end", "go to [the] line end"],
            _seq("End of line", "end"),
            [("go to end of line", "Move the cursor to the end of the line")],
            semantic=("move to the end of the line",),
        ),
        _c(
            "edit.line_start",
            E,
            ["[go to] [the] (start|beginning) of [the] line", "line start", "go to [the] line start"],
            _seq("Start of line", "home"),
            [("go to start of line", "Move the cursor to the start of the line")],
            semantic=("move to the beginning of the line",),
        ),
        _c(
            "edit.doc_start",
            E,
            ["go to [the] (top|start|beginning) [of [the] (document|page|file)]", "(top|start|beginning) of [the] (document|page|file)"],
            _seq("Go to top", "ctrl+home"),
            [("go to top", "Move to the start of the document or page")],
            semantic=("all the way up to the start", "scroll all the way to the top", "back to the very top", "top of the page please"),
        ),
        _c(
            "edit.doc_end",
            E,
            ["go to [the] (bottom|end) [of [the] (document|page|file)]", "(bottom|end) of [the] (document|page|file)"],
            _seq("Go to bottom", "ctrl+end"),
            [("go to bottom", "Move to the end of the document or page")],
            semantic=("jump to the bottom of the page", "take me to the end", "all the way down to the end"),
        ),
        _c("edit.word_left", E, ["(word left|previous word|back [a] word) <times>"], _repeated("Word left", "ctrl+left"), [("word left/right [number]", "Move the cursor by words")]),
        _c("edit.word_right", E, ["(word right|next word|forward [a] word) <times>"], _repeated("Word right", "ctrl+right")),
        # "undo" is the app's own undo (Ctrl+Z); "undo that" asks Sightline to reverse its last action.
        _c("edit.undo", E, ["undo <times>", "undo {count}"], _repeated("Undo", "ctrl+z"), [("undo [number]", "Undo in the app (Ctrl+Z)", "undo 3 times")], semantic=("reverse my last edit", "revert that change", "undo what i just did")),
        _c(
            "action.undo",
            E,
            ["undo (that|it|this)", "undo [the] last (action|command)"],
            _simple("action.undo", "Undo that"),
            [("undo that", "Reverse the last thing Sightline did, or say why it can't", "undo that")],
        ),
        _c("edit.redo", E, ["redo <it> <times>", "redo {count}"], _repeated("Redo", "ctrl+y"), [("redo [number]", "Redo")], semantic=("do it again after undo",)),
        _c("edit.copy", E, ["copy <it>", "copy [the] (text|selection)"], _seq("Copy", "ctrl+c"), [("copy", "Copy the selection")], semantic=("put this on the clipboard", "copy the selected text")),
        _c("edit.cut", E, ["cut <it>", "cut [the] (text|selection)"], _seq("Cut", "ctrl+x"), [("cut", "Cut the selection")], semantic=("cut this to the clipboard",)),
        _c("edit.paste", E, ["paste <it>", "paste [the] (text|clipboard)"], _seq("Paste", "ctrl+v"), [("paste", "Paste")], semantic=("insert the clipboard contents", "paste what i copied", "drop the clipboard text here")),
        _c("edit.paste_plain", E, ["paste (as plain text|plain text|without formatting)"], _seq("Paste as plain text", "ctrl+shift+v"), [("paste as plain text", "Paste without formatting")]),
        _c("edit.copy_all", E, ["copy (all|everything)"], _seq("Copy all", "ctrl+a", "ctrl+c"), [("copy all", "Select everything and copy it")]),
        _c("edit.save", E, ["save <it>", "save [the] (file|document|changes)"], _seq("Save", "ctrl+s"), [("save", "Save")], semantic=("store my changes", "keep my changes")),
        _c("edit.save_as", E, ["save as"], _seq("Save as", "ctrl+shift+s"), [("save as", "Save under a new name")]),
        _c(
            "edit.find",
            E,
            ["find", "find (on|in) [the] (page|document)", "search (this|the) (page|document)", "find {query}"],
            _b_find,
            [("find [words]", "Open Find, and search for the words if you say them", "find invoice")],
            free_text=True,
            semantic=("look for something on this page",),
        ),
        _c("edit.bold", E, ["bold <it>", "make (it|that|this) bold"], _seq("Bold", "ctrl+b"), [("bold / italic / underline", "Format the selected text")]),
        _c("edit.italic", E, ["(italic|italics|italicise|italicize) <it>", "make (it|that|this) italic"], _seq("Italic", "ctrl+i")),
        _c("edit.underline", E, ["underline <it>"], _seq("Underline", "ctrl+u")),
        _c("edit.new_document", E, ["new (document|file)"], _seq("New document", "ctrl+n"), [("new document", "Create a new document")]),
        _c("edit.print", E, ["print (this|[the] (page|document))"], _seq("Print", "ctrl+p"), [("print this", "Open the print dialog")]),
        _c(
            "text.select",
            E,
            ["select {phrase}"],
            lambda s, c: _one(_a("text.select", phrase=str(s["phrase"]).strip()), f"Select {str(s['phrase']).strip()}"),
            [("select <words>", "Select words in the text field by saying them", "select quick brown")],
            chainable=False,
            free_text=True,
            fuzzy=False,
        ),
        _c(
            "text.select_range",
            E,
            ["select from {phrase:start} (to|until|through) {phrase:end}"],
            lambda s, c: _one(_a("text.select_range", start=str(s["start"]).strip(), end=str(s["end"]).strip()), f"Select from {str(s['start']).strip()} to {str(s['end']).strip()}"),
            [("select from <words> to <words>", "Select everything between two phrases", "select from quick to lazy")],
            chainable=False,
            free_text=True,
            fuzzy=False,
        ),
        _c(
            "text.caret",
            E,
            ["(move|go|cursor|put [the] cursor|move [the] cursor) {caret_where:where} {phrase}"],
            lambda s, c: _one(_a("text.caret", where=s["where"], phrase=str(s["phrase"]).strip()), f"Move {s['where']} {str(s['phrase']).strip()}"),
            [("move before/after <words>", "Put the cursor next to words in the text field", "move after brown")],
            chainable=False,
            free_text=True,
            fuzzy=False,
        ),
        _c(
            "login.fill",
            E,
            ["fill [in] [the|my] (login|log in|sign in|password|details)", "(log|sign) me in", "auto fill [the|my] [(login|password)]", "autofill [the|my] [(login|password)]"],
            _simple("login.fill", "Fill login"),
            [("fill login", "Let your password manager fill in the sign-in", "fill login")],
        ),
        # ------------------------------------------------------------------ dictation
        _c(
            "text.type",
            D,
            ["type {text}", "dictate {text}"],
            _b_type,
            [("type <words>", "Type the words, even when they sound like a command", "type see you tomorrow")],
            chainable=False,
            free_text=True,
            fuzzy=False,
        ),
        _c(
            "text.spell",
            D,
            ["spell [out] {letters}"],
            _b_spell,
            [("spell <letters>", "Spell letter by letter: plain letters, NATO (alpha, bravo) or Talon (air, bat, cap); say \"capital\" before a letter for upper case", "spell alpha bravo charlie")],
            chainable=False,
            fuzzy=False,
        ),
        _c(
            "text.format",
            D,
            ["{formatter} {text}"],
            _b_format,
            [("<format> <words>", "snake, camel, hammer, kebab, constant, dotted, smash, all caps, cap, title, say", "snake user name")],
            chainable=False,
            free_text=True,
            fuzzy=False,
        ),
        # The service adds the words (or the last dictated phrase) to vocabulary.txt and the recogniser's hotwords.
        _c(
            "speech.vocabulary",
            D,
            ["add [that|this|it] to [the|my] vocabulary", "add {phrase:words} to [the|my] vocabulary"],
            lambda s, c: Outcome(display="Add to vocabulary"),
            [("add to vocabulary", "Teach Sightline the word you just dictated or spelled, or say add <word> to vocabulary")],
            contexts=frozenset({"global", "dictation"}),
            chainable=False,
            free_text=True,
        ),
        _c("text.new_line", D, ["new line", "newline", "next line"], _b_text("\n", "New line"), [("new line", "Start a new line")]),
        _c("text.new_paragraph", D, ["new paragraph"], _b_text("\n\n", "New paragraph"), [("new paragraph", "Start a new paragraph")]),
        _c(
            "speech.scratch",
            D,
            ["scratch [that]", "strike that", "nope that", "undo dictation", "delete what i (said|typed)"],
            _simple("speech.scratch", "Scratched"),
            [("scratch that", "Remove the words you just dictated")],
            contexts=frozenset({"global", "dictation"}),
            semantic=("undo my last dictation", "erase what i just dictated"),
            risky=True,
        ),
        # ------------------------------------------------------------------ windows and apps
        _c(
            "app.open",
            W,
            ["(open|launch|start|run) {app_name}"],
            _b_app,
            [("open <app>", "Open an app from the Start menu", "open notepad")],
            chainable=False,
            wait_after=True,
        ),
        _c(
            "window.switch",
            W,
            ["(switch|change) to {window_name}", "(focus|bring up) {window_name}"],
            _b_window,
            [("switch to <window>", "Bring a window to the front, by app or title", "switch to chrome")],
            chainable=False,
            wait_after=True,
        ),
        _c(
            "window.close",
            W,
            ["close <window>", "close [the|this] (app|application|program)", "quit [the|this] (app|application|program)"],
            _simple("window.close", "Close window"),
            [("close window", "Close the window in front")],
            semantic=("exit this application", "end this program", "kill this window", "get rid of this app", "make this window go away"),
            risky=True,
        ),
        _c(
            "window.minimize",
            W,
            ["<minimise> <window>", "<minimise>", "hide <window>"],
            _simple("window.minimize", "Minimise window"),
            [("minimise window", "Minimise the window in front")],
            semantic=("tuck this window away", "shrink the window down", "put the window on the taskbar"),
        ),
        _c(
            "window.maximize",
            W,
            ["<maximise> <window>", "<maximise>", "make <window> (full screen|bigger)"],
            _simple("window.maximize", "Maximise window"),
            [("maximise window", "Make the window fill the screen")],
            semantic=("enlarge the window", "full size window"),
        ),
        _c(
            "window.restore",
            W,
            ["restore <window>", "(unmaximise|unmaximize) <window>"],
            _simple("window.restore", "Restore window"),
            [("restore window", "Return the window to its normal size")],
        ),
        _c(
            "window.snap",
            W,
            ["snap [the|this] [window] [to the] {side}", "(move|put) <window> to the {side}"],
            lambda s, c: _one(_a("window.snap", side=s["side"]), f"Snap window {s['side']}"),
            [("snap <left|right|up|down>", "Snap the window to half of the screen", "snap left")],
        ),
        _c(
            "window.next",
            W,
            ["next window", "switch (window|windows)", "other window"],
            _simple("window.next", "Next window"),
            [("next window / previous window", "Cycle through open windows")],
            semantic=("go to the other window", "cycle windows"),
        ),
        _c("window.previous", W, ["(previous|last) window"], _simple("window.previous", "Previous window")),
        _c(
            "desktop.show",
            W,
            ["show [the] desktop", "go to [the] desktop", "<minimise> (all|everything) [windows]"],
            _simple("desktop.show", "Show desktop"),
            [("show desktop", "Minimise everything and show the desktop")],
            semantic=("hide all the windows",),
        ),
        _c("windows.start_menu", W, ["[open] [the] start menu", "open start"], _seq("Start menu", "win"), [("start menu", "Open the Start menu")]),
        _c("windows.task_view", W, ["task view", "show [all] [the] windows"], _seq("Task view", "win+tab"), [("task view", "Show all open windows")]),
        _c("windows.settings", W, ["[open] windows settings"], _seq("Windows settings", "win+i"), [("windows settings", "Open Windows Settings")]),
        _c("windows.file_explorer", W, ["[open] file explorer", "[open] my files"], _seq("File Explorer", "win+e"), [("file explorer", "Open File Explorer")]),
        # ------------------------------------------------------------------ web browser
        _c(
            "browser.back",
            B,
            ["go back <times>", "(page|browser) back <times>", "back a page"],
            _repeated("Go back", "alt+left"),
            [("go back / go forward", "Previous or next page in the browser")],
            semantic=("return to the page before", "back to the last page", "previous web page"),
        ),
        _c("browser.forward", B, ["go forward <times>", "(page|browser) forward <times>", "forward a page"], _repeated("Go forward", "alt+right")),
        _c("browser.new_tab", B, ["new tab", "open [a] new tab"], _seq("New tab", "ctrl+t"), [("new tab / close tab", "Open or close a browser tab")], semantic=("open another tab",)),
        _c("browser.close_tab", B, ["close [the|this] tab"], _seq("Close tab", "ctrl+w"), semantic=("shut this tab",), risky=True),
        _c("browser.next_tab", B, ["next tab <times>", "tab right"], _repeated("Next tab", "ctrl+tab"), [("next tab / previous tab", "Move between tabs")]),
        _c("browser.previous_tab", B, ["(previous|last) tab <times>", "tab left"], _repeated("Previous tab", "ctrl+shift+tab")),
        _c("browser.reopen_tab", B, ["reopen [the] [last] [closed] tab", "restore [the] [last] tab", "undo close tab"], _seq("Reopen tab", "ctrl+shift+t"), [("reopen tab", "Bring back the tab you just closed")]),
        _c(
            "browser.go_to_tab",
            B,
            ["(go to|switch to) tab {count}"],
            lambda s, c: _keys(f"Tab {_count(s)}", f"ctrl+{min(9, _count(s))}"),
            [("go to tab <number>", "Jump to a tab by position", "go to tab 2")],
        ),
        _c("browser.refresh", B, ["(refresh|reload) [the] [page]"], _seq("Refresh", "f5"), [("refresh", "Reload the page")], semantic=("update the page",)),
        _c("browser.address_bar", B, ["[go to] [the] address bar", "focus [the] address bar"], _seq("Address bar", "ctrl+l"), [("address bar", "Put the cursor in the address bar")]),
        _c(
            "web.go",
            B,
            ["go to {url}", "(open|visit) [the] [(website|site|page)] {url}"],
            _b_url,
            [("go to <web address>", "Open a website", "go to wikipedia dot org")],
            chainable=False,
        ),
        _c(
            "web.search",
            B,
            ["search (for|the web for|google for) {query}", "google {query}"],
            _b_search,
            [("search for <words>", "Search the web", "search for weather in london")],
            chainable=False,
            free_text=True,
        ),
        _c("browser.bookmark", B, ["bookmark (this|[this] page|it)"], _seq("Bookmark", "ctrl+d"), [("bookmark this", "Bookmark the page")]),
        _c("browser.zoom_in", B, ["zoom in"], _seq("Zoom in", "ctrl+="), [("zoom in / zoom out / reset zoom", "Make the page bigger or smaller")], semantic=("make the text bigger",)),
        _c("browser.zoom_out", B, ["zoom out"], _seq("Zoom out", "ctrl+-"), semantic=("make the text smaller",)),
        _c("browser.zoom_reset", B, ["(reset|normal) zoom", "zoom (reset|normal)"], _seq("Reset zoom", "ctrl+0")),
        # ------------------------------------------------------------------ sound and media
        _c(
            "system.volume",
            S,
            ["[turn] [the] volume {vertical} <times>", "turn [the] volume {vertical}", ("[turn] [the] volume {vertical} [by] {count} percent", {"percent": True})],
            _b_volume,
            [("volume <up|down> [number]", "Change the volume: a number of steps, or say percent", "volume up 3")],
        ),
        _c("system.louder", S, ["louder", "turn it up"], _simple("system.volume", "Volume up", direction="up", amount=VOLUME_STEP), semantic=("raise the volume", "more volume", "increase the sound level", "sound up please", "turn the audio up")),
        _c("system.quieter", S, ["(quieter|softer)", "turn it down"], _simple("system.volume", "Volume down", direction="down", amount=VOLUME_STEP), semantic=("lower the volume", "less volume", "reduce the sound level", "sound down please", "turn the audio down")),
        _c("system.mute", S, ["(mute|unmute) [the] [(sound|volume|audio|speakers|computer)]"], _simple("system.volume", "Mute on or off", direction="mute"), [("mute / unmute", "Turn the sound off or on")], semantic=("no sound", "switch the audio off", "mute the sound")),
        _c(
            "system.play_pause",
            S,
            ["play", "pause", "play pause", "(play|pause|resume) [the] (music|video|media|song)", "resume playing"],
            _simple("system.media", "Play or pause", key="play_pause"),
            [("play / pause", "Play or pause music and video")],
            semantic=("start the music", "pause the video"),
        ),
        _c("system.next_track", S, ["next (track|song)", "skip (track|song)"], _simple("system.media", "Next track", key="next"), [("next track / previous track", "Skip songs")], semantic=("next tune", "play the following track", "skip to the next one")),
        _c("system.previous_track", S, ["previous (track|song)", "last (track|song)"], _simple("system.media", "Previous track", key="previous"), semantic=("play the previous song", "go back a song", "the song before this one")),
        _c("system.stop_media", S, ["stop [the] (music|video|media|playback)"], _simple("system.media", "Stop playback", key="stop"), [("stop music", "Stop playback")]),
        # ------------------------------------------------------------------ Sightline
        _c(
            "pointer.pause",
            L,
            ["pause (eyes|eye control|eye tracking|tracking|[the] pointer|[the] mouse)", "(eyes|eye control|tracking) off", "stop tracking", "freeze [the] (pointer|mouse)"],
            _simple("pointer.pause", "Eye control paused"),
            [("pause eyes / resume eyes", "Stop or start the pointer following your eyes")],
            semantic=("stop the pointer moving with my eyes", "disable gaze tracking"),
        ),
        _c(
            "pointer.resume",
            L,
            ["resume (eyes|eye control|eye tracking|tracking|[the] pointer|[the] mouse)", "(eyes|eye control|tracking) on", "start tracking", "unfreeze [the] (pointer|mouse)"],
            _simple("pointer.resume", "Eye control on"),
            semantic=("let my eyes move the pointer again", "enable gaze tracking"),
        ),
        _c("pointer.toggle", L, ["toggle (eyes|eye control|tracking)"], _simple("pointer.toggle", "Eye control on or off")),
        _c(
            "pointer.restart",
            L,
            ["restart [the] (tracking|eye tracking|head tracking|tracker)", "reset [the] (tracking|eye tracking|tracker)"],
            _simple("pointer.restart", "Restarting tracking"),
            [("restart tracking", "Start eye and head tracking again if it seems stuck")],
            semantic=("the tracking is stuck", "fix the eye tracking", "the tracking has frozen"),
        ),
        _c("pointer.mode_gaze", L, ["(eyes|eye|gaze) only", "(gaze|eye) mode", "use [my] eyes only"], _simple("pointer.mode", "Eyes only", mode="gaze"), [("eyes only / head only / eyes and head", "Choose what moves the pointer")]),
        _c("pointer.mode_head", L, ["head only", "head mode", "head (mouse|pointer)", "use [my] head only"], _simple("pointer.mode", "Head only", mode="head")),
        _c("pointer.mode_hybrid", L, ["eyes and head", "head and eyes", "hybrid (mode|pointer)", "normal pointer"], _simple("pointer.mode", "Eyes and head", mode="hybrid")),
        _c(
            "pointer.recenter",
            L,
            ["recenter [the] [(pointer|head|mouse)]", "(center|centre) [my|the] head", "reset [my|the] head [position]"],
            _simple("pointer.recenter", "Recentred"),
            [("recenter", "Treat your current head position as straight ahead")],
            semantic=("reset where my head is",),
        ),
        _c("dwell.on", L, ["dwell on", "(turn on|enable|start) dwell [clicking]", "dwell clicking on"], _simple("dwell.on", "Dwell clicking on"), [("dwell on / dwell off", "Click by holding your gaze still")], semantic=("click when i stare at something",)),
        _c("dwell.off", L, ["dwell off", "(turn off|disable|stop) dwell [clicking]", "dwell clicking off"], _simple("dwell.off", "Dwell clicking off")),
        _c("dwell.toggle", L, ["toggle dwell"], _simple("dwell.toggle", "Dwell clicking on or off")),
        _c(
            "dwell.click_type",
            L,
            ["dwell {click_type} [click]", "(set|change) [the] (dwell|click) type to {click_type} [click]", "{click_type} click mode"],
            lambda s, c: _one(_a("dwell.click_type", type=s["click_type"]), f"Dwell click: {s['click_type']}"),
            [("dwell <left|right|middle|double|drag>", "What the next dwell does", "dwell right click")],
        ),
        _c("ui.labels.show", L, ["show [the] (numbers|labels|click numbers)", "numbers", "labels", "number everything"], _simple("ui.labels.show", "Showing numbers"), [("show numbers", "Number everything clickable; then say a number")], semantic=("number the links and buttons",)),
        _c("ui.labels.hide", L, ["hide [the] (numbers|labels)", "(numbers|labels) off"], _simple("ui.labels.hide", "Numbers hidden"), contexts=frozenset({"global", "labels"})),
        _c("ui.grid.show", L, ["show [the] grid", "[mouse] grid", "grid on"], _simple("ui.grid.show", "Showing grid"), [("show grid", "Split the screen into 9 numbered squares to click precisely")], semantic=("divide the screen into squares",)),
        _c("ui.grid.hide", L, ["hide [the] grid", "close [the] grid", "grid off"], _simple("ui.grid.hide", "Grid hidden"), contexts=frozenset({"global", "grid"})),
        _c("ui.zoom.show", L, ["zoom", "show zoom", "zoom lens", "magnify here", "zoom (here|there|on this)"], _simple("ui.zoom.show", "Zoom lens"), [("zoom", "Magnify the area at the pointer; your next click lands there")], semantic=("magnify where i am looking",)),
        _c("ui.zoom.hide", L, ["(hide|close) zoom", "zoom off"], _simple("ui.zoom.hide", "Zoom closed")),
        _c("keyboard.show", L, ["show [the] keyboard", "open [the] [on screen] keyboard", "keyboard [on]"], _simple("keyboard.show", "Keyboard shown"), [("show keyboard / hide keyboard", "The Windows on-screen keyboard, which you can type on with your eyes")], semantic=("display the typing keyboard", "i want to type with the keyboard")),
        _c("keyboard.hide", L, ["hide [the] keyboard", "close [the] [on screen] keyboard", "keyboard off"], _simple("keyboard.hide", "Keyboard hidden")),
        _c("keyboard.toggle", L, ["toggle [the] [on screen] keyboard"], _simple("keyboard.toggle", "Keyboard on or off")),
        _c("magnifier.toggle", L, ["magnifier", "(open|start|show|close|stop|hide|toggle) [the] magnifier", "magnifier (on|off)"], _simple("magnifier.toggle", "Magnifier on or off"), [("magnifier", "Turn Windows Magnifier on or off")], semantic=("make everything on the screen bigger", "turn on the screen magnifier", "enlarge the whole screen")),
        _c("magnifier.zoom_in", L, ["magnify more", "magnifier (in|more|zoom in)"], _simple("magnifier.zoom", "Magnify more", direction="in"), [("magnify more / magnify less", "Change the Magnifier zoom")]),
        _c("magnifier.zoom_out", L, ["magnify less", "magnifier (out|less|zoom out)"], _simple("magnifier.zoom", "Magnify less", direction="out")),
        _c(
            "ui.help.show",
            L,
            ["what can i say", "show [me] [the] commands", "[show] help", "list [the] commands", "what are the commands"],
            _simple("ui.help.show", "What you can say"),
            [("what can I say", "Show this list")],
            semantic=("which commands are there", "what are my options", "what can i do here"),
        ),
        _c("ui.help.hide", L, ["(hide|close) [the] (help|commands)"], _simple("ui.help.hide", "Help closed"), contexts=frozenset({"global", "help"})),
        _c("ui.settings.show", L, ["[open|show] [sightline] settings", "settings"], _simple("ui.settings.show", "Settings"), [("settings", "Open Sightline settings")], semantic=("change the options",)),
        _c(
            "ui.calibrate",
            L,
            ["(calibrate|recalibrate) [my] [(gaze|eye tracking|pointer)]", "calibration"],
            _simple("ui.calibrate", "Calibrate", kind="gaze"),
            [("calibrate", "Calibrate where you are looking (also calibrate eyes, calibrate head, full calibration)", "calibrate")],
            semantic=("the pointer is not where i am looking",),
            wait_after=True,  # "calibrate" often goes on ("calibrate head"); starting the wrong calibration costs more than a short wait
        ),
        _c("ui.calibrate_eyes", L, ["(calibrate|recalibrate) [my] (eyes|eye|blinks|blinking|winks|eye clicks)"], _simple("ui.calibrate", "Calibrate eye clicks", kind="eyes"), [("calibrate eyes", "Calibrate blinks and winks")]),
        _c("ui.calibrate_head", L, ["(calibrate|recalibrate) [my] head [(pointer|tracking|movement|movements)]"], _simple("ui.calibrate", "Calibrate head", kind="head"), [("calibrate head", "Calibrate how your head moves the pointer")]),
        _c("ui.calibrate_gestures", L, ["(calibrate|recalibrate) [my] [face] gestures", "[face] gesture (trainer|training)"], _simple("ui.calibrate", "Calibrate face gestures", kind="gestures")),
        _c("ui.calibrate_full", L, ["full calibration", "calibrate everything"], _simple("ui.calibrate", "Full calibration", kind="full")),
        _c(
            "ui.calibrate_drift",
            L,
            ["fix [the] (pointer|mouse|cursor)", "(fix|correct) [the] drift", "pointer fix"],
            _simple("ui.calibrate", "Fix pointer", kind="drift"),
            [("fix pointer", "Look at one dot for a moment to put the pointer back where you look", "fix pointer")],
        ),
        _c(
            "ui.practice.show",
            L,
            ["(practice|practise) [room]", "(open|show|start) [the] (practice|practise) [room]"],
            _simple("ui.practice.show", "Practice room"),
            [("practice room", "Large targets for practising clicks and voice commands", "practice room")],
        ),
        _c(
            "pointer.reading_on",
            L,
            ["reading (mode|scroll) [on]", "(start|turn on|enable) [the] reading (mode|scroll)"],
            _simple("pointer.reading_scroll", "Reading mode on", on=True),
            [("reading mode / reading mode off", "Scroll by looking: low on the page scrolls down, high scrolls up", "reading mode")],
            wait_after=True,  # "reading mode ... off" must not turn it on first
        ),
        _c("pointer.reading_off", L, ["reading (mode|scroll) off", "(stop|end|exit|turn off|disable) [the] reading (mode|scroll)"], _simple("pointer.reading_scroll", "Reading mode off", on=False)),
        _c(
            "ui.menu.show",
            AM,
            ["[action] menu", "(show|open) [the] [action] menu", "menu here"],
            _simple("ui.menu.show", "Action menu"),
            [("menu", "Open the action menu where you are looking: right click, drag, scroll and more", "menu")],
        ),
        _c("ui.menu.hide", AM, ["(hide|close) [the] [action] menu", "menu off"], _simple("ui.menu.hide", "Menu closed"), [("close menu", "Close the action menu")], contexts=frozenset({"global", "menu"})),
        _c("ui.dock.toggle", L, ["(show|hide|toggle) [the] dock"], _simple("ui.dock.toggle", "Dock"), [("show dock / hide dock", "Show or hide the eye-control dock")]),
        _c(
            "speech.mode_dictation",
            L,
            ["dictation mode", "start dictation", "(start|begin) dictating", "dictate"],
            _b_mode("dictation", "Dictation mode"),
            [("dictation mode", "Type everything you say until you say \"stop dictation\"")],
        ),
        _c("speech.mode_command", L, ["command mode", "commands only"], _b_mode("command", "Command mode"), [("command mode", "Only run commands; never type")], contexts=frozenset({"global", "dictation"})),
        _c(
            "speech.mode_auto",
            L,
            ["(auto|automatic|normal|mixed) mode", "stop dictation", "(stop|end|finish) dictating"],
            _b_mode("auto", "Auto mode"),
            [("normal mode", "Commands, and typing when a text box has focus")],
            contexts=frozenset({"global", "dictation"}),
        ),
        _c("speech.listen_off", L, ["(turn off|mute|disable) [the] microphone", "microphone off"], _simple("speech.listen", "Microphone off", on=False), [("microphone off", "Turn the microphone off; turn it back on from the dock")], risky=True),
        _c("speech.repeat", L, ["(repeat|do) that [again] <times>", "again <times>", "repeat <times>"], _b_repeat, [("again [number]", "Repeat the last command", "again 3 times")]),
        _c(
            "key.stop",
            L,
            ["stop", "stop (that|it|everything)", "halt"],
            lambda s, c: Outcome((_a("key.release_all"),), "Stopped", state=(("continuous_scroll", None),)),
            [("stop", "Stop scrolling and let go of held keys and buttons")],
        ),
        _c(
            "ui.cancel",
            L,
            ["cancel", "never mind", "close (that|it|this|overlay)"],
            lambda s, c: Outcome((_a("ui.cancel"), _a("key.release_all")), "Cancelled"),
            [("cancel", "Close numbers, grid, zoom or help")],
            contexts=frozenset({"global", "labels", "grid", "wizard", "help"}),
        ),
        _c(
            "app.quit",
            L,
            ["(quit|exit|close) sightline"],
            _simple("app.quit", "Closing Sightline"),
            [("quit Sightline", "Close Sightline")],
            exact_only=True,
            fuzzy=False,
            chainable=False,
        ),
        # ------------------------------------------------------------------ "did you mean" choices
        _c(
            "speech.choose",
            L,
            ["{choice:index}", "(number|option|choice) {choice:index}", "[the] {choice_ord:index} [1|option|choice]"],
            lambda s, c: _one(_a("speech.choose", index=int(s["index"])), f"Choice {s['index']}"),
            [("one / two / three", "After \"did you mean\", choose one of the suggestions")],
            contexts=frozenset({"choices"}),
            chainable=False,
            fuzzy=False,
        ),
        # An offer to remember what was heard as the user's own way of saying a command (aliases.py).
        # Only while that offer is up; "one" works too, as for any numbered choice.
        _c(
            "speech.remember",
            L,
            ["yes [please]", "yeah", "yep", "yes remember (it|that|this)", "remember (it|that|this)"],
            lambda s, c: _one(_a("speech.choose", index=1), "Remember"),
            [("yes", "After \"remember ... as ...\": keep what Sightline heard as your way of saying that command")],
            contexts=frozenset({"offer"}),
            chainable=False,
            fuzzy=False,
        ),
        # ------------------------------------------------------------------ overlays: numbers
        _c("labels.select", "Numbers and grid", ["[click] [on] [number] {count:number}", "choose {count:number}", "pick {count:number}"], _b_labels_select("left", 1), [("<number>", "While numbers are shown, or when a few targets are numbered for you: click that number", "12")], contexts=frozenset({"labels"})),
        _c("labels.double", "Numbers and grid", ["double click [number] {count:number}"], _b_labels_select("left", 2), [("double click <number>", "Double-click a number")], contexts=frozenset({"labels"})),
        _c("labels.right", "Numbers and grid", ["right click [number] {count:number}"], _b_labels_select("right", 1), [("right click <number>", "Right-click a number")], contexts=frozenset({"labels"})),
        _c("labels.middle", "Numbers and grid", ["middle click [number] {count:number}"], _b_labels_select("middle", 1), contexts=frozenset({"labels"})),
        # ------------------------------------------------------------------ overlays: grid
        _c("grid.select", "Numbers and grid", ["[square|cell|box] {cell}"], lambda s, c: _one(_a("ui.grid.select", cell=int(s["cell"])), f"Square {s['cell']}"), [("<1 to 9>", "While the grid is shown: zoom into that square")], contexts=frozenset({"grid"})),
        _c("grid.click", "Numbers and grid", ["[left] click <it>"], _b_grid_click("left", 1, "Click"), [("click", "While the grid is shown: click the middle of the square")], contexts=frozenset({"grid"})),
        _c("grid.double", "Numbers and grid", ["double click <it>"], _b_grid_click("left", 2, "Double-click"), contexts=frozenset({"grid"})),
        _c("grid.right", "Numbers and grid", ["right click <it>"], _b_grid_click("right", 1, "Right-click"), contexts=frozenset({"grid"})),
        _c("grid.back", "Numbers and grid", ["back", "go back", "undo", "zoom out"], _simple("ui.grid.back", "Back"), [("back", "While the grid is shown: undo the last square")], contexts=frozenset({"grid"})),
        # ------------------------------------------------------------------ overlays: setup and calibration
        _c("wizard.next", "Setup", ["next", "continue", "done", "okay", "i am ready", "ready", "start", "finish"], _simple("ui.wizard.next", "Next"), [("next / back / skip", "Move through setup and calibration")], contexts=frozenset({"wizard"})),
        _c("wizard.back", "Setup", ["back", "go back", "previous"], _simple("ui.wizard.back", "Back"), contexts=frozenset({"wizard"})),
        _c("wizard.skip", "Setup", ["skip", "skip (this|that|it|step)"], _simple("ui.wizard.skip", "Skip"), contexts=frozenset({"wizard"})),
        # ------------------------------------------------------------------ overlays: numbered targets after an unsure gaze click
        # Bare numbers work as they do for the numbers overlay; the labels row of the help covers both.
        _c(
            "chooser.select",
            "Numbers and grid",
            ["[click] [on] [number] {chooser_number:number}", "(choose|pick) [number] {chooser_number:number}"],
            _b_chooser("left", 1),
            contexts=frozenset({"chooser"}),
        ),
        _c("chooser.double", "Numbers and grid", ["double click [number] {chooser_number:number}"], _b_chooser("left", 2), contexts=frozenset({"chooser"})),
        _c("chooser.right", "Numbers and grid", ["right click [number] {chooser_number:number}"], _b_chooser("right", 1), contexts=frozenset({"chooser"})),
        _c("chooser.middle", "Numbers and grid", ["middle click [number] {chooser_number:number}"], _b_chooser("middle", 1), contexts=frozenset({"chooser"})),
        _c("chooser.cancel", "Numbers and grid", ["cancel", "never mind", "none [of (them|those)]", "neither [of (them|those)]"], _simple("ui.chooser.cancel", "Cancelled"), contexts=frozenset({"chooser"})),
        # ------------------------------------------------------------------ overlays: the action menu
        _c(
            "menu.select",
            AM,
            ["[number|option|item] {menu_number:number}", "(choose|pick|select) [number|option|item] {menu_number:number}"],
            lambda s, c: _one(_a("ui.menu.select", number=_count(s, "number")), f"Item {_count(s, 'number')}"),
            [("<number>", "While the action menu is open: choose that item")],
            contexts=frozenset({"menu"}),
        ),
        _c(
            "menu.item",
            AM,
            ["{menu_item:item}", "(choose|pick|select) [the] {menu_item:item}"],
            _b_menu_item,
            [("right click / double click / drag / scroll / zoom / show numbers / keyboard", "While the action menu is open: choose an item by name", "right click")],
            contexts=frozenset({"menu"}),
        ),
        # ------------------------------------------------------------------ overlays: the countdown before a hard-to-reverse action
        # Short and forgiving: a missed "no" here closes or deletes something.  Only these words:
        # "stop" and "never mind" cancel through their usual commands, and anything else is not for it.
        _c(
            "confirm.cancel",
            L,
            ["cancel", "no", "(don't|dont|do not)", "wait"],
            _simple("ui.cancel", "Cancelled"),
            [("cancel / no / wait", "During the countdown before something hard to undo: don't do it")],
            contexts=frozenset({"confirm"}),
        ),
        # ------------------------------------------------------------------ overlays: the settings window
        _c("settings.next", SW, ["next [setting|one]", "go to [the] next setting"], _simple("ui.settings.next", "Next setting"), [("next / back", "Move to the next or the previous setting")], contexts=frozenset({"settings"})),
        _c("settings.previous", SW, ["back", "go back", "previous [setting|one]", "last setting", "go to [the] previous setting"], _simple("ui.settings.previous", "Previous setting"), contexts=frozenset({"settings"})),
        _c(
            "settings.more",
            SW,
            ["(more|up|faster|higher|longer|bigger|increase [it]) <times>", "a (bit|little) more"],
            _b_settings_adjust("up"),
            [("more / less", "Change the current setting by one step (up and down, faster and slower work too)", "more")],
            contexts=frozenset({"settings"}),
        ),
        _c("settings.less", SW, ["(less|down|slower|lower|shorter|smaller|decrease [it]) <times>", "a (bit|little) less"], _b_settings_adjust("down"), contexts=frozenset({"settings"})),
        _c(
            "settings.set",
            SW,
            ["(set|change|put) [it|this|that] to {setting_value}"],
            _b_settings_set,
            [("set to <value>", "Set the current setting: a number, on or off, or one of its choices", "set to 3")],
            contexts=frozenset({"settings"}),
            chainable=False,
        ),
        _c(
            "settings.preset",
            SW,
            ["[use] [the] {preset:name} [(preset|presets|settings)]", "(choose|pick|apply) [the] {preset:name} [(preset|settings)]"],
            lambda s, c: _one(_a("ui.settings.preset", name=s["name"]), f"Preset: {s['name']}"),
            [("steady hands / tremor / tired eyes / defaults", "Use a group of settings made for that", "tremor")],
            contexts=frozenset({"settings"}),
        ),
    ]
    return cmds


def sleep_and_wake(wake_phrase: str, sleep_phrase: str) -> list[CommandSpec]:
    """The sleep and wake commands depend on settings, so they are built separately."""
    wake = normalize_command(wake_phrase) or "wake up"
    sleep = normalize_command(sleep_phrase) or "go to sleep"
    return [
        _c(
            "speech.sleep",
            "Sightline",
            [_escape(sleep), "stop listening", "go to sleep"],
            _b_mode("sleep", "Sleeping"),
            [(sleep_phrase or "go to sleep", f"Stop listening until you say \"{wake_phrase}\"")],
            contexts=frozenset({"global", "dictation"}),
            chainable=False,
        ),
        _c(
            "speech.wake",
            "Sightline",
            [_escape(wake)],
            _b_wake,
            [(wake_phrase or "wake up", "Start listening again after sleep")],
            contexts=frozenset({"sleep", "global"}),
            chainable=False,
            fuzzy=True,
        ),
    ]


def _escape(text: str) -> str:
    """Make a user-supplied phrase safe to use as a hassil template."""
    out = []
    for ch in text:
        if ch in "()[]{}<>|;":
            continue
        out.append(ch)
    return "".join(out).strip()


def help_rows(commands: list[CommandSpec]) -> list[CommandHelp]:
    rows: list[CommandHelp] = []
    seen: set[tuple[str, str]] = set()
    for cmd in commands:
        for row in cmd.help:
            key = (cmd.category, row.phrase)
            if key in seen:
                continue
            seen.add(key)
            rows.append(CommandHelp(cmd.category, row.phrase, row.description, row.example))
    return rows
