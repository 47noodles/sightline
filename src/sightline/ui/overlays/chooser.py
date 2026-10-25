"""The local number chooser: when a gaze click could mean two to six targets, number them and let the user pick.

Concept review item 10.  Webcam gaze is off by 60 to 230 px, several toolbar buttons'
worth, so the clickable nearest a gaze click is often not the one the user looked at.
The literature's answer is to carry the uncertainty and decide late, but only when unsure:
a confirm step costs time, so the chooser must be the exception, one number or one blink.

An interceptor looks at every ``mouse.click`` from the eyes, dwell or voice:

* When the pointer can be trusted the click goes straight through: the head has steered
  it since gaze last placed it, or the pointer is head-only (or vision is not placing it).
* Otherwise it counts the clickables within the expected gaze error (``error_px``, 60 px
  before calibration, at most 250).  One goes through; two to six are held back and given
  large numbered chips; more than six open the zoom lens on that spot.

Each chip shows a number and the element's name, joined to the element by a line and an
outline that turn amber while the pointer rests on the chip.  Candidates in a row (a
toolbar) get a row of chips numbered left to right, a column (a list) a column numbered
top to bottom, and a cluster a ring numbered clockwise from the top; near an edge or in
a crowd the clearest layout that fits wins.  Saying the number, or a blink or
dwell on a chip, re-runs the original click (same button and count) on that element, with
``chosen=True`` so it is not held back again.  ``ui.chooser.cancel``, ``ui.cancel`` or 8 s
without a choice closes it (longer while the pointer rests on a chip, up to 30 s).

A choice is the best lesson click learning gets: where the user really looked when gaze
missed.  So the re-run carries ``aim_t``, the moment the first click was aimed (the speech
onset, or just before the blink), and an eye or dwell click comes back as Sightline's own
(source "ui"), because vision pairs an eye click with its latest blink nearby, which
would be the blink on the chip.
"""

from __future__ import annotations

import itertools
import logging
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QFontMetricsF, QPainter, QPainterPath

from sightline.core.actions import ActionError
from sightline.core.events import EyeGesture, PointerSample
from sightline.core.types import Action, ElementInfo, Rect

from ..coords import ScreenGeometry
from ..services import UiServices
from .base import OverlayWindow, solid_panel, two_tone_path
from .shared import (
    TILE_HEIGHT,
    USER_SOURCES,
    OverlaySlot,
    OwnWindows,
    Teardown,
    TileWindow,
    gaze_tolerance,
    point_of,
    register_ui_actions,
    tile_at,
)

if TYPE_CHECKING:
    from sightline.core.context import AppContext

log = logging.getLogger(__name__)

NAME = "chooser"
CHOSEN = "chosen"  # set on the click a choice re-runs, so the interceptor lets it through
PRIORITY = 50  # after the zoom lens (110), numbers and grid (100), help (90) and the action menu (60)
MAX_CHOICES = 6
DEFAULT_ERROR_PX = 60.0  # before calibration has measured the real error
MAX_ERROR_PX = 250.0
STALE_S = 2.0  # no pointer sample for this long: vision is not placing the pointer
LIDS_LOOKBACK_S = 0.12  # vision aims a blink click this long before the lids begin to move (its gestures.lookback_s)
CHOOSER_SECONDS = 8.0
MAX_SECONDS = 30.0  # the pointer resting on a chip keeps them up, but not for ever
RECHECK_MS = 1500
LABEL_MAX = 120.0  # logical pixels of an element's name shown on its chip
REACH_CAP = 60.0  # logical pixels past the search radius that the ring keeps clear for long candidates
HINT = "Which one? Say its number, or look at it and blink."


# =========================================================================== can the pointer be trusted


def search_radius(error_px: float | None) -> float:
    """How far round a gaze click to look for what was meant, in physical pixels."""
    if error_px is None or not math.isfinite(error_px) or error_px <= 0:
        return DEFAULT_ERROR_PX
    return min(MAX_ERROR_PX, float(error_px))


@dataclass(frozen=True, slots=True)
class _Judgement:
    received: float  # clock time of the latest sample
    phase: str  # the latest phase as published
    from_gaze: bool  # gaze placed the pointer last (a jump in hybrid mode, or gaze-only)
    steered: bool  # the head has moved it since, as vision reports (``PointerSample.steered``)
    error_px: float | None


@dataclass(frozen=True, slots=True)
class _Blink:
    t: float  # when vision finished the gesture
    onset: float  # when the lids began to move
    x: float  # where it clicks
    y: float


class PointerTrust:
    """Whether the head has checked the pointer since gaze last placed it, from the pointer samples.

    Vision reports "fine" from about 80 ms after every jump whether or not the head has
    moved, so the phase alone cannot say the head steered; ``PointerSample.steered`` does.
    Vision sets it as soon as the head (or a nudge, or the mouse) has moved the pointer a
    couple of pixels after a jump, and clears it on the next jump; it keeps its value through
    "frozen" samples (the eyes closing for a click, a reading hold).  The phases say whether
    gaze placed the pointer at all: "warp" and "gaze" mean it did, "head" that the head
    alone drives it.  It also notes the last eye gesture, so a held click knows when the
    user aimed it.  Updated on the vision thread and read on the action thread: each event
    replaces one immutable record.
    """

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._j: _Judgement | None = None
        self._blink: _Blink | None = None

    def on_gesture(self, e: EyeGesture) -> None:
        self._blink = _Blink(e.t, e.t - max(0.0, e.duration), e.x, e.y)

    def aimed_at(self, action: Action) -> float:
        """When the user aimed a click (``time.perf_counter``), so a choice made later teaches click learning the right moment.

        A spoken click carries its speech onset.  An eye click is aimed just before the lids
        began to move for its gesture, as vision anchors it.  A dwell click, and anything
        else, was aimed as it arrived.
        """
        if action.aim_t is not None:
            return float(action.aim_t)
        now = self._clock()
        blink, x, y = self._blink, action.arg("x"), action.arg("y")
        if action.source == "eye" and blink is not None and x is not None and y is not None:
            if now - blink.t < 1.0 and abs(blink.x - float(x)) <= 2.0 and abs(blink.y - float(y)) <= 2.0:
                return blink.onset - LIDS_LOOKBACK_S
        return now

    def on_sample(self, e: PointerSample) -> None:
        j = self._j
        from_gaze = j.from_gaze if j is not None else False
        if e.phase in ("warp", "gaze"):
            from_gaze = True
        elif e.phase == "head":
            from_gaze = False
        self._j = _Judgement(self._clock(), e.phase, from_gaze, bool(e.steered), e.error_px)

    def verdict(self, mode: object) -> tuple[bool, float]:
        """(trusted, search radius).  ``mode`` is ``state.pointer_mode``: "hybrid", "head" or "gaze"."""
        j = self._j
        radius = search_radius(j.error_px if j is not None else None)
        if j is None or self._clock() - j.received > STALE_S:
            return True, radius  # vision is not placing the pointer: nothing to doubt
        if j.phase in ("paused", "lost"):
            return True, radius
        if mode == "head":
            return True, radius
        if mode == "gaze":
            return False, radius  # gaze-only users have no head fine-tune to fall back on
        if j.from_gaze and not j.steered:
            return False, radius
        return True, radius


# =========================================================================== candidates (pure)


def pick_candidates(found: Sequence[ElementInfo]) -> list[ElementInfo]:
    """Clickables that can be told apart: no empty rectangles, one per rectangle (the nearest kept)."""
    out: list[ElementInfo] = []
    seen: set[tuple[int, int, int, int]] = set()
    for el in found:
        r = el.rect
        if r.is_empty():
            continue
        key = (round(r.x), round(r.y), round(r.w), round(r.h))
        if key in seen:
            continue
        seen.add(key)
        out.append(el)
    return out


def click_point(rect: Rect, x: float, y: float, radius: float) -> tuple[float, float]:
    """Where choosing an element clicks it: its centre, unless it is large and its centre far from where the user looked.

    Then just inside its edge nearest that point, so picking a wide list row does not click
    hundreds of pixels away.  The desktop snaps clicks by the same rule.
    """
    cx, cy = rect.center
    if (cx - x) ** 2 + (cy - y) ** 2 <= (1.5 * radius) ** 2:
        return (float(int(cx)), float(int(cy)))
    inset_x = min(12.0, rect.w / 4.0)
    inset_y = min(12.0, rect.h / 4.0)
    px = min(max(x, rect.x + inset_x), rect.right - 1 - inset_x)
    py = min(max(y, rect.y + inset_y), rect.bottom - 1 - inset_y)
    return (float(int(px)), float(int(py)))


def click_points(rects: Sequence[Rect], x: float, y: float, radius: float) -> list[tuple[float, float]]:
    """``click_point`` for each, moved off any smaller candidate inside it (a row's centre can be one of its links)."""
    out: list[tuple[float, float]] = []
    for i, rect in enumerate(rects):
        point = click_point(rect, x, y, radius)
        inner = [r for j, r in enumerate(rects) if j != i and r.area < rect.area and rect.intersects(r)]
        if any(r.inflate(2.0).contains(*point) for r in inner):
            point = _free_point(rect, inner, point) or point
        out.append(point)
    return out


def _free_point(rect: Rect, blockers: Sequence[Rect], near: tuple[float, float]) -> tuple[float, float] | None:
    best: tuple[float, tuple[float, float]] | None = None
    for i in range(7):
        for j in range(7):
            px = rect.x + rect.w * (i + 0.5) / 7.0
            py = rect.y + rect.h * (j + 0.5) / 7.0
            if any(b.inflate(2.0).contains(px, py) for b in blockers):
                continue
            d = (px - near[0]) ** 2 + (py - near[1]) ** 2
            if best is None or d < best[0]:
                best = (d, (float(int(px)), float(int(py))))
    return best[1] if best is not None else None


# =========================================================================== chip layout (pure, physical pixels)


@dataclass(frozen=True, slots=True)
class ChipSpot:
    index: int  # which candidate the chip is for
    number: int  # 1-based
    rect: Rect  # physical pixels


STEP_DEG = 3.0


def clockwise_degrees(dx: float, dy: float) -> float:
    """The direction of (dx, dy) on screen (y grows down), in degrees clockwise from 12 o'clock."""
    return math.degrees(math.atan2(dx, -dy)) % 360.0


def _angle_gap(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def _support(w: float, h: float, deg: float) -> float:
    """Half the extent of a w x h rectangle along a direction."""
    r = math.radians(deg)
    return abs(math.sin(r)) * w / 2.0 + abs(math.cos(r)) * h / 2.0


def _chip_rect(cx: float, cy: float, radius: float, deg: float, w: float, h: float) -> Rect:
    """A w x h chip in direction ``deg`` whose nearest side is ``radius`` from the centre."""
    r = math.radians(deg)
    d = radius + _support(w, h, deg)
    x, y = cx + d * math.sin(r), cy - d * math.cos(r)
    return Rect(x - w / 2.0, y - h / 2.0, w, h)


def _fits(r: Rect, work: Rect) -> bool:
    return r.x >= work.x and r.y >= work.y and r.right <= work.right and r.bottom <= work.bottom


def _free_arc(cx: float, cy: float, radius: float, w: float, h: float, work: Rect) -> tuple[float, float] | None:
    """The widest run of directions in which a chip stays on screen: (start, span) clockwise, span 360 for all round."""
    n = int(round(360.0 / STEP_DEG))
    ok = [_fits(_chip_rect(cx, cy, radius, k * STEP_DEG, w, h), work) for k in range(n)]
    if all(ok):
        return (0.0, 360.0)
    if not any(ok):
        return None
    first_blocked = ok.index(False)
    best_start, best_len, run_start, run_len = 0, 0, 0, 0
    for k in range(1, n + 1):
        i = (first_blocked + k) % n
        if ok[i]:
            if run_len == 0:
                run_start = i
            run_len += 1
            if run_len > best_len:
                best_start, best_len = run_start, run_len
        else:
            run_len = 0
    return (best_start * STEP_DEG, (best_len - 1) * STEP_DEG)


def _directions(targets: Sequence[tuple[float, float]], cx: float, cy: float) -> list[float]:
    """Each target's direction from the centre.  One at the centre takes the middle of the widest empty sector."""
    raw: list[float | None] = [None if math.hypot(x - cx, y - cy) < 2.0 else clockwise_degrees(x - cx, y - cy) for x, y in targets]
    known = sorted(a for a in raw if a is not None)
    missing = [i for i, a in enumerate(raw) if a is None]
    if not missing:
        return [a for a in raw if a is not None]
    if not known:
        start, width = 0.0, 360.0
    else:
        start, width = known[-1], known[0] + 360.0 - known[-1]  # the sector that wraps past 12 o'clock
        for a, b in zip(known, known[1:]):
            if b - a > width:
                start, width = a, b - a
    out = list(raw)
    for k, i in enumerate(missing):
        out[i] = (start + width * (k + 1) / (len(missing) + 1)) % 360.0
    return [float(a) for a in out if a is not None]


def _slots_all_round(angles: Sequence[float]) -> list[float]:
    """Evenly spaced directions, turned to match the candidates' own directions in clockwise order."""
    n = len(angles)
    order = sorted(range(n), key=lambda i: angles[i])
    spacing = 360.0 / n
    best: tuple[float, float, int] | None = None
    for shift in range(n):
        for k in range(int(spacing / STEP_DEG)):
            base = k * STEP_DEG
            cost = sum(_angle_gap(angles[i], base + ((j + shift) % n) * spacing) ** 2 for j, i in enumerate(order))
            if best is None or cost < best[0] - 1e-9:
                best = (cost, base, shift)
    assert best is not None
    _cost, base, shift = best
    slots = [0.0] * n
    for j, i in enumerate(order):
        slots[i] = (base + ((j + shift) % n) * spacing) % 360.0
    return slots


def _slots_on_arc(angles: Sequence[float], start: float, span: float) -> list[float]:
    """Directions spread over an arc, in the candidates' clockwise order starting from the blocked side."""
    n = len(angles)
    cut = start + span + (360.0 - span) / 2.0  # the middle of the sector off screen
    order = sorted(range(n), key=lambda i: (angles[i] - cut) % 360.0)
    slots = [0.0] * n
    for k, i in enumerate(order):
        slots[i] = (start + (span * k / (n - 1) if n > 1 else span / 2.0)) % 360.0
    return slots


def _numbers(slots: Sequence[float], first: float) -> list[int]:
    """1 for the slot reached first going clockwise from ``first``, and so on."""
    order = sorted(range(len(slots)), key=lambda i: (slots[i] - first) % 360.0)
    numbers = [0] * len(slots)
    for rank, i in enumerate(order):
        numbers[i] = rank + 1
    return numbers


def _overlapping(rects: Sequence[Rect], gap: float) -> bool:
    grown = [r.inflate(gap / 2.0) for r in rects]
    return any(a.intersects(b) for i, a in enumerate(grown) for b in grown[i + 1 :])


def _farthest(rect: Rect, cx: float, cy: float) -> float:
    return max(math.hypot(x - cx, y - cy) for x in (rect.left, rect.right) for y in (rect.top, rect.bottom))


def place_chips(
    targets: Sequence[tuple[float, float]],
    rects: Sequence[Rect],
    sizes: Sequence[tuple[float, float]],
    work: Rect,
    *,
    gap: float,
    clear: float,
) -> list[ChipSpot]:
    """Where each chip goes and its number.  All physical pixels.

    ``targets`` are where each candidate would be clicked, ``rects`` the candidates and
    ``sizes`` the chips.  Candidates in a row (a toolbar, tabs) get a row of chips above
    them, or below, numbered left to right; candidates in a column (a menu, a list) get a
    column beside them, numbered top to bottom.  A clockwise ring would number a toolbar
    right to left.  Anything spread both ways gets a ring round it, evenly spaced (as far
    apart as chips can be, which a gaze pick needs) and numbered clockwise from the top;
    near an edge the ring becomes the widest arc that stays on screen.  When the natural
    layout has no room, the cheapest that fits wins: short lines, no chip over a
    candidate, no line across another.  ``clear`` limits how much of a long candidate (a
    wide list row) a ring keeps clear of, and is the strips' last resort.
    """
    n = len(targets)
    if n == 0:
        return []
    xs = [p[0] for p in targets]
    ys = [p[1] for p in targets]
    span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
    flat = 0.35 * max(s[1] for s in sizes)
    if span_x > 0 and span_y <= max(flat, 0.3 * span_x):
        spots = _strip(targets, rects, sizes, work, gap, horizontal=True)
        if spots is not None:
            return spots
    elif span_y > 0 and span_x <= max(flat, 0.3 * span_y):
        spots = _strip(targets, rects, sizes, work, gap, horizontal=False)
        if spots is not None:
            return spots
    cx, cy = sum(xs) / n, sum(ys) / n
    box = Rect(cx - clear, cy - clear, 2 * clear, 2 * clear)
    near = [r.intersection(box) if r.intersects(box) else r for r in rects]
    options = [_ring(targets, rects, sizes, work, gap, clear)]
    options += [_strip(targets, extent, sizes, work, gap, horizontal=h) for h in (True, False) for extent in (rects, near)]
    fitting = [spots for spots in options if spots]
    if fitting:
        return min(fitting, key=lambda spots: layout_cost(spots, rects))
    return _strip(targets, near, sizes, work, gap, horizontal=True, anywhere=True) or []


COVER_COST = 1000.0  # a chip hiding a candidate costs as much as this much line, times the share of the chip over it
CROSS_COST = 150.0  # a line across another candidate, or across another line


def layout_cost(spots: Sequence[ChipSpot], rects: Sequence[Rect]) -> float:
    """How hard a layout is to read: the lines' length, plus chips over candidates and lines that cross."""
    cost = 0.0
    lines: list[tuple[int, tuple[float, float], tuple[float, float]]] = []
    for spot in spots:
        start, end = _leader(spot.rect, rects[spot.index])
        lines.append((spot.index, start, end))
        cost += math.hypot(end[0] - start[0], end[1] - start[1])
        area = max(1.0, spot.rect.area)
        for r in rects:
            if spot.rect.intersects(r):
                cost += COVER_COST * spot.rect.intersection(r).area / area
    for index, start, end in lines:
        cost += CROSS_COST * sum(1 for j, r in enumerate(rects) if j != index and _segment_hits(start, end, r))
    for k, (_i, a1, b1) in enumerate(lines):
        cost += CROSS_COST * sum(1 for _j, a2, b2 in lines[k + 1 :] if _segments_cross(a1, b1, a2, b2))
    return cost


def _segment_hits(a: tuple[float, float], b: tuple[float, float], r: Rect) -> bool:
    """Whether the segment a-b passes through the rectangle (Liang-Barsky clipping)."""
    if a == b:
        return False
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in ((-dx, a[0] - r.left), (dx, r.right - a[0]), (-dy, a[1] - r.top), (dy, r.bottom - a[1])):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 >= t1:
            return False
    return True


def _segments_cross(a1: tuple[float, float], b1: tuple[float, float], a2: tuple[float, float], b2: tuple[float, float]) -> bool:
    def side(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    d1, d2 = side(a2, b2, a1), side(a2, b2, b1)
    d3, d4 = side(a1, b1, a2), side(a1, b1, b2)
    return d1 * d2 < 0 and d3 * d4 < 0


def _ring(
    targets: Sequence[tuple[float, float]], rects: Sequence[Rect], sizes: Sequence[tuple[float, float]], work: Rect, gap: float, clear: float
) -> list[ChipSpot] | None:
    n = len(targets)
    cx = sum(p[0] for p in targets) / n
    cy = sum(p[1] for p in targets) / n
    angles = _directions(targets, cx, cy)
    reach = max(min(_farthest(r, cx, cy), clear) for r in rects)
    w = max(s[0] for s in sizes)
    h = max(s[1] for s in sizes)
    radius = reach + gap
    for _ in range(24):
        arc = _free_arc(cx, cy, radius, w, h, work)
        if arc is None:
            break
        start, span = arc
        if span >= 360.0:
            slots = _slots_all_round(angles)
            numbers = _numbers(slots, -180.0 / n)  # the chip nearest 12 o'clock is 1
        else:
            slots = _slots_on_arc(angles, start, span)
            numbers = _numbers(slots, start)
        placed = [_chip_rect(cx, cy, radius, slots[i], sizes[i][0], sizes[i][1]) for i in range(n)]
        if not _overlapping(placed, gap) and all(_fits(r, work) for r in placed):
            return [ChipSpot(i, numbers[i], placed[i]) for i in range(n)]
        radius *= 1.12
    return None


def _strip(
    targets: Sequence[tuple[float, float]],
    extent: Sequence[Rect],
    sizes: Sequence[tuple[float, float]],
    work: Rect,
    gap: float,
    *,
    horizontal: bool,
    anywhere: bool = False,
) -> list[ChipSpot] | None:
    """A row of chips above (or below) ``extent``, or a column beside it, each as level with its target as room allows.

    ``extent`` is what the chips keep clear of: the candidates, or the part of them near
    the targets.  None when there is no room.  ``anywhere`` is the last resort: kept on
    screen whatever it covers.
    """
    n = len(targets)
    cx = sum(p[0] for p in targets) / n
    axis = 0 if horizontal else 1
    order = sorted(range(n), key=lambda i: (targets[i][axis], targets[i][1 - axis]))
    lengths = [sizes[i][axis] for i in order]
    lo, hi = (work.left, work.right) if horizontal else (work.top, work.bottom)
    starts = _spread([targets[i][axis] for i in order], lengths, gap, lo, hi)
    if starts is None:
        if not anywhere:
            return None
        starts = [lo + sum(lengths[:k]) + gap * k for k in range(n)]
    thick = max(s[1 - axis] for s in sizes)
    if horizontal:
        sides = [min(r.top for r in extent) - gap - thick, max(r.bottom for r in extent) + gap]
        lo, hi = work.top, work.bottom
    else:
        right, left = max(r.right for r in extent) + gap, min(r.left for r in extent) - gap - thick
        # Right of them, unless the left side is nearer the targets (a click near the left of a wide row).
        sides = [right, left] if right - cx <= cx - (left + thick) + 1.0 else [left, right]
        lo, hi = work.left, work.right
    across = next((v for v in sides if lo <= v and v + thick <= hi), None)
    if across is None:
        if not anywhere:
            return None
        across = max(lo, min(sides[0], hi - thick))
    out: list[ChipSpot] = []
    for number, (i, start) in enumerate(zip(order, starts), start=1):
        w, h = sizes[i]
        if horizontal:
            out.append(ChipSpot(i, number, Rect(start, across + (thick - h) / 2.0, w, h)))
        else:
            out.append(ChipSpot(i, number, Rect(across, start, w, h)))
    return out


def _spread(centres: Sequence[float], lengths: Sequence[float], gap: float, lo: float, hi: float) -> list[float] | None:
    """Starts for items along a line, each centred on its wish where it can be, ``gap`` apart, inside [lo, hi].

    Items that would overlap form a group, and the group sits where its members want it on
    average, the usual way to place labels along a line.  None when they cannot all fit.
    """
    n = len(centres)
    if sum(lengths) + gap * (n - 1) > hi - lo:
        return None
    offsets = [0.0] * n  # each item's start within its group
    groups: list[tuple[int, int, float]] = []  # (first index, last index, group start)
    for i in range(n):
        groups.append((i, i, centres[i] - lengths[i] / 2.0))
        while len(groups) > 1:
            a_first, a_last, a_start = groups[-2]
            b_first, b_last, b_start = groups[-1]
            if a_start + offsets[a_last] + lengths[a_last] + gap <= b_start:
                break
            shift = offsets[a_last] + lengths[a_last] + gap
            for k in range(b_first, b_last + 1):
                offsets[k] += shift
            wanted = [centres[k] - lengths[k] / 2.0 - offsets[k] for k in range(a_first, b_last + 1)]
            groups[-2:] = [(a_first, b_last, sum(wanted) / len(wanted))]
    starts = [0.0] * n
    for first, last, start in groups:
        for k in range(first, last + 1):
            starts[k] = start + offsets[k]
    if starts[0] < lo:
        starts = [s + (lo - starts[0]) for s in starts]
    overflow = starts[-1] + lengths[-1] - hi
    if overflow > 0:
        starts = [s - overflow for s in starts]
    return starts


def _union(rects: Sequence[Rect]) -> Rect:
    return Rect.from_ltrb(min(r.left for r in rects), min(r.top for r in rects), max(r.right for r in rects), max(r.bottom for r in rects))


def _leader(chip: Rect, outline: Rect) -> tuple[tuple[float, float], tuple[float, float]]:
    """A straight line from the chip to the nearest point of the element's outline (none when they touch)."""
    cx, cy = chip.center
    end = (min(max(cx, outline.left), outline.right), min(max(cy, outline.top), outline.bottom))
    start = (min(max(end[0], chip.left), chip.right), min(max(end[1], chip.top), chip.bottom))
    return start, end


# =========================================================================== windows


class _Marks(OverlayWindow):
    """Click-through: an outline round each candidate, a line to its chip, and a one-line hint."""

    def __init__(self, services: UiServices) -> None:
        super().__init__(services, name="chooser-marks")
        self.outlines: list[tuple[int, Rect]] = []  # (number, physical outline rectangle)
        self.leaders: list[tuple[int, tuple[float, float], tuple[float, float]]] = []
        self.hint_rect: Rect | None = None
        self.hint = HINT
        self.armed: int | None = None

    def _local(self, r: Rect) -> QRectF:
        a = self.logical_point(r.left, r.top)
        b = self.logical_point(r.right, r.bottom)
        return QRectF(a, b)

    def paintEvent(self, _event) -> None:  # noqa: ANN001 - Qt signature
        t = self.svc.theme
        pal = t.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for armed in (False, True):  # the armed one last, so it is on top
            for number, (x1, y1), (x2, y2) in self.leaders:
                if (number == self.armed) != armed:
                    continue
                path = QPainterPath(self.logical_point(x1, y1))
                path.lineTo(self.logical_point(x2, y2))
                if armed:
                    two_tone_path(p, path, pal.accent, pal.on_accent, t.pxf(3.0), t.pxf(1.25))
                else:
                    two_tone_path(p, path, pal.ink, pal.halo, t.pxf(2.0), t.pxf(1.25))
            for number, rect in self.outlines:
                if (number == self.armed) != armed:
                    continue
                path = QPainterPath()
                path.addRect(self._local(rect))
                if armed:
                    two_tone_path(p, path, pal.accent, pal.on_accent, t.pxf(3.5), t.pxf(1.25))
                else:
                    two_tone_path(p, path, pal.ink, pal.halo, t.pxf(2.5), t.pxf(1.5))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if self.hint_rect is not None:
            box = self._local(self.hint_rect)
            solid_panel(p, box, pal.surface, pal.ink)
            p.setFont(t.font("label"))
            p.setPen(pal.ink)
            p.drawText(box, int(Qt.AlignmentFlag.AlignCenter), self.hint)
        p.end()


# =========================================================================== the chooser


@dataclass(frozen=True, slots=True)
class _Request:
    token: int
    action: Action  # the click that was held back
    point: tuple[float, float]
    radius: float
    elements: tuple[ElementInfo, ...]
    aimed_at: float  # time.perf_counter() when the user aimed the click, for click learning


@dataclass(frozen=True, slots=True)
class _Live:
    """What the action thread may read while the chooser is up.  Replaced whole, never changed."""

    token: int
    point: tuple[float, float]
    radius: float
    chips: tuple[tuple[int, Rect], ...]  # (number, physical rectangle)
    cluster: tuple[Rect, ...]

    def same_place(self, x: float, y: float, tolerance: float) -> bool:
        """Another click where the first one was: within the search radius or on a candidate."""
        if math.hypot(x - self.point[0], y - self.point[1]) <= self.radius:
            return True
        return any(r.distance_to(x, y) <= tolerance for r in self.cluster)


@dataclass(frozen=True, slots=True)
class _Shown:
    request: _Request
    targets: tuple[tuple[float, float], ...]
    spots: tuple[ChipSpot, ...]
    screen: ScreenGeometry


def _chip_label(element: ElementInfo) -> str:
    return " ".join(element.name.split())


class NumberChooser(QObject):
    def __init__(self, services: UiServices) -> None:
        super().__init__()
        self.svc = services
        self.trust = PointerTrust()
        self.seconds = CHOOSER_SECONDS
        self.slot = OverlaySlot(services, NAME, self._close)
        self.marks = _Marks(services)
        self.chips = [TileWindow(services, "chooser-chip") for _ in range(MAX_CHOICES)]
        for i, window in enumerate(self.chips):
            window.tile.clicked.connect(lambda _checked=False, i=i: self._chip_clicked(i))
        self._own = OwnWindows()
        self._tokens = itertools.count(1)
        self._live: _Live | None = None
        self._shown: _Shown | None = None
        self._armed: int | None = None
        self._opened_at = 0.0
        self._alive = True
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._expire)
        services.cursor.moved.connect(self._on_cursor)
        services.theme.changed.connect(self._relayout)
        services.screens.changed.connect(self.close)

    # ------------------------------------------------------------------ for tests and tools

    @property
    def is_open(self) -> bool:
        return self._shown is not None

    @property
    def spots(self) -> tuple[ChipSpot, ...]:
        return self._shown.spots if self._shown is not None else ()

    @property
    def targets(self) -> tuple[tuple[float, float], ...]:
        return self._shown.targets if self._shown is not None else ()

    @property
    def elements(self) -> tuple[ElementInfo, ...]:
        return self._shown.request.elements if self._shown is not None else ()

    @property
    def held(self) -> Action | None:
        """The click waiting for a choice."""
        return self._shown.request.action if self._shown is not None else None

    def chip_window(self, number: int) -> TileWindow | None:
        spot = next((s for s in self.spots if s.number == number), None)
        return self.chips[spot.index] if spot is not None else None

    # ------------------------------------------------------------------ actions (GUI thread)

    def select(self, action: Action) -> None:
        shown = self._shown
        if shown is None:
            raise ActionError("There are no numbers to choose from.")
        token = action.arg("token")
        if token is not None and int(token) != shown.request.token:
            raise ActionError("Those numbers have gone. Please try again.")
        try:
            number = int(action.arg("number"))
        except (TypeError, ValueError):
            raise ActionError("Say the number you want, like two.") from None
        if not self._choose(number, action.arg("button"), action.arg("count")):
            raise ActionError(f"There is no number {number}. Say a number from 1 to {len(shown.spots)}.")

    def close(self, _action: Action | None = None) -> None:
        if self._shown is not None or self.slot.is_current:
            self._close()

    # ------------------------------------------------------------------ the interceptor (action thread)

    def intercept(self, action: Action) -> Action | None:
        """Hold back a gaze click that could mean two to six things.  Reads only immutable snapshots."""
        if action.name != "mouse.click":
            return action
        if action.arg(CHOSEN) or action.arg("snap", True) is False or action.source not in USER_SOURCES:
            return action
        point = point_of(self.svc, action)
        if point is None:
            return action
        x, y = point
        live = self._live
        if live is not None:
            number = tile_at(live.chips, x, y, gaze_tolerance(self.svc, x, y))
            if number is not None:
                return Action("ui.chooser.select", {"number": number, "token": live.token}, source=action.source, utterance=action.utterance)
        if self._own.at(x, y):
            if live is not None:
                self.svc.bridge.post(lambda token=live.token: self._close_token(token))
            return action  # the dock, a chip of something else: Sightline's own windows are never in doubt
        ctx = self.svc.ctx
        if ctx.state.get("drag_active"):
            return action  # a click while dragging drops; never hold that back
        trusted, radius = self.trust.verdict(ctx.state.get("pointer_mode"))
        if live is not None:
            if not trusted and live.same_place(x, y, gaze_tolerance(self.svc, x, y)):
                self.svc.bridge.post(lambda token=live.token: self._keep_open(token))
                return None  # the same unsure spot again: the numbers are the way through
            self.svc.bridge.post(lambda token=live.token: self._close_token(token))
        if trusted or ctx.desktop is None:
            return action
        try:
            found = ctx.desktop.uia.clickables_near(x, y, radius)
        except Exception:
            log.exception("clickables_near failed; the click goes through")
            return action
        elements = pick_candidates(found)
        if len(elements) <= 1:
            return action
        if len(elements) > MAX_CHOICES:
            if not ctx.actions.has("ui.zoom.show"):
                return action
            log.info("Chooser: %d targets near (%d, %d); opening the zoom lens", len(elements), x, y)
            ctx.actions.dispatch(Action("ui.zoom.show", {"x": x, "y": y}, source=action.source, utterance=action.utterance, aim_t=action.aim_t))
            return None
        request = _Request(next(self._tokens), action, (x, y), radius, tuple(elements), self.trust.aimed_at(action))
        self.svc.bridge.post(lambda: self._open(request))
        return None

    # ------------------------------------------------------------------ showing (GUI thread)

    def _open(self, request: _Request) -> None:
        if not self._alive:
            return
        rects = [e.rect for e in request.elements]
        targets = tuple(click_points(rects, request.point[0], request.point[1], request.radius))
        screen = self.svc.screens.map.screen_at(*request.point)
        self._shown = _Shown(request, targets, (), screen)
        self.slot.opened()  # closes the numbers, grid, zoom, help or menu if one is up
        self._present()
        self._opened_at = time.monotonic()
        self._timer.start(int(self.seconds * 1000))
        log.info("Chooser: %d targets near (%d, %d), radius %d", len(rects), request.point[0], request.point[1], request.radius)

    def _present(self) -> None:
        shown = self._shown
        if shown is None:
            return
        t = self.svc.theme
        screen = shown.screen
        dpr = screen.dpr
        elements = shown.request.elements
        sizes: list[tuple[float, float]] = []
        for i, element in enumerate(elements):
            tile = self.chips[i].tile
            tile.set_content("", _chip_label(element))
            sizes.append((tile.natural_width(max_label=t.pxf(LABEL_MAX)) * dpr, t.pxf(TILE_HEIGHT) * dpr))
        gap = t.pxf(16) * dpr
        clear = shown.request.radius + t.pxf(REACH_CAP) * dpr
        spots = place_chips(shown.targets, [e.rect for e in elements], sizes, screen.work_rect, gap=gap, clear=clear)
        self._shown = _Shown(shown.request, shown.targets, tuple(spots), screen)

        outline_gap = 3.0 * dpr
        outlines = {spot.number: elements[spot.index].rect.inflate(outline_gap) for spot in spots}
        leaders = [(spot.number, *_leader(spot.rect, outlines[spot.number])) for spot in spots]
        hint_rect = self._hint_rect(spots, list(outlines.values()), screen)
        boxes = [*outlines.values(), *(s.rect for s in spots), *([hint_rect] if hint_rect is not None else [])]
        area = _union(boxes).inflate(8.0 * dpr).intersection(screen.native)
        self.marks.screen_geometry = screen
        self.marks.place_physical(area)
        self.marks.outlines = [(n, r) for n, r in outlines.items()]
        self.marks.leaders = leaders
        self.marks.hint_rect = hint_rect
        self.marks.armed = None
        self._armed = None

        used = {spot.index for spot in spots}
        for spot in spots:
            window = self.chips[spot.index]
            window.tile.set_content(str(spot.number), _chip_label(elements[spot.index]))
            window.tile.set_armed(False)
            window.place(spot.rect)
        for i, window in enumerate(self.chips):
            if i not in used:
                window.hide()
        self._live = _Live(
            shown.request.token,
            shown.request.point,
            shown.request.radius,
            tuple((spot.number, spot.rect) for spot in spots),
            tuple(e.rect for e in elements),
        )
        self.marks.show()
        self.marks.update()
        for spot in spots:
            window = self.chips[spot.index]
            window.show()
            window.raise_()
            window.update()
        self._on_cursor(*self.svc.cursor.pos)

    def _hint_rect(self, spots: Sequence[ChipSpot], outlines: Sequence[Rect], screen: ScreenGeometry) -> Rect | None:
        """A plain line under the chips (above them when there is no room), centred on them."""
        t = self.svc.theme
        dpr = screen.dpr
        fm = QFontMetricsF(t.font("label"))
        w = (fm.horizontalAdvance(HINT) + t.pxf(28)) * dpr
        h = (fm.height() + t.pxf(16)) * dpr
        gap = t.pxf(12) * dpr
        boxes = [s.rect for s in spots] + list(outlines)
        left = min(b.left for b in boxes)
        right = max(b.right for b in boxes)
        work = screen.work_rect
        y = max(b.bottom for b in boxes) + gap
        if y + h > work.bottom:
            y = min(b.top for b in boxes) - gap - h
        if y < work.top:
            return None  # no room either side; the chips speak for themselves
        x = max(work.left, min((left + right) / 2.0 - w / 2.0, work.right - w))
        return Rect(x, y, w, h)

    def _relayout(self) -> None:
        if self._shown is not None:
            self._present()

    def _close(self) -> None:
        """Hide everything.  Also OverlayState's closer, so it ends by calling ``slot.closed``."""
        self._timer.stop()
        self._live = None
        self._shown = None
        self._armed = None
        for window in self.chips:
            window.tile.set_armed(False)
            window.hide()
        self.marks.hide()
        self.marks.armed = None
        self.slot.closed()

    def _close_token(self, token: int) -> None:
        if self._shown is not None and self._shown.request.token == token:
            self.close()

    def _keep_open(self, token: int) -> None:
        if self._shown is not None and self._shown.request.token == token:
            self._timer.start(int(self.seconds * 1000))

    def _expire(self) -> None:
        if self._shown is None:
            return
        if self._pointer_on_chips() and time.monotonic() - self._opened_at < MAX_SECONDS:
            self._timer.start(RECHECK_MS)  # the user may be aiming at one: do not pull it away
            return
        log.info("Chooser: no choice made")
        self.close()

    def _pointer_on_chips(self) -> bool:
        live = self._live
        if live is None:
            return False
        x, y = self.svc.cursor.pos
        return tile_at(live.chips, x, y, 2.4 * gaze_tolerance(self.svc, x, y)) is not None

    # ------------------------------------------------------------------ picking

    def _chip_clicked(self, index: int) -> None:
        spot = next((s for s in self.spots if s.index == index), None)
        if spot is not None:
            self._choose(spot.number, None, None)

    def _choose(self, number: int, button: object, count: object) -> bool:
        shown = self._shown
        if shown is None:
            return False
        spot = next((s for s in shown.spots if s.number == number), None)
        if spot is None:
            return False
        original = shown.request.action
        x, y = shown.targets[spot.index]
        try:
            clicks = int(count) if count is not None else int(original.arg("count", 1) or 1)
        except (TypeError, ValueError):
            clicks = 1
        changes = {"button": button or original.arg("button", "left"), "count": clicks, "x": x, "y": y, "snap": False, CHOSEN: True}
        if original.source in ("eye", "dwell"):
            # Sent as Sightline's own click, aimed when the user aimed the first one.  Vision learns from an
            # eye click by pairing it with its latest blink nearby, which would be the blink on the chip.
            rerun = Action(original.name, {**original.args, **changes}, source="ui", aim_t=shown.request.aimed_at)
        else:
            rerun = original.with_args(**changes)  # a spoken click keeps its speech onset
        element = shown.request.elements[spot.index]
        self.close()
        log.info("Chooser: picked %d (%s)", number, element.control_type)  # not its name: names can be private
        self.svc.ctx.actions.dispatch(rerun)
        return True

    def _on_cursor(self, x: float, y: float) -> None:
        live = self._live
        if live is None:
            return
        number = tile_at(live.chips, x, y, gaze_tolerance(self.svc, x, y))
        if number == self._armed:
            return
        self._armed = number
        for spot in self.spots:
            self.chips[spot.index].tile.set_armed(spot.number == number)
        self.marks.armed = number
        self.marks.update()

    # ------------------------------------------------------------------ uninstall

    def shutdown(self) -> None:
        self._alive = False
        self.close()
        self.slot.release()
        for signal, slot in (
            (self.svc.cursor.moved, self._on_cursor),
            (self.svc.theme.changed, self._relayout),
            (self.svc.screens.changed, self.close),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        for window in [*self.chips, self.marks]:
            window.hide()
            window.deleteLater()


def install(ctx: AppContext, services: UiServices) -> Callable[[], None]:
    """Hold back unsure gaze clicks and offer numbered choices.  Returns a function that undoes it all."""
    chooser = NumberChooser(services)
    teardown = Teardown()
    teardown.overlay = chooser
    for event_type, handler in ((PointerSample, chooser.trust.on_sample), (EyeGesture, chooser.trust.on_gesture)):
        teardown.add(ctx.bus.subscribe(event_type, handler).cancel)
    names = register_ui_actions(
        services,
        [
            ("ui.chooser.select", chooser.select, "Pick one of the numbered targets after an unsure gaze click"),
            ("ui.chooser.cancel", chooser.close, "Close the numbered targets without clicking"),
        ],
    )
    teardown.add(lambda: [ctx.actions.unregister(name) for name in names])
    teardown.add(chooser.shutdown)
    teardown.add(ctx.actions.add_interceptor(chooser.intercept, priority=PRIORITY, owner="ui.chooser"))
    return teardown


__all__ = [
    "CHOSEN",
    "ChipSpot",
    "NumberChooser",
    "PointerTrust",
    "click_point",
    "click_points",
    "clockwise_degrees",
    "install",
    "layout_cost",
    "pick_candidates",
    "place_chips",
    "search_radius",
]
