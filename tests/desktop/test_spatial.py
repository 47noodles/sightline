from __future__ import annotations

import random
import time

from sightline.core.types import ElementInfo, Rect
from sightline.desktop.spatial import (
    ClickableSnapshot,
    SpatialIndex,
    dedupe,
    nearest_first,
    resolve_target,
    snap_point,
)
from sightline.desktop.uia import ElementRef, _drop_inner_duplicates

ROOT = 1001
OTHER = 2002


def el(x, y, w, h, name="", kind="Button", root=ROOT) -> ElementInfo:
    return ElementInfo(Rect(x, y, w, h), name, kind, clickable=True, handle=ElementRef("scan", (1, 0), root))


def snapshot(*elements, roots=(ROOT,)) -> ClickableSnapshot:
    return ClickableSnapshot(elements=tuple(elements), roots=frozenset(roots), t=time.perf_counter(), generation=1)


def test_containing_prefers_the_smallest_element():
    big = el(0, 0, 400, 300, "Card", "ListItem")
    small = el(10, 10, 50, 20, "Like")
    snap = snapshot(big, small)
    assert snap.containing(20, 15, ROOT).name == "Like"
    assert snap.containing(200, 200, ROOT).name == "Card"
    assert snap.containing(500, 500, ROOT) is None


def test_nearest_within_radius_and_root_filter():
    a = el(100, 100, 40, 20, "A")
    b = el(300, 100, 40, 20, "B", root=OTHER)
    snap = snapshot(a, b, roots=(ROOT, OTHER))
    assert snap.nearest(150, 110, 20, ROOT).name == "A"  # 10 px to the right of A
    assert snap.nearest(150, 110, 5, ROOT) is None
    assert snap.nearest(290, 110, 20, ROOT) is None  # B is closer but belongs to another window
    assert snap.nearest(290, 110, 20, OTHER).name == "B"


def test_resolve_inside_clicks_the_point_itself():
    button = el(100, 100, 80, 30, "Save")
    target = resolve_target(110, 105, snapshot(button), root=ROOT, snap=True, radius=48)
    assert (target.x, target.y, target.snapped, target.element.name) == (110, 105, False, "Save")


def test_resolve_snaps_to_centre_of_a_nearby_small_element():
    button = el(100, 100, 80, 30, "Save")
    target = resolve_target(200, 110, snapshot(button), root=ROOT, snap=True, radius=48)
    assert target.snapped and target.element.name == "Save"
    assert (target.x, target.y) == (140, 115)


def test_resolve_without_snap_or_out_of_range_keeps_the_point():
    button = el(100, 100, 80, 30, "Save")
    snap = snapshot(button)
    assert resolve_target(200, 110, snap, root=ROOT, snap=False, radius=48).element is None
    far = resolve_target(400, 400, snap, root=ROOT, snap=True, radius=48)
    assert (far.x, far.y, far.element) == (400, 400, None)


def test_resolve_ignores_elements_of_windows_not_under_the_point():
    button = el(100, 100, 80, 30, "Save")
    target = resolve_target(110, 105, snapshot(button), root=OTHER, snap=True, radius=48)
    assert target.element is None and (target.x, target.y) == (110, 105)


def test_live_element_used_when_the_cache_misses():
    live = el(500, 500, 30, 30, "New")
    target = resolve_target(510, 510, snapshot(), root=ROOT, snap=True, radius=48, under_point=live)
    assert target.element is live and not target.snapped


def test_large_element_snaps_near_the_pointer_not_its_far_centre():
    row = Rect(0, 100, 1600, 40)
    x, y = snap_point(row, 1500, 160, 48)
    assert 1480 <= x <= 1500 and 100 < y < 140
    x, y = snap_point(Rect(100, 100, 40, 20), 150, 110, 48)
    assert (x, y) == (120, 110)


def test_index_is_fast_and_matches_brute_force():
    rng = random.Random(7)
    elements = [el(rng.randrange(0, 1900), rng.randrange(0, 1060), rng.randrange(8, 120), rng.randrange(8, 40)) for _ in range(3000)]
    index = SpatialIndex(elements)
    for _ in range(200):
        x, y = rng.uniform(0, 1920), rng.uniform(0, 1080)
        expected = min(
            (e for e in elements if e.rect.distance_to(x, y) <= 48),
            key=lambda e: (e.rect.distance_to(x, y), e.rect.area, elements.index(e)),
            default=None,
        )
        assert index.nearest(x, y, 48) is expected
    started = time.perf_counter()
    for _ in range(1000):
        index.nearest(rng.uniform(0, 1920), rng.uniform(0, 1080), 48)
    per_query = (time.perf_counter() - started) / 1000
    assert per_query < 0.001  # "never blocks": well under a millisecond with 3000 elements


def test_dedupe_and_cap():
    a = el(0, 0, 10, 10, "A")
    assert len(dedupe([a, el(0, 0, 10, 10, "A"), el(0, 0, 10, 10, "B")])) == 2
    many = [el(i * 20, 0, 10, 10, str(i)) for i in range(50)]
    kept = nearest_first(many, 0, 0, 5)
    assert [e.name for e in kept] == ["0", "1", "2", "3", "4"]


def test_inner_text_of_a_link_is_dropped():
    link = el(100, 100, 120, 24, "Read more", "Hyperlink")
    text = el(104, 104, 100, 16, "Read more", "Text")
    lone_text = el(400, 400, 100, 16, "Clickable label", "Text")
    kept = _drop_inner_duplicates([link, text, lone_text])
    assert [e.control_type for e in kept] == ["Hyperlink", "Text"]
    assert kept[1].name == "Clickable label"


def test_snapshot_names_are_unique_and_ordered():
    snap = snapshot(el(0, 0, 5, 5, "File"), el(10, 0, 5, 5, "Edit"), el(20, 0, 5, 5, "File"), el(30, 0, 5, 5, ""))
    assert snap.names() == ["File", "Edit"]


def test_within_lists_every_element_in_range_nearest_first():
    inside = el(100, 100, 40, 20, "Inside")
    big = el(80, 90, 200, 60, "Row", "ListItem")
    right = el(160, 100, 40, 20, "Right")
    far = el(400, 100, 40, 20, "Far")
    other = el(100, 130, 40, 20, "Other window", root=OTHER)
    snap = snapshot(inside, big, right, far, other, roots=(ROOT, OTHER))
    names = [e.name for e in snap.within(110, 110, 60)]
    assert names == ["Inside", "Row", "Other window", "Right"]  # equal distance: the smaller element first
    assert [e.name for e in snap.within(110, 110, 60, frozenset({OTHER}))] == ["Other window"]
    assert snap.within(110, 110, -5) == [inside, big]  # a negative radius means "touching the point"
    assert snapshot().within(0, 0, 100) == []


def test_within_matches_brute_force():
    rng = random.Random(11)
    elements = [el(rng.randrange(0, 1900), rng.randrange(0, 1060), rng.randrange(8, 120), rng.randrange(8, 40)) for _ in range(2000)]
    index = SpatialIndex(elements)
    for _ in range(100):
        x, y, r = rng.uniform(0, 1920), rng.uniform(0, 1080), rng.uniform(0, 250)
        expected = sorted(
            (e for e in elements if e.rect.distance_to(x, y) <= r),
            key=lambda e: (e.rect.distance_to(x, y), e.rect.area, elements.index(e)),
        )
        assert index.within(x, y, r) == expected