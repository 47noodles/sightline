"""Physical to logical conversion, per screen, with mixed DPI and monitors left of and above the primary."""

from __future__ import annotations

import pytest

from sightline.core.types import Rect
from sightline.ui.coords import ScreenGeometry, ScreenMap, logical_to_phys, phys_to_logical

# A realistic awkward layout: a 125% laptop panel as primary, a 4K monitor at 200% to its
# right and higher up, and a 100% monitor to the left at negative coordinates.
LAPTOP = ScreenGeometry("laptop", Rect(0, 0, 1920, 1080), 1.25, Rect(0, 0, 1920, 1032), primary=True)
FOURK = ScreenGeometry("4k", Rect(1920, -240, 3840, 2160), 2.0)
LEFT = ScreenGeometry("left", Rect(-1280, 100, 1280, 1024), 1.0)
SMAP = ScreenMap([LAPTOP, FOURK, LEFT])


def test_logical_screen_keeps_native_origin_and_shrinks():
    assert FOURK.logical == Rect(1920, -240, 1920, 1080)
    assert LAPTOP.logical == Rect(0, 0, 1536, 864)
    assert LEFT.logical == LEFT.native


@pytest.mark.parametrize("screen", [LAPTOP, FOURK, LEFT])
def test_round_trip_every_screen(screen):
    for fx, fy in ((0.0, 0.0), (0.5, 0.5), (0.999, 0.25), (0.1, 0.9)):
        x = screen.native.x + fx * screen.native.w
        y = screen.native.y + fy * screen.native.h
        lx, ly = SMAP.to_logical(x, y)
        assert SMAP.screen_at_logical(lx, ly) is screen
        bx, by = SMAP.to_physical(lx, ly)
        assert bx == pytest.approx(x)
        assert by == pytest.approx(y)


def test_known_values():
    assert phys_to_logical(1920 + 1000, -240 + 500, FOURK) == (1920 + 500, -240 + 250)
    assert phys_to_logical(1000, 500, LAPTOP) == (800, 400)
    assert logical_to_phys(800, 400, LAPTOP) == (1000, 500)
    assert phys_to_logical(-640, 612, LEFT) == (-640, 612)


def test_points_in_gaps_use_the_nearest_screen():
    # Below the 4K monitor's bottom edge but right of the laptop: nearest is the 4K screen.
    assert SMAP.screen_at(3000, 1950) is FOURK
    # Far left of everything.
    assert SMAP.screen_at(-5000, 500) is LEFT


def test_logical_screens_never_overlap():
    screens = SMAP.screens
    for a in screens:
        for b in screens:
            if a is not b:
                assert not a.logical.intersects(b.logical)


def test_rect_conversion_uses_the_screen_holding_most_of_it():
    straddling = Rect(1800, 100, 300, 100)  # 120 px on the laptop, 180 px on the 4K screen
    assert SMAP.screen_for_rect(straddling) is FOURK
    logical = SMAP.rect_to_logical(straddling)
    assert logical.w == pytest.approx(150)
    assert logical.h == pytest.approx(50)


def test_centered_logical_rect_lands_on_the_point_screen():
    rect, screen = SMAP.centered_logical_rect(1925, 500, 200, 200)  # just inside the 4K screen
    assert screen is FOURK
    cx, cy = rect.center
    assert SMAP.screen_at_logical(cx, cy) is FOURK
    assert SMAP.to_physical(cx, cy, FOURK) == pytest.approx((1925, 500))


def test_virtual_rect_and_primary():
    assert SMAP.virtual_rect() == Rect.from_ltrb(-1280, -240, 5760, 1920)
    assert SMAP.primary is LAPTOP


def test_empty_map_has_a_default_screen():
    assert ScreenMap([]).primary.native == Rect(0, 0, 1920, 1080)


# --------------------------------------------------------------------------- against Qt itself


@pytest.mark.gui
def test_screen_map_matches_qt_offscreen_screens(qapp):
    from PySide6.QtGui import QGuiApplication

    smap = ScreenMap.from_qt()
    if len(smap.screens) < 2:
        pytest.skip("needs the two-screen offscreen configuration")
    by_name = {s.name: s for s in smap.screens}
    primary = by_name["primary-125"]
    right = by_name["right-200"]
    assert primary.native == Rect(0, 0, 1920, 1080) and primary.dpr == pytest.approx(1.25)
    assert right.native == Rect(1920, -240, 3840, 2160) and right.dpr == pytest.approx(2.0)
    for s in smap.screens:
        q = s.key
        lx, ly = smap.to_logical(s.native.x + s.native.w * 0.75, s.native.y + s.native.h * 0.25)
        assert QGuiApplication.screenAt(_qpoint(lx, ly)) is q


@pytest.mark.gui
def test_overlay_window_lands_on_the_right_screen(shell):
    from sightline.ui.overlays.base import OverlayWindow

    smap = shell.screens.map
    if len(smap.screens) < 2:
        pytest.skip("needs the two-screen offscreen configuration")
    window = OverlayWindow(shell.services, name="probe")
    window.resize(100, 100)
    # A point on the 200% screen: the window must be scaled by that screen.
    window.center_on_physical(4000, 600)
    window.show()
    assert window.screen().name() == "right-200"
    lx, ly = smap.to_logical(4000, 600)
    assert window.geometry().center().x() == pytest.approx(lx, abs=1)
    assert window.geometry().center().y() == pytest.approx(ly, abs=1)
    # And back onto the 125% screen.
    window.center_on_physical(960, 540)
    assert window.screen().name() == "primary-125"
    assert window.geometry().center().x() == pytest.approx(768, abs=1)
    window.hide()


def _qpoint(x: float, y: float):
    from PySide6.QtCore import QPoint

    return QPoint(round(x), round(y))
