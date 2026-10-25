"""The precision overlays and state.active_overlay, driven through real actions on a fake desktop."""

from __future__ import annotations

import pytest

from sightline.core.actions import ActionError
from sightline.core.types import Action, ElementInfo, Rect
from sightline.ui.bridge import process_events_until

pytestmark = pytest.mark.gui


def _elements(ctx) -> list[ElementInfo]:
    elements = [
        ElementInfo(Rect(200, 200, 160, 40), "Save", "Button", clickable=True),
        ElementInfo(Rect(400, 200, 160, 40), "Open", "Button", clickable=True),
        ElementInfo(Rect(200, 400, 300, 30), "A link", "Hyperlink", clickable=True),
    ]
    ctx.desktop.uia.elements = elements
    return elements


def _run(ctx, name: str, **args):
    return ctx.actions.execute_now(Action(name, args, source="test"))


def _clicks(recorder) -> list[Action]:
    return [a for a in recorder if a.name == "mouse.click"]


def _open_labels(shell, ctx) -> None:
    _run(ctx, "ui.labels.show")
    assert process_events_until(lambda: shell.labels.is_open, timeout=3.0)


# =========================================================================== labels


def test_labels_show_number_and_select_by_voice(recorder, ui_ctx, shell):
    elements = _elements(ui_ctx)
    _open_labels(shell, ui_ctx)
    assert ui_ctx.state.active_overlay == "labels"
    labels = shell.labels.labels
    assert [lb.number for lb in labels] == [1, 2, 3]
    assert shell.labels.windows, "a labels window is showing"
    _run(ui_ctx, "ui.labels.select", number=2)
    click = _clicks(recorder)[-1]
    assert (click.arg("x"), click.arg("y")) == elements[1].rect.center
    assert click.arg("snap") is False
    assert not shell.labels.is_open
    assert ui_ctx.state.active_overlay is None


def test_labels_select_unknown_number_keeps_them_open(recorder, ui_ctx, shell):
    _elements(ui_ctx)
    _open_labels(shell, ui_ctx)
    with pytest.raises(ActionError, match="no number 9"):
        _run(ui_ctx, "ui.labels.select", number=9)
    assert shell.labels.is_open


def test_looking_at_a_badge_and_blinking_selects_it(recorder, ui_ctx, shell):
    elements = _elements(ui_ctx)
    _open_labels(shell, ui_ctx)
    badge = shell.labels.labels[2].badge
    # An eye click arrives as mouse.click with the gaze position.
    _run(ui_ctx, "mouse.click", button="left", x=badge.center[0], y=badge.center[1])
    click = _clicks(recorder)[-1]
    assert (click.arg("x"), click.arg("y")) == elements[2].rect.center
    assert click.arg("snap") is False
    assert not shell.labels.is_open


def test_a_click_elsewhere_goes_through_and_closes_the_numbers(recorder, ui_ctx, shell):
    _elements(ui_ctx)
    _open_labels(shell, ui_ctx)
    _run(ui_ctx, "mouse.click", x=1500, y=900)
    click = _clicks(recorder)[-1]
    assert (click.arg("x"), click.arg("y")) == (1500, 900)
    assert process_events_until(lambda: not shell.labels.is_open)
    assert ui_ctx.state.active_overlay is None
    # The interceptor is gone: a second click is untouched and nothing breaks.
    _run(ui_ctx, "mouse.click", x=10, y=10)
    assert _clicks(recorder)[-1].arg("x") == 10


def test_spoken_click_on_the_numbers_aims_where_the_user_looked(recorder, ui_ctx, shell):
    elements = _elements(ui_ctx)
    _open_labels(shell, ui_ctx)
    ui_ctx.desktop.input.pos = (1500, 900)  # the pointer moved on while the user was speaking
    ui_ctx.vision.aim = shell.labels.labels[1].badge.center
    ui_ctx.actions.execute_now(Action("mouse.click", source="voice", aim_t=42.0))
    # The countdown asks too (what would this click hit?), always for the moment speech began.
    assert ui_ctx.vision.aim_calls and set(ui_ctx.vision.aim_calls) == {42.0}
    click = _clicks(recorder)[-1]
    assert (click.arg("x"), click.arg("y")) == elements[1].rect.center


def test_labels_with_nothing_clickable_show_a_toast(recorder, ui_ctx, shell):
    ui_ctx.desktop.uia.elements = []
    _run(ui_ctx, "ui.labels.show")
    assert process_events_until(lambda: any("Nothing to number" in t.entry.text for t in shell.toasts.toasts), timeout=3.0)
    assert ui_ctx.state.active_overlay is None


# =========================================================================== grid


def test_grid_narrows_moves_the_pointer_and_clicks(recorder, ui_ctx, shell):
    ui_ctx.desktop.input.pos = (500, 500)
    shell.cursor.poll()
    _run(ui_ctx, "ui.grid.show")
    assert ui_ctx.state.active_overlay == "grid"
    monitor = shell.screens.map.screen_at(500, 500).native
    _run(ui_ctx, "ui.grid.select", cell=9)
    region = shell.grid.state.current
    assert region == Rect(monitor.x + monitor.w * 2 / 3, monitor.y + monitor.h * 2 / 3, monitor.w / 3, monitor.h / 3)
    move = [a for a in recorder if a.name == "mouse.move"][-1]
    assert (move.arg("x"), move.arg("y")) == pytest.approx(region.center)
    _run(ui_ctx, "ui.grid.select", cell=1)
    _run(ui_ctx, "ui.grid.back")
    assert shell.grid.state.current == region
    _run(ui_ctx, "ui.grid.click", button="right")
    click = _clicks(recorder)[-1]
    assert (click.arg("x"), click.arg("y")) == pytest.approx(region.center)
    assert click.arg("button") == "right" and click.arg("snap") is False
    assert shell.grid.state is None
    assert ui_ctx.state.active_overlay is None


def test_grid_eye_click_selects_a_cell_and_plain_click_clicks_the_centre(recorder, ui_ctx, shell):
    ui_ctx.desktop.input.pos = (500, 500)
    shell.cursor.poll()
    _run(ui_ctx, "ui.grid.show")
    monitor = shell.grid.state.current
    # Look at the top-left cell and blink.
    _run(ui_ctx, "mouse.click", x=monitor.x + 10, y=monitor.y + 10)
    assert shell.grid.state.depth == 1
    assert shell.grid.state.current.x == monitor.x
    assert not _clicks(recorder), "the eye click picked a cell instead of clicking"
    # "click" by voice has no coordinates: it clicks the current centre.
    _run(ui_ctx, "mouse.click")
    click = _clicks(recorder)[-1]
    assert click.arg("snap") is False
    assert shell.grid.state is None


def test_spoken_grid_opens_on_the_monitor_the_user_looked_at(recorder, ui_ctx, shell):
    second = shell.screens.map.screens[1]
    ui_ctx.desktop.input.pos = (500, 500)
    shell.cursor.poll()
    ui_ctx.vision.aim = second.native.center
    ui_ctx.actions.execute_now(Action("ui.grid.show", source="voice", aim_t=7.0))
    assert shell.grid.state.current == second.native
    _run(ui_ctx, "ui.grid.hide")
    ui_ctx.vision.aim = None  # vision not driving the pointer: the cursor decides
    ui_ctx.actions.execute_now(Action("ui.grid.show", source="voice", aim_t=8.0))
    assert shell.grid.state.current == shell.screens.map.screens[0].native


def test_grid_rejects_bad_cells(recorder, ui_ctx, shell):
    with pytest.raises(ActionError):
        _run(ui_ctx, "ui.grid.select", cell=3)  # not open
    _run(ui_ctx, "ui.grid.show")
    with pytest.raises(ActionError):
        _run(ui_ctx, "ui.grid.select", cell=10)


# =========================================================================== zoom lens


def _grab_blank(source, _screen):
    from PySide6.QtGui import QColor, QImage

    img = QImage(max(1, round(source.w)), max(1, round(source.h)), QImage.Format.Format_ARGB32)
    img.fill(QColor("#336699"))
    return img, source


def test_zoom_interceptor_maps_the_next_click_and_is_removed(recorder, ui_ctx, shell):
    shell.zoom._grabber = _grab_blank
    _run(ui_ctx, "ui.zoom.show", x=960, y=540)
    assert ui_ctx.state.active_overlay == "zoom"
    assert shell.zoom.interceptor_installed
    assert process_events_until(lambda: shell.zoom.mapping is not None, timeout=2.0)
    mapping = shell.zoom.mapping
    assert mapping.zoom == pytest.approx(3.0)
    lx, ly = mapping.lens.center
    # Look 30 px right of the lens centre: the real click lands 10 px right of the focus point.
    _run(ui_ctx, "mouse.click", x=lx + 30, y=ly)
    click = _clicks(recorder)[-1]
    assert (click.arg("x"), click.arg("y")) == (970, 540)
    assert click.arg("snap") is False
    assert process_events_until(lambda: not shell.zoom.interceptor_installed)
    assert ui_ctx.state.active_overlay is None
    _run(ui_ctx, "mouse.click", x=lx + 30, y=ly)
    assert _clicks(recorder)[-1].arg("x") == lx + 30


def test_zoom_click_outside_the_lens_goes_through_unchanged(recorder, ui_ctx, shell):
    shell.zoom._grabber = _grab_blank
    _run(ui_ctx, "ui.zoom.show", x=960, y=540)
    assert process_events_until(lambda: shell.zoom.mapping is not None, timeout=2.0)
    _run(ui_ctx, "mouse.click", x=50, y=50)
    click = _clicks(recorder)[-1]
    assert (click.arg("x"), click.arg("y")) == (50, 50)
    assert click.arg("snap") is None
    assert process_events_until(lambda: not shell.zoom.interceptor_installed)


def test_spoken_zoom_aims_where_the_user_looked_then_falls_back_to_the_cursor(recorder, ui_ctx, shell):
    shell.zoom._grabber = _grab_blank
    ui_ctx.desktop.input.pos = (300, 300)
    shell.cursor.poll()
    ui_ctx.vision.aim = (960, 540)
    ui_ctx.actions.execute_now(Action("ui.zoom.show", source="voice", aim_t=5.0))
    assert process_events_until(lambda: shell.zoom.mapping is not None, timeout=2.0)
    assert shell.zoom.window.focus == (960, 540)
    _run(ui_ctx, "ui.zoom.hide")
    ui_ctx.vision.aim = None
    ui_ctx.actions.execute_now(Action("ui.zoom.show", source="voice", aim_t=6.0))
    assert process_events_until(lambda: shell.zoom.mapping is not None, timeout=2.0)
    assert shell.zoom.window.focus == (300, 300)
    assert ui_ctx.vision.aim_calls == [5.0, 6.0]
    ui_ctx.vision = None  # no vision at all
    _run(ui_ctx, "ui.zoom.hide")
    ui_ctx.actions.execute_now(Action("ui.zoom.show", source="voice", aim_t=7.0))
    assert process_events_until(lambda: shell.zoom.mapping is not None, timeout=2.0)
    assert shell.zoom.window.focus == (300, 300)


def test_zoom_hide_removes_the_interceptor(recorder, ui_ctx, shell):
    shell.zoom._grabber = _grab_blank
    _run(ui_ctx, "ui.zoom.show")
    _run(ui_ctx, "ui.zoom.hide")
    assert not shell.zoom.interceptor_installed
    assert ui_ctx.state.active_overlay is None


# =========================================================================== help, cancel, bookkeeping


def test_help_sheet_takes_scrolling_while_open(recorder, ui_ctx, shell):
    from sightline.core.types import CommandHelp

    ui_ctx.speech.commands_help = lambda: [CommandHelp(f"Group {i // 10}", f"phrase {i}", "does a thing", "") for i in range(80)]
    _run(ui_ctx, "ui.help.show")
    assert ui_ctx.state.active_overlay == "help"
    bar = shell.help.scroll.verticalScrollBar()
    assert bar.maximum() > 0
    _run(ui_ctx, "mouse.scroll", direction="down")
    assert not [a for a in recorder if a.name == "mouse.scroll"], "the scroll went to the sheet"
    assert process_events_until(lambda: bar.value() > 0)
    _run(ui_ctx, "ui.help.hide")
    assert ui_ctx.state.active_overlay is None
    _run(ui_ctx, "mouse.scroll", direction="down")
    assert [a for a in recorder if a.name == "mouse.scroll"]


def test_one_overlay_at_a_time_and_cancel_closes_it(recorder, ui_ctx, shell):
    _elements(ui_ctx)
    _open_labels(shell, ui_ctx)
    _run(ui_ctx, "ui.grid.show")
    assert ui_ctx.state.active_overlay == "grid"
    assert not shell.labels.is_open
    _run(ui_ctx, "ui.help.show")
    assert ui_ctx.state.active_overlay == "help"
    assert shell.grid.state is None
    _run(ui_ctx, "ui.cancel")
    assert ui_ctx.state.active_overlay is None
    assert not shell.help.isVisible()
    _run(ui_ctx, "ui.cancel")  # nothing open: harmless


def test_someone_elses_overlay_value_is_restored(recorder, ui_ctx, shell):
    ui_ctx.state.update(active_overlay="keyboard")
    _run(ui_ctx, "ui.grid.show")
    assert ui_ctx.state.active_overlay == "grid"
    _run(ui_ctx, "ui.grid.hide")
    assert ui_ctx.state.active_overlay == "keyboard"


def test_wizard_actions_reach_the_open_wizard_and_are_harmless_otherwise(recorder, ui_ctx, shell):
    _run(ui_ctx, "ui.wizard.next")  # nothing open
    _run(ui_ctx, "ui.calibrate", kind="gaze")
    assert ui_ctx.state.active_overlay == "calibration"
    _run(ui_ctx, "ui.grid.show")
    assert ui_ctx.state.active_overlay == "grid"
    _run(ui_ctx, "ui.grid.hide")
    assert ui_ctx.state.active_overlay == "calibration"
    _run(ui_ctx, "ui.wizard.skip")  # gaze is optional: on to the last page
    _run(ui_ctx, "ui.wizard.next")  # Finish
    assert process_events_until(lambda: ui_ctx.state.active_overlay is None)


def test_every_ui_action_is_registered_on_the_ui_thread(shell, ui_ctx):
    from sightline.core.actions import ActionRegistry

    expected = {
        "ui.labels.show", "ui.labels.hide", "ui.labels.select",
        "ui.grid.show", "ui.grid.hide", "ui.grid.back", "ui.grid.select", "ui.grid.click",
        "ui.zoom.show", "ui.zoom.hide", "ui.help.show", "ui.help.hide",
        "ui.settings.show", "ui.settings.next", "ui.settings.previous", "ui.settings.adjust",
        "ui.settings.set", "ui.settings.preset", "ui.practice.show", "ui.calibrate", "ui.dock.toggle",
        "ui.wizard.next", "ui.wizard.back", "ui.wizard.skip", "ui.cancel",
    }  # fmt: skip
    assert expected <= set(ui_ctx.actions.names())
    specs = ui_ctx.actions._specs  # noqa: SLF001 - checking the thread choice
    assert all(specs[name].thread == "ui" for name in expected)
    assert isinstance(ui_ctx.actions, ActionRegistry)
