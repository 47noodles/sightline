"""The action menu at the aim point: eight numbered tiles round the target, picked by number, name or a blink."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from sightline.core.actions import ActionError
from sightline.core.events import PointerSample
from sightline.core.types import Action, ElementInfo, Rect
from sightline.ui.bridge import process_events_until
from sightline.ui.overlays import action_menu as menu_mod
from sightline.ui.overlays import chooser as chooser_mod
from sightline.ui.overlays.action_menu import MAIN_ITEMS, find_item, lattice

pytestmark = pytest.mark.gui

A = (960.0, 500.0)
EXTRA = ("keyboard.show", "mouse.drag_toggle", "mouse.scroll_start", "mouse.scroll_stop", "key.release_all")


@pytest.fixture(autouse=True)
def _shell_without_its_overlays(shell):
    """These tests install their own overlay, so take out the ones the shell installs at start."""
    for uninstall in reversed(shell._overlay_uninstalls):
        uninstall()
    shell._overlay_uninstalls = []


@pytest.fixture
def menu(recorder, ui_ctx, shell):
    for name in EXTRA:
        if not ui_ctx.actions.has(name):
            ui_ctx.actions.register(name, recorder.append)
    remove = menu_mod.install(ui_ctx, shell.services)
    ui_ctx.actions.start()
    try:
        yield remove.overlay
    finally:
        ui_ctx.actions.stop()
        remove()


def _do(ctx, name: str, source: str = "voice", **args) -> None:
    ctx.actions.dispatch(Action(name, args, source=source))


def _now(ctx, name: str, **args):
    return ctx.actions.execute_now(Action(name, args, source="voice"))


def _open(ctx, menu, x: float = A[0], y: float = A[1]) -> None:
    _do(ctx, "ui.menu.show", x=x, y=y)
    assert process_events_until(lambda: menu.is_open and menu.anchor == (x, y), timeout=3.0)


def _calls(recorder, name: str) -> list[Action]:
    return [a for a in recorder if a.name == name]


def _wait_for(recorder, name: str, count: int = 1) -> Action:
    assert process_events_until(lambda: len(_calls(recorder, name)) >= count, timeout=3.0), f"{name} was dispatched"
    return _calls(recorder, name)[-1]


def _settle(seconds: float = 0.15) -> None:
    process_events_until(lambda: False, timeout=seconds)


def _inside(r: Rect, work: Rect) -> bool:
    return work.left <= r.left and r.right <= work.right and work.top <= r.top and r.bottom <= work.bottom


# =========================================================================== opening


def test_it_opens_eight_numbered_tiles_round_the_point_with_the_target_left_clear(menu, ui_ctx, shell):
    _open(ui_ctx, menu)
    assert ui_ctx.state.active_overlay == "menu"
    tiles = menu.visible_tiles()
    assert [w.tile.number for w in tiles] == [str(n) for n in range(1, 9)]
    assert [w.tile.text() for w in tiles] == [item.label for item in MAIN_ITEMS]
    rects = [w.physical for w in tiles]
    assert not any(r.contains(*A) for r in rects), "no tile covers the target"
    assert menu.grid.hole.contains(*A)
    assert not any(a.intersects(b) for i, a in enumerate(rects) for b in rects[i + 1 :])
    work = shell.screens.map.screen_at(*A).work_rect
    assert all(_inside(r, work) for r in rects)
    for window in tiles:
        flags = window.windowFlags()
        assert not flags & Qt.WindowType.WindowTransparentForInput, "a blink is a real click, so tiles take clicks"
        assert flags & Qt.WindowType.WindowDoesNotAcceptFocus, "the window the user is working in keeps focus"
        assert window.height() >= shell.theme.px(64)
        assert window.tile.isEnabled()
    assert menu.mark.isVisible()
    assert menu.mark.windowFlags() & Qt.WindowType.WindowTransparentForInput


def test_a_spoken_menu_opens_where_the_user_looked_and_its_actions_aim_there(menu, recorder, ui_ctx):
    ui_ctx.vision.aim = (700.0, 400.0)
    ui_ctx.desktop.input.pos = (1500, 900)
    ui_ctx.actions.dispatch(Action("ui.menu.show", source="voice", utterance="menu", aim_t=42.0))
    assert process_events_until(lambda: menu.is_open, timeout=3.0)
    assert menu.anchor == (700.0, 400.0)
    _do(ui_ctx, "ui.menu.select", item="double click")
    click = _wait_for(recorder, "mouse.click")
    assert (click.arg("x"), click.arg("y")) == (700.0, 400.0)
    assert click.aim_t == 42.0


def test_opening_it_again_moves_it(menu, ui_ctx):
    _open(ui_ctx, menu)
    _open(ui_ctx, menu, 300, 300)
    assert menu.grid.hole.contains(300, 300)
    assert len(menu.visible_tiles()) == 8


def test_pointing_at_a_tile_arms_it(menu, ui_ctx, shell):
    _open(ui_ctx, menu)
    ui_ctx.desktop.input.pos = menu.windows[2].physical.center
    shell.cursor.poll()
    assert menu.windows[2].tile.armed
    assert not any(w.tile.armed for i, w in enumerate(menu.windows) if i != 2)


# =========================================================================== picking


@pytest.mark.parametrize(
    "item, name, args",
    [
        ("double", "mouse.click", {"button": "left", "count": 2}),
        ("right", "mouse.click", {"button": "right", "count": 1}),
        ("middle", "mouse.click", {"button": "middle", "count": 1}),
        ("drag", "mouse.drag_toggle", {}),
    ],
)
def test_each_pointer_item_acts_at_the_point_and_closes_the_menu(menu, recorder, ui_ctx, item, name, args):
    _open(ui_ctx, menu)
    number = [i.key for i in MAIN_ITEMS].index(item) + 1
    _do(ui_ctx, "ui.menu.select", number=number)
    action = _wait_for(recorder, name)
    assert (action.arg("x"), action.arg("y")) == A
    for key, value in args.items():
        assert action.arg(key) == value
    assert action.source == "ui", "a picked tile is not a gaze guess for the chooser to doubt"
    assert not menu.is_open
    assert ui_ctx.state.active_overlay is None
    assert not menu.visible_tiles() and not menu.mark.isVisible()


def test_keyboard_opens_the_on_screen_keyboard(menu, recorder, ui_ctx):
    _open(ui_ctx, menu)
    _do(ui_ctx, "ui.menu.select", item="keyboard")
    _wait_for(recorder, "keyboard.show")
    assert not menu.is_open


def test_zoom_here_opens_the_lens_on_the_point(menu, ui_ctx, shell):
    from test_overlays import _grab_blank

    shell.zoom._grabber = _grab_blank
    _open(ui_ctx, menu)
    _do(ui_ctx, "ui.menu.select", item="zoom here")
    assert process_events_until(lambda: shell.zoom.mapping is not None, timeout=3.0)
    assert shell.zoom.window.focus == A
    assert ui_ctx.state.active_overlay == "zoom"
    assert not menu.is_open


def test_show_numbers_puts_numbers_on_the_window(menu, ui_ctx, shell):
    ui_ctx.desktop.uia.elements = [ElementInfo(Rect(200, 200, 160, 40), "Save", "Button", clickable=True), ElementInfo(Rect(400, 200, 160, 40), "Open", "Button", clickable=True)]
    _open(ui_ctx, menu)
    _do(ui_ctx, "ui.menu.select", number=7)
    assert process_events_until(lambda: shell.labels.is_open, timeout=3.0)
    assert ui_ctx.state.active_overlay == "labels"
    assert not menu.is_open


def test_names_find_their_items():
    def key(name):
        item = find_item(MAIN_ITEMS, name)
        return item.key if item else None

    assert key("Right click") == "right" and key("context menu") == "right"
    assert key("the keyboard") == "keyboard" and key("on-screen keyboard") == "keyboard"
    assert key("drag from here") == "drag" and key("drop here") == "drag"
    assert key("scroll") == "scroll" and key("Zoom here") == "zoom" and key("show_numbers") == "numbers"
    assert key("fly") is None


def test_a_blink_on_a_tile_picks_it(menu, recorder, ui_ctx):
    _open(ui_ctx, menu)
    cx, cy = menu.windows[1].physical.center
    _do(ui_ctx, "mouse.click", source="eye", button="left", count=1, x=cx + 30, y=cy + 4)
    click = _wait_for(recorder, "mouse.click")
    assert click.arg("button") == "right" and (click.arg("x"), click.arg("y")) == A
    _settle()
    assert len(_calls(recorder, "mouse.click")) == 1, "the blink on the tile was not itself a click"
    assert not menu.is_open


def test_a_real_click_on_a_tile_picks_it(menu, recorder, ui_ctx):
    _open(ui_ctx, menu)
    menu.windows[0].tile.click()
    click = _wait_for(recorder, "mouse.click")
    assert click.arg("count") == 2 and (click.arg("x"), click.arg("y")) == A


def test_a_click_off_the_tiles_goes_through_and_closes_the_menu(menu, recorder, ui_ctx):
    _open(ui_ctx, menu)
    _do(ui_ctx, "mouse.click", source="eye", button="left", count=1, x=200, y=900)
    click = _wait_for(recorder, "mouse.click")
    assert (click.arg("x"), click.arg("y")) == (200, 900) and click.source == "eye"
    assert process_events_until(lambda: not menu.is_open, timeout=3.0)


def test_select_complains_plainly(menu, ui_ctx):
    with pytest.raises(ActionError, match="not open"):
        _now(ui_ctx, "ui.menu.select", number=1)
    _open(ui_ctx, menu)
    with pytest.raises(ActionError, match="no number 9"):
        _now(ui_ctx, "ui.menu.select", number=9)
    with pytest.raises(ActionError, match="nothing called fly"):
        _now(ui_ctx, "ui.menu.select", item="fly")
    with pytest.raises(ActionError, match="Say a number"):
        _now(ui_ctx, "ui.menu.select")
    with pytest.raises(ActionError, match="has gone"):
        _now(ui_ctx, "ui.menu.select", number=1, token=999)
    assert menu.is_open, "a mistake leaves the menu up"


def test_an_item_nobody_can_run_is_shown_disabled(menu, recorder, ui_ctx):
    ui_ctx.actions.unregister("keyboard.show")
    _open(ui_ctx, menu)
    assert not menu.windows[7].tile.isEnabled()
    assert all(w.tile.isEnabled() for w in menu.windows[:7])
    with pytest.raises(ActionError, match="Keyboard is not available"):
        _now(ui_ctx, "ui.menu.select", number=8)
    menu.windows[7].tile.click()
    _settle()
    assert menu.is_open and not _calls(recorder, "keyboard.show")


def test_while_dragging_the_drag_tile_drops(menu, recorder, ui_ctx):
    ui_ctx.state.update(drag_active=True)
    _open(ui_ctx, menu)
    assert menu.windows[3].tile.text() == "Drop here"
    _do(ui_ctx, "ui.menu.select", item="drop")
    _wait_for(recorder, "mouse.drag_toggle")


# =========================================================================== scrolling


def _start_scrolling(ctx, menu, recorder) -> None:
    _open(ctx, menu)
    _do(ctx, "ui.menu.select", item="scroll here")
    _wait_for(recorder, "mouse.scroll_start")
    assert process_events_until(lambda: menu.mode == "scroll", timeout=3.0)


def test_scroll_here_scrolls_at_the_point_and_offers_stop_up_and_down(menu, recorder, ui_ctx):
    _start_scrolling(ui_ctx, menu, recorder)
    move, start = _calls(recorder, "mouse.move")[-1], _calls(recorder, "mouse.scroll_start")[-1]
    assert (move.arg("x"), move.arg("y")) == A, "the wheel scrolls what is under the pointer"
    assert start.arg("direction") == "down" and (start.arg("x"), start.arg("y")) == A
    tiles = menu.visible_tiles()
    assert [w.tile.text() for w in tiles] == ["Stop scrolling", "Scroll up", "Scroll down"]
    assert [w.tile.is_on for w in tiles] == [False, False, True]
    assert not any(w.physical.contains(*A) for w in tiles)
    assert not menu.mark.isVisible(), "the page moves under the point while it scrolls"
    assert ui_ctx.state.active_overlay == "menu"

    _do(ui_ctx, "ui.menu.select", number=2)
    assert _wait_for(recorder, "mouse.scroll_start", 2).arg("direction") == "up"
    assert process_events_until(lambda: menu.visible_tiles()[1].tile.is_on, timeout=3.0)
    assert menu.is_open and menu.scrolling == "up"

    _do(ui_ctx, "ui.menu.select", item="stop")
    _wait_for(recorder, "mouse.scroll_stop")
    assert process_events_until(lambda: not menu.is_open, timeout=3.0)
    assert ui_ctx.state.active_overlay is None
    _settle()
    assert len(_calls(recorder, "mouse.scroll_stop")) == 1


@pytest.mark.parametrize("closer", ["ui.cancel", "ui.menu.hide"])
def test_closing_the_scroll_controls_stops_the_scrolling(menu, recorder, ui_ctx, closer):
    _start_scrolling(ui_ctx, menu, recorder)
    _do(ui_ctx, closer)
    _wait_for(recorder, "mouse.scroll_stop")
    assert not menu.is_open


def test_scrolling_stopped_another_way_retires_the_controls(menu, recorder, ui_ctx):
    _start_scrolling(ui_ctx, menu, recorder)
    _do(ui_ctx, "mouse.scroll_stop")  # "stop scrolling", or the dock
    assert process_events_until(lambda: not menu.is_open, timeout=3.0)
    _settle()
    assert len(_calls(recorder, "mouse.scroll_stop")) == 1, "nothing left to stop"


def test_scroll_words_are_ignored_quietly_outside_scroll_mode(menu, recorder, ui_ctx):
    words = ("stop scrolling", "stop", "up", "scroll up", "down")
    for word in words:
        _now(ui_ctx, "ui.menu.select", item=word)  # the scroll controls just went: no complaint
    _open(ui_ctx, menu)
    for word in words:
        _now(ui_ctx, "ui.menu.select", item=word)  # nothing scrolling to steer yet: no complaint either
    assert menu.mode == "main", "the menu waits for a real choice"
    _settle()
    assert not _calls(recorder, "mouse.scroll_start") and not _calls(recorder, "mouse.scroll_stop")
    with pytest.raises(ActionError, match="nothing called fly"):
        _now(ui_ctx, "ui.menu.select", item="fly")  # other words still get an answer
    _now(ui_ctx, "ui.menu.select", item="scroll down")  # also what "Scroll here" answers to
    assert _wait_for(recorder, "mouse.scroll_start").arg("direction") == "down"
    assert menu.mode == "scroll"
    _now(ui_ctx, "ui.menu.select", item="up")  # now it steers
    assert _wait_for(recorder, "mouse.scroll_start", 2).arg("direction") == "up"


def test_the_scroll_controls_wait_for_the_user(menu, recorder, ui_ctx):
    menu.seconds = 0.05
    _start_scrolling(ui_ctx, menu, recorder)
    _settle(0.3)
    assert menu.mode == "scroll"


# =========================================================================== closing


@pytest.mark.parametrize("closer", ["ui.menu.hide", "ui.cancel"])
def test_hide_and_cancel_close_it(menu, recorder, ui_ctx, closer):
    _open(ui_ctx, menu)
    _do(ui_ctx, closer)
    assert process_events_until(lambda: not menu.is_open, timeout=3.0)
    assert ui_ctx.state.active_overlay is None
    _settle()
    assert not _calls(recorder, "mouse.click") and not _calls(recorder, "mouse.scroll_stop")


def test_it_closes_by_itself_unless_the_pointer_rests_on_a_tile(menu, ui_ctx, shell, monkeypatch):
    monkeypatch.setattr(menu_mod, "RECHECK_MS", 40)
    menu.seconds = 0.1
    _open(ui_ctx, menu)
    ui_ctx.desktop.input.pos = menu.windows[4].physical.center
    shell.cursor.poll()
    _settle(0.3)
    assert menu.is_open, "the user is reading the tiles"
    ui_ctx.desktop.input.pos = (100, 1000)
    shell.cursor.poll()
    assert process_events_until(lambda: not menu.is_open, timeout=2.0)
    assert ui_ctx.state.active_overlay is None


def test_another_overlay_replaces_it(menu, ui_ctx):
    _open(ui_ctx, menu)
    _now(ui_ctx, "ui.grid.show")
    assert not menu.is_open
    assert ui_ctx.state.active_overlay == "grid"
    _now(ui_ctx, "ui.grid.hide")
    assert ui_ctx.state.active_overlay is None


def test_uninstall_removes_everything(recorder, ui_ctx, shell):
    remove = menu_mod.install(ui_ctx, shell.services)
    assert all(ui_ctx.actions.has(n) for n in ("ui.menu.show", "ui.menu.hide", "ui.menu.select"))
    _now(ui_ctx, "ui.menu.show", x=500, y=500)
    menu = remove.overlay
    assert menu.is_open
    remove()
    remove()
    assert not menu.is_open
    assert not any(ui_ctx.actions.has(n) for n in ("ui.menu.show", "ui.menu.hide", "ui.menu.select"))
    assert ui_ctx.state.active_overlay is None


# =========================================================================== edges and corners

WORK = Rect(0, 0, 1920, 1080)


@pytest.mark.parametrize("point", [(4, 4), (1916, 4), (4, 1076), (1916, 1076), (960, 2), (2, 540), (1918, 540), (960, 1078)])
def test_the_block_stays_on_screen_and_never_covers_the_target(point):
    x, y = point
    grid = lattice(x, y, 230, 90, 15, WORK)
    assert len(grid.tiles) == 8
    assert all(_inside(r, WORK) for r in grid.tiles)
    assert not any(r.contains(x, y) for r in grid.tiles)
    assert grid.hole.inflate(15).contains(x, y)


@pytest.mark.parametrize("point", [(6, 6), (1914, 1074), (1920 + 3840 - 8, -240 + 8), (1920 + 10, 1900)])
def test_near_edges_the_menu_stays_on_its_screen(menu, ui_ctx, shell, point):
    _open(ui_ctx, menu, *point)
    work = shell.screens.map.screen_at(*point).work_rect
    tiles = menu.visible_tiles()
    assert len(tiles) == 8
    assert all(_inside(w.physical, work) for w in tiles)
    assert not any(w.physical.contains(*point) for w in tiles)


# =========================================================================== with the number chooser


def test_menu_clicks_are_not_second_guessed_by_the_chooser(menu, recorder, ui_ctx, shell):
    remove = chooser_mod.install(ui_ctx, shell.services)
    try:
        ui_ctx.desktop.uia.elements = [ElementInfo(Rect(900 + i * 44, 480, 40, 40), f"B{i}", "Button", clickable=True) for i in range(3)]
        ui_ctx.bus.publish(PointerSample(0.0, *A, phase="warp", error_px=60.0))
        _open(ui_ctx, menu)
        _do(ui_ctx, "mouse.click", source="eye", x=menu.windows[1].physical.center[0], y=menu.windows[1].physical.center[1])
        click = _wait_for(recorder, "mouse.click")
        assert click.arg("button") == "right"
        assert not remove.overlay.is_open
    finally:
        remove()
