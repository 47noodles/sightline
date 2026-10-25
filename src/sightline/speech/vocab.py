"""Spoken vocabulary: alphabets, key names, symbols, dictation punctuation and formatter words.

Most of the word lists are adapted from talonhub/community
(https://github.com/talonhub/community, commit de1c2a33), which is MIT
licensed:

    Copyright (c) 2021 Jeff Knaus, Ryan Hileman, Zach Dwiel, Michael
    Arntzenius, and others.  Permission is hereby granted, free of charge,
    to any person obtaining a copy of this software ... (MIT licence, full
    text in THIRD_PARTY_NOTICES.md).

Sources: core/keys/letter.talon-list, core/keys/arrow_key.talon-list,
core/keys/function_key.talon-list, core/keys/win/special_key.talon-list,
core/keys/win/modifier_key.talon-list, core/keys/symbols.py,
core/formatters/*.talon-list and core/text/prose_snippets.talon-list.

Changes from Talon: Windows meanings for "delete" (forward delete) and
"windows" (the Windows key), NATO and plain letters next to the Talon
alphabet, and a few extra spoken forms that speech recognisers tend to
produce ("full stop", "question mark").  Key names on the right-hand side
are the names Sightline sends in ``key.press`` combos.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- letters

# Talon's alphabet: short, acoustically distinct words.
TALON_ALPHABET: dict[str, str] = {
    "air": "a",
    "bat": "b",
    "cap": "c",
    "drum": "d",
    "each": "e",
    "fine": "f",
    "gust": "g",
    "harp": "h",
    "sit": "i",
    "jury": "j",
    "crunch": "k",
    "look": "l",
    "made": "m",
    "near": "n",
    "odd": "o",
    "pit": "p",
    "quench": "q",
    "red": "r",
    "sun": "s",
    "trap": "t",
    "urge": "u",
    "vest": "v",
    "whale": "w",
    "plex": "x",
    "yank": "y",
    "zip": "z",
}

NATO_ALPHABET: dict[str, str] = {
    "alpha": "a",
    "alfa": "a",
    "bravo": "b",
    "charlie": "c",
    "delta": "d",
    "echo": "e",
    "foxtrot": "f",
    "golf": "g",
    "hotel": "h",
    "india": "i",
    "juliet": "j",
    "juliett": "j",
    "kilo": "k",
    "lima": "l",
    "mike": "m",
    "november": "n",
    "oscar": "o",
    "papa": "p",
    "quebec": "q",
    "romeo": "r",
    "sierra": "s",
    "tango": "t",
    "uniform": "u",
    "victor": "v",
    "whiskey": "w",
    "whisky": "w",
    "x ray": "x",
    "xray": "x",
    "yankee": "y",
    "zulu": "z",
}

# Plain letters, plus the spellings recognisers produce for a letter said on its own.
# Only used where a letter is expected ("press ...", "spell ..."), never as bare commands,
# because words like "you", "are" and "see" are far more often just words.
PLAIN_LETTERS: dict[str, str] = {chr(c): chr(c) for c in range(ord("a"), ord("z") + 1)}
LETTER_SOUNDALIKES: dict[str, str] = {
    "ay": "a",
    "bee": "b",
    "be": "b",
    "see": "c",
    "sea": "c",
    "dee": "d",
    "ef": "f",
    "eff": "f",
    "gee": "g",
    "aitch": "h",
    "eye": "i",
    "jay": "j",
    "kay": "k",
    "el": "l",
    "ell": "l",
    "em": "m",
    "en": "n",
    "oh": "o",
    "pee": "p",
    "cue": "q",
    "queue": "q",
    "are": "r",
    "ar": "r",
    "es": "s",
    "ess": "s",
    "tee": "t",
    "tea": "t",
    "you": "u",
    "vee": "v",
    "double you": "w",
    "ex": "x",
    "why": "y",
    "zed": "z",
    "zee": "z",
}


def all_letter_words() -> dict[str, str]:
    """Every spoken form of a letter (plain, NATO, Talon and sound-alikes)."""
    out: dict[str, str] = {}
    out.update(PLAIN_LETTERS)
    out.update(LETTER_SOUNDALIKES)
    out.update(NATO_ALPHABET)
    out.update(TALON_ALPHABET)
    return out


# --------------------------------------------------------------------------- keys

MODIFIER_KEYS: dict[str, str] = {
    "control": "ctrl",
    "ctrl": "ctrl",
    "command": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "option": "alt",
    "windows": "win",
    "windows key": "win",
    "win": "win",
    "super": "win",
    "start key": "win",
}

ARROW_KEYS: dict[str, str] = {
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "up arrow": "up",
    "down arrow": "down",
    "left arrow": "left",
    "right arrow": "right",
    "arrow up": "up",
    "arrow down": "down",
    "arrow left": "left",
    "arrow right": "right",
}

# Keys that are also safe to say on their own as a command ("enter", "tab three times").
SPECIAL_KEYS: dict[str, str] = {
    "enter": "enter",
    "return": "enter",
    "tab": "tab",
    "escape": "escape",
    "space": "space",
    "spacebar": "space",
    "space bar": "space",
    "backspace": "backspace",
    "back space": "backspace",
    "wipe": "backspace",
    "delete": "delete",
    "forward delete": "delete",
    "home": "home",
    "end": "end",
    "page up": "pageup",
    "page down": "pagedown",
    "insert": "insert",
    "menu key": "menu",
    "print screen": "printscreen",
    "caps lock": "capslock",
}

FUNCTION_KEYS: dict[str, str] = {}
for _n in range(1, 21):
    # After number normalisation "f five" arrives as "f 5"; some recognisers write "F5".
    FUNCTION_KEYS[f"f {_n}"] = f"f{_n}"
    FUNCTION_KEYS[f"f{_n}"] = f"f{_n}"

DIGIT_KEYS: dict[str, str] = {str(d): str(d) for d in range(10)}

# Symbol keys by their command names.  The value is the character; ``key_name``
# turns it into the name used in a key combo.
SYMBOL_KEYS: dict[str, str] = {
    "back tick": "`",
    "backtick": "`",
    "grave": "`",
    "comma": ",",
    "period": ".",
    "full stop": ".",
    "dot": ".",
    "point": ".",
    "semicolon": ";",
    "semi colon": ";",
    "colon": ":",
    "question mark": "?",
    "exclamation mark": "!",
    "exclamation point": "!",
    "bang": "!",
    "asterisk": "*",
    "star": "*",
    "hash": "#",
    "hash sign": "#",
    "number sign": "#",
    "percent": "%",
    "percent sign": "%",
    "at sign": "@",
    "at symbol": "@",
    "ampersand": "&",
    "and sign": "&",
    "hyphen": "-",
    "minus": "-",
    "dash": "-",
    "equals": "=",
    "equal sign": "=",
    "plus": "+",
    "plus sign": "+",
    "tilde": "~",
    "underscore": "_",
    "under score": "_",
    "left paren": "(",
    "open paren": "(",
    "paren": "(",
    "right paren": ")",
    "close paren": ")",
    "left bracket": "[",
    "open bracket": "[",
    "bracket": "[",
    "right bracket": "]",
    "close bracket": "]",
    "left brace": "{",
    "open brace": "{",
    "brace": "{",
    "right brace": "}",
    "close brace": "}",
    "slash": "/",
    "forward slash": "/",
    "backslash": "\\",
    "back slash": "\\",
    "less than": "<",
    "left angle": "<",
    "greater than": ">",
    "right angle": ">",
    "caret": "^",
    "pipe": "|",
    "apostrophe": "'",
    "quote": "'",
    "single quote": "'",
    "double quote": '"',
    "dollar": "$",
    "dollar sign": "$",
}

# Characters that are typed with shift on a US layout.  A spoken symbol on its
# own is typed as text (layout independent); with modifiers it becomes a combo.
SHIFTED_SYMBOLS = set('~!@#$%^&*()_+{}|:"<>?')


def key_name(char_or_key: str) -> str:
    """Name used inside a ``key.press`` combo.  "+" cannot appear literally because it separates keys."""
    return "plus" if char_or_key == "+" else char_or_key


# --------------------------------------------------------------------------- dictation punctuation

# Spoken punctuation in dictation (Talon's "command and dictation" symbol forms
# plus its prose snippets).  Longest phrases are matched first.
PUNCTUATION_WORDS: dict[str, str] = {
    "comma": ",",
    "period": ".",
    "full stop": ".",
    "question mark": "?",
    "exclamation mark": "!",
    "exclamation point": "!",
    "colon": ":",
    "semicolon": ";",
    "semi colon": ";",
    "ellipsis": "...",
    "dot dot dot": "...",
    "hyphen": "-",
    "en dash": "–",
    "em dash": "\u2014",
    "open paren": "(",
    "left paren": "(",
    "close paren": ")",
    "right paren": ")",
    "open bracket": "[",
    "close bracket": "]",
    "open quote": "“",
    "close quote": "”",
    "apostrophe": "'",
    "asterisk": "*",
    "ampersand": "&",
    "at sign": "@",
    "hash sign": "#",
    "percent sign": "%",
    "dollar sign": "$",
    "pound sign": "£",
    "euro sign": "€",
    "degree sign": "°",
    "forward slash": "/",
    "back tick": "`",
    "smiley face": ":-)",
    "new line": "\n",
    "newline": "\n",
    "new paragraph": "\n\n",
}

# --------------------------------------------------------------------------- formatters

# Spoken formatter word -> Talon formatter ids, applied right to left.  Common
# English words from Talon's list ("list", "sentence", "string", "speak",
# "padded") are left out: they start ordinary sentences too often.
FORMATTERS: dict[str, str] = {
    "say": "NOOP",
    "cap": "CAPITALIZE",
    "title": "CAPITALIZE_ALL_WORDS",
    "all caps": "ALL_CAPS",
    "all cap": "ALL_CAPS",
    "upper case": "ALL_CAPS",
    "uppercase": "ALL_CAPS",
    "all down": "ALL_LOWERCASE",
    "lower case": "ALL_LOWERCASE",
    "lowercase": "ALL_LOWERCASE",
    "camel": "PRIVATE_CAMEL_CASE",
    "camel case": "PRIVATE_CAMEL_CASE",
    "hammer": "PUBLIC_CAMEL_CASE",
    "pascal": "PUBLIC_CAMEL_CASE",
    "pascal case": "PUBLIC_CAMEL_CASE",
    "snake": "SNAKE_CASE",
    "snake case": "SNAKE_CASE",
    "kebab": "DASH_SEPARATED",
    "kebab case": "DASH_SEPARATED",
    "kabob": "DASH_SEPARATED",  # how recognisers often write "kebab"
    "constant": "ALL_CAPS,SNAKE_CASE",
    "dotted": "DOT_SEPARATED",
    "smash": "NO_SPACES",
    "dub string": "DOUBLE_QUOTED_STRING",
    "dunder": "DOUBLE_UNDERSCORE",
    "packed": "DOUBLE_COLON_SEPARATED",
    "slasher": "ALL_SLASHES",
    "conga": "SLASH_SEPARATED",
}

# Formatters whose output is prose, so dictation spacing rules still apply.
PROSE_FORMATTERS = {"NOOP", "CAPITALIZE", "CAPITALIZE_ALL_WORDS", "ALL_CAPS", "ALL_LOWERCASE"}

# --------------------------------------------------------------------------- numbers and fillers

NUMBER_HOMOPHONES: dict[str, int] = {
    # Only applied where the grammar expects a number ("tab for times", "scroll down to").
    "won": 1,
    "to": 2,
    "too": 2,
    "tu": 2,
    "tree": 3,
    "free": 3,
    "for": 4,
    "fore": 4,
    "ate": 8,
}

# Words that flip a command's meaning.  A fuzzy match must be clearly closer to one side than
# the other ("hot keyboard" is as close to "hide keyboard" as to "show keyboard").
OPPOSITE_WORDS: frozenset[frozenset[str]] = frozenset(
    frozenset(pair.split())
    for pair in (
        "show hide",
        "on off",
        "open close",
        "start stop",
        "pause resume",
        "mute unmute",
        "in out",
        "up down",
        "left right",
        "next previous",
        "forward back",
        "bigger smaller",
        "faster slower",
        "louder quieter",
        "minimise maximise",
        "minimize maximize",
        "undo redo",
        "copy paste",
        "top bottom",
        "enable disable",
        "hold release",
    )
)

FILLER_WORDS = {"um", "umm", "uh", "uhh", "er", "erm", "ah", "hmm", "mm", "mhm"}
