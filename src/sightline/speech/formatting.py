"""Dictation formatting: spoken punctuation, capitalisation, spacing, spelling and code formatters.

The spacing and capitalisation rules (``no_space_after``, ``no_space_before``,
``auto_capitalize``) and the formatter implementations are adapted from
talonhub/community, core/text/text_and_dictation.py and
core/formatters/formatters.py, commit de1c2a33, MIT licence, Copyright (c)
2021 Jeff Knaus, Ryan Hileman, Zach Dwiel, Michael Arntzenius, and others.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from text_to_num import alpha2digit

from .normalize import clean_transcript
from .vocab import FORMATTERS, PUNCTUATION_WORDS, SYMBOL_KEYS, all_letter_words

# --------------------------------------------------------------------------- spacing (Talon)

_NO_SPACE_AFTER = re.compile(
    r"""
  (?:
    [\s\-_/#@([{‘“]
  | (?<!\w)[$£€¥₩₽₹]
  | (?: ^ | [\s([{\-'"] ) ['"]
  )$""",
    re.VERBOSE,
)
_NO_SPACE_BEFORE = re.compile(
    r"""
  ^(?:
    [\s\-_.,!?/%)\]}’”]
  | [$£€¥₩₽₹](?!\w)
  | [;:](?!-\)|-\()
  | ['"] (?: $ | [\s)\]}\-'".,!?;:/] )
  | 's(?!\w)
  )""",
    re.VERBOSE,
)
_NO_CAP_AFTER = re.compile(r"(e\.g\.|i\.e\.)$")


def omit_space_after(text: str) -> bool:
    return bool(not text or _NO_SPACE_AFTER.search(text))


def omit_space_before(text: str) -> bool:
    return bool(not text or _NO_SPACE_BEFORE.search(text))


def needs_space_between(before: str, after: str) -> bool:
    return not (omit_space_after(before) or omit_space_before(after))


def auto_capitalize(text: str, state: str | None = None) -> tuple[str, str | None]:
    """Capitalise sentence starts in ``text``.

    ``state`` "sentence start" capitalises the first word, None does not.
    Returns the text and the state after it.  Unlike Talon, a single new line
    also starts a sentence: people dictating letters and lists expect
    "Dear Sam, new line, thanks" to put "Thanks" on the new line.
    """
    output: list[str] = []
    charge = state in ("sentence start", "after newline")
    newline = state == "after newline"
    sentence_end = False
    so_far = ""
    for c in text:
        if (sentence_end and c in " \n\t") or c == "\n":
            charge = True
        elif charge and (c.isalnum() or c in ",:"):
            charge = False
            c = c.upper()
        output.append(c)
        so_far += c
        newline = c == "\n"
        sentence_end = c in ".!?" and not _NO_CAP_AFTER.search(so_far)
    result = "".join(output)
    if charge or sentence_end:
        return result, "sentence start"
    return result, "after newline" if newline else None


def state_after(before: str | None) -> str | None:
    """Capitalisation state implied by the text already in the field."""
    if before is None or not before.strip():
        return "sentence start"
    _, state = auto_capitalize(before, None)
    return state


# --------------------------------------------------------------------------- recogniser output to dictation words

# Common words a recogniser capitalises only because they start its output.
# Mid-sentence they go back to lower case; anything else (names, places) keeps
# the recogniser's capital letter.
_COMMON_WORDS = frozenset(
    """
a about above after again against all almost also always am an and another any anyone anything are around as ask
at away back be because been before being below best better between both but by call can could day did do does
doing done down each either else even ever every everyone everything few find first for from get give go going good
got great had has have having he her here hers him his how however i if in into is it its just keep know last
later least less let let's like little look lot made make many may maybe me might more most much must my need never
new next no not nothing now of off often oh ok okay on once one only or other our out over please put quite rather
really right said same say see send she should since so some someone something sometimes soon still such sure take
tell than thank thanks that the their them then there these they thing things think this those though through time
to today together tomorrow too try under until up us use very want was we well were what when where whether which
while who whom whose why will with without would yes yesterday yet you your yours also hello hi hey dear sorry
great just next please thanks yeah sure right well actually anyway basically finally first second then
""".split()
)

_TRAILING_ASR_PUNCT = re.compile(r"(?<=[\w)\]'\"])[.,!?;:]+(?=\s|$)")
_LEADING_ASR_PUNCT = re.compile(r"(?:^|(?<=\s))[\"'(\[]+(?=\w)")


def _recase_first_word(text: str) -> str:
    match = re.match(r"^(\W*)([A-Za-z][\w']*)", text)
    if not match:
        return text
    word = match.group(2)
    lower = word.lower()
    if word == "I" or lower.startswith(("i'", "i’")) and word[0] == "I":
        return text
    if len(word) > 1 and word.isupper():
        return text  # acronym
    if any(ch.isupper() for ch in word[1:]):
        return text  # iPhone, McDonald
    if lower in _COMMON_WORDS:
        return text[: match.start(2)] + lower + text[match.end(2) :]
    return text


def _apply_spoken_punctuation(text: str) -> str:
    words = text.split(" ")
    if not words:
        return text
    phrases = sorted(PUNCTUATION_WORDS, key=lambda p: -len(p.split()))
    out: list[str] = []
    i = 0
    lowered = [w.lower().strip(".,!?;:") for w in words]
    while i < len(words):
        for phrase in phrases:
            parts = phrase.split()
            n = len(parts)
            if lowered[i : i + n] == parts:
                out.append("\x00" + PUNCTUATION_WORDS[phrase])
                i += n
                break
        else:
            out.append(words[i])
            i += 1
    # Join, gluing punctuation to the previous word where the spacing rules say so.
    result = ""
    for piece in out:
        token = piece[1:] if piece.startswith("\x00") else piece
        if not result:
            result = token
        elif token in ("\n", "\n\n"):
            result = result.rstrip(" ") + token
        elif needs_space_between(result, token):
            result += " " + token
        else:
            result += token
    return result


def dictation_text(
    raw: str,
    *,
    spoken_punctuation: bool = True,
    keep_recogniser_punctuation: bool = False,
    convert_numbers: bool = True,
) -> str:
    """Turn one recognised utterance into the words to type, before spacing and capitalisation."""
    text = clean_transcript(raw)
    if not text:
        return ""
    if not keep_recogniser_punctuation:
        text = _TRAILING_ASR_PUNCT.sub("", text)
        text = _LEADING_ASR_PUNCT.sub("", text)
    text = _recase_first_word(text)
    if convert_numbers and any(ch.isalpha() for ch in text):
        try:
            # Ten and above as digits, smaller numbers stay as words (the usual style-guide rule).
            text = alpha2digit(text, "en", threshold=10)
        except Exception:
            pass
    if spoken_punctuation:
        text = _apply_spoken_punctuation(text)
    text = text.replace("“", '"').replace("”", '"')
    return re.sub(r"[ \t]+", " ", text).strip(" ")


def place_text(text: str, before: str | None, *, auto_cap: bool = True, smart_spacing: bool = True) -> str:
    """Add the leading space and sentence capitals that ``text`` needs after ``before``.

    ``before`` is the text already left of the caret, or None when unknown
    (treated as the start of an empty field).
    """
    if not text:
        return ""
    context = before or ""
    if smart_spacing and context and needs_space_between(context, text):
        text = " " + text
    if auto_cap:
        text, _ = auto_capitalize(text, state_after(before))
    return text


# --------------------------------------------------------------------------- formatters (Talon)


def _capitalize_first(text: str) -> str:
    stripped = text.lstrip()
    prefix = text[: len(text) - len(stripped)]
    return prefix + stripped[:1].upper() + stripped[1:]


def _code_format(text: str, delimiter: str, first: Callable[[str], str] | None, rest: Callable[[str], str] | None) -> str:
    text = re.sub(r"[^\w\d\s.,]+", "", text)
    words = re.split(r"([^\w\d]+)", text)
    groups: list[str] = []
    group: list[str] = []
    is_first = True
    for word in words:
        if not word or word.isspace():
            continue
        if word.isnumeric():
            is_first = True
        elif not word.isalnum():
            groups.append(delimiter.join(group))
            word = word.strip()
            if word != ".":
                word += " "
            is_first = True
            groups.append(word)
            group = []
            continue
        elif is_first:
            is_first = False
            if first:
                word = first(word)
        elif rest:
            word = rest(word)
        group.append(word)
    groups.append(delimiter.join(group))
    return "".join(groups)


_TITLE_LOWER = {"a", "an", "and", "as", "at", "but", "by", "en", "for", "if", "in", "nor", "of", "on", "or", "per", "the", "to", "v", "via", "vs"}


def _title(text: str) -> str:
    words = [x for x in re.split(r"(\s+)", text) if x]
    following_symbol = False
    for i, word in enumerate(words):
        if word.isspace():
            continue
        is_first = i == 0
        is_last = i == len(words) - 1
        if word.islower() and not (word in _TITLE_LOWER and not is_first and not is_last and not following_symbol):
            words[i] = word.capitalize()
        following_symbol = not word[-1].isalnum()
    return "".join(words)


def _lower(s: str) -> str:
    return s.lower()


def _cap(s: str) -> str:
    return s.capitalize()


_FORMATTER_FUNCS: dict[str, Callable[[str], str]] = {
    "NOOP": lambda t: t,
    "DOUBLE_QUOTED_STRING": lambda t: f'"{t}"',
    "SINGLE_QUOTED_STRING": lambda t: f"'{t}'",
    "ALL_CAPS": lambda t: t.upper(),
    "ALL_LOWERCASE": lambda t: t.lower(),
    "CAPITALIZE_ALL_WORDS": _title,
    "CAPITALIZE": lambda t: re.sub(r"^\s*\S+", lambda m: _capitalize_first(m.group()), t),
    "NO_SPACES": lambda t: _code_format(t, "", _lower, _lower),
    "PRIVATE_CAMEL_CASE": lambda t: _code_format(t, "", _lower, _cap),
    "PUBLIC_CAMEL_CASE": lambda t: _code_format(t, "", _cap, _cap),
    "SNAKE_CASE": lambda t: _code_format(t, "_", _lower, _lower),
    "DASH_SEPARATED": lambda t: _code_format(t, "-", _lower, _lower),
    "DOT_SEPARATED": lambda t: _code_format(t, ".", _lower, _lower),
    "SLASH_SEPARATED": lambda t: _code_format(t, "/", _lower, _lower),
    "ALL_SLASHES": lambda t: _code_format(t, "/", lambda w: f"/{w.lower()}", _lower),
    "DOUBLE_UNDERSCORE": lambda t: _code_format(t, "__", _lower, _lower),
    "DOUBLE_COLON_SEPARATED": lambda t: _code_format(t, "::", _lower, _lower),
}


def apply_formatters(text: str, formatter_ids: str) -> str:
    """Apply comma-separated Talon formatter ids, right to left (as Talon does)."""
    for name in reversed(formatter_ids.split(",")):
        text = _FORMATTER_FUNCS[name](text)
    return text


def formatter_for(word: str) -> str | None:
    return FORMATTERS.get(word)


# --------------------------------------------------------------------------- spelling

_LETTERS = all_letter_words()
_UPPER_PREFIXES = {"capital", "upper", "uppercase", "ship"}
_SPELL_SYMBOLS = {k: v for k, v in SYMBOL_KEYS.items()}
_SPELL_SYMBOLS.update({"space": " "})


def spell(words: str) -> str | None:
    """Letters, digits and symbols said one by one ("air bat cap", "capital alpha 2") as text.

    Returns None when a word is not a letter, digit or symbol, so "spell ..."
    never types a guess.
    """
    tokens = words.split()
    out: list[str] = []
    upper_next = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        two = " ".join(tokens[i : i + 2])
        if tok in _UPPER_PREFIXES:
            upper_next = True
            i += 1
            continue
        if two in _LETTERS or two in _SPELL_SYMBOLS:
            ch = _LETTERS.get(two) or _SPELL_SYMBOLS[two]
            i += 2
        elif tok in _LETTERS:
            ch = _LETTERS[tok]
            i += 1
        elif tok.isdigit():
            ch = tok
            i += 1
        elif tok in _SPELL_SYMBOLS:
            ch = _SPELL_SYMBOLS[tok]
            i += 1
        elif len(tok) == 1 and not tok.isalnum():
            ch = tok
            i += 1
        else:
            return None
        if upper_next:
            ch = ch.upper()
            upper_next = False
        out.append(ch)
    return "".join(out) if out else None
