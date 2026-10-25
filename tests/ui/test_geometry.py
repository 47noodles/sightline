"""Pure geometry: label layout, grid subdivision, zoom lens mapping (no Qt)."""

from __future__ import annotations

import random

import pytest

from sightline.core.types import Rect
from sightline.ui.geometry import (
    GridState,
    LensMapping,
    grid_cell_at,
    grid_cells,
    label_at,
    layout_labels,
    lens_geometry,
    reading_order,
)

MONITOR = Rect(0, 0, 1920, 1080)


def badge(number: int, _monitor: Rect) -> tuple[float, float]:
    return (10.0 * len(str(number)) + 10.0, 22.0)


def _no_overlaps(labels):
    for i, a in enumerate(labels):
        for b in labels[i + 1 :]:
            assert not a.badge.intersects(b.badge), (a, b)


# =========================================================================== labels


def test_labels_are_numbered_in_reading_order_without_gaps():
    rects = [Rect(500, 300, 80, 30), Rect(100, 100, 80, 30), Rect(300, 102, 80, 30), Rect(100, 300, 80, 30)]
    labels = layout_labels(rects, [MONITOR], badge)
    assert [lb.number for lb in labels] == [1, 2, 3, 4]
    assert [lb.index for lb in labels] == [1, 2, 3, 0]


def test_labels_never_overlap_in_a_dense_toolbar():
    rects = [Rect(10 + i * 24, 40, 22, 22) for i in range(60)]  # 60 small icons in a row
    rects += [Rect(10 + i * 24, 64, 22, 22) for i in range(60)]
    labels = layout_labels(rects, [MONITOR], badge, limit=400)
    assert len(labels) > 60
    _no_overlaps(labels)
    for lb in labels:
        assert MONITOR.contains(lb.badge.x, lb.badge.y)
        assert lb.badge.right <= MONITOR.right and lb.badge.bottom <= MONITOR.bottom


def test_labels_stay_inside_their_monitor_at_the_edges():
    rects = [Rect(0, 0, 30, 30), Rect(1890, 1050, 30, 30), Rect(1900, 0, 20, 20)]
    labels = layout_labels(rects, [MONITOR], badge)
    assert len(labels) == 3
    for lb in labels:
        assert lb.badge.left >= 0 and lb.badge.top >= 0
        assert lb.badge.right <= 1920 and lb.badge.bottom <= 1080


def test_labels_random_pages_have_no_overlaps_and_respect_the_cap():
    rng = random.Random(7)
    rects = [Rect(rng.uniform(0, 1880), rng.uniform(0, 1050), rng.uniform(8, 200), rng.uniform(8, 60)) for _ in range(900)]
    labels = layout_labels(rects, [MONITOR], badge, limit=150)
    assert len(labels) == 150
    _no_overlaps(labels)
    assert [lb.number for lb in labels] == list(range(1, 151))


def test_labels_skip_tiny_offscreen_and_duplicate_elements():
    rects = [
        Rect(100, 100, 3, 3),  # tiny
        Rect(5000, 100, 50, 20),  # on no monitor
        Rect(200, 200, 100, 30),
        Rect(201, 201, 100, 30),  # same bounds give or take a pixel (a button and its text)
    ]
    labels = layout_labels(rects, [MONITOR], badge)
    assert [lb.index for lb in labels] == [2]


def test_labels_across_two_monitors_click_the_element_centre():
    left = Rect(-1280, 0, 1280, 1024)
    rects = [Rect(-600, 400, 100, 40), Rect(600, 400, 100, 40)]
    labels = layout_labels(rects, [MONITOR, left], badge)
    assert len(labels) == 2
    assert labels[0].point == (-550, 420)
    assert labels[1].point == (650, 420)
    assert left.contains(*labels[0].badge.center)


def test_label_at_uses_a_tolerance_and_picks_the_nearest():
    labels = layout_labels([Rect(100, 100, 80, 30), Rect(400, 100, 80, 30)], [MONITOR], badge)
    first = labels[0]
    x, y = first.badge.center
    assert label_at(labels, x, y) is first
    assert label_at(labels, first.badge.right + 5, y) is None
    assert label_at(labels, first.badge.right + 5, y, tolerance=8) is first


def test_reading_order_treats_a_slightly_lower_item_as_the_same_row():
    rects = [Rect(300, 104, 50, 20), Rect(100, 100, 50, 20), Rect(100, 160, 50, 20)]
    assert reading_order(rects) == [1, 0, 2]


# =========================================================================== grid


def test_grid_cells_are_a_phone_keypad():
    cells = grid_cells(Rect(0, 0, 900, 600))
    assert cells[0] == Rect(0, 0, 300, 200)
    assert cells[4] == Rect(300, 200, 300, 200)
    assert cells[8] == Rect(600, 400, 300, 200)
    assert sum(c.area for c in cells) == pytest.approx(900 * 600)


def test_grid_cell_at_edges():
    region = Rect(100, 100, 300, 300)
    assert grid_cell_at(region, 100, 100) == 1
    assert grid_cell_at(region, 399.9, 399.9) == 9
    assert grid_cell_at(region, 250, 250) == 5
    assert grid_cell_at(region, 400, 250) is None


def test_grid_state_narrows_and_goes_back():
    state = GridState.start(MONITOR)
    assert state.center == (960, 540)
    state = state.select(9).select(1)
    assert state.depth == 2
    assert state.current == Rect(1280, 720, 640 / 3, 120)
    assert state.center == pytest.approx((1280 + 320 / 3, 780))
    state = state.back()
    assert state.current == Rect(1280, 720, 640, 360)
    assert state.back().back().depth == 0  # back at the top stays at the top
    with pytest.raises(ValueError):
        state.select(0)


def test_grid_can_divide_until_cells_are_tiny():
    state = GridState.start(MONITOR)
    steps = 0
    while state.can_divide(20):
        state = state.select(5)
        steps += 1
    assert steps == 3  # 640x360, 213x120, 71x40; then 23.7x13.3 is too small
    cx, cy = state.center
    assert (cx, cy) == pytest.approx((960, 540))


# =========================================================================== zoom lens


def test_lens_maps_its_centre_to_the_focus_point():
    m = lens_geometry(960, 540, 450, 450, 3.0, MONITOR)
    assert m.zoom == pytest.approx(3.0)
    assert m.to_source(*m.lens.center) == pytest.approx((960, 540))
    # 30 px right of the lens centre is 10 px right of the focus point.
    assert m.to_source(m.lens.center[0] + 30, m.lens.center[1] - 60) == pytest.approx((970, 520))


def test_lens_near_a_corner_stays_on_screen_and_stays_exact():
    m = lens_geometry(5, 1075, 450, 450, 3.0, MONITOR)
    assert m.lens.left >= 0 and m.lens.bottom <= 1080
    assert m.source.left >= 0 and m.source.bottom <= 1080
    # The focus point is no longer the lens centre, but mapping there and back is exact.
    lx, ly = m.to_lens(5, 1075)
    assert m.contains(lx, ly)
    assert m.to_source(lx, ly) == pytest.approx((5, 1075))


def test_lens_round_trip_everywhere():
    m = LensMapping(Rect(100, 100, 300, 300), Rect(200, 200, 100, 100))
    for x in (100, 150.5, 399):
        for y in (100, 333, 399):
            assert m.to_lens(*m.to_source(x, y)) == pytest.approx((x, y))
