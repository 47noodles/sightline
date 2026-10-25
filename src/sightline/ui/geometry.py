"""Pure geometry for the precision overlays: label placement, the 3x3 grid and the zoom lens.

Everything here works in physical pixels and has no Qt dependency, so it
can run off the GUI thread and be unit tested directly.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sightline.core.types import Rect

# =========================================================================== numbered labels


@dataclass(frozen=True, slots=True)
class PlacedLabel:
    number: int
    badge: Rect  # where the number is drawn
    target: Rect  # the element, clipped to its monitor
    point: tuple[float, float]  # where selecting it clicks
    index: int  # position in the caller's element list


BadgeSize = Callable[[int, Rect], tuple[float, float]]  # (number, monitor) -> (width, height)


class _SpatialIndex:
    """A coarse spatial hash, so overlap checks stay cheap with hundreds of rectangles."""

    def __init__(self, cell: float = 64.0) -> None:
        self._cell = cell
        self._buckets: dict[tuple[int, int], list[tuple[Rect, int]]] = {}

    def _keys(self, r: Rect) -> list[tuple[int, int]]:
        c = self._cell
        x0, x1 = math.floor(r.left / c), math.floor(r.right / c)
        y0, y1 = math.floor(r.top / c), math.floor(r.bottom / c)
        return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]

    def add(self, r: Rect, tag: int = -1) -> None:
        for key in self._keys(r):
            self._buckets.setdefault(key, []).append((r, tag))

    def collides(self, r: Rect) -> bool:
        for key in self._keys(r):
            for other, _tag in self._buckets.get(key, ()):
                if r.intersects(other):
                    return True
        return False

    def overlap_area(self, r: Rect, exclude: int) -> float:
        """Total area of ``r`` covered by stored rectangles other than those tagged ``exclude``."""
        seen: set[int] = set()
        total = 0.0
        for key in self._keys(r):
            for other, tag in self._buckets.get(key, ()):
                if tag == exclude or id(other) in seen or not r.intersects(other):
                    continue
                seen.add(id(other))
                total += r.intersection(other).area
        return total


_DUP_BUCKET = 16.0
_DUP_TOLERANCE = 4.0


def _near_duplicate(rect: Rect, kept: dict[tuple[int, int], list[Rect]]) -> bool:
    """True when an already kept rectangle has every edge within a few pixels of this one."""
    cx, cy = rect.center
    bx, by = int(cx // _DUP_BUCKET), int(cy // _DUP_BUCKET)
    tol = _DUP_TOLERANCE
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for other in kept.get((bx + dx, by + dy), ()):
                if (
                    abs(other.x - rect.x) <= tol
                    and abs(other.y - rect.y) <= tol
                    and abs(other.right - rect.right) <= tol
                    and abs(other.bottom - rect.bottom) <= tol
                ):
                    return True
    return False


def _clip_to_monitor(rect: Rect, monitors: Sequence[Rect]) -> tuple[Rect, Rect] | None:
    cx, cy = rect.center
    for m in monitors:
        if m.contains(cx, cy):
            clipped = rect.intersection(m)
            return (clipped, m) if not clipped.is_empty() else None
    return None


def reading_order(rects: Sequence[Rect], band: float = 12.0) -> list[int]:
    """Indices sorted top to bottom, then left to right, treating near-equal rows as one row."""
    return sorted(range(len(rects)), key=lambda i: (math.floor(rects[i].center[1] / band), rects[i].x, rects[i].y))


def layout_labels(
    rects: Sequence[Rect],
    monitors: Sequence[Rect],
    badge_size: BadgeSize,
    *,
    limit: int = 150,
    gap: float = 2.0,
    min_size: float = 6.0,
    pause: Callable[[], None] | None = None,
) -> list[PlacedLabel]:
    """Place numbered badges next to clickable elements without any two badges overlapping.

    Elements smaller than ``min_size``, off every monitor, or duplicating an
    earlier element's bounds are skipped.  Numbers run in reading order and
    have no gaps; elements whose badge cannot be placed are left out.
    ``pause`` is called every few dozen elements; the UI passes one that
    yields the GIL, so laying out a busy page off the GUI thread never
    starves the GUI thread for long.
    """
    candidates: list[tuple[Rect, Rect, int]] = []  # (clipped rect, monitor, original index)
    kept: dict[tuple[int, int], list[Rect]] = {}
    for i, r in enumerate(rects):
        if r.w < min_size or r.h < min_size:
            continue
        clipped = _clip_to_monitor(r, monitors)
        if clipped is None:
            continue
        rect, monitor = clipped
        if rect.w < min_size or rect.h < min_size:
            continue
        # Nested elements often share bounds (a button and its text); keep the first.
        if _near_duplicate(rect, kept):
            continue
        cx, cy = rect.center
        kept.setdefault((int(cx // _DUP_BUCKET), int(cy // _DUP_BUCKET)), []).append(rect)
        candidates.append((rect, monitor, i))

    order = reading_order([c[0] for c in candidates])
    elements = _SpatialIndex()
    for k, (rect, _monitor, _index) in enumerate(candidates):
        elements.add(rect, k)
    badges = _SpatialIndex()
    placed: list[PlacedLabel] = []
    for step, k in enumerate(order):
        if len(placed) >= limit:
            break
        if pause is not None and step % 32 == 31:
            pause()
        rect, monitor, index = candidates[k]
        number = len(placed) + 1
        bw, bh = badge_size(number, monitor)
        badge = _place_badge(k, rect, monitor, bw, bh, gap, badges, elements)
        if badge is None:
            continue
        badges.add(badge.inflate(gap))
        placed.append(PlacedLabel(number, badge, rect, rect.center, index))
    return placed


def _place_badge(
    k: int, rect: Rect, monitor: Rect, bw: float, bh: float, gap: float, badges: _SpatialIndex, elements: _SpatialIndex
) -> Rect | None:
    """The cheapest free spot next to the element.

    Covering another element costs most (it may hide that element's label),
    covering its own element costs less, and the order below breaks ties:
    beside the element in the margin reads best, as in a numbered list.
    """
    cx, cy = rect.center
    options = (
        (rect.x - bw - gap, cy - bh / 2),  # left, centred vertically
        (rect.x + gap, rect.y + gap),  # inside, top-left
        (rect.x, rect.y - bh - gap),  # above, left-aligned
        (rect.right + gap, cy - bh / 2),  # right
        (rect.x, rect.bottom + gap),  # below
        (rect.x - bw / 2, rect.y - bh / 2),  # on the top-left corner
        (cx - bw / 2, cy - bh / 2),  # centred: it hides what you click
    )
    area = bw * bh
    best: tuple[float, Rect] | None = None
    for preference, (x, y) in enumerate(options):
        badge = Rect(x, y, bw, bh)
        if not _inside(badge, monitor) or badges.collides(badge):
            continue
        own = badge.intersection(rect).area / area if badge.intersects(rect) else 0.0
        other = min(2.0, elements.overlap_area(badge, k) / area)
        cost = 3.0 * other + own + 0.06 * preference
        if best is None or cost < best[0]:
            best = (cost, badge)
    if best is not None:
        return best[1]
    # Crowded: walk outward around the element's top-left corner.
    step = max(bh / 2.0, 4.0)
    for ring in range(1, 5):
        for dx, dy in ((0, -1), (-1, 0), (1, 0), (0, 1), (-1, -1), (1, -1), (-1, 1), (1, 1)):
            badge = Rect(rect.x + dx * ring * step, rect.y + dy * ring * step, bw, bh)
            if _inside(badge, monitor) and not badges.collides(badge):
                return badge
    return None


def _inside(r: Rect, bounds: Rect) -> bool:
    return r.left >= bounds.left and r.top >= bounds.top and r.right <= bounds.right and r.bottom <= bounds.bottom


def label_at(labels: Sequence[PlacedLabel], x: float, y: float, tolerance: float = 0.0) -> PlacedLabel | None:
    """The badge under a point (gaze is imprecise, so callers pass a tolerance); nearest wins."""
    best: tuple[float, PlacedLabel] | None = None
    for label in labels:
        d = label.badge.distance_to(x, y)
        if d <= tolerance and (best is None or d < best[0]):
            best = (d, label)
    return best[1] if best else None


# =========================================================================== 3x3 grid


def grid_cells(region: Rect) -> list[Rect]:
    """Nine cells, numbered 1 to 9 left to right, top to bottom (like a phone keypad)."""
    w, h = region.w / 3.0, region.h / 3.0
    return [Rect(region.x + col * w, region.y + row * h, w, h) for row in range(3) for col in range(3)]


def grid_cell_at(region: Rect, x: float, y: float) -> int | None:
    if not region.contains(x, y):
        return None
    col = min(2, int((x - region.x) / (region.w / 3.0)))
    row = min(2, int((y - region.y) / (region.h / 3.0)))
    return row * 3 + col + 1


@dataclass(frozen=True, slots=True)
class GridState:
    """The grid's history: ``regions[0]`` is the whole monitor, the last one is current."""

    regions: tuple[Rect, ...]

    @classmethod
    def start(cls, monitor: Rect) -> GridState:
        return cls((monitor,))

    @property
    def current(self) -> Rect:
        return self.regions[-1]

    @property
    def depth(self) -> int:
        return len(self.regions) - 1

    @property
    def center(self) -> tuple[float, float]:
        return self.current.center

    def select(self, cell: int) -> GridState:
        if not 1 <= cell <= 9:
            raise ValueError(f"cell must be 1 to 9, got {cell}")
        return GridState((*self.regions, grid_cells(self.current)[cell - 1]))

    def back(self) -> GridState:
        return GridState(self.regions[:-1]) if len(self.regions) > 1 else self

    def can_divide(self, min_cell: float) -> bool:
        """Whether another level would still leave cells at least ``min_cell`` pixels on each side."""
        return self.current.w / 3.0 >= min_cell and self.current.h / 3.0 >= min_cell


# =========================================================================== zoom lens


@dataclass(frozen=True, slots=True)
class LensMapping:
    """A magnified view: ``lens`` is where it is drawn, ``source`` is the screen area it shows."""

    lens: Rect
    source: Rect

    @property
    def zoom(self) -> float:
        return self.lens.w / self.source.w if self.source.w else 1.0

    def contains(self, x: float, y: float) -> bool:
        return self.lens.contains(x, y)

    def to_source(self, x: float, y: float) -> tuple[float, float]:
        """The real screen point under a point inside the lens."""
        sx = self.source.x + (x - self.lens.x) * self.source.w / self.lens.w
        sy = self.source.y + (y - self.lens.y) * self.source.h / self.lens.h
        return (sx, sy)

    def to_lens(self, x: float, y: float) -> tuple[float, float]:
        lx = self.lens.x + (x - self.source.x) * self.lens.w / self.source.w
        ly = self.lens.y + (y - self.source.y) * self.lens.h / self.source.h
        return (lx, ly)


def _shift_inside(r: Rect, bounds: Rect) -> Rect:
    x = min(max(r.x, bounds.x), bounds.right - r.w) if r.w <= bounds.w else bounds.x
    y = min(max(r.y, bounds.y), bounds.bottom - r.h) if r.h <= bounds.h else bounds.y
    return Rect(x, y, min(r.w, bounds.w), min(r.h, bounds.h))


def lens_geometry(cx: float, cy: float, lens_w: float, lens_h: float, zoom: float, bounds: Rect) -> LensMapping:
    """Centre a lens and its source area on a point, both kept inside ``bounds`` (the monitor).

    Near an edge both shift inward, so the point is no longer at the centre,
    but ``to_source`` stays exact because it uses the two rectangles directly.
    """
    zoom = max(1.0, zoom)
    lens = _shift_inside(Rect(cx - lens_w / 2.0, cy - lens_h / 2.0, lens_w, lens_h), bounds)
    sw, sh = lens.w / zoom, lens.h / zoom
    source = _shift_inside(Rect(cx - sw / 2.0, cy - sh / 2.0, sw, sh), bounds)
    return LensMapping(lens, source)
