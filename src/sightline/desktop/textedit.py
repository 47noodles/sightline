"""Finding a spoken phrase in a text field: "select", "select from ... to ..." and "move before/after".

The UI Automation thread reads a window of text around the caret
(TextPattern), finds the phrase there and selects it with a text range.
Where a field cannot be driven that way, the same search runs on the text
before the caret and the caret walks there with the arrow keys, holding
Shift to select.  Both paths use these functions, which need no Windows.

Matching is forgiving in the ways speech needs.  Case does not matter, any
short run of spaces, punctuation or line breaks between the words counts as
a gap ("quick brown" finds "quick, brown"), straight and curly apostrophes
are the same, and only whole words match ("cat" does not find
"concatenate").  When the phrase occurs more than once, the occurrence
nearest the caret wins and one before the caret wins a tie, because people
mostly correct what they have just said.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

TextOpKind = Literal["select", "range", "caret"]

# Arrow-key presses a walk may take.  Each press takes about 25 ms, so this is about six seconds.
MAX_WALK_STEPS = 250

_WORD = re.compile(r"\w+(?:['’]\w+)*")
_APOSTROPHE = re.compile(r"['’]")
_GAP = r"[\W_]{1,12}"  # what may separate two spoken words in the text


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class TextQuery:
    """One text command.  ``kind`` "range" selects from ``phrase`` to ``end_phrase``."""

    kind: TextOpKind
    phrase: str
    end_phrase: str = ""
    where: Literal["before", "after"] = "after"  # for "caret"


@dataclass(frozen=True, slots=True)
class TextEditResult:
    """What the UI Automation side did, or the keys the action thread should press instead.

    ``status``: "done" (selected or moved through UI Automation), "walk" (press ``keys``),
    "not_found" (``missing`` names the phrase), "password", "unsupported" (no way to read the
    field's text) or "too_far" (found, but more arrow presses away than a walk allows).
    """

    status: Literal["done", "walk", "not_found", "password", "unsupported", "too_far"]
    keys: tuple[tuple[str, int], ...] = ()
    missing: str = ""


def phrase_pattern(phrase: str) -> re.Pattern[str] | None:
    words = _WORD.findall(phrase or "")
    if not words:
        return None
    pieces = [_APOSTROPHE.sub(lambda _m: "['’]", re.escape(word)) for word in words]
    return re.compile(r"(?<!\w)" + _GAP.join(pieces) + r"(?!\w)", re.IGNORECASE)


def find_all(text: str, phrase: str) -> list[Span]:
    pattern = phrase_pattern(phrase)
    if pattern is None:
        return []
    return [Span(m.start(), m.end()) for m in pattern.finditer(text)]


def nearest(spans: list[Span], caret: int) -> Span | None:
    """The span nearest the caret; one before the caret wins a tie, and one around it counts as before."""

    def key(span: Span) -> tuple[int, int, int]:
        if span.start >= caret and span.end > caret:
            return (span.start - caret, 1, span.start)
        return (max(0, caret - span.end), 0, -span.start)

    return min(spans, key=key) if spans else None


def locate(text: str, caret: int, query: TextQuery) -> Span | str:
    """The span to select (or, for "caret", the empty span where the caret goes).

    Returns the phrase that could not be found instead, for the error message.
    """
    first = nearest(find_all(text, query.phrase), caret)
    if first is None:
        return query.phrase
    if query.kind == "select":
        return first
    if query.kind == "caret":
        pos = first.start if query.where == "before" else first.end
        return Span(pos, pos)
    ends = find_all(text, query.end_phrase)
    later = [s for s in ends if s.start >= first.end]
    if later:
        last = min(later, key=lambda s: s.start)
    else:
        # "select from lazy to quick" when the words come the other way round: still the span between them.
        earlier = [s for s in ends if s.end <= first.start]
        if not earlier:
            return query.end_phrase
        last = max(earlier, key=lambda s: s.start)
    return Span(min(first.start, last.start), max(first.end, last.end))


def key_steps(text: str, i: int, j: int) -> int:
    """Arrow presses between two indexes: a CR LF pair is one step."""
    if j < i:
        i, j = j, i
    piece = text[i:j]
    return len(piece) - piece.count("\r\n")


def walk_plan(text: str, caret: int, span: Span, *, select: bool, collapse: bool) -> tuple[tuple[str, int], ...] | None:
    """Arrow keys that move the caret from ``caret`` to ``span`` (and select it).  None when too far.

    ``collapse``: text is selected now, and ``caret`` is the start of the selection.  One Left
    press collapses a selection to its start in every common editor, without moving further.
    """
    keys: list[tuple[str, int]] = [("left", 1)] if collapse else []
    if not select:
        target = span.start
        if target < caret:
            keys.append(("left", key_steps(text, target, caret)))
        elif target > caret:
            keys.append(("right", key_steps(text, caret, target)))
    elif span.end <= caret:
        keys.append(("left", key_steps(text, span.end, caret)))
        keys.append(("shift+left", key_steps(text, span.start, span.end)))
    elif span.start >= caret:
        keys.append(("right", key_steps(text, caret, span.start)))
        keys.append(("shift+right", key_steps(text, span.start, span.end)))
    else:
        keys.append(("left", key_steps(text, span.start, caret)))
        keys.append(("shift+right", key_steps(text, span.start, span.end)))
    plan = tuple((combo, n) for combo, n in keys if n > 0)
    if sum(n for _combo, n in plan) > MAX_WALK_STEPS:
        return None
    return plan


def same_text(a: str, b: str) -> bool:
    """Whether two readings of the same text agree, ignoring how line breaks and spaces were reported."""
    return " ".join(a.split()) == " ".join(b.split())
