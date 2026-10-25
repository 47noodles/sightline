"""Matching spoken names to apps, windows and on-screen buttons.

Names are indexed by their spoken forms (``normalize.spoken_forms``) and
resolved with RapidFuzz.  A name is only accepted when it scores well and
clearly beats every other candidate, so "switch to code" never lands on the
wrong window because two titles were nearly as good.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz, process

from .normalize import normalize_command, spoken_forms

_TITLE_SPLIT = re.compile(r"\s+[-\u2013\u2014|:\u00b7]\s+")


@dataclass(frozen=True, slots=True)
class Resolved:
    item: Any
    name: str  # the display name of the item
    score: float  # 0..100


class NameIndex:
    """Spoken forms -> items.  Immutable once built, so readers never need a lock."""

    def __init__(self, entries: Iterable[tuple[str, Any, list[str]]] = (), *, min_score: float = 85.0, margin: float = 4.0) -> None:
        self._forms: list[str] = []
        self._owners: list[int] = []
        self._items: list[tuple[str, Any]] = []
        self._exact: dict[str, int] = {}
        self._min_score = min_score
        self._margin = margin
        for name, item, forms in entries:
            index = len(self._items)
            self._items.append((name, item))
            for form in forms:
                if form and len(form) >= 2:
                    self._forms.append(form)
                    self._owners.append(index)
                    self._exact.setdefault(form, index)

    def __len__(self) -> int:
        return len(self._items)

    def forms(self) -> list[str]:
        return list(self._forms)

    def names(self) -> list[str]:
        return [name for name, _ in self._items]

    def resolve(self, spoken: str) -> Resolved | None:
        query = normalize_command(spoken)
        if len(query) < 2 or not self._forms:
            return None
        exact = self._exact.get(query)
        if exact is not None:
            name, item = self._items[exact]
            return Resolved(item, name, 100.0)
        hits = process.extract(query, self._forms, scorer=fuzz.WRatio, limit=8, score_cutoff=self._min_score - 15)
        best_by_owner: dict[int, float] = {}
        for form, score, idx in hits:
            owner = self._owners[idx]
            # WRatio rewards a short query inside a long name ("code" in "visual studio code").
            # Scale that down when the lengths differ a lot, so whole-name matches win.
            ratio = len(query) / max(len(form), 1)
            adjusted = score if ratio >= 0.5 else score * (0.92 + 0.16 * ratio)
            if adjusted > best_by_owner.get(owner, 0.0):
                best_by_owner[owner] = adjusted
        if not best_by_owner:
            return None
        ranked = sorted(best_by_owner.items(), key=lambda kv: -kv[1])
        owner, score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        if score < self._min_score or score - runner_up < self._margin:
            return None
        name, item = self._items[owner]
        return Resolved(item, name, float(score))


def window_forms(title: str, process_name: str = "") -> list[str]:
    """A window answers to its title, each " - " separated part of it, and its process name."""
    forms: list[str] = []
    for part in [title, *_TITLE_SPLIT.split(title)]:
        for form in spoken_forms(part):
            if form not in forms:
                forms.append(form)
    proc = re.sub(r"\.exe$", "", process_name or "", flags=re.IGNORECASE)
    for form in spoken_forms(proc):
        if form not in forms:
            forms.append(form)
    return forms


class DynamicNames:
    """The apps, windows and button names the grammar can refer to, refreshed from the desktop.

    Each ``set_*`` builds a new index and swaps it in, so resolving on the
    speech thread never waits for a refresh on another thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.apps = NameIndex()
        self.windows = NameIndex()
        self.elements = NameIndex()
        self.version = 0

    def set_apps(self, names: Iterable[str]) -> None:
        index = NameIndex(((n, n, spoken_forms(n)) for n in names if n), min_score=86.0)
        with self._lock:
            self.apps = index
            self.version += 1

    def set_windows(self, windows: Iterable[tuple[str, str, int]]) -> None:
        """(title, process, hwnd) triples."""
        index = NameIndex(((title, (title, hwnd), window_forms(title, proc)) for title, proc, hwnd in windows if title), min_score=85.0)
        with self._lock:
            self.windows = index
            self.version += 1

    def set_elements(self, names: Iterable[str]) -> None:
        seen: set[str] = set()
        unique: list[str] = []
        for n in names:
            n = (n or "").strip()
            if n and n.lower() not in seen and len(n) <= 80:
                seen.add(n.lower())
                unique.append(n)
        index = NameIndex(((n, n, spoken_forms(n)) for n in unique), min_score=84.0)
        with self._lock:
            self.elements = index
            self.version += 1

    # ------------------------------------------------------------------ resolvers for the grammar

    def resolve_app(self, spoken: str) -> tuple[str, float] | None:
        hit = self.apps.resolve(spoken)
        return (hit.item, hit.score / 100.0) if hit else None

    def resolve_window(self, spoken: str) -> tuple[str, int | None, float] | None:
        hit = self.windows.resolve(spoken)
        if hit is None:
            return None
        title, hwnd = hit.item
        return (title, hwnd, hit.score / 100.0)

    def resolve_element(self, spoken: str) -> tuple[str, float] | None:
        hit = self.elements.resolve(spoken)
        return (hit.item, hit.score / 100.0) if hit else None
