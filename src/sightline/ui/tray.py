"""The notification-area icon and its menu: pause or resume eyes, sleep or wake voice, calibrate, settings, help, quit."""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from sightline import APP_NAME
from sightline.core.events import StateChanged
from sightline.core.types import Action, SpeechMode

from . import icons
from .services import UiServices
from .theme import DARK, LIGHT, taskbar_prefers_dark

log = logging.getLogger(__name__)


class Tray(QObject):
    def __init__(self, services: UiServices) -> None:
        super().__init__()
        self.svc = services
        self.menu = QMenu()
        self.menu.setTitle(APP_NAME)
        self.actions: dict[str, QAction] = {}
        for key, label in (
            ("eyes", "Pause eyes"),
            ("voice", "Sleep voice"),
            (None, None),
            ("calibrate", "Calibrate"),
            ("settings", "Settings"),
            ("help", "What can I say"),
            ("dock", "Hide the dock"),
            (None, None),
            ("quit", "Quit Sightline"),
        ):
            if key is None:
                self.menu.addSeparator()
                continue
            action = self.menu.addAction(label)
            action.triggered.connect(lambda _checked=False, k=key: self.trigger(k))
            self.actions[key] = action
        self.menu.aboutToShow.connect(self.sync)
        self.icon: QSystemTrayIcon | None = None
        self.dock_visible = lambda: True  # replaced by the shell

    def start(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.info("No system tray on this platform; the tray icon is skipped")
            return
        self.icon = QSystemTrayIcon(self._icon(), self)
        self.icon.setContextMenu(self.menu)
        self.icon.setToolTip(APP_NAME)
        self.icon.activated.connect(self._activated)
        self.icon.show()
        self.sync()

    def stop(self) -> None:
        if self.icon is not None:
            self.icon.hide()
            self.icon = None

    def _icon(self):  # noqa: ANN202 - QIcon
        ink = DARK.ink if taskbar_prefers_dark() else LIGHT.ink
        return icons.app_icon(ink, paused=bool(self.svc.ctx.state.get("pointer_paused")))

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            # A plain click opens the same menu: many users never right-click.
            if self.icon is not None:
                self.sync()
                self.menu.popup(self.icon.geometry().center())

    def on_state(self, e: StateChanged) -> None:
        if e.field in ("pointer_paused", "speech_mode"):
            self.sync()
            if self.icon is not None and e.field == "pointer_paused":
                self.icon.setIcon(self._icon())

    def sync(self) -> None:
        st = self.svc.ctx.state.snapshot()
        registry = self.svc.ctx.actions
        self.actions["eyes"].setText("Resume eyes" if st.pointer_paused else "Pause eyes")
        self.actions["eyes"].setEnabled(registry.has("pointer.pause"))
        asleep = SpeechMode(st.speech_mode) == SpeechMode.SLEEP
        self.actions["voice"].setText("Wake voice" if asleep else "Sleep voice")
        self.actions["voice"].setEnabled(registry.has("speech.mode"))
        self.actions["dock"].setText("Hide the dock" if self.dock_visible() else "Show the dock")
        if self.icon is not None:
            eyes = "eyes paused" if st.pointer_paused else "eyes on"
            voice = "voice asleep" if asleep else "voice on"
            self.icon.setToolTip(f"{APP_NAME}: {eyes}, {voice}")

    def trigger(self, key: str) -> None:
        st = self.svc.ctx.state.snapshot()
        if key == "eyes":
            name, args = ("pointer.resume" if st.pointer_paused else "pointer.pause"), {}
        elif key == "voice":
            asleep = SpeechMode(st.speech_mode) == SpeechMode.SLEEP
            name, args = "speech.mode", {"mode": SpeechMode.AUTO.value if asleep else SpeechMode.SLEEP.value}
        elif key == "calibrate":
            name, args = "ui.calibrate", {"kind": "full"}
        elif key == "settings":
            name, args = "ui.settings.show", {}
        elif key == "help":
            name, args = "ui.help.show", {}
        elif key == "dock":
            name, args = "ui.dock.toggle", {}
        elif key == "quit":
            name, args = "app.quit", {}
        else:
            return
        self.svc.ctx.actions.dispatch(Action(name, args, source="ui"))
