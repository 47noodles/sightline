"""The local number chooser: unsure gaze clicks are held back and the candidates numbered.

Runs offscreen on the fake desktop with the real action thread, so the interceptor runs
where it does in the app and its posts cross to the GUI thread through the bridge.
"""

from __future__ import annotations

import time

import pytest
from PySide6.QtCore import Qt

from sightline.core.actions import ActionError
from sightline.core.events import EyeGesture, PointerSample
from sightline.core.types import Action, ElementInfo, PointerMode, Rect
from sightline.ui.bridge import process_events_until
from sightline.ui.overlays import chooser as chooser_mod
from sightline.ui.overlays.chooser import CHOSEN, LIDS_LOOKBACK_S, ChipSpot, click_points, layout_cost, place_chips, search_radius

pytestmark = pytest.mark.gui

P = (466.0, 320.0)  # between "Italic" and "Underline"


@pytest.fixture(autouse=True)
def _shell_without_its_overlays(shell):
    """These tests install their own overlay, so take out the ones the shell installs at start."""
    for uninstall in reversed(shell._overlay_uninstalls):
        uninstall()
    shell._overlay_uninstalls = []


@pytest.fixture
def chooser(recorder, ui_ctx, shell):
    remove = chooser_mod.install(ui_ctx, shell.services)
    ui_ctx.actions.start()
    try:
        yield remove.overlay
    finally:
        ui_ctx.actions.stop()
        remove()


def _toolbar(ctx) -> list[ElementInfo]:
    elements = [ElementInfo(Rect(400 + i * 44, 300, 40, 40), name, "Button", clickable=True) for i, name in enumerate(("Bold", "Italic", "Underline", "Strike"))]
    ctx.desktop.uia.elements = elements
    return elements


def _sample(ctx, x: float, y: float, phase: str, error: float | None = 50.0, steered: bool = False) -> None:
    ctx.bus.publish(PointerSample(time.perf_counter(), x, y, phase=phase, error_px=error, steered=steered))


def _gaze_jump(ctx, x: float = P[0], y: float = P[1], error: float | None = 50.0) -> None:
    """Gaze put the pointer here and the head has not moved it since (vision says "fine" anyway)."""
    _sample(ctx, x, y, "warp", error)
    _sample(ctx, x, y, "fine", error)


def _click(ctx, x: float = P[0], y: float = P[1], source: str = "eye", **args) -> None:
    ctx.actions.dispatch(Action("mouse.click", {"button": "left", "count": 1, "x": x, "y": y, **args}, source=source))


def _clicks(recorder) -> list[Action]:
    return [a for a in recorder if a.name == "mouse.click"]


def _open(ctx, chooser, **click_args) -> None:
    _toolbar(ctx)
    _gaze_jump(ctx)
    _click(ctx, **click_args)
    assert process_events_until(lambda: chooser.is_open, timeout=3.0), "the unsure click opened the chooser"


def _run(ctx, name: str, source: str = "voice", **args) -> None:
    ctx.actions.dispatch(Action(name, args, source=source))


def _settle(seconds: float = 0.15) -> None:
    process_events_until(lambda: False, timeout=seconds)


# =========================================================================== holding back and numbering


def test_an_unsure_gaze_click_is_held_and_its_candidates_numbered(chooser, recorder, ui_ctx, shell):
    _open(ui_ctx, chooser)
    assert not _clicks(recorder), "the click waits for a choice"
    assert ui_ctx.state.active_overlay == "chooser"
    names = {chooser.elements[s.index].name: s.number for s in chooser.spots}
    # Three buttons in a row: a row of chips, numbered left to right.
    assert names == {"Bold": 1, "Italic": 2, "Underline": 3}
    work = shell.screens.map.screen_at(*P).work_rect
    chips = [chooser.chip_window(n) for n in (1, 2, 3)]
    for window in chips:
        assert window.isVisible()
        assert window.tile.number in ("1", "2", "3")
        r = window.physical
        assert work.left <= r.left and r.right <= work.right and work.top <= r.top and r.bottom <= work.bottom
        assert not any(r.intersects(e.rect) for e in chooser.elements), "chips never cover the candidates"
        flags = window.windowFlags()
        assert not flags & Qt.WindowType.WindowTransparentForInput, "a blink is a real click, so chips take clicks"
        assert flags & Qt.WindowType.WindowDoesNotAcceptFocus
        assert window.height() >= shell.theme.px(64)
    rects = [w.physical for w in chips]
    assert not any(a.intersects(b) for i, a in enumerate(rects) for b in rects[i + 1 :])
    assert chooser.marks.isVisible() and len(chooser.marks.leaders) == 3
    assert chooser.marks.windowFlags() & Qt.WindowType.WindowTransparentForInput, "the outlines never eat a click"
    assert chooser.chip_window(2).tile.text() == "Italic"


def test_saying_a_number_reruns_the_original_click_on_that_element(chooser, recorder, ui_ctx):
    _open(ui_ctx, chooser, button="right")
    elements = {e.name: e for e in chooser.elements}
    _run(ui_ctx, "ui.chooser.select", number=3)
    assert process_events_until(lambda: bool(_clicks(recorder)), timeout=3.0)
    click = _clicks(recorder)[-1]
    assert (click.arg("x"), click.arg("y")) == elements["Underline"].rect.center
    assert click.arg("button") == "right" and click.arg("count") == 1, "same button and count as the held click"
    assert click.arg("snap") is False and click.arg(CHOSEN) is True
    assert click.source == "ui" and click.aim_t is not None, "Sightline's own click, aimed when the first one was"
    assert not chooser.is_open
    assert ui_ctx.state.active_overlay is None


def test_click_learning_is_told_when_the_user_aimed_not_when_they_chose(chooser, recorder, ui_ctx):
    _toolbar(ui_ctx)
    _gaze_jump(ui_ctx)
    blink_end = time.perf_counter()
    # Vision publishes the gesture just before its click: the lids began to move 0.4 s earlier.
    ui_ctx.bus.publish(EyeGesture(blink_end, "long_blink", 0.4, P[0], P[1]))
    _click(ui_ctx)
    assert process_events_until(lambda: chooser.is_open, timeout=3.0)
    _settle(0.3)  # the user reads the numbers, then blinks on one
    cx, cy = chooser.chip_window(1).physical.center
    ui_ctx.bus.publish(EyeGesture(time.perf_counter(), "long_blink", 0.4, cx, cy))
    _click(ui_ctx, cx, cy)
    assert process_events_until(lambda: bool(_clicks(recorder)), timeout=3.0)
    click = _clicks(recorder)[-1]
    assert click.source == "ui", "not an eye click, so vision does not pair it with the blink on the chip"
    assert click.aim_t == pytest.approx(blink_end - 0.4 - LIDS_LOOKBACK_S), "just before the first blink, as vision aims it"


def test_a_chosen_dwell_click_is_aimed_when_the_dwell_ended(chooser, recorder, ui_ctx):
    _toolbar(ui_ctx)
    _gaze_jump(ui_ctx)
    before = time.perf_counter()
    _click(ui_ctx, source="dwell")
    assert process_events_until(lambda: chooser.is_open, timeout=3.0)
    after = time.perf_counter()
    _run(ui_ctx, "ui.chooser.select", number=1)
    assert process_events_until(lambda: bool(_clicks(recorder)), timeout=3.0)
    click = _clicks(recorder)[-1]
    assert click.source == "ui" and before <= click.aim_t <= after


def test_a_blink_on_a_chip_picks_it(chooser, recorder, ui_ctx):
    _open(ui_ctx, chooser, count=2)
    bold = next(e for e in chooser.elements if e.name == "Bold")
    cx, cy = chooser.chip_window(1).physical.center
    _click(ui_ctx, cx + 20, cy - 5)
    assert process_events_until(lambda: bool(_clicks(recorder)), timeout=3.0)
    click = _clicks(recorder)[-1]
    assert (click.arg("x"), click.arg("y")) == bold.rect.center
    assert click.arg("count") == 2
    assert len(_clicks(recorder)) == 1, "the blink on the chip was not itself a click"
    assert not chooser.is_open


def test_a_real_click_on_a_chip_picks_it(chooser, recorder, ui_ctx):
    _open(ui_ctx, chooser)
    italic = next(e for e in chooser.elements if e.name == "Italic")
    chooser.chip_window(2).tile.click()
    assert process_events_until(lambda: bool(_clicks(recorder)), timeout=3.0)
    assert (_clicks(recorder)[-1].arg("x"), _clicks(recorder)[-1].arg("y")) == italic.rect.center
    assert not chooser.is_open


def test_pointing_at_a_chip_arms_it_and_its_element(chooser, ui_ctx, shell):
    _open(ui_ctx, chooser)
    ui_ctx.desktop.input.pos = chooser.chip_window(2).physical.center
    shell.cursor.poll()
    assert chooser.chip_window(2).tile.armed and not chooser.chip_window(1).tile.armed
    assert chooser.marks.armed == 2


def test_spoken_click_aims_where_the_user_looked(chooser, recorder, ui_ctx):
    _toolbar(ui_ctx)
    _gaze_jump(ui_ctx)
    ui_ctx.desktop.input.pos = (1500, 900)  # the pointer moved on while the user was speaking
    ui_ctx.vision.aim = P
    ui_ctx.actions.dispatch(Action("mouse.click", source="voice", utterance="click", aim_t=42.0))
    assert process_events_until(lambda: chooser.is_open, timeout=3.0)
    assert 42.0 in ui_ctx.vision.aim_calls
    _run(ui_ctx, "ui.chooser.select", number=2)
    assert process_events_until(lambda: bool(_clicks(recorder)), timeout=3.0)
    click = _clicks(recorder)[-1]
    assert click.source == "voice" and click.aim_t == 42.0, "click learning anchors where the user looked"


# =========================================================================== when the pointer is trusted


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(lambda ctx: (_sample(ctx, *P, "warp"), _sample(ctx, P[0] - 12, P[1], "fine", steered=True)), id="head-steered-after-the-jump"),
        pytest.param(lambda ctx: (_sample(ctx, *P, "warp"), _sample(ctx, P[0] - 3, P[1], "fine", steered=True)), id="steered-by-a-few-pixels"),
        pytest.param(lambda ctx: (ctx.state.update(pointer_mode=PointerMode.HEAD), _sample(ctx, *P, "warp")), id="head-only-mode"),
        pytest.param(lambda ctx: _sample(ctx, *P, "head"), id="head-phase-before-calibration"),
        pytest.param(lambda ctx: None, id="vision-not-running"),
        pytest.param(lambda ctx: (_gaze_jump(ctx), _sample(ctx, *P, "paused")), id="eyes-paused"),
    ],
)
def test_a_trusted_pointer_clicks_straight_through(chooser, recorder, ui_ctx, setup):
    _toolbar(ui_ctx)
    setup(ui_ctx)
    _click(ui_ctx)
    assert process_events_until(lambda: bool(_clicks(recorder)), timeout=3.0)
    click = _clicks(recorder)[-1]
    assert (click.arg("x"), click.arg("y")) == P and click.arg(CHOSEN) is None
    _settle()
    assert not chooser.is_open


def test_fine_without_steering_still_asks_and_a_freeze_keeps_the_judgement(chooser, recorder, ui_ctx):
    _toolbar(ui_ctx)
    _gaze_jump(ui_ctx)
    _sample(ui_ctx, *P, "frozen")  # the eyes closing for the click
    _click(ui_ctx)
    assert process_events_until(lambda: chooser.is_open, timeout=3.0)
    _run(ui_ctx, "ui.chooser.cancel")
    assert process_events_until(lambda: not chooser.is_open, timeout=3.0)
    # Steered by the head, then frozen for the blink: trusted.
    _sample(ui_ctx, *P, "warp")
    _sample(ui_ctx, P[0] + 10, P[1], "fine", steered=True)
    _sample(ui_ctx, P[0] + 10, P[1], "frozen", steered=True)
    _click(ui_ctx, P[0] + 10)
    assert process_events_until(lambda: bool(_clicks(recorder)), timeout=3.0)
    assert not chooser.is_open


def test_vision_says_whether_the_head_steered_not_how_far_the_pointer_moved(chooser, recorder, ui_ctx):
    _toolbar(ui_ctx)
    # Moved 20 px since the jump without the head steering it (the jump settling): still a gaze guess.
    _sample(ui_ctx, *P, "warp")
    _sample(ui_ctx, P[0] + 20, P[1], "fine")
    _click(ui_ctx, P[0] + 20)
    assert process_events_until(lambda: chooser.is_open, timeout=3.0)
    _run(ui_ctx, "ui.chooser.cancel")
    assert process_events_until(lambda: not chooser.is_open, timeout=3.0)
    # Steered, then gaze jumps again: the new spot is a guess until the head checks it.
    _sample(ui_ctx, *P, "fine", steered=True)
    _sample(ui_ctx, P[0] + 4, P[1], "warp")
    _sample(ui_ctx, P[0] + 4, P[1], "fine")
    _click(ui_ctx, P[0] + 4)
    assert process_events_until(lambda: chooser.is_open, timeout=3.0)
    assert not _clicks(recorder)


def test_gaze_only_users_are_always_asked(chooser, recorder, ui_ctx):
    _toolbar(ui_ctx)
    ui_ctx.state.update(pointer_mode=PointerMode.GAZE)
    _sample(ui_ctx, *P, "gaze")
    _sample(ui_ctx, P[0] + 30, P[1], "gaze")
    _click(ui_ctx, P[0] + 30)
    assert process_events_until(lambda: chooser.is_open, timeout=3.0)
    assert not _clicks(recorder)


@pytest.mark.parametrize(
    "args, source",
    [({"snap": False}, "eye"), ({CHOSEN: True}, "eye"), ({}, "ui"), ({}, "keyboard")],
    ids=["snap-off", "already-chosen", "from-the-ui", "from-the-keyboard"],
)
def test_clicks_that_need_no_check_pass(chooser, recorder, ui_ctx, args, source):
    _toolbar(ui_ctx)
    _gaze_jump(ui_ctx)
    _click(ui_ctx, source=source, **args)
    assert process_events_until(lambda: bool(_clicks(recorder)), timeout=3.0)
    _settle()
    assert not chooser.is_open


def test_a_drop_during_a_drag_is_never_held(chooser, recorder, ui_ctx):
    _toolbar(ui_ctx)
    _gaze_jump(ui_ctx)
    ui_ctx.state.update(drag_active=True)
    _click(ui_ctx)
    assert process_events_until(lambda: bool(_clicks(recorder)), timeout=3.0)
    assert not chooser.is_open


# =========================================================================== how many candidates


def test_one_candidate_goes_through(chooser, recorder, ui_ctx):
    ui_ctx.desktop.uia.elements = [ElementInfo(Rect(440, 300, 40, 40), "Italic", "Button", clickable=True), ElementInfo(Rect(800, 300, 40, 40), "Far", "Button", clickable=True)]
    _gaze_jump(ui_ctx)
    _click(ui_ctx)
    assert process_events_until(lambda: bool(_clicks(recorder)), timeout=3.0)
    assert (_clicks(recorder)[-1].arg("x"), _clicks(recorder)[-1].arg("y")) == P, "passed through unchanged; the desktop snaps it"
    assert not chooser.is_open


def test_more_than_six_candidates_open_the_zoom_lens_there(chooser, recorder, ui_ctx, shell):
    from test_overlays import _grab_blank

    shell.zoom._grabber = _grab_blank
    ui_ctx.desktop.uia.elements = [ElementInfo(Rect(400 + (i % 4) * 30, 290 + (i // 4) * 30, 26, 26), f"Icon {i}", "Button", clickable=True) for i in range(8)]
    _gaze_jump(ui_ctx, 455, 315, error=60)
    _click(ui_ctx, 455, 315)
    assert process_events_until(lambda: shell.zoom.mapping is not None, timeout=3.0)
    assert shell.zoom.window.focus == (455, 315)
    assert ui_ctx.state.active_overlay == "zoom"
    assert not _clicks(recorder) and not chooser.is_open


def test_the_search_radius_follows_the_measured_error(chooser, recorder, ui_ctx):
    assert search_radius(None) == 60 and search_radius(float("nan")) == 60 and search_radius(-3) == 60
    assert search_radius(90) == 90 and search_radius(1000) == 250
    # "Strike" is 66 px from the click: inside an 80 px error, outside 60 px.
    _toolbar(ui_ctx)
    _gaze_jump(ui_ctx, error=80.0)
    _click(ui_ctx)
    assert process_events_until(lambda: chooser.is_open, timeout=3.0)
    assert len(chooser.spots) == 4
    _run(ui_ctx, "ui.chooser.cancel")
    assert process_events_until(lambda: not chooser.is_open, timeout=3.0)
    _gaze_jump(ui_ctx, error=None)
    _click(ui_ctx)
    assert process_events_until(lambda: chooser.is_open, timeout=3.0)
    assert len(chooser.spots) == 3


# =========================================================================== closing


def test_cancel_closes_it_without_clicking(chooser, recorder, ui_ctx):
    for name in ("ui.chooser.cancel", "ui.cancel"):
        _open(ui_ctx, chooser)
        _run(ui_ctx, name)
        assert process_events_until(lambda: not chooser.is_open, timeout=3.0), name
        assert ui_ctx.state.active_overlay is None
    _settle()
    assert not _clicks(recorder)
    assert not any(w.isVisible() for w in chooser.chips) and not chooser.marks.isVisible()


def test_it_closes_by_itself_unless_the_pointer_rests_on_a_chip(chooser, ui_ctx, shell, monkeypatch):
    monkeypatch.setattr(chooser_mod, "RECHECK_MS", 40)
    chooser.seconds = 0.1
    _open(ui_ctx, chooser)
    ui_ctx.desktop.input.pos = chooser.chip_window(1).physical.center
    shell.cursor.poll()
    _settle(0.3)
    assert chooser.is_open, "the user may be aiming at a chip"
    ui_ctx.desktop.input.pos = (1500, 900)
    shell.cursor.poll()
    assert process_events_until(lambda: not chooser.is_open, timeout=2.0)
    assert ui_ctx.state.active_overlay is None


def test_another_unsure_click_on_the_same_spot_keeps_the_numbers(chooser, recorder, ui_ctx):
    _open(ui_ctx, chooser)
    first = chooser.spots
    _click(ui_ctx, P[0] + 4, P[1] + 2)
    _settle()
    assert chooser.is_open and chooser.spots == first
    assert not _clicks(recorder)


def test_a_click_elsewhere_closes_the_numbers_and_is_judged_afresh(chooser, recorder, ui_ctx):
    _open(ui_ctx, chooser)
    _gaze_jump(ui_ctx, 1500, 800)
    _click(ui_ctx, 1500, 800)
    assert process_events_until(lambda: bool(_clicks(recorder)), timeout=3.0)
    assert (_clicks(recorder)[-1].arg("x"), _clicks(recorder)[-1].arg("y")) == (1500, 800)
    assert process_events_until(lambda: not chooser.is_open, timeout=3.0)


def test_select_complains_plainly(chooser, ui_ctx):
    with pytest.raises(ActionError, match="no numbers"):
        ui_ctx.actions.execute_now(Action("ui.chooser.select", {"number": 1}, source="voice"))
    _open(ui_ctx, chooser)
    with pytest.raises(ActionError, match="no number 7"):
        ui_ctx.actions.execute_now(Action("ui.chooser.select", {"number": 7}, source="voice"))
    with pytest.raises(ActionError, match="have gone"):
        ui_ctx.actions.execute_now(Action("ui.chooser.select", {"number": 1, "token": 999}, source="eye"))
    assert chooser.is_open, "a mistake leaves the numbers up"


def test_another_overlay_replaces_it(chooser, ui_ctx):
    _open(ui_ctx, chooser)
    ui_ctx.actions.execute_now(Action("ui.grid.show", source="test"))
    assert not chooser.is_open
    assert ui_ctx.state.active_overlay == "grid"
    ui_ctx.actions.execute_now(Action("ui.grid.hide", source="test"))
    assert ui_ctx.state.active_overlay is None


def test_uninstall_removes_the_actions_and_the_interceptor(recorder, ui_ctx, shell):
    remove = chooser_mod.install(ui_ctx, shell.services)
    assert ui_ctx.actions.has("ui.chooser.select") and ui_ctx.actions.has("ui.chooser.cancel")
    assert ui_ctx.actions._specs["ui.chooser.select"].thread == "ui"  # noqa: SLF001
    remove()
    remove()  # twice is harmless
    assert not ui_ctx.actions.has("ui.chooser.select")
    _toolbar(ui_ctx)
    _gaze_jump(ui_ctx)
    ui_ctx.actions.execute_now(Action("mouse.click", {"x": P[0], "y": P[1]}, source="eye"))
    assert _clicks(recorder), "nothing holds clicks back any more"


# =========================================================================== layout and click points (pure)

WORK = Rect(0, 0, 1920, 1020)


def _no_overlaps(spots, rects) -> None:
    chips = [s.rect for s in spots]
    assert not any(a.intersects(b) for i, a in enumerate(chips) for b in chips[i + 1 :])
    for chip in chips:
        assert WORK.left <= chip.left and chip.right <= WORK.right and WORK.top <= chip.top and chip.bottom <= WORK.bottom
        assert not any(chip.intersects(r) for r in rects if r.w < 200), "small candidates stay visible"


def test_a_row_gets_chips_numbered_left_to_right_even_in_a_corner():
    rects = [Rect(12 + i * 45, 55, 38, 38) for i in range(6)]
    targets = click_points(rects, 100, 70, 90)
    spots = place_chips(targets, rects, [(150, 90)] * 6, WORK, gap=20, clear=160)
    assert [s.index for s in sorted(spots, key=lambda s: s.number)] == [0, 1, 2, 3, 4, 5]
    _no_overlaps(spots, rects)


def test_a_column_gets_chips_numbered_top_to_bottom_beside_it():
    rects = [Rect(1850, 300 + i * 40, 60, 30) for i in range(4)]  # against the right edge
    targets = click_points(rects, 1880, 360, 90)
    spots = place_chips(targets, rects, [(160, 90)] * 4, WORK, gap=20, clear=160)
    assert [s.index for s in sorted(spots, key=lambda s: s.number)] == [0, 1, 2, 3]
    assert all(s.rect.right <= 1850 for s in spots), "no room on the right, so on the left"
    _no_overlaps(spots, rects)


def test_a_cluster_gets_a_ring_numbered_clockwise_from_the_top():
    rects = [Rect(900 + c * 50, 500 + r * 50, 40, 40) for r in range(2) for c in range(2)]
    targets = click_points(rects, 945, 545, 80)
    spots = place_chips(targets, rects, [(72, 90)] * 4, WORK, gap=20, clear=160)
    by_number = {s.number: s.index for s in spots}
    assert by_number == {1: 0, 2: 1, 3: 3, 4: 2}, "top left, top right, bottom right, bottom left"
    _no_overlaps(spots, rects)


@pytest.mark.parametrize("point", [(20, 20), (1900, 20), (20, 1000), (1900, 1000)])
def test_rings_stay_on_screen_in_every_corner(point):
    x, y = point
    rects = [Rect(x - 25 + c * 30 - 10, y - 25 + r * 30 - 10, 24, 24) for r in range(2) for c in range(3)]
    rects = [r for r in rects if WORK.contains(*r.center)] or [Rect(x - 12, y - 12, 24, 24), Rect(x + 15, y + 15, 24, 24)]
    targets = click_points(rects, x, y, 80)
    spots = place_chips(targets, rects, [(110, 90)] * len(rects), WORK, gap=20, clear=160)
    assert sorted(s.number for s in spots) == list(range(1, len(rects) + 1))
    _no_overlaps(spots, rects)


def test_a_list_of_wide_rows_gets_its_column_beside_the_rows_not_over_them():
    rows = [Rect(150, 590 + i * 55, 650, 32) for i in range(4)]
    targets = click_points(rows, 250, 680, 90)
    spots = place_chips(targets, rows, [(190, 90)] * 4, WORK, gap=20, clear=165)
    assert [s.index for s in sorted(spots, key=lambda s: s.number)] == [0, 1, 2, 3]
    assert all(s.rect.left >= 800 for s in spots), "beside the rows' far end, since the left has no room"
    _no_overlaps(spots, [])
    assert not any(s.rect.intersects(r) for s in spots for r in rows)


def test_in_a_corner_a_cluster_takes_the_layout_with_the_shortest_clearest_lines():
    # Back, Forward and Reload under the first tab, in the top left corner.
    rects = [Rect(12, 55, 38, 38), Rect(57, 55, 38, 38), Rect(102, 55, 38, 38), Rect(10, 7, 225, 38)]
    targets = click_points(rects, 76, 74, 60)
    sizes = [(140, 90), (165, 90), (155, 90), (190, 90)]
    spots = place_chips(targets, rects, sizes, WORK, gap=20, clear=135)
    _no_overlaps(spots, rects)
    assert max(s.rect.bottom for s in spots) < 300, "close by, not flung round a wide arc"
    assert layout_cost(spots, rects) < 1000


def test_layout_cost_counts_long_lines_covered_candidates_and_crossings():
    a, b = Rect(100, 100, 40, 40), Rect(200, 100, 40, 40)
    near = [ChipSpot(0, 1, Rect(90, 30, 60, 50)), ChipSpot(1, 2, Rect(190, 30, 60, 50))]
    crossed = [ChipSpot(0, 1, Rect(190, 30, 60, 50)), ChipSpot(1, 2, Rect(90, 30, 60, 50))]
    covering = [ChipSpot(0, 1, Rect(95, 95, 60, 50)), ChipSpot(1, 2, Rect(190, 30, 60, 50))]
    assert layout_cost(near, [a, b]) < layout_cost(crossed, [a, b])
    assert layout_cost(near, [a, b]) + 500 < layout_cost(covering, [a, b])


def test_choosing_a_wide_row_clicks_near_where_the_user_looked_and_off_its_links():
    row = Rect(100, 400, 900, 40)
    link = Rect(130, 405, 120, 30)
    near_link, far_row = click_points([row, link], 190, 420, 60)
    assert near_link != far_row
    assert not link.contains(*near_link), "picking the row must not click the link inside it"
    assert row.contains(*near_link)
    assert abs(near_link[0] - 190) < 200, "near the gaze, not the middle of a 900 px row"
    assert far_row == link.center
