"""The settings window: everything by eyes or voice, nothing to drag or type.

* "Most used": pointer speed, blink to click, voice sensitivity, the pause
  that ends a command and dwell time, each as five plain choices around the
  value it had when the window opened ("As is" puts it back), with a live
  preview.
* "Presets": named groups of values, each listing what it changes.
* One page per area, generated from the ``setting()`` metadata: steppers
  for numbers (the value in words between large minus and plus buttons),
  buttons for choices and on or off, and what Sightline has learned by
  itself (``readonly``, such as the pauses inside your commands) as words
  with nothing to press.
* "For a helper": the advanced settings behind one door, and the settings
  file for the few values that need typing (the wake phrase).

While open, ``state.active_overlay`` is "settings" and voice drives it with
``ui.settings.next``, ``previous``, ``adjust``, ``set`` and ``preset``.
Every change is saved at once with ``ctx.settings_changed(section)``.

Voice can turn a safety setting (``protected``) on but not off: that is
refused with ``PROTECTED_MESSAGE``, which says to look at Off and blink or
dwell instead (review S-01).  Buttons pressed with the eyes or dwell are not
limited.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QRectF, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPainter
from PySide6.QtWidgets import QHBoxLayout, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from sightline.core import paths
from sightline.core.actions import ActionError
from sightline.core.events import MicLevel, SettingsChanged
from sightline.core.types import Action

from .. import settings_model as sm
from ..controls import ChoiceRow, Preview, Stepper
from ..services import UiServices
from ..theme import frame_rect
from ..widgets import GazeButton, Rule, SightlineWindow, text

log = logging.getLogger(__name__)

PREVIEWS = {"vision.head_speed": "speed", "vision.dwell_ms": "ring", "vision.blink_click_ms": "duration", "speech.command_endpoint_ms": "duration"}
FRONT_PAGE, PRESETS_PAGE, HELPER_PAGE = "Most used", "Presets", "For a helper"
READ_ONLY_MESSAGE = "Sightline learns this one by itself, so there is nothing to change."
PROTECTED_MESSAGE = "This one keeps you safe, so voice can't turn it off.  Look at Off and blink, or dwell on it."


class _Row(QWidget):
    """A row that can be the current one for voice: drawn with a strong frame."""

    def __init__(self, window: SettingsWindow) -> None:
        super().__init__()
        self.window_ = window
        self.current = False
        t = window.svc.theme
        self.box = QVBoxLayout(self)
        m = t.px(12)
        self.box.setContentsMargins(m, m, m, m)
        self.box.setSpacing(t.px(8))

    def set_current(self, on: bool) -> None:
        if on != self.current:
            self.current = on
            self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001
        super().paintEvent(event)
        if self.current:
            t = self.window_.svc.theme
            p = QPainter(self)
            frame_rect(p, QRectF(0, 0, self.width(), self.height()), t.palette.ink, t.pxf(2.5))
            p.end()

    # Voice: every row answers these; rows that cannot do one raise ActionError.  ``source`` is the
    # action's source, so a voice change to a safety setting can be refused.
    def adjust(self, direction: int, source: str = "") -> None:
        raise ActionError("This one has no steps.")

    def set_spoken(self, value: Any, source: str = "") -> None:
        raise ActionError("Choose this one with the buttons.")

    def refresh(self) -> None:
        pass


class SettingRow(_Row):
    def __init__(self, window: SettingsWindow, info: sm.FieldInfo) -> None:
        super().__init__(window)
        self.info = info
        t = window.svc.theme
        title = text(info.label + ("  (after a restart)" if info.restart else ""), "heading")
        self.box.addWidget(title)
        if info.help and info.kind != "gestures":  # the gesture buttons say it in plain words already
            help_line = text(info.help, "small")
            help_line.setProperty("role", "secondary")
            self.box.addWidget(help_line)
        self.stepper: Stepper | None = None
        self.choices: ChoiceRow | None = None
        self.gestures: dict[str, ChoiceRow] = {}
        self.value_label = None
        self.preview: Preview | None = None
        if info.readonly:
            # Something Sightline learns by itself: the value in words, and nothing to press.
            self.value_label = text("", "heading")
            self.box.addWidget(self.value_label)
        elif info.kind == "number":
            less, more = ("Shorter", "Longer") if info.unit in ("ms", "s") else ("Less", "More")
            self.stepper = Stepper(t, less=less, more=more)
            self.stepper.stepped.connect(self.adjust)
            self.box.addWidget(self.stepper)
        elif info.kind == "bool":
            self.choices = ChoiceRow(t, [(True, "On"), (False, "Off")], columns=2)
            self.choices.chosen.connect(lambda v: window.change(info, v))
            self.box.addWidget(self.choices)
        elif info.kind == "choice" and len(info.choices) <= 4:
            self.choices = ChoiceRow(t, info.choices)
            self.choices.chosen.connect(lambda v: window.change(info, v))
            self.box.addWidget(self.choices)
        elif info.kind == "choice":
            self.stepper = Stepper(t, less="Previous", more="Next")
            self.stepper.stepped.connect(self.adjust)
            self.box.addWidget(self.stepper)
        elif info.kind == "gestures":
            for key, name in sm.GESTURE_NAMES.items():
                self.box.addWidget(text(name, "label"))
                row = ChoiceRow(t, list(sm.GESTURE_CHOICES.items()), columns=3)
                row.chosen.connect(lambda v, k=key: window.change_gesture(k, str(v)))
                self.box.addWidget(row)
                self.gestures[key] = row
        else:
            self.value_label = text("", "heading")
            self.box.addWidget(self.value_label)
            note = text("A helper can change this in the settings file (For a helper, then Open the settings file).", "small")
            note.setProperty("role", "secondary")
            self.box.addWidget(note)
        kind = PREVIEWS.get(info.key)
        if kind and not info.readonly:
            self.preview = Preview(t, kind)
            self.box.addWidget(self.preview)
        self.refresh()

    def refresh(self) -> None:
        info = self.info
        value = sm.get_value(self.window_.settings, info)
        words = sm.describe(info, value)
        if self.stepper is not None:
            if info.kind == "number":
                self.stepper.set_value(words, can_less=value > info.minimum, can_more=value < info.maximum)
            else:
                keys = [k for k, _ in info.choices]
                i = keys.index(value) if value in keys else -1
                self.stepper.set_value(words, can_less=i > 0, can_more=i < len(keys) - 1)
        if self.choices is not None:
            self.choices.set_current(value)
        for key, row in self.gestures.items():
            row.set_current(value.get(key, "none"))
        if self.value_label is not None:
            self.value_label.setText(words)
        if self.preview is not None:
            self.preview.set_value(float(value))

    def adjust(self, direction: int, source: str = "") -> None:
        if self.info.readonly:
            raise ActionError(READ_ONLY_MESSAGE)
        if self.info.kind not in ("number", "choice", "bool"):
            raise ActionError("Choose this one with the buttons.")
        value = sm.get_value(self.window_.settings, self.info)
        self.window_.change(self.info, sm.step_value(self.info, value, 1 if direction > 0 else -1), source=source)

    def set_spoken(self, value: Any, source: str = "") -> None:
        if self.info.readonly:
            raise ActionError(READ_ONLY_MESSAGE)
        parsed = sm.parse_spoken(self.info, value)
        if parsed is None:
            raise ActionError(f"{self.info.label} can't be set to {value}.")
        self.window_.change(self.info, parsed, source=source)


class FrontRow(_Row):
    """A most-used setting as five plain choices around the value it had when the window opened."""

    def __init__(self, window: SettingsWindow, item: sm.FrontItem) -> None:
        super().__init__(window)
        self.item = item
        self.info = sm.find(window.settings, item.key)
        t = window.svc.theme
        head = QHBoxLayout()
        head.addWidget(text(item.title, "heading"))
        head.addStretch(1)
        self.value_label = text("", "heading", wrap=False)
        head.addWidget(self.value_label)
        self.box.addLayout(head)
        self.base = sm.get_value(window.settings, self.info)
        self.levels = sm.front_levels(self.info, item, self.base)
        self.choices = ChoiceRow(t, [(i, word) for i, (word, _v) in enumerate(self.levels)], columns=5)
        self.choices.chosen.connect(lambda i: window.change(self.info, self.levels[int(i)][1]))
        self.box.addWidget(self.choices)
        self.preview = Preview(t, item.preview) if item.preview else None
        if self.preview is not None:
            self.box.addWidget(self.preview)
        self.refresh()

    def refresh(self) -> None:
        value = sm.get_value(self.window_.settings, self.info)
        self.value_label.setText(sm.describe(self.info, value))
        self.choices.set_current(sm.front_level_index(self.levels, value))
        # Levels that clamp to the same value as "As is" (at the end of the range) cannot do anything.
        self.choices.set_enabled_values([i for i, (_w, v) in enumerate(self.levels) if i == 2 or v != self.base])
        if self.preview is not None and self.preview.kind != "hearing":
            self.preview.set_value(float(value))

    def adjust(self, direction: int, source: str = "") -> None:
        value = sm.get_value(self.window_.settings, self.info)
        index = sm.front_level_index(self.levels, value)
        # The words run from less to more of the quantity; for sensitivity "more" means a lower threshold.
        index = 2 if index is None else index
        index = max(0, min(4, index + (1 if direction > 0 else -1)))
        self.window_.change(self.info, self.levels[index][1], source=source)

    def set_spoken(self, value: Any, source: str = "") -> None:
        word = " ".join(str(value).lower().split())
        for w, v in self.levels:
            if w.lower() == word:
                self.window_.change(self.info, v, source=source)
                return
        parsed = sm.parse_spoken(self.info, value)
        if parsed is None:
            raise ActionError(f"{self.item.title} can't be set to {value}.")
        self.window_.change(self.info, parsed, source=source)


class PresetRow(_Row):
    def __init__(self, window: SettingsWindow, preset: sm.Preset) -> None:
        super().__init__(window)
        self.preset = preset
        t = window.svc.theme
        head = QHBoxLayout()
        words = QVBoxLayout()
        words.addWidget(text(preset.name, "heading"))
        summary = text(preset.summary, "body")
        words.addWidget(summary)
        head.addLayout(words, 1)
        self.use = GazeButton(t, "Use these", "check", min_height=64)
        self.use.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.use.clicked.connect(lambda: window.apply_preset(preset.name))
        head.addWidget(self.use)
        self.box.addLayout(head)
        self.lines = text("", "small")
        self.lines.setProperty("role", "secondary")
        self.box.addWidget(self.lines)
        self.refresh()

    def refresh(self) -> None:
        self.lines.setText("\n".join(sm.preset_lines(self.window_.settings, self.preset)))

    def adjust(self, direction: int, source: str = "") -> None:
        self.window_.apply_preset(self.preset.name, source=source)


class SettingsWindow(SightlineWindow):
    overlay_name = "settings"

    def __init__(self, services: UiServices) -> None:
        super().__init__(services.theme, "Sightline settings")
        self.svc = services
        self.settings = services.ctx.settings
        t = services.theme
        self.fields = sm.fields(self.settings)
        self.open_file = lambda path: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        self.page_names = [FRONT_PAGE, PRESETS_PAGE, *(name for name, _g in sm.PAGES), HELPER_PAGE]
        self.page_name = FRONT_PAGE
        self.rows: list[_Row] = []
        self.current = 0
        self.advanced_open = False

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        nav = QWidget()
        nav.setObjectName("SettingsNav")
        nav_box = QVBoxLayout(nav)
        nav_box.setContentsMargins(t.px(12), t.px(16), t.px(12), t.px(16))
        nav_box.setSpacing(0)
        nav_box.addWidget(text("Settings", "title"))
        nav_box.addSpacing(t.px(10))
        self.nav_buttons: dict[str, GazeButton] = {}
        for name in self.page_names:
            b = GazeButton(t, name, None, min_height=64, flat=True)
            b.clicked.connect(lambda _c=False, n=name: self.show_page(n))
            nav_box.addWidget(b)
            self.nav_buttons[name] = b
        nav_box.addStretch(1)
        self.close_button = GazeButton(t, "Close", "x", min_height=64)
        self.close_button.clicked.connect(self.close)
        nav_box.addWidget(self.close_button)
        nav.setFixedWidth(t.px(270))
        root.addWidget(nav)
        root.addWidget(Rule(t, vertical=True, strong=True))

        right = QVBoxLayout()
        right.setContentsMargins(t.px(24), t.px(16), t.px(24), t.px(16))
        right.setSpacing(t.px(10))
        self.page_title = text("", "display")
        right.addWidget(self.page_title)
        self.page_intro = text("", "body")
        self.page_intro.setProperty("role", "secondary")
        right.addWidget(self.page_intro)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right.addWidget(self.scroll, 1)
        footer = QHBoxLayout()
        self.up_button = GazeButton(t, "Scroll up", "chevrons-up", min_height=64)
        self.down_button = GazeButton(t, "Scroll down", "chevrons-down", min_height=64)
        for b in (self.up_button, self.down_button):
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            b.setMinimumWidth(t.px(190))
        self.up_button.clicked.connect(lambda: self.scroll_by(-1))
        self.down_button.clicked.connect(lambda: self.scroll_by(1))
        footer.addWidget(self.up_button)
        footer.addWidget(self.down_button)
        footer.addStretch(1)
        self.hint = text("Say \"next setting\", \"more\", \"less\" or \"set to\" a value.", "small", wrap=False)
        self.hint.setProperty("role", "secondary")
        footer.addWidget(self.hint)
        right.addLayout(footer)
        root.addLayout(right, 1)

        self._removers = [services.bridge.on(SettingsChanged, self._on_settings), services.bridge.on(MicLevel, self._on_mic)]
        self.show_page(FRONT_PAGE)

    # ------------------------------------------------------------------ pages

    def show_page(self, name: str) -> None:
        self.page_name = name
        for n, b in self.nav_buttons.items():
            b.set_on(n == name)
        body = QWidget()
        box = QVBoxLayout(body)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        self.rows = []
        t = self.svc.theme
        intro = ""
        if name == FRONT_PAGE:
            intro = "The five most changed settings. \"As is\" puts one back to how it was when you opened this window."
            for item in sm.FRONT:
                self._add(box, FrontRow(self, item))
            more = QHBoxLayout()
            for label, icon, action in (("Calibrate again", "crosshair", Action("ui.calibrate", {"kind": "full"}, source="ui")), ("Practice room", "target", Action("ui.practice.show", source="ui"))):
                b = GazeButton(t, label, icon, min_height=64)
                b.clicked.connect(lambda _c=False, a=action: self.svc.ctx.actions.dispatch(a))
                more.addWidget(b)
            more.addStretch(1)
            box.addSpacing(t.px(12))
            box.addLayout(more)
        elif name == PRESETS_PAGE:
            intro = "Each preset changes a group of settings at once. You can change any of them afterwards."
            for preset in sm.all_presets(self.settings):
                self._add(box, PresetRow(self, preset))
        elif name == HELPER_PAGE:
            intro = "Advanced settings, for a helper with a mouse and keyboard."
            door = QHBoxLayout()
            self.door_button = GazeButton(t, "Hide advanced settings" if self.advanced_open else "Show advanced settings", None, min_height=64)
            self.door_button.clicked.connect(self.toggle_advanced)
            door.addWidget(self.door_button)
            open_file = GazeButton(t, "Open the settings file", None, min_height=64)
            open_file.clicked.connect(lambda: self.open_file(paths.settings_file()))
            door.addWidget(open_file)
            door.addStretch(1)
            box.addLayout(door)
            box.addSpacing(t.px(12))
            if self.advanced_open:
                for page, groups in sm.PAGES:
                    advanced = sm.page_fields(self.fields, groups, advanced=True)
                    if advanced:
                        box.addWidget(text(page, "title"))
                        for info in advanced:
                            self._add(box, SettingRow(self, info))
        else:
            groups = dict(sm.PAGES)[name]
            infos = sm.page_fields(self.fields, groups, advanced=False)
            group = None
            for info in infos:
                if info.group != group:
                    group = info.group
                    label = text(group, "label")
                    label.setProperty("role", "secondary")
                    box.addSpacing(t.px(10))
                    box.addWidget(label)
                self._add(box, SettingRow(self, info))
        box.addStretch(1)
        self.page_title.setText(name)
        self.page_intro.setText(intro)
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self.scroll.setWidget(body)
        self.current = 0
        self._mark_current()

    def _add(self, box: QVBoxLayout, row: _Row) -> None:
        box.addWidget(row)
        box.addWidget(Rule(self.svc.theme))
        self.rows.append(row)

    def toggle_advanced(self) -> None:
        self.advanced_open = not self.advanced_open
        self.show_page(HELPER_PAGE)

    def scroll_by(self, direction: int) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.value() + direction * max(40, int(self.scroll.viewport().height() * 0.8)))

    # ------------------------------------------------------------------ changing values

    def change(self, info: sm.FieldInfo, value: Any, source: str = "") -> None:
        """Store a value from a button (no source) or from an action; voice may not weaken a safety setting."""
        if not sm.voice_may_set(info, sm.get_value(self.settings, info), value, source):
            raise ActionError(PROTECTED_MESSAGE)
        if sm.set_value(self.settings, info, value):
            self.svc.ctx.settings_changed(info.section)
        self.refresh_rows()

    def change_gesture(self, gesture: str, action: str) -> None:
        actions = dict(self.settings.vision.gesture_actions)
        actions[gesture] = action
        self.settings.vision.gesture_actions = actions
        self.svc.ctx.settings_changed("vision")
        self.refresh_rows()

    def apply_preset(self, name: str, source: str = "") -> None:
        preset = sm.find_preset(self.settings, name)
        if preset is None:
            raise ActionError(f"There is no preset called {name}.")
        for section in sorted(sm.apply_preset(self.settings, preset, source=source)):
            self.svc.ctx.settings_changed(section)
        self.refresh_rows()
        self.svc.notify(f"Using {preset.name}.", "info", key="preset")

    def refresh_rows(self) -> None:
        for row in self.rows:
            row.refresh()

    def _on_settings(self, _e: SettingsChanged) -> None:
        if self.isVisible():
            self.refresh_rows()

    def _on_mic(self, e: MicLevel) -> None:
        if not self.isVisible():
            return
        for row in self.rows:
            preview = getattr(row, "preview", None)
            if preview is not None and preview.kind == "hearing":
                preview.set_hearing(e.speaking)

    # ------------------------------------------------------------------ voice

    def _mark_current(self) -> None:
        for i, row in enumerate(self.rows):
            row.set_current(i == self.current)
        if self.rows:
            self.scroll.ensureWidgetVisible(self.rows[self.current], 0, self.svc.theme.px(40))

    def next_setting(self) -> None:
        if self.current + 1 < len(self.rows):
            self.current += 1
            self._mark_current()
            return
        i = self.page_names.index(self.page_name)
        self.show_page(self.page_names[(i + 1) % len(self.page_names)])

    def previous_setting(self) -> None:
        if self.current > 0:
            self.current -= 1
            self._mark_current()
            return
        i = self.page_names.index(self.page_name)
        self.show_page(self.page_names[(i - 1) % len(self.page_names)])
        self.current = max(0, len(self.rows) - 1)
        self._mark_current()

    def current_row(self) -> _Row:
        if not self.rows:
            raise ActionError("There is nothing to change on this page.")
        return self.rows[self.current]

    def adjust(self, direction: str | int, source: str = "") -> None:
        step = 1 if str(direction).lower() in ("up", "more", "1", "+1", "higher", "longer", "faster") else -1
        self.current_row().adjust(step, source=source)

    def set_spoken(self, value: Any, source: str = "") -> None:
        self.current_row().set_spoken(value, source=source)

    # ------------------------------------------------------------------ window

    def open_on_screen(self) -> None:
        t = self.svc.theme
        smap = self.svc.screens.map
        screen = smap.screen_at(*self.svc.cursor.pos)
        work = smap.rect_to_logical(screen.work_rect, screen)
        w, h = min(t.pxf(1180), work.w * 0.94), min(t.pxf(860), work.h * 0.94)
        if not self.isVisible():
            self.setGeometry(round(work.x + (work.w - w) / 2), round(work.y + (work.h - h) / 2), round(w), round(h))
        self.refresh_rows()
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:  # noqa: ANN001
        super().closeEvent(event)

    def shutdown(self) -> None:
        for remove in self._removers:
            remove()
