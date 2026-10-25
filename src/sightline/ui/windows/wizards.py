"""The set-up wizard (first run) and the calibration wizard (``ui.calibrate``), built from shared pages.

Set-up order: welcome (who is doing it), Windows checks, downloads, then
camera and microphone (they need the engines, which need the downloads),
hand over (helper mode only), eye clicks, gaze, head, the optional mouth
check, and finish, which opens the practice room.  The engines start the
moment the downloads are ready, not at the end, because every page after
that needs them.

Everything here works by eyes and voice once handed over: the big Next
button doubles as Start on pages that run something, and "next", "back"
and "skip" arrive as ``ui.wizard.*`` actions.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget

from sightline import APP_NAME
from sightline.core.events import CameraFrame, GazeModelChanged, MicLevel, TranscriptFinal
from sightline.core.interfaces import CalibrationReport
from sightline.core.types import Action, SystemCheck

from ..controls import ChoiceRow, LevelMeter
from ..measures import comfort_line
from ..services import UiServices
from ..theme import frame_rect
from ..widgets import GazeButton, ProgressLine, RuledTable, Rule, SightlineWindow, TableRow, text
from . import targets
from .models import ModelDownloadView

log = logging.getLogger(__name__)

HOW_IT_WORKS = (
    TableRow(("Look", "The pointer goes where you look. Small head movements fine-tune it.")),
    TableRow(("Close both eyes", "Clicks. You hear a tick when it is ready; it clicks when you open them.")),
    TableRow(("Wink your right eye", "Right click.")),
    TableRow(("Speak", "Commands like \"scroll down\" run. In a text box, what you say is typed.")),
)
CAMERA_ADVICE = (
    TableRow(("Where", "At the top centre of the screen, or just below it looking slightly up. Below sees eyelids better.")),
    TableRow(("Light", "Light your face from the front, for example a lamp behind the screen. A bright window behind you hides your eyes.")),
    TableRow(("Calls", "If you need this camera for video calls, a second cheap USB camera for Sightline avoids clashes.")),
)
MOUTH_SENTENCE = "My computer listens when I look at it and speak."
READ_S, QUIET_S = 5.0, 4.0


def _ms(seconds: float) -> int:
    return max(1, int(seconds * 1000 * targets.TIME_SCALE))


# =========================================================================== framework


class WizardPage(QWidget):
    title = ""
    optional = False  # shows "Skip this step"
    changed = Signal()

    def __init__(self, wizard: WizardWindow) -> None:
        super().__init__()
        self.wizard = wizard
        self.svc = wizard.svc
        t = self.svc.theme
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(0, 0, 0, 0)
        self.root.setSpacing(t.px(14))
        self.heading = text(self.title, "display")
        self.root.addWidget(self.heading)

    def para(self, content: str, role: str = "body", *, secondary: bool = False) -> QLabel:
        label = text(content, role)
        if secondary:
            label.setProperty("role", "secondary")
        self.root.addWidget(label)
        return label

    def available(self) -> bool:
        return True

    def ready(self) -> bool:
        return True

    def busy(self) -> bool:
        return False

    def start_action(self) -> bool:
        """Do the page's main thing (Next doubles as Start).  True when something started."""
        return False

    def accept(self) -> None:
        """Called when Next leaves the page forwards."""

    def enter(self) -> None:
        pass

    def leave(self) -> None:
        pass

    def next_text(self) -> str:
        return "Next"

    def close_page(self) -> None:
        self.leave()


class WizardWindow(SightlineWindow):
    overlay_name = "setup"

    def __init__(self, services: UiServices, title: str) -> None:
        super().__init__(services.theme, title)
        self.svc = services
        t = services.theme
        self.pages: list[WizardPage] = []
        self.index = -1
        self.results: list[tuple[str, str]] = []  # (step, outcome) for the last page
        self._closing = False
        self.open_uri: Callable[[str], object] = lambda uri: QDesktopServices.openUrl(QUrl(uri))

        root = QVBoxLayout(self)
        m = t.px(32)
        root.setContentsMargins(m, t.px(24), m, t.px(24))
        root.setSpacing(t.px(14))
        header = QHBoxLayout()
        self.step_label = text("", "label", wrap=False)
        self.step_label.setProperty("role", "secondary")
        header.addWidget(self.step_label)
        header.addStretch(1)
        self.helper_tag = text("Helper mode: use the mouse and keyboard", "label", wrap=False)
        self.helper_tag.hide()
        header.addWidget(self.helper_tag)
        self.hand_over_button = GazeButton(t, "Hand over", "arrow-right", min_height=64, primary=True)
        self.hand_over_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.hand_over_button.clicked.connect(self.hand_over)
        self.hand_over_button.hide()
        header.addWidget(self.hand_over_button)
        root.addLayout(header)
        root.addWidget(Rule(t, strong=True))
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        root.addWidget(Rule(t))
        nav = QHBoxLayout()
        nav.setSpacing(t.px(12))
        self.back_button = GazeButton(t, "Back", "arrow-left", min_height=64)
        self.skip_button = GazeButton(t, "Skip this step", None, min_height=64)
        self.next_button = GazeButton(t, "Next", "arrow-right", min_height=64, primary=True)
        for b in (self.back_button, self.skip_button, self.next_button):
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            b.setMinimumWidth(t.px(190))
        self.back_button.clicked.connect(self.wizard_back)
        self.skip_button.clicked.connect(self.wizard_skip)
        self.next_button.clicked.connect(self.wizard_next)
        nav.addWidget(self.back_button)
        nav.addStretch(1)
        nav.addWidget(self.skip_button)
        nav.addWidget(self.next_button)
        root.addLayout(nav)

    # ------------------------------------------------------------------ building

    def add_page(self, page: WizardPage) -> WizardPage:
        self.pages.append(page)
        self.stack.addWidget(page)
        page.changed.connect(self._update_nav)
        return page

    def begin(self) -> None:
        t = self.svc.theme
        smap = self.svc.screens.map
        screen = smap.screen_at(*self.svc.cursor.pos)
        work = smap.rect_to_logical(screen.work_rect, screen)
        w, h = min(t.pxf(1040), work.w * 0.92), min(t.pxf(800), work.h * 0.94)
        self.setGeometry(round(work.x + (work.w - w) / 2), round(work.y + (work.h - h) / 2), round(w), round(h))
        self._go(self._next_index(-1))
        self.show()
        self.raise_()
        self.activateWindow()

    @property
    def page(self) -> WizardPage | None:
        return self.pages[self.index] if 0 <= self.index < len(self.pages) else None

    def note_result(self, step: str, outcome: str) -> None:
        self.results = [(s, o) for s, o in self.results if s != step] + [(step, outcome)]

    def screen_geometry_now(self):  # noqa: ANN201 - ScreenGeometry
        g = self.geometry()
        smap = self.svc.screens.map
        return smap.screen_at_logical(g.center().x(), g.center().y())

    # ------------------------------------------------------------------ navigation (also the Wizard protocol)

    def _next_index(self, start: int) -> int:
        for i in range(start + 1, len(self.pages)):
            if self.pages[i].available():
                return i
        return -1

    def _previous_index(self, start: int) -> int:
        for i in range(start - 1, -1, -1):
            if self.pages[i].available():
                return i
        return -1

    def wizard_next(self) -> None:
        page = self.page
        if page is None or page.busy():
            return
        if not page.ready():
            page.start_action()
            self._update_nav()
            return
        page.accept()
        if self.page is not page or self._closing:
            return  # accept() moved on or closed the wizard itself
        nxt = self._next_index(self.index)
        if nxt < 0:
            self.finish()
            return
        self._go(nxt)

    def wizard_back(self) -> None:
        page = self.page
        if page is not None and page.busy():
            return
        prev = self._previous_index(self.index)
        if prev >= 0:
            self._go(prev)

    def wizard_skip(self) -> None:
        page = self.page
        if page is None or not page.optional or page.busy():
            return
        nxt = self._next_index(self.index)
        if nxt < 0:
            self.finish()
        else:
            self._go(nxt)

    def go_to(self, page: WizardPage) -> None:
        if page in self.pages:
            self._go(self.pages.index(page))

    def _go(self, index: int) -> None:
        if index < 0:
            return
        current = self.page
        if current is not None:
            current.leave()
        self.index = index
        page = self.pages[index]
        self.stack.setCurrentWidget(page)
        page.enter()
        self._update_nav()

    def _update_nav(self) -> None:
        page = self.page
        if page is None:
            return
        shown = [p for p in self.pages if p.available()]
        position = shown.index(page) + 1 if page in shown else self.index + 1
        self.step_label.setText(f"Step {position} of {len(shown)}")
        self.back_button.setVisible(self._previous_index(self.index) >= 0)
        self.skip_button.setVisible(page.optional)
        self.next_button.set_label(page.next_text())
        busy = page.busy()
        self.next_button.setEnabled(not busy)
        self.back_button.setEnabled(not busy)
        self.skip_button.setEnabled(not busy)

    def hand_over(self) -> None:
        """Helper mode ends: eyes and voice from here on."""

    def finish(self) -> None:
        self.close()

    # ------------------------------------------------------------------ closing

    def closeEvent(self, event) -> None:  # noqa: ANN001
        self._closing = True
        for page in self.pages:
            try:
                page.close_page()
            except Exception:
                log.exception("Closing a wizard page failed")
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: ANN001
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.wizard_next()
            return
        super().keyPressEvent(event)


# =========================================================================== set-up pages


class WelcomePage(WizardPage):
    title = f"Welcome to {APP_NAME}"

    def __init__(self, wizard: SetupWizard) -> None:
        super().__init__(wizard)
        t = self.svc.theme
        self.para(f"{APP_NAME} lets you use this computer with your eyes and your voice. Setting up takes about ten minutes.")
        table = RuledTable(t, ("", ""), (0.8, 2.0))
        table.set_rows(HOW_IT_WORKS)
        self.root.addWidget(table)
        self.root.addWidget(text("Who is doing the first steps?", "heading"))
        self.para(
            "A helper can do the first steps with a mouse and keyboard (Windows settings, downloads, camera angle), "
            "then hand over. From then on everything works with eyes and voice.",
            secondary=True,
        )
        self.who = ChoiceRow(t, [(True, "A helper is setting this up"), (False, "I am setting this up myself")], columns=2)
        self.who.chosen.connect(self._chosen)
        self.root.addWidget(self.who)
        self.root.addStretch(1)
        self.who.set_current(wizard.helper_mode)

    def _chosen(self, helper: object) -> None:
        self.wizard.set_helper_mode(bool(helper))  # type: ignore[attr-defined]
        self.who.set_current(bool(helper))


class ChecksPage(WizardPage):
    title = "Windows settings"

    def __init__(self, wizard: WizardWindow) -> None:
        super().__init__(wizard)
        t = self.svc.theme
        self.para("Sightline depends on a few Windows settings. Anything marked below can be fixed by a helper with Open settings.")
        self.status = self.para("Checking", "label", secondary=True)
        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(t.px(10))
        self.root.addLayout(self.rows_box)
        self.again = GazeButton(t, "Check again", None, min_height=64)
        self.again.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.again.clicked.connect(self.run)
        self.root.addWidget(self.again)
        self.root.addStretch(1)
        self.checks: list[SystemCheck] = []
        self.fix_buttons: dict[str, GazeButton] = {}
        self._request = 0

    def available(self) -> bool:
        return self.svc.ctx.desktop is not None

    def enter(self) -> None:
        if not self.checks:
            self.run()

    def run(self) -> None:
        self._request += 1
        request = self._request
        desktop = self.svc.ctx.desktop
        self.status.setText("Checking")

        def work() -> None:
            try:
                checks = list(desktop.system_checks())
            except Exception:
                log.exception("System checks failed")
                checks = []
            self.svc.bridge.post(lambda: self._show(request, checks))

        threading.Thread(target=work, name="sightline-checks", daemon=True).start()

    def _show(self, request: int, checks: list[SystemCheck]) -> None:
        if request != self._request:
            return
        self.checks = checks
        while self.rows_box.count():
            item = self.rows_box.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.fix_buttons = {}
        t = self.svc.theme
        attention = by_hand = 0
        for check in checks:
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(t.px(14))
            # ok None: Windows does not say (camera sharing, for one), so a person looks with the Settings link.
            word = "Fine" if check.ok else ("Check this by hand" if check.ok is None else "Needs attention")
            if check.ok is False:
                attention += 1
            elif check.ok is None:
                by_hand += 1
            status = text(word, "heading", wrap=False)
            status.setMinimumWidth(t.px(170))
            line.addWidget(status)
            words = QVBoxLayout()
            words.setSpacing(t.px(2))
            words.addWidget(text(check.label, "heading"))
            if check.detail and not check.ok:
                detail = text(check.detail, "body")
                detail.setProperty("role", "secondary")
                words.addWidget(detail)
            line.addLayout(words, 1)
            if check.fix_uri and not check.ok:
                fix = GazeButton(t, "Open settings", None, min_height=64)
                fix.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
                fix.clicked.connect(lambda _c=False, uri=check.fix_uri: self.wizard.open_uri(uri))
                line.addWidget(fix)
                self.fix_buttons[check.id] = fix
            self.rows_box.addWidget(row)
            self.rows_box.addWidget(Rule(t))
        if not checks:
            self.status.setText("Could not read the Windows settings. Setting up can carry on.")
        elif attention or by_hand:
            parts = []
            if attention:
                parts.append(f"{attention} setting{'s need' if attention != 1 else ' needs'} attention")
            if by_hand:
                parts.append(f"{by_hand} to check by hand")
            self.status.setText(f"{' and '.join(parts).capitalize()}. Setting up can carry on either way.")
        else:
            self.status.setText("Everything is fine.")
        self.wizard.note_result("Windows settings", self.status.text())
        self.changed.emit()


class ModelsPage(WizardPage):
    title = "Download what Sightline needs"

    def __init__(self, wizard: SetupWizard) -> None:
        super().__init__(wizard)
        self.view = ModelDownloadView(self.svc)
        self.view.done.connect(self._done)
        self.root.addWidget(self.view)
        self.root.addStretch(1)

    def ready(self) -> bool:
        return self.view.complete

    def enter(self) -> None:
        self.view.start()

    def _done(self) -> None:
        self.wizard.models_ready()  # type: ignore[attr-defined]
        self.changed.emit()
        if not self.wizard.helper_mode and self.isVisible():  # type: ignore[attr-defined]
            # On their own the user may not manage a mouse: carry on to the camera by itself.
            QTimer.singleShot(_ms(1.2), self, self._auto_next)

    def _auto_next(self) -> None:
        if self.wizard.page is self:
            self.wizard.wizard_next()

    def close_page(self) -> None:
        self.view.close_view()


class CameraView(QWidget):
    """The camera preview with the face landmarks, or a plain message while there is none."""

    def __init__(self, page: WizardPage) -> None:
        super().__init__()
        self._theme = page.svc.theme
        self.image: QImage | None = None
        self.landmarks = None
        self.message = "Starting the camera"
        self.setMinimumSize(self._theme.px(360), self._theme.px(240))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_frame(self, e: CameraFrame) -> None:
        img = e.image
        try:
            h, w = int(img.shape[0]), int(img.shape[1])
            self.image = QImage(img.tobytes(), w, h, 3 * w, QImage.Format.Format_BGR888).copy()
        except Exception:
            log.debug("Could not show a camera frame", exc_info=True)
            return
        self.landmarks = e.landmarks
        self.message = ""
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        t = self._theme
        pal = t.palette
        p = QPainter(self)
        rect = QRectF(0, 0, self.width(), self.height())
        p.fillRect(rect, pal.ground)
        frame_rect(p, rect, pal.rule_strong)
        if self.image is not None and not self.image.isNull():
            iw, ih = self.image.width(), self.image.height()
            s = min((rect.width() - 2) / iw, (rect.height() - 2) / ih)
            target = QRectF((rect.width() - iw * s) / 2, (rect.height() - ih * s) / 2, iw * s, ih * s)
            p.drawImage(target, self.image)
            if self.landmarks is not None:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(pal.accent)
                try:
                    for x, y in self.landmarks[::4]:
                        p.drawEllipse(target.x() + float(x) * s - 1.5, target.y() + float(y) * s - 1.5, 3.0, 3.0)
                except Exception:
                    log.debug("Bad landmarks in a preview frame", exc_info=True)
        else:
            p.setFont(t.font("body"))
            p.setPen(pal.ink_2)
            p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), self.message)
        p.end()


class CameraPage(WizardPage):
    title = "Camera"

    def __init__(self, wizard: WizardWindow) -> None:
        super().__init__(wizard)
        t = self.svc.theme
        body = QHBoxLayout()
        body.setSpacing(t.px(20))
        self.view = CameraView(self)
        body.addWidget(self.view, 3)
        side = QVBoxLayout()
        side.setSpacing(t.px(10))
        side.addWidget(text("Which camera", "heading"))
        self.choice_box = QVBoxLayout()
        side.addLayout(self.choice_box)
        self.face = text("", "label")
        side.addWidget(self.face)
        side.addStretch(1)
        body.addLayout(side, 2)
        self.root.addLayout(body, 1)
        advice = RuledTable(t, ("", ""), (0.5, 3.0))
        advice.set_rows(CAMERA_ADVICE)
        self.root.addWidget(advice)
        self.choices: ChoiceRow | None = None
        self.cameras: list = []
        self._last_frame = 0.0
        self._remove = self.svc.bridge.on(CameraFrame, self._on_frame)
        self._request = 0

    def available(self) -> bool:
        return self.svc.ctx.vision is not None

    def enter(self) -> None:
        vision = self.svc.ctx.vision
        vision.set_preview(True)
        self._request += 1
        request = self._request

        def work() -> None:
            try:
                cams = list(vision.cameras())
            except Exception:
                log.exception("Could not list cameras")
                cams = []
            self.svc.bridge.post(lambda: self._show_cameras(request, cams))

        threading.Thread(target=work, name="sightline-cameras", daemon=True).start()

    def leave(self) -> None:
        vision = self.svc.ctx.vision
        if vision is not None:
            try:
                vision.set_preview(False)
            except Exception:
                log.debug("Could not stop the preview", exc_info=True)

    def close_page(self) -> None:
        self.leave()
        self._remove()

    def _show_cameras(self, request: int, cams: list) -> None:
        if request != self._request:
            return
        self.cameras = cams
        if self.choices is not None:
            self.choices.deleteLater()
        if not cams:
            self.face.setText("No camera found. Plug one in, then go back and forward again.")
            self.choices = None
            return
        self.choices = ChoiceRow(self.svc.theme, [(c.index, c.name) for c in cams], columns=1)
        self.choices.set_current(self.svc.ctx.settings.vision.camera_index)
        self.choices.chosen.connect(self._choose)
        self.choice_box.addWidget(self.choices)

    def _choose(self, index: object) -> None:
        try:
            self.svc.ctx.vision.set_camera(int(index))  # vision saves the choice itself
        except Exception as e:
            log.warning("Could not switch camera: %s", e)
            self.svc.notify("That camera could not be opened.", "warn", key="camera")
            return
        if self.choices is not None:
            self.choices.set_current(index)
        self.view.image = None
        self.view.message = "Switching camera"
        self.view.update()

    def _on_frame(self, e: CameraFrame) -> None:
        if not self.isVisible():
            return
        self.view.set_frame(e)
        found = e.landmarks is not None
        self.face.setText("Face found. Look at the screen as you normally would." if found else "No face yet. Move so the camera sees your whole face.")


class MicrophonePage(WizardPage):
    title = "Microphone"

    def __init__(self, wizard: WizardWindow) -> None:
        super().__init__(wizard)
        t = self.svc.theme
        self.root.addWidget(text("Which microphone", "heading"))
        self.choice_box = QVBoxLayout()
        self.root.addLayout(self.choice_box)
        self.root.addWidget(text("Say \"hello Sightline\"", "heading"))
        self.meter = LevelMeter(t)
        self.root.addWidget(self.meter)
        self.result = self.para("Nothing heard yet.", "label")
        self.para("A headset microphone hears you best. A laptop microphone works in a quiet room.", secondary=True)
        self.root.addStretch(1)
        self.choices: ChoiceRow | None = None
        self.heard_s = 0.0
        self.passed = False
        self._last = 0.0
        self._remove = [self.svc.bridge.on(MicLevel, self._on_level), self.svc.bridge.on(TranscriptFinal, self._on_text)]
        self._request = 0

    def available(self) -> bool:
        return self.svc.ctx.speech is not None

    def enter(self) -> None:
        speech = self.svc.ctx.speech
        self._request += 1
        request = self._request

        def work() -> None:
            try:
                mics = list(speech.microphones())
            except Exception:
                log.exception("Could not list microphones")
                mics = []
            self.svc.bridge.post(lambda: self._show_mics(request, mics))

        threading.Thread(target=work, name="sightline-mics", daemon=True).start()

    def close_page(self) -> None:
        for remove in self._remove:
            remove()

    def _show_mics(self, request: int, mics: list) -> None:
        if request != self._request:
            return
        if self.choices is not None:
            self.choices.deleteLater()
            self.choices = None
        if not mics:
            self.result.setText("No microphone found. Plug one in, then go back and forward again.")
            return
        current = self.svc.ctx.settings.speech.microphone or next((m.id for m in mics if m.default), mics[0].id)
        self.choices = ChoiceRow(self.svc.theme, [(m.id, m.name) for m in mics], columns=1)
        self.choices.set_current(current)
        self.choices.chosen.connect(self._choose)
        self.choice_box.addWidget(self.choices)

    def _choose(self, mic_id: object) -> None:
        try:
            self.svc.ctx.speech.set_microphone(str(mic_id))
        except Exception as e:
            log.warning("Could not switch microphone: %s", e)
            self.svc.notify("That microphone could not be opened.", "warn", key="mic")
            return
        if self.choices is not None:
            self.choices.set_current(mic_id)
        self.heard_s, self.passed = 0.0, False
        self.result.setText("Nothing heard yet.")

    def _on_level(self, e: MicLevel) -> None:
        if not self.isVisible():
            return
        now = time.monotonic()
        self.meter.set_level(e.rms, e.speaking)
        if e.speaking and self._last:
            self.heard_s += min(0.2, now - self._last)
        self._last = now
        if not self.passed and self.heard_s >= 0.5:
            self._pass("Sightline hears you.")

    def _on_text(self, e: TranscriptFinal) -> None:
        if self.isVisible() and e.text.strip():
            self._pass(f"Heard: \"{e.text.strip()}\"")

    def _pass(self, message: str) -> None:
        self.passed = True
        self.result.setText(message)
        self.wizard.note_result("Microphone", "Working")


class HandOverPage(WizardPage):
    title = "Hand over"

    def __init__(self, wizard: SetupWizard) -> None:
        super().__init__(wizard)
        self.para("The next steps are for the person who will use Sightline: teaching it their eyes, then a short practice.")
        table = RuledTable(self.svc.theme, ("", ""), (0.9, 2.0))
        table.set_rows(
            [
                TableRow(("To press a button", "Look at it and close both eyes until the tick, then open them.")),
                TableRow(("Or say", "\"next\", \"back\" or \"skip\".")),
                TableRow(("The helper", "Can stay nearby. Moving the mouse still works; eye control waits until it stops.")),
            ]
        )
        self.root.addWidget(table)
        self.root.addStretch(1)

    def available(self) -> bool:
        return bool(getattr(self.wizard, "helper_mode", False))

    def next_text(self) -> str:
        return "Hand over"

    def accept(self) -> None:
        self.wizard.hand_over()


class _RunPage(WizardPage):
    """A page whose Next starts a calibration run, shows its progress and keeps the result."""

    optional = True

    def __init__(self, wizard: WizardWindow) -> None:
        super().__init__(wizard)
        self.run: targets._Run | None = None
        self.report: CalibrationReport | None = None
        self.message = text("", "label")

    def available(self) -> bool:
        return self.svc.ctx.vision is not None

    def ready(self) -> bool:
        return self.report is not None and self.report.ok

    def busy(self) -> bool:
        return self.run is not None and self.run.running

    def next_text(self) -> str:
        if self.busy():
            return "Working"
        if self.ready():
            return "Next"
        return "Try again" if self.report is not None or (self.run is not None and self.run.stopped_reason) else "Start"

    def close_page(self) -> None:
        if self.run is not None:
            self.run.close()


class EyesPage(_RunPage):
    title = "Eye clicks"

    def __init__(self, wizard: WizardWindow) -> None:
        super().__init__(wizard)
        t = self.svc.theme
        self.para("Sightline learns what your eyes look like open, closed and winking. A tick says start; a second tick says stop.")
        self.winks = ChoiceRow(t, [(True, "I can wink"), (False, "I can't wink")], columns=2)
        self.winks.set_current(True)
        self.winks.chosen.connect(lambda v: self.winks.set_current(v))
        self.root.addWidget(self.winks)
        self.instruction = text("Choose Start when ready.", "title")
        self.root.addWidget(self.instruction)
        self.bar = ProgressLine(t)
        self.root.addWidget(self.bar)
        self.root.addWidget(self.message)
        self.root.addStretch(1)

    def _can_wink(self) -> bool:
        current = [v for v, b in self.winks.buttons if b.is_on]
        return bool(current[0]) if current else True

    def start_action(self) -> bool:
        if self.busy():
            return False
        steps = [s for s in targets.EYE_STEPS if self._can_wink() or not s.label.startswith("wink")]
        self.report = None
        self.run = targets.LabelRun(self.svc, "eyes", steps)
        self.run.changed.connect(self._on_step)
        self.run.finished.connect(self._done)
        self.message.setText("")
        self.run.start()
        self.changed.emit()
        return True

    def _on_step(self, step: object, progress: float) -> None:
        if step is None:
            self.instruction.setText("Working it out")
            self.bar.set_value(1.0)
            return
        self.instruction.setText(step.instruction)  # type: ignore[attr-defined]
        self.bar.set_value(progress)

    def _done(self, report: CalibrationReport | None) -> None:
        self.report = report
        if report is None:
            self.instruction.setText(self.run.stopped_reason if self.run else "Stopped.")
        else:
            self.instruction.setText("Done" if report.ok else "That did not work")
            self.message.setText(report.message)
            self.wizard.note_result("Eye clicks", report.message)
            if report.ok and not self._can_wink() and self.svc.ctx.settings.vision.winks:
                self.svc.ctx.settings.vision.winks = False  # winks would only misfire
                self.svc.ctx.settings_changed("vision")
                self.message.setText(report.message + " Wink clicks are now off.")
            if report.ok:
                self.svc.sounds.play("success")
        self.changed.emit()


class _DotsPage(_RunPage):
    kind = "gaze"

    def __init__(self, wizard: WizardWindow) -> None:
        super().__init__(wizard)
        self.root.addWidget(self.message)

    def _screen(self):  # noqa: ANN202
        return self.wizard.screen_geometry_now()

    def start_action(self) -> bool:
        if self.busy():
            return False
        self.report = None
        self._start_run(self.kind)
        return True

    def _start_run(self, kind: str) -> None:
        self.run = targets.DotRun(self.svc, kind, self._screen())
        self.run.finished.connect(lambda report, k=kind: self._run_done(k, report))
        self.message.setText("")
        self.run.start()
        self.changed.emit()

    def _run_done(self, kind: str, report: CalibrationReport | None) -> None:
        self.report = report
        if report is None:
            self.message.setText(self.run.stopped_reason if self.run else "Stopped.")
        else:
            self.message.setText(report.message)
        self.changed.emit()


class GazePage(_DotsPage):
    title = "Where you look"
    kind = "gaze"

    def __init__(self, wizard: WizardWindow) -> None:
        super().__init__(wizard)
        self.root.insertWidget(1, text(
            "Dots appear all over the screen, corners too, because the Start button, close buttons and the dock live there. "
            "Look at each one until it shrinks. Keep your head still. Then a few more dots check the result.",
            "body",
        ))
        self.comfort = text("", "body")
        self.root.addWidget(self.comfort)
        self.root.addStretch(1)
        self.validation: CalibrationReport | None = None
        self._gaze_report: CalibrationReport | None = None
        self.accuracy_px: float | None = None
        self._remove = self.svc.bridge.on(GazeModelChanged, self._on_model)

    def _on_model(self, e: GazeModelChanged) -> None:
        if e.source == "calibration" and e.accuracy_px is not None:
            self.accuracy_px = float(e.accuracy_px)

    def start_action(self) -> bool:
        self.validation = None
        self.comfort.setText("")
        return super().start_action()

    def _run_done(self, kind: str, report: CalibrationReport | None) -> None:
        if kind == "gaze":
            if report is not None and report.ok:
                self._gaze_report = report
                self.message.setText(report.message)
                self._start_run("validate")  # check it on fresh dots before calling it done
                return
            super()._run_done(kind, report)
            return
        # Validation only measures; the calibration is already active, so a stopped check keeps it.
        final = self._gaze_report
        if report is not None:
            self.validation = report
            final = CalibrationReport(
                True, "gaze", report.message, report.mean_error_px, report.mean_error_deg, report.per_point_error_px, report.samples,
                report.details,  # the check's comfort sentence, measured on fresh dots
            )
        super()._run_done(kind, final)
        if final is not None and final.ok:
            self._show_comfort(final)
            self.wizard.note_result("Where you look", final.message)
            self.svc.sounds.play("success")

    def _show_comfort(self, report: CalibrationReport) -> None:
        screen = self._screen()
        error_px = report.mean_error_px if report.mean_error_px is not None else self.accuracy_px
        mm_per_px = targets.monitor_mm_per_px(self.svc, screen)
        line = comfort_line(error_px, report.mean_error_deg, mm_per_px, screen.dpr, report.details)
        self.comfort.setText(line or "")
        if line:
            self.wizard.note_result("Comfortable size", line)

    def close_page(self) -> None:
        super().close_page()
        self._remove()


class HeadPage(_DotsPage):
    title = "Head pointer"
    kind = "head"

    def __init__(self, wizard: WizardWindow) -> None:
        super().__init__(wizard)
        self.root.insertWidget(1, text("Turn your head to point your nose at each dot. This tunes how far small head movements move the pointer.", "body"))
        self.root.addStretch(1)

    def _run_done(self, kind: str, report: CalibrationReport | None) -> None:
        super()._run_done(kind, report)
        if report is not None:
            self.wizard.note_result("Head pointer", report.message)


class GesturesPage(_RunPage):
    title = "Face gestures"

    def __init__(self, wizard: WizardWindow) -> None:
        super().__init__(wizard)
        t = self.svc.theme
        self.para("Face gestures can click too, for when blinking is tiring. Hold each one when you hear the tick. "
                  "Then talk and read for half a minute each, so Sightline learns what not to count.")
        self.instruction = text("Choose Start when ready.", "title")
        self.root.addWidget(self.instruction)
        self.note = text("", "body")
        self.root.addWidget(self.note)
        self.bar = ProgressLine(t)
        self.root.addWidget(self.bar)
        self.root.addWidget(self.message)
        self.offer_box = QVBoxLayout()
        self.root.addLayout(self.offer_box)
        self.root.addStretch(1)
        self.offers: dict[str, ChoiceRow] = {}

    def start_action(self) -> bool:
        if self.busy():
            return False
        self.report = None
        self.run = targets.LabelRun(self.svc, "gestures", targets.GESTURE_STEPS)
        self.run.changed.connect(self._on_step)
        self.run.finished.connect(self._done)
        self.run.start()
        self.changed.emit()
        return True

    def _on_step(self, step: object, progress: float) -> None:
        if step is None:
            self.instruction.setText("Working it out")
            self.note.setText("")
            return
        self.instruction.setText(step.instruction)  # type: ignore[attr-defined]
        self.note.setText(step.note)  # type: ignore[attr-defined]
        self.bar.set_value(progress)

    def _done(self, report: CalibrationReport | None) -> None:
        self.report = report
        if report is None:
            self.instruction.setText(self.run.stopped_reason if self.run else "Stopped.")
            self.changed.emit()
            return
        self.instruction.setText("Done" if report.ok else "That did not work")
        self.message.setText(report.message)
        self.wizard.note_result("Face gestures", report.message)
        scores = report.details.get("scores") or report.details.get("gesture_scores") or {}
        if report.ok and isinstance(scores, dict):
            best = [g for g, _s in sorted(scores.items(), key=lambda kv: -float(kv[1])) if g != "neutral"][:3]
            self._offer(best)
        self.changed.emit()

    def _offer(self, gestures: list[str]) -> None:
        from ..settings_model import GESTURE_CHOICES

        t = self.svc.theme
        current = self.svc.ctx.settings.vision.gesture_actions
        names = {"brow_raise": "Raise eyebrows", "smile": "Smile", "mouth_open": "Open mouth", "cheek_puff": "Puff cheeks"}
        if gestures:
            self.offer_box.addWidget(text("These worked best for you. Choose what each one does:", "heading"))
        for g in gestures:
            self.offer_box.addWidget(text(names.get(g, g.replace("_", " ").capitalize()), "label"))
            row = ChoiceRow(t, [(k, GESTURE_CHOICES[k]) for k in ("click", "right_click", "double_click", "none")], columns=4)
            row.set_current(current.get(g, "none"))
            row.chosen.connect(lambda v, g=g, row=row: self._map(g, str(v), row))
            self.offer_box.addWidget(row)
            self.offers[g] = row

    def _map(self, gesture: str, action: str, row: ChoiceRow) -> None:
        actions = dict(self.svc.ctx.settings.vision.gesture_actions)
        actions[gesture] = action
        self.svc.ctx.settings.vision.gesture_actions = actions
        self.svc.ctx.settings_changed("vision")
        row.set_current(action)


class MouthPage(WizardPage):
    title = "Mouth check"
    optional = True

    def __init__(self, wizard: WizardWindow) -> None:
        super().__init__(wizard)
        t = self.svc.theme
        self.para("Optional. If Sightline can see your mouth move when you talk, it can ignore a television or another person in the room.")
        self.instruction = text("Choose Start, then read the sentence aloud.", "title")
        self.root.addWidget(self.instruction)
        self.bar = ProgressLine(t)
        self.root.addWidget(self.bar)
        self.message = text("", "body")
        self.root.addWidget(self.message)
        self.gate = ChoiceRow(t, [(True, "Turn it on"), (False, "Leave it off")], columns=2)
        self.gate.chosen.connect(self._set_gate)
        self.gate.hide()
        self.root.addWidget(self.gate)
        self.root.addStretch(1)
        self.phase = ""
        self.result: tuple[float, float] | None = None
        self.works: bool | None = None
        self._t: dict[str, float] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)

    def available(self) -> bool:
        vision = self.svc.ctx.vision
        if vision is None or not callable(getattr(vision, "attention", None)):
            return False
        now = time.perf_counter()
        try:
            probe = vision.attention(now - 1.0, now)
        except Exception:
            log.debug("attention() failed", exc_info=True)
            return False
        return probe is not None and probe.mouth is not None

    def busy(self) -> bool:
        return self.phase in ("read", "quiet")

    def ready(self) -> bool:
        return self.works is not None

    def next_text(self) -> str:
        if self.busy():
            return "Working"
        return "Next" if self.ready() else "Start"

    def start_action(self) -> bool:
        if self.busy():
            return False
        self.works = None
        self.gate.hide()
        self.message.setText("")
        self.phase = "read"
        self._t = {"read": time.perf_counter()}
        self.instruction.setText(f"Read aloud: \"{MOUTH_SENTENCE}\"")
        self.svc.sounds.play("hold_1")
        self._timer.start()
        QTimer.singleShot(_ms(READ_S), self, self._quiet)
        self.changed.emit()
        return True

    def _quiet(self) -> None:
        if self.phase != "read":
            return
        self._t["quiet"] = time.perf_counter()
        self.phase = "quiet"
        self.instruction.setText("Now stay quiet and keep looking at the screen.")
        self.svc.sounds.play("calibration_point")
        QTimer.singleShot(_ms(QUIET_S), self, self._judge)

    def _tick(self) -> None:
        if self.phase == "read":
            self.bar.set_value(0.5 * min(1.0, (time.perf_counter() - self._t["read"]) / (_ms(READ_S) / 1000)))
        elif self.phase == "quiet":
            self.bar.set_value(0.5 + 0.5 * min(1.0, (time.perf_counter() - self._t["quiet"]) / (_ms(QUIET_S) / 1000)))

    def _judge(self) -> None:
        if self.phase != "quiet":
            return
        self._timer.stop()
        end = time.perf_counter()
        self.phase = "done"
        self.bar.set_value(1.0)
        vision = self.svc.ctx.vision
        # Skip the first tenth of each part: people start reading, and stop, a moment late.
        settle_read = 0.1 * (self._t["quiet"] - self._t["read"])
        settle_quiet = 0.1 * (end - self._t["quiet"])
        try:
            speaking = vision.attention(self._t["read"] + settle_read, self._t["quiet"])
            quiet = vision.attention(self._t["quiet"] + settle_quiet, end)
        except Exception:
            log.debug("attention() failed", exc_info=True)
            speaking = quiet = None
        if speaking is None or quiet is None or speaking.mouth is None or quiet.mouth is None:
            self.works = False
            self.instruction.setText("Sightline could not see your mouth clearly")
            self.message.setText("It stays off. That is fine: facing the screen is already checked.")
        else:
            self.result = (float(speaking.mouth), float(quiet.mouth))
            self.works = speaking.mouth >= 0.5 and quiet.mouth <= 0.2 and speaking.mouth - quiet.mouth >= 0.35
            if self.works:
                self.instruction.setText("It works for you")
                self.message.setText("Turn on \"Also require your mouth to move\"? Speech then only acts while your mouth moves.")
                self.gate.show()
                self.gate.set_current(self.svc.ctx.settings.speech.mouth_gate)
            else:
                self.instruction.setText("Not reliable enough for you")
                self.message.setText("It stays off. That is fine: facing the screen is already checked.")
        self.wizard.note_result("Mouth check", self.instruction.text())
        self.changed.emit()

    def _set_gate(self, on: object) -> None:
        self.svc.ctx.settings.speech.mouth_gate = bool(on)
        self.svc.ctx.settings_changed("speech")
        self.gate.set_current(bool(on))

    def close_page(self) -> None:
        self._timer.stop()
        self.phase = ""


class FinishPage(WizardPage):
    title = "You are ready"

    def __init__(self, wizard: SetupWizard) -> None:
        super().__init__(wizard)
        t = self.svc.theme
        speech = self.svc.ctx.settings.speech
        self.para("Next comes a short practice with big targets, so clicking with your eyes feels natural before you need it.")
        phrases = RuledTable(t, ("Say", "To"), (0.9, 2.0))
        phrases.set_rows(
            [
                TableRow(("what can I say", "See every command.")),
                TableRow(("show numbers", "Number everything you can click, then say a number.")),
                TableRow((f"{speech.sleep_phrase}, {speech.wake_phrase}", "Stop listening, and start again.")),
                TableRow(("calibrate", "Teach Sightline your eyes again, any time.")),
            ]
        )
        self.root.addWidget(phrases)
        self.skip_practice = GazeButton(t, "Finish without practice", None, min_height=64)
        self.skip_practice.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.skip_practice.clicked.connect(lambda: wizard.finish(practice=False))
        self.root.addWidget(self.skip_practice)
        self.root.addStretch(1)

    def next_text(self) -> str:
        return "Start practice"


class DonePage(WizardPage):
    title = "Done"

    def __init__(self, wizard: WizardWindow) -> None:
        super().__init__(wizard)
        self.table = RuledTable(self.svc.theme, ("Step", "Result"), (0.9, 2.2))
        self.root.addWidget(self.table)
        self.root.addStretch(1)

    def enter(self) -> None:
        rows = [TableRow((step, outcome)) for step, outcome in self.wizard.results]
        self.table.set_rows(rows or [TableRow(("Nothing changed", "Every step was skipped."))])

    def next_text(self) -> str:
        return "Finish"


# =========================================================================== the two wizards


class SetupWizard(WizardWindow):
    overlay_name = "setup"

    def __init__(self, services: UiServices, on_complete: Callable[[], None], ensure_models: Callable[[Callable[[], None]], None] | None = None) -> None:
        super().__init__(services, f"Set up {APP_NAME}")
        self.helper_mode = True
        self._on_complete = on_complete
        self._ensure_models = ensure_models
        self.engines_started = False
        self._finished = False
        self.practice_requested = False
        self.welcome = self.add_page(WelcomePage(self))
        self.checks = self.add_page(ChecksPage(self))
        self.models = self.add_page(ModelsPage(self))
        self.camera = self.add_page(CameraPage(self))
        self.microphone = self.add_page(MicrophonePage(self))
        self.handover = self.add_page(HandOverPage(self))
        self.eyes = self.add_page(EyesPage(self))
        self.gaze = self.add_page(GazePage(self))
        self.head = self.add_page(HeadPage(self))
        self.mouth = self.add_page(MouthPage(self))
        self.finish_page = self.add_page(FinishPage(self))
        self.set_helper_mode(True)

    def set_helper_mode(self, on: bool) -> None:
        self.helper_mode = on
        self.helper_tag.setVisible(on)
        self.hand_over_button.setVisible(on and self.engines_started)
        self._update_nav()

    def models_ready(self) -> None:
        if self.engines_started:
            return
        self.engines_started = True
        self._on_complete()  # start the engines now: the camera page needs them
        if self.helper_mode:
            self._dispatch("pointer.pause")  # the helper's mouse should not fight the eye pointer
            self.hand_over_button.show()

    def hand_over(self) -> None:
        if not self.helper_mode:
            return
        self.set_helper_mode(False)
        self._dispatch("pointer.resume")
        self.svc.notify("Handed over. Look at a button and close both eyes to press it, or say next.", "info", key="handover")
        if self.page is self.handover:
            nxt = self._next_index(self.index)
            if nxt >= 0:
                self._go(nxt)

    def _dispatch(self, name: str) -> None:
        if self.svc.ctx.actions.has(name):
            self.svc.ctx.actions.dispatch(Action(name, source="ui"))

    def finish(self, practice: bool = True) -> None:  # type: ignore[override]
        if self._finished:
            return
        self._finished = True
        settings = self.svc.ctx.settings
        settings.general.first_run_complete = True
        try:
            self.svc.ctx.settings_changed("general")
        except OSError:
            log.exception("Could not save settings")
        if self.helper_mode:
            self._dispatch("pointer.resume")
        self.svc.sounds.play("success")
        self.practice_requested = practice
        self.close()
        if not self.engines_started:
            self.engines_started = True
            self._on_complete()
        if practice:
            self.svc.ctx.actions.dispatch(Action("ui.practice.show", source="ui"))

    def closeEvent(self, event) -> None:  # noqa: ANN001
        super().closeEvent(event)
        if not self._finished:
            # Closed early: carry on without marking setup done, so it comes back next time.
            self._finished = True
            if self.helper_mode and self.engines_started:
                self._dispatch("pointer.resume")
            if not self.engines_started:
                self.engines_started = True
                if self.models.view.complete or self._ensure_models is None:
                    self._on_complete()
                else:
                    self._ensure_models(self._on_complete)


CALIBRATION_TITLES = {
    "eyes": "Calibrate eye clicks",
    "gaze": "Calibrate where you look",
    "head": "Calibrate the head pointer",
    "gestures": "Face gestures",
    "full": "Calibrate everything",
}


class CalibrationWizard(WizardWindow):
    overlay_name = "calibration"

    def __init__(self, services: UiServices, kind: str) -> None:
        kind = kind if kind in CALIBRATION_TITLES else "full"
        super().__init__(services, CALIBRATION_TITLES[kind])
        self.kind = kind
        build: dict[str, list[type[WizardPage]]] = {
            "eyes": [EyesPage],
            "gaze": [GazePage],
            "head": [HeadPage],
            "gestures": [GesturesPage],
            "full": [CameraPage, EyesPage, GazePage, HeadPage, GesturesPage, MouthPage],
        }
        for cls in build[kind]:
            self.add_page(cls(self))
        self.add_page(DonePage(self))

    def finish(self) -> None:
        self.close()


def run_drift_fix(services: UiServices) -> targets.DotRun | None:
    """"Fix pointer": one dot in the middle of the current monitor for 1.5 s, then an offset.  No wizard."""
    if services.ctx.vision is None:
        services.notify("Eye tracking is not running.", "warn", key="drift")
        return None
    screen = services.screens.map.screen_at(*services.cursor.pos)
    run = targets.DotRun(services, "drift", screen)

    def done(report: CalibrationReport | None) -> None:
        if report is None:
            services.notify(run.stopped_reason or "Stopped.", "info", key="drift")
        else:
            services.notify(report.message or ("Pointer corrected." if report.ok else "Could not correct the pointer."), "info" if report.ok else "warn", key="drift")
        run.close()

    run.finished.connect(done)
    run.start()
    return run
