"""Key names and the key-combo parser ("ctrl+shift+t", "win+d", "page down", "volume up").

Parsing is pure and layout independent.  Turning a parsed key into a virtual
key and scan code happens later through a ``KeyResolver`` bound to the
keyboard layout of the window that will receive the keys, so "ctrl+/" finds
the slash key on a German keyboard too.

Non-character keys carry fixed set-1 scan codes and the extended (E0) flag.
Injected keys always include the scan code: remote desktop clients, virtual
machines and games read scan codes, not virtual keys.

The spoken symbol names ("dot", "slash", "backtick", "left bracket") follow
the vocabulary of talonhub/community (MIT, github.com/talonhub/community,
``core/keys``); the table here is written for this module, not copied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

# --------------------------------------------------------------------------- virtual keys

VK_BACK = 0x08
VK_TAB = 0x09
VK_CLEAR = 0x0C
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_PAUSE = 0x13
VK_CAPITAL = 0x14
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21
VK_NEXT = 0x22
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_SNAPSHOT = 0x2C
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_APPS = 0x5D
VK_NUMPAD0 = 0x60
VK_MULTIPLY = 0x6A
VK_ADD = 0x6B
VK_SUBTRACT = 0x6D
VK_DECIMAL = 0x6E
VK_DIVIDE = 0x6F
VK_F1 = 0x70
VK_NUMLOCK = 0x90
VK_SCROLL = 0x91
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_BROWSER_BACK = 0xA6
VK_BROWSER_FORWARD = 0xA7
VK_BROWSER_REFRESH = 0xA8
VK_BROWSER_STOP = 0xA9
VK_BROWSER_SEARCH = 0xAA
VK_BROWSER_FAVORITES = 0xAB
VK_BROWSER_HOME = 0xAC
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_LAUNCH_MAIL = 0xB4
VK_LAUNCH_MEDIA_SELECT = 0xB5
VK_LAUNCH_APP1 = 0xB6
VK_LAUNCH_APP2 = 0xB7
VK_OEM_1 = 0xBA  # ;: on US keyboards
VK_OEM_PLUS = 0xBB  # the + key on any layout
VK_OEM_COMMA = 0xBC  # the , key on any layout
VK_OEM_MINUS = 0xBD  # the - key on any layout
VK_OEM_PERIOD = 0xBE  # the . key on any layout
VK_OEM_2 = 0xBF  # /? on US keyboards
VK_OEM_3 = 0xC0  # `~
VK_OEM_4 = 0xDB  # [{
VK_OEM_5 = 0xDC  # \|
VK_OEM_6 = 0xDD  # ]}
VK_OEM_7 = 0xDE  # '"

MODIFIER_VKS = frozenset({VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU, VK_LWIN, VK_RWIN})


class KeyComboError(ValueError):
    """A key name or combo that cannot be parsed.  The message is plain English for the user."""


@dataclass(frozen=True, slots=True)
class Key:
    """One physical key, before it is bound to a keyboard layout.

    Exactly one of ``vk`` or ``char`` is set.  ``scan`` and ``extended`` are
    fixed for non-character keys; character keys (letters, digits, symbols)
    get their scan code from the layout.
    """

    name: str
    vk: int = 0
    scan: int = 0
    extended: bool = False
    char: str = ""  # symbol keys found through the layout, e.g. "/" or "["
    modifier: bool = False


@dataclass(frozen=True, slots=True)
class Chord:
    """Keys pressed together: modifiers first, released in reverse order."""

    modifiers: tuple[Key, ...] = ()
    keys: tuple[Key, ...] = ()
    text: str = field(default="", compare=False)

    @property
    def all_keys(self) -> tuple[Key, ...]:
        return self.modifiers + self.keys


@dataclass(frozen=True, slots=True)
class KeyStroke:
    """A key bound to a layout: what SendInput needs."""

    vk: int
    scan: int
    extended: bool
    name: str = ""


# --------------------------------------------------------------------------- the table

# name -> (vk, scan, extended).  Scan codes are keyboard scan code set 1.
_FIXED: dict[str, tuple[int, int, bool]] = {
    # modifiers (left by default)
    "ctrl": (VK_LCONTROL, 0x1D, False),
    "rctrl": (VK_RCONTROL, 0x1D, True),
    "shift": (VK_LSHIFT, 0x2A, False),
    "rshift": (VK_RSHIFT, 0x36, False),
    "alt": (VK_LMENU, 0x38, False),
    "ralt": (VK_RMENU, 0x38, True),
    "win": (VK_LWIN, 0x5B, True),
    "rwin": (VK_RWIN, 0x5C, True),
    # editing and navigation
    "enter": (VK_RETURN, 0x1C, False),
    "numpadenter": (VK_RETURN, 0x1C, True),
    "escape": (VK_ESCAPE, 0x01, False),
    "tab": (VK_TAB, 0x0F, False),
    "space": (VK_SPACE, 0x39, False),
    "backspace": (VK_BACK, 0x0E, False),
    "delete": (VK_DELETE, 0x53, True),
    "insert": (VK_INSERT, 0x52, True),
    "home": (VK_HOME, 0x47, True),
    "end": (VK_END, 0x4F, True),
    "pageup": (VK_PRIOR, 0x49, True),
    "pagedown": (VK_NEXT, 0x51, True),
    "up": (VK_UP, 0x48, True),
    "down": (VK_DOWN, 0x50, True),
    "left": (VK_LEFT, 0x4B, True),
    "right": (VK_RIGHT, 0x4D, True),
    "capslock": (VK_CAPITAL, 0x3A, False),
    "numlock": (VK_NUMLOCK, 0x45, True),  # NumLock needs the extended flag when injected (Microsoft KB 127190)
    "scrolllock": (VK_SCROLL, 0x46, False),
    "printscreen": (VK_SNAPSHOT, 0x37, True),
    "pause": (VK_PAUSE, 0x45, False),
    "apps": (VK_APPS, 0x5D, True),
    # number pad
    "numpad0": (VK_NUMPAD0, 0x52, False),
    "numpad1": (VK_NUMPAD0 + 1, 0x4F, False),
    "numpad2": (VK_NUMPAD0 + 2, 0x50, False),
    "numpad3": (VK_NUMPAD0 + 3, 0x51, False),
    "numpad4": (VK_NUMPAD0 + 4, 0x4B, False),
    "numpad5": (VK_NUMPAD0 + 5, 0x4C, False),
    "numpad6": (VK_NUMPAD0 + 6, 0x4D, False),
    "numpad7": (VK_NUMPAD0 + 7, 0x47, False),
    "numpad8": (VK_NUMPAD0 + 8, 0x48, False),
    "numpad9": (VK_NUMPAD0 + 9, 0x49, False),
    "numpadmultiply": (VK_MULTIPLY, 0x37, False),
    "numpadadd": (VK_ADD, 0x4E, False),
    "numpadsubtract": (VK_SUBTRACT, 0x4A, False),
    "numpaddecimal": (VK_DECIMAL, 0x53, False),
    "numpaddivide": (VK_DIVIDE, 0x35, True),
    # media and browser keys (all E0-prefixed)
    "volumemute": (VK_VOLUME_MUTE, 0x20, True),
    "volumedown": (VK_VOLUME_DOWN, 0x2E, True),
    "volumeup": (VK_VOLUME_UP, 0x30, True),
    "playpause": (VK_MEDIA_PLAY_PAUSE, 0x22, True),
    "nexttrack": (VK_MEDIA_NEXT_TRACK, 0x19, True),
    "previoustrack": (VK_MEDIA_PREV_TRACK, 0x10, True),
    "mediastop": (VK_MEDIA_STOP, 0x24, True),
    "browserback": (VK_BROWSER_BACK, 0x6A, True),
    "browserforward": (VK_BROWSER_FORWARD, 0x69, True),
    "browserrefresh": (VK_BROWSER_REFRESH, 0x67, True),
    "browserstop": (VK_BROWSER_STOP, 0x68, True),
    "browsersearch": (VK_BROWSER_SEARCH, 0x65, True),
    "browserfavorites": (VK_BROWSER_FAVORITES, 0x66, True),
    "browserhome": (VK_BROWSER_HOME, 0x32, True),
    "launchmail": (VK_LAUNCH_MAIL, 0x6C, True),
    "launchmedia": (VK_LAUNCH_MEDIA_SELECT, 0x6D, True),
    "launchapp1": (VK_LAUNCH_APP1, 0x6B, True),
    "launchapp2": (VK_LAUNCH_APP2, 0x21, True),
    # keys whose meaning is the same on every layout (Microsoft documents these four VKs that way)
    "plus": (VK_OEM_PLUS, 0x0D, False),
    "comma": (VK_OEM_COMMA, 0x33, False),
    "minus": (VK_OEM_MINUS, 0x0C, False),
    "period": (VK_OEM_PERIOD, 0x34, False),
}

# F1-F24.  F13-F24 follow the scan codes Windows maps them to.
_F_SCANS = [0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x40, 0x41, 0x42, 0x43, 0x44, 0x57, 0x58,
            0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x6B, 0x6C, 0x6D, 0x6E, 0x76]  # fmt: skip
for _i, _sc in enumerate(_F_SCANS):
    _FIXED[f"f{_i + 1}"] = (VK_F1 + _i, _sc, False)

_MODIFIER_NAMES = frozenset({"ctrl", "rctrl", "shift", "rshift", "alt", "ralt", "win", "rwin"})

# Symbol keys located through the layout: canonical name -> character.
_SYMBOLS: dict[str, str] = {
    "equals": "=",
    "slash": "/",
    "backslash": "\\",
    "semicolon": ";",
    "colon": ":",
    "quote": "'",
    "doublequote": '"',
    "backtick": "`",
    "tilde": "~",
    "leftbracket": "[",
    "rightbracket": "]",
    "leftbrace": "{",
    "rightbrace": "}",
    "leftparen": "(",
    "rightparen": ")",
    "lessthan": "<",
    "greaterthan": ">",
    "question": "?",
    "exclamation": "!",
    "at": "@",
    "hash": "#",
    "dollar": "$",
    "percent": "%",
    "caret": "^",
    "ampersand": "&",
    "asterisk": "*",
    "underscore": "_",
    "pipe": "|",
}

# Spoken and written aliases -> canonical name.  Keys are compared after ``_squash``.
_ALIASES: dict[str, str] = {
    # modifiers
    "control": "ctrl", "ctl": "ctrl", "lctrl": "ctrl", "leftctrl": "ctrl", "leftcontrol": "ctrl",
    "rightctrl": "rctrl", "rightcontrol": "rctrl", "rcontrol": "rctrl",
    "lshift": "shift", "leftshift": "shift", "rightshift": "rshift",
    "lalt": "alt", "leftalt": "alt", "option": "alt",
    "rightalt": "ralt", "altgr": "ralt", "altgraph": "ralt",
    "windows": "win", "windowskey": "win", "winkey": "win", "super": "win", "meta": "win", "cmd": "win",
    "command": "win", "lwin": "win", "leftwin": "win", "leftwindows": "win", "start": "win",
    "rightwin": "rwin", "rightwindows": "rwin",
    # editing and navigation
    "return": "enter", "enterkey": "enter",
    "keypadenter": "numpadenter", "numenter": "numpadenter",
    "esc": "escape", "escapekey": "escape", "cancel": "escape",
    "tabkey": "tab",
    "spacebar": "space", "spacekey": "space",
    "bksp": "backspace", "backspacekey": "backspace",
    "del": "delete", "forwarddelete": "delete", "deletekey": "delete",
    "ins": "insert",
    "pgup": "pageup",
    "pgdn": "pagedown", "pgdown": "pagedown",
    "uparrow": "up", "arrowup": "up",
    "downarrow": "down", "arrowdown": "down",
    "leftarrow": "left", "arrowleft": "left",
    "rightarrow": "right", "arrowright": "right",
    "caps": "capslock",
    "scrlk": "scrolllock",
    "prtsc": "printscreen", "prtscn": "printscreen", "prtscr": "printscreen", "printscr": "printscreen",
    "snapshot": "printscreen", "print": "printscreen",
    "break": "pause", "pausebreak": "pause",
    "menu": "apps", "menukey": "apps", "application": "apps", "applicationkey": "apps",
    "contextmenu": "apps", "contextmenukey": "apps",
    # number pad words
    "numpadplus": "numpadadd", "keypadplus": "numpadadd", "numplus": "numpadadd", "add": "numpadadd",
    "numpadminus": "numpadsubtract", "keypadminus": "numpadsubtract", "numminus": "numpadsubtract",
    "subtract": "numpadsubtract",
    "numpadstar": "numpadmultiply", "numpadasterisk": "numpadmultiply", "numpadtimes": "numpadmultiply",
    "keypadstar": "numpadmultiply", "numstar": "numpadmultiply", "multiply": "numpadmultiply",
    "numpadslash": "numpaddivide", "keypadslash": "numpaddivide", "numslash": "numpaddivide", "divide": "numpaddivide",
    "numpadpoint": "numpaddecimal", "numpaddot": "numpaddecimal", "numpadperiod": "numpaddecimal",
    "keypadpoint": "numpaddecimal", "numpoint": "numpaddecimal", "decimal": "numpaddecimal",
    # media and browser
    "volup": "volumeup", "volumeupkey": "volumeup",
    "voldown": "volumedown", "volumedownkey": "volumedown",
    "mute": "volumemute", "volmute": "volumemute",
    "mediaplaypause": "playpause", "play": "playpause", "mediaplay": "playpause", "playpausemedia": "playpause",
    "mediapause": "playpause",
    "medianext": "nexttrack", "nextsong": "nexttrack",
    "medianexttrack": "nexttrack",
    "prevtrack": "previoustrack", "mediaprevious": "previoustrack", "mediaprev": "previoustrack",
    "previoussong": "previoustrack", "mediaprevioustrack": "previoustrack", "prevsong": "previoustrack",
    "stopmedia": "mediastop",
    "browserfavourites": "browserfavorites", "browserrefreshkey": "browserrefresh",
    "mail": "launchmail", "mycomputer": "launchapp1", "calculatorkey": "launchapp2", "mediaselect": "launchmedia",
    # symbols with fixed keys
    "dot": "period", "fullstop": "period", "point": "period",
    "dash": "minus", "hyphen": "minus",
    "pluskey": "plus",
    # symbols found through the layout
    "equal": "equals", "equalsign": "equals",
    "forwardslash": "slash",
    "backslashkey": "backslash",
    "semi": "semicolon",
    "apostrophe": "quote", "singlequote": "quote",
    "dubquote": "doublequote", "doublequotes": "doublequote",
    "grave": "backtick", "graveaccent": "backtick", "backquote": "backtick",
    "lbracket": "leftbracket", "openbracket": "leftbracket", "leftsquare": "leftbracket", "bracket": "leftbracket",
    "rbracket": "rightbracket", "closebracket": "rightbracket", "rightsquare": "rightbracket",
    "lbrace": "leftbrace", "openbrace": "leftbrace", "leftcurly": "leftbrace", "curly": "leftbrace",
    "rbrace": "rightbrace", "closebrace": "rightbrace", "rightcurly": "rightbrace",
    "lparen": "leftparen", "openparen": "leftparen", "paren": "leftparen", "leftparenthesis": "leftparen",
    "rparen": "rightparen", "closeparen": "rightparen", "rightparenthesis": "rightparen",
    "less": "lessthan", "leftangle": "lessthan", "langle": "lessthan",
    "greater": "greaterthan", "rightangle": "greaterthan", "rangle": "greaterthan",
    "questionmark": "question",
    "exclamationmark": "exclamation", "bang": "exclamation",
    "atsign": "at",
    "pound": "hash", "numbersign": "hash", "hashtag": "hash", "sharp": "hash",
    "dollarsign": "dollar",
    "percentsign": "percent",
    "amper": "ampersand", "andsign": "ampersand",
    "star": "asterisk",
    "underscorekey": "underscore", "downscore": "underscore",
    "verticalbar": "pipe",
}  # fmt: skip

_DIGIT_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}  # fmt: skip

_NUMPAD_PREFIXES = ("numpad", "keypad", "num", "kp")


def _squash(text: str) -> str:
    """Lower-case and drop spaces, underscores and hyphens inside words ("Page-Down" -> "pagedown")."""
    return re.sub(r"[\s_]+|(?<=\w)-(?=\w)", "", text.strip().lower())


def _lookup(name: str) -> Key | None:
    """One key from a single token, or None."""
    raw = name.strip()
    if not raw:
        return None
    if len(raw) == 1:
        return _char_key(raw)
    squashed = _squash(raw)
    canonical = _ALIASES.get(squashed, squashed)
    if canonical in _FIXED:
        vk, scan, ext = _FIXED[canonical]
        return Key(canonical, vk=vk, scan=scan, extended=ext, modifier=canonical in _MODIFIER_NAMES)
    if canonical in _SYMBOLS:
        return Key(canonical, char=_SYMBOLS[canonical])
    if canonical in _DIGIT_WORDS:
        return _char_key(_DIGIT_WORDS[canonical])
    for prefix in _NUMPAD_PREFIXES:
        if canonical.startswith(prefix):
            rest = canonical[len(prefix) :]
            rest = _DIGIT_WORDS.get(rest, rest)
            if len(rest) == 1 and rest.isdigit():
                vk, scan, ext = _FIXED[f"numpad{rest}"]
                return Key(f"numpad{rest}", vk=vk, scan=scan, extended=ext)
    if re.fullmatch(r"f([1-9]|1\d|2[0-4])", canonical):
        vk, scan, ext = _FIXED[canonical]
        return Key(canonical, vk=vk, scan=scan, extended=ext)
    return None


def _char_key(ch: str) -> Key | None:
    if ch.isascii() and ch.isalpha():
        return Key(ch.lower(), vk=ord(ch.upper()))
    if ch.isascii() and ch.isdigit():
        return Key(ch, vk=ord(ch))
    if ch == " ":
        return _lookup("space")
    if ch == "+":
        return _lookup("plus")
    if ch == ",":
        return _lookup("comma")
    if ch == "-":
        return _lookup("minus")
    if ch == ".":
        return _lookup("period")
    if ch.isprintable() and not ch.isspace():
        return Key(ch, char=ch)
    return None


def key_names() -> list[str]:
    """Every canonical key name the parser accepts (for help sheets and the speech grammar)."""
    names = set(_FIXED) | set(_SYMBOLS)
    names.update(chr(c) for c in range(ord("a"), ord("z") + 1))
    names.update(str(d) for d in range(10))
    return sorted(names)


def parse_key(name: str) -> Key:
    key = _lookup(name)
    if key is None:
        # A multi-word name nobody squashed into the table, e.g. "page  down" with odd spacing.
        chord = _split_words(name)
        if chord is not None and len(chord) == 1:
            return chord[0]
        raise KeyComboError(f"I do not know the key “{name.strip()}”")
    return key


def _split_words(text: str) -> list[Key] | None:
    """Greedy longest-match split of space-separated words into keys ("ctrl shift page down")."""
    words = text.split()
    if not words:
        return None
    out: list[Key] = []
    i = 0
    while i < len(words):
        for j in range(len(words), i, -1):
            key = _lookup(" ".join(words[i:j]))
            if key is not None:
                out.append(key)
                i = j
                break
        else:
            return None
    return out


def parse_combo(combo: str) -> Chord:
    """Parse "ctrl+shift+t", "win d", "alt+f4", "page down", "ctrl++", "volume up" into a Chord.

    Raises KeyComboError with a plain-English message for anything unknown.
    """
    if combo is None or not str(combo).strip():
        raise KeyComboError("No key was given")
    text = str(combo).strip()
    tokens: list[str]
    if text == "+":
        tokens = ["+"]
    else:
        parts = re.split(r"\s*\+\s*", text)
        tokens = []
        # Empty pieces come from a literal plus: "ctrl++" splits into ["ctrl", "", ""].
        trailing_plus = False
        for part in parts:
            if part == "":
                trailing_plus = True
                continue
            tokens.append(part)
        if trailing_plus:
            tokens.append("+")
    keys: list[Key] = []
    for token in tokens:
        key = _lookup(token)
        if key is not None:
            keys.append(key)
            continue
        split = _split_words(token)
        if split is None:
            raise KeyComboError(f"I do not know the key “{token.strip()}”")
        keys.extend(split)
    modifiers = tuple(k for k in keys if k.modifier)
    others = tuple(k for k in keys if not k.modifier)
    # Keep the order the user gave for modifiers; drop exact duplicates ("ctrl+ctrl+c").
    modifiers = tuple(dict.fromkeys(modifiers))
    others = tuple(dict.fromkeys(others))
    return Chord(modifiers, others, text)


# --------------------------------------------------------------------------- binding to a layout


class KeyResolver(Protocol):
    """Turns layout-dependent keys into virtual keys and scan codes."""

    def scan_for_vk(self, vk: int) -> tuple[int, bool]:
        """(scan code, extended) for a virtual key in this layout; (0, False) when unknown."""

    def vk_for_char(self, ch: str) -> tuple[int, bool, bool, bool] | None:
        """(vk, needs_shift, needs_ctrl, needs_alt) that types ``ch`` in this layout, or None."""


# US layout fallbacks when the real layout cannot produce a character.
_US_CHARS: dict[str, tuple[int, bool]] = {
    ";": (VK_OEM_1, False), ":": (VK_OEM_1, True),
    "=": (VK_OEM_PLUS, False), "+": (VK_OEM_PLUS, True),
    ",": (VK_OEM_COMMA, False), "<": (VK_OEM_COMMA, True),
    "-": (VK_OEM_MINUS, False), "_": (VK_OEM_MINUS, True),
    ".": (VK_OEM_PERIOD, False), ">": (VK_OEM_PERIOD, True),
    "/": (VK_OEM_2, False), "?": (VK_OEM_2, True),
    "`": (VK_OEM_3, False), "~": (VK_OEM_3, True),
    "[": (VK_OEM_4, False), "{": (VK_OEM_4, True),
    "\\": (VK_OEM_5, False), "|": (VK_OEM_5, True),
    "]": (VK_OEM_6, False), "}": (VK_OEM_6, True),
    "'": (VK_OEM_7, False), '"': (VK_OEM_7, True),
    "!": (0x31, True), "@": (0x32, True), "#": (0x33, True), "$": (0x34, True), "%": (0x35, True),
    "^": (0x36, True), "&": (0x37, True), "*": (0x38, True), "(": (0x39, True), ")": (0x30, True),
}  # fmt: skip

# US scan codes for character keys, for the offline resolver used in tests.
_US_SCANS: dict[int, int] = {
    **{ord(c): s for c, s in zip("1234567890", range(0x02, 0x0C), strict=True)},
    **{ord(c): s for c, s in zip("QWERTYUIOP", range(0x10, 0x1A), strict=True)},
    **{ord(c): s for c, s in zip("ASDFGHJKL", range(0x1E, 0x27), strict=True)},
    **{ord(c): s for c, s in zip("ZXCVBNM", range(0x2C, 0x33), strict=True)},
    VK_OEM_MINUS: 0x0C, VK_OEM_PLUS: 0x0D, VK_OEM_4: 0x1A, VK_OEM_6: 0x1B, VK_OEM_1: 0x27,
    VK_OEM_7: 0x28, VK_OEM_3: 0x29, VK_OEM_5: 0x2B, VK_OEM_COMMA: 0x33, VK_OEM_PERIOD: 0x34, VK_OEM_2: 0x35,
}  # fmt: skip


class UsKeyResolver:
    """A fixed US-English layout.  Used by tests and as the fallback when Windows cannot answer."""

    def scan_for_vk(self, vk: int) -> tuple[int, bool]:
        return (_US_SCANS.get(vk, 0), False)

    def vk_for_char(self, ch: str) -> tuple[int, bool, bool, bool] | None:
        if ch.isascii() and ch.isalpha():
            return (ord(ch.upper()), ch.isupper(), False, False)
        if ch.isascii() and ch.isdigit():
            return (ord(ch), False, False, False)
        hit = _US_CHARS.get(ch)
        if hit is None:
            return None
        return (hit[0], hit[1], False, False)


_SHIFT = Key("shift", vk=VK_LSHIFT, scan=0x2A, modifier=True)
_CTRL = Key("ctrl", vk=VK_LCONTROL, scan=0x1D, modifier=True)
_ALT = Key("alt", vk=VK_LMENU, scan=0x38, modifier=True)


def resolve_chord(chord: Chord, resolver: KeyResolver) -> tuple[list[KeyStroke], list[KeyStroke]]:
    """Bind a chord to a layout: returns (modifier strokes, main strokes).

    Symbol keys that need shift (or AltGr) on this layout add those
    modifiers, so "ctrl+?" becomes ctrl+shift+/ on a US keyboard.
    """
    modifiers: list[Key] = list(chord.modifiers)
    mains: list[KeyStroke] = []
    for key in chord.keys:
        if key.char:
            found = resolver.vk_for_char(key.char) or UsKeyResolver().vk_for_char(key.char)
            if found is None:
                raise KeyComboError(f"Your keyboard has no key for “{key.char}”")
            vk, shift, ctrl, alt = found
            for needed, mod in ((ctrl, _CTRL), (alt, _ALT), (shift, _SHIFT)):
                if needed and not any(m.vk == mod.vk for m in modifiers):
                    modifiers.append(mod)
            scan, ext = resolver.scan_for_vk(vk)
            if not scan:
                scan, ext = UsKeyResolver().scan_for_vk(vk)
            mains.append(KeyStroke(vk, scan, ext, key.name))
        elif key.scan:
            mains.append(KeyStroke(key.vk, key.scan, key.extended, key.name))
        else:
            scan, ext = resolver.scan_for_vk(key.vk)
            if not scan:
                scan, ext = UsKeyResolver().scan_for_vk(key.vk)
            mains.append(KeyStroke(key.vk, scan, ext, key.name))
    mods = [KeyStroke(m.vk, m.scan, m.extended, m.name) for m in modifiers]
    return mods, mains


def stroke_for_key(key: Key, resolver: KeyResolver) -> KeyStroke:
    """A single key bound to the layout (no automatic shift), for key_down / key_up."""
    if key.scan:
        return KeyStroke(key.vk, key.scan, key.extended, key.name)
    if key.char:
        found = resolver.vk_for_char(key.char) or UsKeyResolver().vk_for_char(key.char)
        if found is None:
            raise KeyComboError(f"Your keyboard has no key for “{key.char}”")
        vk = found[0]
    else:
        vk = key.vk
    scan, ext = resolver.scan_for_vk(vk)
    if not scan:
        scan, ext = UsKeyResolver().scan_for_vk(vk)
    return KeyStroke(vk, scan, ext, key.name)
