"""The settings model (pure) and the settings window built for gaze and voice."""

from __future__ import annotations

import pytest

from sightline.core.actions import ActionError
from sightline.core.events import SettingsChanged
from sightline.core.types import Action
from sightline.settings import Settings
from sightline.ui import settings_model as sm
from sightline.ui.bridge import process_events_until

# =========================================================================== model


def test_fields_come_from_the_metadata_and_skip_hidden_ones():
    s = Settings()
    infos = {f.key: f for f in sm.fields(s)}
    assert "vision.camera_index" not in infos and "general.first_run_complete" not in infos
    assert infos["vision.head_speed"].kind == "number"
    assert infos["vision.pointer_mode"].kind == "choice"
    assert infos["vision.winks"].kind == "bool"
    assert infos["speech.wake_phrase"].kind == "text"
    assert infos["vision.gesture_actions"].kind == "gestures"
    assert infos["vision.camera_width"].kind == "text", "numbers without a range are left to a helper"
    assert infos["vision.dwell_ms"].step == 50


@pytest.mark.parametrize(
    ("key", "value", "words"),
    [
        ("vision.blink_click_ms", 350, "0.35 seconds"),
        ("vision.dwell_ms", 1000, "1 second"),
        ("ui.sound_volume", 0.6, "60%"),
        ("vision.head_speed", 1.2, "1.2 times"),
        ("ui.scale", 1.25, "125%"),
        ("desktop.snap_radius_px", 48, "48 pixels"),
        ("vision.winks", False, "Off"),
        ("vision.resume_zone", "top_left", "Top left corner"),
    ],
)
def test_values_read_as_words(key, value, words):
    assert sm.describe(sm.find(Settings(), key), value) == words


def test_steps_snap_to_round_values_and_stop_at_the_ends():
    s = Settings()
    dwell = sm.find(s, "vision.dwell_ms")
    assert sm.step_value(dwell, 900, 1) == 950
    assert sm.step_value(dwell, 925, 1) == 950 and sm.step_value(dwell, 925, -1) == 900
    assert sm.step_value(dwell, 3000, 1) == 3000
    zone = sm.find(s, "vision.resume_zone")
    assert sm.step_value(zone, "top", 1) == "top_left"
    assert sm.step_value(zone, "off", 1) == "off"


def test_spoken_values_become_settings():
    s = Settings()
    assert sm.parse_spoken(sm.find(s, "vision.blink_click_ms"), "0.5") == 500
    assert sm.parse_spoken(sm.find(s, "vision.dwell_ms"), 1.5) == 1500
    assert sm.parse_spoken(sm.find(s, "ui.sound_volume"), 60) == 0.6
    assert sm.parse_spoken(sm.find(s, "ui.scale"), 150) == 1.5
    assert sm.parse_spoken(sm.find(s, "vision.resume_zone"), "top left") == "top_left"
    assert sm.parse_spoken(sm.find(s, "vision.resume_zone"), 2) == "top_left"
    assert sm.parse_spoken(sm.find(s, "vision.winks"), "off") is False
    assert sm.parse_spoken(sm.find(s, "vision.winks"), "purple") is None


def test_front_levels_sit_around_the_opening_value():
    s = Settings()
    speed = sm.find(s, "vision.head_speed")
    levels = sm.front_levels(speed, sm.FRONT[0], 1.0)
    values = [v for _w, v in levels]
    assert [w for w, _v in levels][2] == "As is" and values[2] == 1.0
    assert values == sorted(values)
    sens = sm.find(s, "speech.speech_threshold")
    item = next(i for i in sm.FRONT if i.key == "speech.speech_threshold")
    more = sm.front_levels(sens, item, 0.5)[4]
    assert more == ("More", 0.3), "more sensitive means a lower threshold"


def test_values_sightline_learns_are_read_only():
    s = Settings()
    info = sm.find(s, "speech.learned_command_pause")
    assert info.readonly and info.kind == "text"
    assert not sm.find(s, "speech.command_endpoint_ms").readonly
    assert sm.set_value(s, info, "0.2 seconds") is False, "nothing in the settings window changes it"
    assert s.speech.learned_command_pause == "Not learned yet"
    assert sm.describe(info, "0.84 seconds") == "0.84 seconds"


def test_presets_say_what_they_change_and_apply():
    s = Settings()
    tremor = sm.find_preset(s, "tremor")
    assert tremor is not None and tremor.name == "Tremor"
    lines = sm.preset_lines(s, tremor)
    assert "Pointer smoothing: 75% (now 50%)" in lines
    assert sm.apply_preset(s, tremor) == {"vision", "desktop"}
    assert s.vision.smoothing == 0.75 and s.desktop.snap_radius_px == 80
    assert sm.apply_preset(s, sm.find_preset(s, "sightline defaults")) == {"vision", "desktop"}
    assert s.vision.smoothing == 0.5


def test_safety_settings_are_protected():
    # Review S-01: voice may turn these on, never off.
    s = Settings()
    for key in ("ui.confirm_risky", "speech.attention_gate", "speech.mouth_gate"):
        assert sm.find(s, key).protected, key
    assert not sm.find(s, "ui.confirm_delay_ms").protected
    info = sm.find(s, "ui.confirm_risky")
    assert sm.weakens(info, True, False)
    assert not sm.weakens(info, False, True) and not sm.weakens(info, True, True)
    assert not sm.voice_may_set(info, True, False, "voice")
    assert sm.voice_may_set(info, False, True, "voice"), "turning one on by voice is fine"
    for source in ("eye", "dwell", "ui", ""):
        assert sm.voice_may_set(info, True, False, source), source
    assert sm.voice_may_set(sm.find(s, "ui.scale"), 1.0, 2.0, "voice")


def test_a_preset_by_voice_never_turns_a_safety_setting_off():
    s = Settings()
    careless = sm.Preset("Careless", "for the test", (("ui.confirm_risky", False), ("ui.scale", 1.25)))
    assert sm.apply_preset(s, careless, source="voice") == {"ui"}
    assert s.ui.confirm_risky is True and s.ui.scale == 1.25
    sm.apply_preset(s, careless)  # from a button the eyes pressed
    assert s.ui.confirm_risky is False


# =========================================================================== window

def _open(ctx, shell):
    ctx.actions.execute_now(Action("ui.settings.show"))
    window = shell._settings_window  # noqa: SLF001
    assert process_events_until(lambda: window.isVisible())
    return window


@pytest.mark.gui
def test_settings_open_for_voice_and_have_nothing_to_drag_or_type(shell, ui_ctx):
    from PySide6.QtWidgets import QAbstractSlider, QAbstractSpinBox, QComboBox, QLineEdit, QScrollBar

    from sightline.ui.widgets import GazeButton

    window = _open(ui_ctx, shell)
    assert ui_ctx.state.active_overlay == "settings"
    assert len(window.rows) == 5 and window.page_name == "Most used"
    for name in window.page_names:
        window.show_page(name)
        process_events_until(lambda: False, timeout=0.02)
        for kind in (QAbstractSlider, QAbstractSpinBox, QComboBox, QLineEdit):
            # The page's own scroll bar is there for a helper's mouse; the big Scroll buttons are the gaze route.
            assert not [w for w in window.findChildren(kind) if w.isVisible() and not isinstance(w, QScrollBar)], (name, kind)
        small = [b.text() for b in window.findChildren(GazeButton) if b.isVisible() and b.height() < shell.theme.px(64)]
        assert not small, (name, small)
    window.close()
    assert process_events_until(lambda: ui_ctx.state.active_overlay is None)
    with pytest.raises(ActionError, match="not open"):
        ui_ctx.actions.execute_now(Action("ui.settings.next"))


@pytest.mark.gui
def test_voice_moves_between_settings_and_changes_them(shell, ui_ctx):
    seen: list[str] = []
    ui_ctx.bus.subscribe(SettingsChanged, lambda e: seen.append(e.section))
    window = _open(ui_ctx, shell)
    run = ui_ctx.actions.execute_now
    run(Action("ui.settings.adjust", {"direction": "up"}))  # pointer speed: a bit faster
    assert ui_ctx.settings.vision.head_speed > 1.0 and seen[-1] == "vision"
    run(Action("ui.settings.set", {"value": "as is"}))
    assert ui_ctx.settings.vision.head_speed == 1.0
    run(Action("ui.settings.next"))
    assert window.current == 1 and window.rows[1].current
    run(Action("ui.settings.set", {"value": "longer"}))  # blink to click
    assert ui_ctx.settings.vision.blink_click_ms == round(350 * 1.6)
    run(Action("ui.settings.preset", {"name": "tired eyes"}))
    assert ui_ctx.settings.vision.dwell_ms == 1300 and ui_ctx.settings.ui.scale == 1.25
    with pytest.raises(ActionError, match="no preset"):
        run(Action("ui.settings.preset", {"name": "jazz hands"}))
    window.show_page("Eyes and pointer")
    first = window.rows[0]
    assert first.info.key == "vision.pointer_mode"
    run(Action("ui.settings.set", {"value": "head only"}))
    assert ui_ctx.settings.vision.pointer_mode.value == "head"
    for _ in range(len(window.rows) + 1):
        run(Action("ui.settings.next"))
    assert window.page_name == "Voice", "next setting carries on to the next page"


@pytest.mark.gui
def test_what_sightline_learned_shows_as_words_with_nothing_to_press(shell, ui_ctx):
    from PySide6.QtWidgets import QLabel

    from sightline.ui.controls import ChoiceRow, Stepper
    from sightline.ui.widgets import GazeButton

    ui_ctx.settings.speech.learned_command_pause = "0.84 seconds"
    window = _open(ui_ctx, shell)
    window.show_page("Voice")
    row = next(r for r in window.rows if r.info.key == "speech.learned_command_pause")
    assert row.value_label.text() == "0.84 seconds"
    assert not row.findChildren(Stepper) and not row.findChildren(ChoiceRow) and not row.findChildren(GazeButton)
    assert not any("settings file" in label.text() for label in row.findChildren(QLabel)), "not something a helper should type in"
    window.current = window.rows.index(row)
    for name, args in (("ui.settings.adjust", {"direction": "up"}), ("ui.settings.set", {"value": 2})):
        with pytest.raises(ActionError, match="learns this one by itself"):
            ui_ctx.actions.execute_now(Action(name, args))
    assert ui_ctx.settings.speech.learned_command_pause == "0.84 seconds"
    # Speech learns on while the window is open.
    ui_ctx.settings.speech.learned_command_pause = "0.9 seconds"
    ui_ctx.settings_changed("speech")
    assert process_events_until(lambda: row.value_label.text() == "0.9 seconds")


@pytest.mark.gui
def test_advanced_settings_wait_behind_a_door(shell, ui_ctx):
    window = _open(ui_ctx, shell)
    window.show_page("For a helper")
    assert window.rows == []
    opened: list = []
    window.open_file = opened.append
    window.toggle_advanced()
    keys = {row.info.key for row in window.rows}
    assert "vision.warp_dwell_ms" in keys and "speech.engine" in keys
    assert all(row.info.advanced for row in window.rows)
    ordinary = set()
    for name, _groups in sm.PAGES:
        window.show_page(name)
        ordinary |= {row.info.key for row in window.rows}
    assert not ordinary & keys


@pytest.mark.gui
def test_voice_can_turn_a_safety_setting_on_but_not_off(shell, ui_ctx):
    window = _open(ui_ctx, shell)
    window.show_page("Look and sound")
    row = next(r for r in window.rows if r.info.key == "ui.confirm_risky")
    window.current = window.rows.index(row)
    run = ui_ctx.actions.execute_now
    for name, args in (("ui.settings.set", {"value": "off"}), ("ui.settings.adjust", {"direction": "down"})):
        with pytest.raises(ActionError, match="voice can't turn it off"):
            run(Action(name, args, source="voice"))
        assert ui_ctx.settings.ui.confirm_risky is True, name
    row.choices.chosen.emit(False)  # a blink or dwell on Off
    assert ui_ctx.settings.ui.confirm_risky is False
    run(Action("ui.settings.set", {"value": "on"}, source="voice"))
    assert ui_ctx.settings.ui.confirm_risky is True
