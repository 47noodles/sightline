"""The UI shell: owns every window, overlay and sound, and the ``ui.*`` actions.

``app.py`` creates it with ``sightline.ui.create(ctx, qt_app)`` and calls
``start``, then ``run_setup`` or ``ensure_models``, and ``stop`` at exit.
Everything here runs on the Qt GUI thread; events reach it through the
bridge and ``thread="ui"`` actions through the bridge's invoker.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget

from sightline.core.actions import ActionError
from sightline.core.events import (
    ActionExecuted,
    AppLifecycle,
    ClickPerformed,
    DwellProgress,
    EyeHold,
    FaceStatus,
    GazeZoneProgress,
    HoverTarget,
    IntentResult,
    MicLevel,
    Notice,
    PointerSample,
    SettingsChanged,
    SpeechStatus,
    StateChanged,
    TranscriptFinal,
    TranscriptPartial,
)
from sightline.core.types import Action

from .bridge import Bridge
from .dock import Dock
from .overlay_state import OverlayState
from .overlays.captions import Captions
from .overlays.choices import DidYouMean
from .overlays.grid import GridOverlay
from .overlays.help import HelpSheet
from .overlays.labels import LabelsOverlay
from .overlays.marks import ClickRipple, ResumeZone, TargetHighlight
from .overlays.pointer import GazeDot, PointerRing
from .overlays.status import HudWindow, StatusModel
from .overlays.toasts import ToastStack
from .overlays.zoom import Grabber, ZoomLens
from .measures import FalseActivations
from .services import CursorTracker, Screens, UiServices
from .sounds import Sounds
from .theme import Theme
from .tray import Tray

log = logging.getLogger(__name__)

FACE_HINT_COOLDOWN_S = 20.0


class UIShell:
    def __init__(self, ctx, qt_app: QApplication, *, grabber: Grabber | None = None) -> None:  # noqa: ANN001 - AppContext
        self.ctx = ctx
        self.app = qt_app
        self._overlay_uninstalls: list[Callable[[], None]] = []
        cfg = ctx.settings.ui
        self.theme = Theme(cfg.theme, cfg.scale)
        self.bridge = Bridge(ctx.bus)
        self.screens = Screens()
        source = ctx.desktop.input.cursor_pos if ctx.desktop is not None else None
        self.cursor = CursorTracker(source)
        self.sounds = Sounds(cfg.sounds, cfg.sound_volume)
        self.overlays = OverlayState(ctx.state)
        self.services = UiServices(ctx, self.theme, self.bridge, self.screens, self.cursor, self.sounds, self.overlays)
        self._grabber = grabber
        self._started = False
        self._dock_shown = False
        self._wizards: list[QWidget] = []
        self._wizard_hooks: set[QWidget] = set()
        self._settings_window: QWidget | None = None
        self._calibration_window: QWidget | None = None
        self._practice_window: QWidget | None = None
        self._drift_run = None
        self.false_activations = FalseActivations()
        self._models_window: QWidget | None = None
        self._face_hints: dict[str, float] = {}
        self._grab_hidden: list[QWidget] = []
        self._registered: list[str] = []

    # ================================================================== UIShell interface

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        svc = self.services
        self.theme.apply_to_app(self.app)
        self.theme.changed.connect(self._on_theme)
        self.ctx.actions.set_ui_invoker(self.bridge.invoke)
        self.sounds.load()

        self.status = StatusModel(svc)
        self.ring = PointerRing(svc)
        self.gaze_dot = GazeDot(svc)
        self.highlight = TargetHighlight(svc)
        self.ripple = ClickRipple(svc)
        self.resume = ResumeZone(svc)
        self.captions = Captions(svc)
        self.did_you_mean = DidYouMean(svc, self.captions)
        self.toasts = ToastStack(svc)
        svc.toasts = self.toasts
        self.hud = HudWindow(svc, self.status)
        self.dock = Dock(svc, self.status)
        self.labels = LabelsOverlay(svc)
        self.grid = GridOverlay(svc)
        self.zoom = ZoomLens(svc, grabber=self._grabber, hide_overlays=self._hide_for_grab, target_point=self.dock.last_target)
        self.help = HelpSheet(svc)
        self.tray = Tray(svc)
        self.tray.dock_visible = lambda: self._dock_shown
        self.overlays.changed.connect(self._on_overlay_changed)

        self._wire_events()
        self._register_actions()
        from .overlays import action_menu, chooser, confirm

        # The countdown first, so its interceptor (priority 120) sees risky actions before anything else.
        self._overlay_uninstalls = [module.install(self.ctx, self.services) for module in (confirm, action_menu, chooser)]
        self.apply_settings(initial=True)
        self.cursor.start()
        self.tray.start()
        # Measure the help sheet's rows once while nothing is happening, so the first "what can I say" opens at once.
        QTimer.singleShot(3000, self.help, self._prewarm_help)
        log.info("UI started")

    def run_setup(self, on_complete: Callable[[], None]) -> None:
        from .setup import create_setup

        done = {"called": False}

        def finished() -> None:
            if done["called"]:
                return
            done["called"] = True
            on_complete()

        window = create_setup(self.services, finished, self.ensure_models)
        self._open_wizard(window)
        begin = getattr(window, "begin", None)
        if callable(begin):
            begin()
        else:
            window.show()
            window.raise_()
            window.activateWindow()

    def ensure_models(self, on_ready: Callable[[], None]) -> None:
        try:
            missing = self.ctx.models.missing()
        except Exception:
            log.exception("Could not check the model files")
            missing = []
        if not missing:
            on_ready()
            return
        from .windows.models import ModelsWindow

        log.info("%d model(s) missing; downloading", len(missing))
        window = ModelsWindow(self.services, on_ready)
        self._models_window = window
        window.begin()

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        from .windows.targets import cancel_running

        cancel_running()  # a calibration in progress tells vision it is over
        self.cursor.stop()
        self.tray.stop()
        self.ring.shutdown()
        self.overlays.close_current()
        self.dock.close_dock()
        for widget in QApplication.topLevelWidgets():
            widget.hide()
        # Without an invoker the registry would run a late ui.* action on its own thread,
        # touching widgets off the GUI thread; unregistering makes it a logged "unknown action".
        for uninstall in reversed(self._overlay_uninstalls):
            uninstall()
        self._overlay_uninstalls = []
        for name in self._registered:
            self.ctx.actions.unregister(name)
        self._registered = []
        self.ctx.actions.set_ui_invoker(None)
        self.bridge.close()
        self.sounds.stop()
        log.info("UI stopped")

    # ================================================================== wiring

    def _wire_events(self) -> None:
        on = self.bridge.on
        on(PointerSample, self.ring.on_sample)
        on(PointerSample, self.gaze_dot.on_sample)
        on(FaceStatus, self.ring.on_face)
        on(FaceStatus, self.status.on_face)
        on(FaceStatus, self._face_hint)
        on(EyeHold, self.sounds.on_eye_hold)
        on(EyeHold, self.ring.on_hold)
        on(DwellProgress, self.ring.on_dwell)
        on(GazeZoneProgress, self.resume.on_zone)
        on(MicLevel, self.status.on_mic)
        on(SpeechStatus, self.status.on_speech_status)
        on(SpeechStatus, self._speech_status)
        on(TranscriptPartial, self.captions.on_partial)
        on(TranscriptFinal, self.captions.on_final)
        on(IntentResult, self.captions.on_intent)
        on(IntentResult, self.did_you_mean.on_intent)
        on(IntentResult, self.sounds.on_intent)
        on(HoverTarget, self.highlight.on_hover)
        on(ClickPerformed, self.sounds.on_click)
        on(ClickPerformed, self.ripple.on_click)
        on(ClickPerformed, self._after_click)
        on(ActionExecuted, self.toasts.on_action)
        on(ActionExecuted, lambda e: self.false_activations.record(e.action.name, e.ok, e.error, e.action.source))
        on(ActionExecuted, self.sounds.on_action)
        on(Notice, self.toasts.on_notice)
        on(Notice, self.sounds.on_notice)
        on(StateChanged, self.status.on_state)
        on(StateChanged, self.ring.on_state)
        on(StateChanged, self.resume.on_state)
        on(StateChanged, self.captions.on_state)
        on(StateChanged, self.dock.on_state)
        on(StateChanged, self.tray.on_state)
        on(StateChanged, self.sounds.on_state)
        on(SettingsChanged, self._on_settings)
        on(AppLifecycle, self._on_lifecycle)

    def _register_actions(self) -> None:
        handlers: list[tuple[str, Callable[[Action], object], str]] = [
            ("ui.labels.show", self.labels.show, "Number everything clickable"),
            ("ui.labels.hide", self.labels.hide, "Hide the numbers"),
            ("ui.labels.select", self.labels.select, "Click a numbered item"),
            ("ui.grid.show", self.grid.show, "Show the 3 by 3 grid"),
            ("ui.grid.hide", self.grid.hide, "Hide the grid"),
            ("ui.grid.select", self.grid.select, "Narrow the grid to a square"),
            ("ui.grid.back", self.grid.back, "Go back one grid step"),
            ("ui.grid.click", self.grid.click, "Click the middle of the grid square"),
            ("ui.zoom.show", self.zoom.show, "Magnify the area around the pointer"),
            ("ui.zoom.hide", self.zoom.hide, "Close the zoom lens"),
            ("ui.help.show", self.help.show_sheet, "Show what you can say"),
            ("ui.help.hide", self.help.hide_sheet, "Hide the help"),
            ("ui.settings.show", self._show_settings, "Open settings"),
            ("ui.settings.next", lambda _a: self._settings().next_setting(), "Next setting"),
            ("ui.settings.previous", lambda _a: self._settings().previous_setting(), "Previous setting"),
            # The source goes along: voice may not turn a safety setting off (review S-01).
            ("ui.settings.adjust", lambda a: self._settings().adjust(a.arg("direction", "up"), source=a.source), "Change the current setting by one step"),
            ("ui.settings.set", lambda a: self._settings().set_spoken(a.arg("value"), source=a.source), "Set the current setting"),
            ("ui.settings.preset", lambda a: self._settings().apply_preset(str(a.arg("name", "")), source=a.source), "Use a settings preset"),
            ("ui.practice.show", self._show_practice, "Open the practice room"),
            ("ui.calibrate", self._show_calibration, "Calibrate"),
            ("ui.dock.toggle", self._toggle_dock, "Show or hide the dock"),
            ("ui.wizard.next", lambda _a: self._wizard_call("wizard_next"), "Next step"),
            ("ui.wizard.back", lambda _a: self._wizard_call("wizard_back"), "Previous step"),
            ("ui.wizard.skip", lambda _a: self._wizard_call("wizard_skip"), "Skip this step"),
            ("ui.cancel", self._cancel, "Close whatever is open"),
        ]
        for name, handler, description in handlers:
            try:
                self.ctx.actions.register(name, handler, thread="ui", description=description)
                self._registered.append(name)
            except ValueError:
                log.warning("Action %s was already registered; keeping the existing one", name)

    # ================================================================== settings

    def apply_settings(self, initial: bool = False) -> None:
        cfg = self.ctx.settings.ui
        self.theme.configure(cfg.theme, cfg.scale)
        self.sounds.configure(cfg.sounds, cfg.sound_volume)
        self.ring.enabled = cfg.show_pointer_ring
        self.ring.suppress(False)
        self.gaze_dot.enabled = cfg.show_gaze_dot
        if not cfg.show_gaze_dot:
            self.gaze_dot.hide()
        self.captions.enabled = cfg.show_captions
        self.captions.hold_seconds = cfg.caption_seconds
        if not cfg.show_captions:
            self.captions.hide_now()
        self.zoom.factor = cfg.zoom_factor
        self.labels.limit = cfg.max_numbers
        desktop_cfg = getattr(self.ctx.settings, "desktop", None)
        self.highlight.enabled = bool(getattr(desktop_cfg, "highlight_target", True))
        self.highlight.suppress(self.overlays.current in ("labels", "grid", "zoom", "chooser", "menu"))
        edge_changed = self.dock.edge != cfg.dock_edge or self.dock.reserve_space != cfg.dock_reserve_space
        self.dock.edge = cfg.dock_edge
        self.dock.reserve_space = cfg.dock_reserve_space
        if initial or cfg.show_dock != self._dock_shown:
            self._set_dock(cfg.show_dock)
        elif edge_changed and self._dock_shown:
            self.dock.close_dock()
            self.dock.open()

    def _on_settings(self, e: SettingsChanged) -> None:
        if e.section in ("ui", "desktop"):
            self.apply_settings()
        if e.section == "vision" and self.help.isVisible():
            self.help.refresh()

    def _on_theme(self) -> None:
        self.theme.apply_to_app(self.app)
        for widget in QApplication.topLevelWidgets():
            widget.update()

    # ================================================================== dock and overlays

    def _set_dock(self, shown: bool) -> None:
        self._dock_shown = shown
        if shown:
            self.hud.hide()
            self.dock.open()
        else:
            self.dock.close_dock()
            self.hud.reposition()
            self.hud.show()

    def _toggle_dock(self, _action: Action) -> None:
        self._set_dock(not self._dock_shown)

    def _on_overlay_changed(self, name: object) -> None:
        self.highlight.suppress(name in ("labels", "grid", "zoom", "chooser", "menu"))

    def _after_click(self, _e: ClickPerformed) -> None:
        if self.overlays.current in ("labels", "grid"):
            self.overlays.close_current()

    def _cancel(self, _action: Action) -> None:
        from .windows.targets import cancel_running

        if cancel_running():  # a calibration run in progress stops first
            return
        if self.dock.flyout.isVisible():
            self.dock.flyout.hide()
        if not self.overlays.close_current():
            self.captions.hide_now()

    def _hide_for_grab(self, hidden: bool) -> None:
        """Take our own marks off the screen while the zoom lens copies it."""
        if hidden:
            self.ring.suppress(True)
            self.highlight.suppress(True)
            candidates: list[QWidget] = [self.gaze_dot, self.ripple, self.captions, self.did_you_mean.bar, self.hud, *self.toasts.toasts]
            self._grab_hidden = [w for w in candidates if w.isVisible()]
            for w in self._grab_hidden:
                w.hide()
        else:
            self.ring.suppress(False)
            self.highlight.suppress(self.overlays.current in ("labels", "grid", "zoom", "chooser", "menu"))
            for w in self._grab_hidden:
                w.show()
            self._grab_hidden = []

    # ================================================================== windows

    def _show_settings(self, _action: Action) -> None:
        from .windows.settings_window import SettingsWindow

        if self._settings_window is None:
            self._settings_window = SettingsWindow(self.services)
        window = self._settings_window
        self._open_wizard(window)  # active_overlay "settings" while it is open, so speech can drive it
        window.open_on_screen()  # type: ignore[attr-defined]

    def _settings(self):  # noqa: ANN202 - SettingsWindow
        window = self._settings_window
        if window is None or not window.isVisible():
            raise ActionError("Settings are not open. Say open settings first.")
        return window

    def _show_calibration(self, action: Action) -> None:
        from .windows.wizards import CalibrationWizard, run_drift_fix

        kind = str(action.arg("kind", "full"))
        if kind == "drift":
            self._drift_run = run_drift_fix(self.services)
            return
        old = self._calibration_window
        if old is not None and old.isVisible():
            old.close()
        window = CalibrationWizard(self.services, kind)
        self._calibration_window = window
        self._open_wizard(window)
        window.begin()

    def _show_practice(self, _action: Action) -> None:
        from .windows.practice import PracticeWindow

        if self._practice_window is None:
            self._practice_window = PracticeWindow(self.services, self.false_activations)
        self._practice_window.open_on_screen()  # type: ignore[attr-defined]

    def _open_wizard(self, window: QWidget) -> None:
        if window in self._wizards:
            self._wizards.remove(window)
        self._wizards.append(window)
        self.overlays.wizard_opened(getattr(window, "overlay_name", "setup"))
        closed = getattr(window, "closed", None)
        if closed is not None and window not in self._wizard_hooks:
            self._wizard_hooks.add(window)
            closed.connect(lambda w=window: self._wizard_closed(w))

    def _wizard_closed(self, window: QWidget) -> None:
        if window in self._wizards:
            self._wizards.remove(window)
        if self._wizards:
            self.overlays.wizard_opened(getattr(self._wizards[-1], "overlay_name", "setup"))
        elif self.overlays.wizard is not None:
            self.overlays.wizard_closed(self.overlays.wizard)

    def _wizard_call(self, method: str) -> None:
        for window in reversed(self._wizards):
            if window.isVisible() and hasattr(window, method):
                getattr(window, method)()
                return

    # ================================================================== event extras

    def _face_hint(self, e: FaceStatus) -> None:
        if not e.hint:
            return
        now = time.monotonic()
        if now - self._face_hints.get(e.hint, -1e9) < FACE_HINT_COOLDOWN_S:
            return
        self._face_hints[e.hint] = now
        self.toasts.show(e.hint, "info", key="face-hint")

    def _speech_status(self, e: SpeechStatus) -> None:
        if e.error:
            self.toasts.show(f"Voice: {e.error}", "error", key="speech-status")

    def _prewarm_help(self) -> None:
        if self._started:
            self.help.refresh()

    def _on_lifecycle(self, e: AppLifecycle) -> None:
        if e.phase == "ready" and self.overlays.wizard != "setup":  # set-up starts the engines halfway through
            self.toasts.show("Sightline is ready.", "info", key="lifecycle")
