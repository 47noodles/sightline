"""The clickable-element snapshot, its spatial index, and the snapping rule.

A snapshot is built on the scanning thread and replaced whole, so readers on
any thread use it without locks and never wait.  A uniform grid keeps
``nearest`` and ``containing`` to a few dozen distance checks however many
elements a page has.

Snapping rule (shared by the hover highlight and ``mouse.click``, so what is
outlined is what gets clicked):

1. A point inside a clickable element of the window under the point clicks
   exactly there (the smallest such element is the one named).
2. Otherwise the nearest clickable of that same window within the snap
   radius is clicked at its centre.
3. Otherwise the point is clicked as it is.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from sightline.core.types import ElementInfo, Rect

CELL = 64.0


@dataclass(frozen=True, slots=True)
class Target:
    """Where a click at a point would land."""

    x: float
    y: float
    element: ElementInfo | None = None
    snapped: bool = False


def element_root(element: ElementInfo) -> int:
    """The top-level window an element belongs to (stored on its handle by the scanner)."""
    handle = element.handle
    return int(getattr(handle, "root", 0) or 0)


class SpatialIndex:
    def __init__(self, elements: Sequence[ElementInfo], cell: float = CELL) -> None:
        self._elements = elements
        self._cell = cell
        self._grid: dict[tuple[int, int], list[int]] = {}
        for i, el in enumerate(elements):
            r = el.rect
            if r.is_empty():
                continue
            for cx in range(int(r.x // cell), int((r.right - 1) // cell) + 1):
                for cy in range(int(r.y // cell), int((r.bottom - 1) // cell) + 1):
                    self._grid.setdefault((cx, cy), []).append(i)

    def _candidates(self, left: float, top: float, right: float, bottom: float) -> set[int]:
        found: set[int] = set()
        cell = self._cell
        for cx in range(int(left // cell), int(right // cell) + 1):
            for cy in range(int(top // cell), int(bottom // cell) + 1):
                found.update(self._grid.get((cx, cy), ()))
        return found

    def containing(self, x: float, y: float, accept: Callable[[ElementInfo], bool] | None = None) -> ElementInfo | None:
        best: tuple[float, int, ElementInfo] | None = None
        for i in self._candidates(x, y, x, y):
            el = self._elements[i]
            if el.rect.contains(x, y) and (accept is None or accept(el)):
                key = (el.rect.area, i)  # equal sizes: the earlier element in document order wins
                if best is None or key < best[:2]:
                    best = (el.rect.area, i, el)
        return best[2] if best else None

    def nearest(
        self, x: float, y: float, radius: float, accept: Callable[[ElementInfo], bool] | None = None
    ) -> ElementInfo | None:
        if radius <= 0:
            return None
        best: tuple[float, float, int, ElementInfo] | None = None
        for i in self._candidates(x - radius, y - radius, x + radius, y + radius):
            el = self._elements[i]
            d = el.rect.distance_to(x, y)
            if d > radius or (accept is not None and not accept(el)):
                continue
            # Equal distances: the smaller element is the more specific target, then document order.
            key = (d, el.rect.area, i)
            if best is None or key < best[:3]:
                best = (d, el.rect.area, i, el)
        return best[3] if best else None

    def within(
        self, x: float, y: float, radius: float, accept: Callable[[ElementInfo], bool] | None = None
    ) -> list[ElementInfo]:
        """Every element whose rectangle comes within ``radius`` of the point, nearest first.

        Ties are ordered as in ``nearest``: the smaller element, then document order.
        """
        radius = max(0.0, radius)
        found: list[tuple[float, float, int, ElementInfo]] = []
        for i in self._candidates(x - radius, y - radius, x + radius, y + radius):
            el = self._elements[i]
            d = el.rect.distance_to(x, y)
            if d <= radius and (accept is None or accept(el)):
                found.append((d, el.rect.area, i, el))
        found.sort(key=lambda item: item[:3])
        return [item[3] for item in found]


@dataclass(frozen=True)
class ClickableSnapshot:
    """Clickable elements of the foreground window, its popups, the taskbar and the window under the pointer."""

    elements: tuple[ElementInfo, ...] = ()
    roots: frozenset[int] = frozenset()  # top-level windows this snapshot covers
    foreground: int = 0
    t: float = 0.0  # time.perf_counter() when the scan finished
    generation: int = 0
    index: SpatialIndex = field(default=None, repr=False, compare=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.index is None:
            object.__setattr__(self, "index", SpatialIndex(self.elements))

    def covers(self, root: int) -> bool:
        return root in self.roots

    def names(self) -> list[str]:
        seen: dict[str, None] = {}
        for el in self.elements:
            if el.name:
                seen.setdefault(el.name, None)
        return list(seen)

    def containing(self, x: float, y: float, root: int | None = None) -> ElementInfo | None:
        accept = None if root is None else (lambda el: element_root(el) == root)
        return self.index.containing(x, y, accept)

    def nearest(self, x: float, y: float, radius: float, root: int | None = None) -> ElementInfo | None:
        accept = None if root is None else (lambda el: element_root(el) == root)
        return self.index.nearest(x, y, radius, accept)

    def within(self, x: float, y: float, radius: float, roots: frozenset[int] | None = None) -> list[ElementInfo]:
        accept = None if roots is None else (lambda el: element_root(el) in roots)
        return self.index.within(x, y, radius, accept)


def resolve_target(
    x: float,
    y: float,
    snapshot: ClickableSnapshot,
    *,
    root: int,
    snap: bool,
    radius: float,
    under_point: ElementInfo | None = None,
) -> Target:
    """Apply the snapping rule.

    ``root`` is the top-level window under the point.  ``under_point`` is a
    clickable found live at the point (ElementFromPoint) when the snapshot
    does not know the element, for example content that appeared since the
    last scan.
    """
    if snapshot.covers(root):
        inside = snapshot.containing(x, y, root)
        if inside is not None:
            return Target(x, y, inside, snapped=False)
    if under_point is not None and under_point.clickable and under_point.rect.contains(x, y):
        return Target(x, y, under_point, snapped=False)
    if snap and radius > 0 and snapshot.covers(root):
        near = snapshot.nearest(x, y, radius, root)
        if near is not None:
            sx, sy = snap_point(near.rect, x, y, radius)
            return Target(sx, sy, near, snapped=True)
    return Target(x, y, None, snapped=False)


def snap_point(rect: Rect, x: float, y: float, radius: float) -> tuple[float, float]:
    """Where to click an element the pointer snapped to.

    The centre of a small element is the safest spot.  For a large element
    (a wide list row, a message card) the centre can be hundreds of pixels
    from where the user looked, so click just inside the edge nearest the
    pointer instead.
    """
    cx, cy = rect.center
    if (cx - x) ** 2 + (cy - y) ** 2 <= (1.5 * radius) ** 2:
        return (float(int(cx)), float(int(cy)))
    inset_x = min(12.0, rect.w / 4.0)
    inset_y = min(12.0, rect.h / 4.0)
    px = min(max(x, rect.x + inset_x), rect.right - 1 - inset_x)
    py = min(max(y, rect.y + inset_y), rect.bottom - 1 - inset_y)
    return (float(int(px)), float(int(py)))


def clip_rect(rect: Rect, bounds: Rect) -> Rect:
    return rect.intersection(bounds)


def dedupe(elements: Iterable[ElementInfo]) -> list[ElementInfo]:
    """Drop exact duplicates (same rect, name and type), which some providers report twice."""
    seen: set[tuple[float, float, float, float, str, str]] = set()
    out: list[ElementInfo] = []
    for el in elements:
        key = (el.rect.x, el.rect.y, el.rect.w, el.rect.h, el.name, el.control_type)
        if key in seen:
            continue
        seen.add(key)
        out.append(el)
    return out


def nearest_first(elements: Sequence[ElementInfo], x: float, y: float, cap: int) -> list[ElementInfo]:
    """At most ``cap`` elements, keeping the ones nearest the pointer."""
    if len(elements) <= cap:
        return list(elements)
    return sorted(elements, key=lambda el: el.rect.distance_to(x, y) if not math.isnan(x) else 0.0)[:cap]
