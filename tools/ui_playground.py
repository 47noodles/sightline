"""Exercise every Sightline visual without a camera, microphone or the real desktop.

Two modes:

``--shots`` (the default) renders offscreen and writes PNGs to ``out/ui/``.
Qt runs on its ``offscreen`` platform with a virtual 1920x1080 screen at
125%, a synthetic desktop (a browser page, a taskbar) is drawn as the
background, and each scene composites every visible Sightline window on top
of it.  Nothing appears on the real screen and the real cursor never moves::

    .venv\\Scripts\\python.exe tools\\ui_playground.py
    .venv\\Scripts\\python.exe tools\\ui_playground.py --only ring labels

``--live`` runs the real shell on the real desktop with a fake event source:
pointer samples follow your mouse, eye holds, dwell, transcripts, intents,
hover targets and notices cycle on a timer, and the overlays open in turn.
It shows windows on screen, so it takes the shared desktop lock::

    .venv\\Scripts\\python.exe tools\\ui_playground.py --live
"""

from __future__ import annotations

import argparse
import itertools
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "ui"
SCREEN_W, SCREEN_H, DPR = 1920, 1080, 1.25
TASKBAR_LOGICAL = 48


def _offscreen_config() -> str:
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = OUT / "offscreen_screen.json"
    cfg.write_text(
        '{"synchronousWindowSystemEvents": true, "windowFrameMargins": false, "screens": ['
        f'{{"name": "playground", "x": 0, "y": 0, "width": {SCREEN_W}, "height": {SCREEN_H}, '
        f'"logicalDpi": {round(96 * DPR)}, "logicalBaseDpi": 96, "dpr": 1}}]}}',
        "utf-8",
    )
    rel = os.path.relpath(cfg, os.getcwd()).replace("\\", "/")
    return f"offscreen:configfile={rel}"


# =========================================================================== the fake world


def build_context(theme: str):  # noqa: ANN201 - AppContext
    """A test context whose fake services behave enough like the real ones to drive the UI."""
    from sightline.core.context import AppContext
    from sightline.core.types import Action, ClickType, ElementInfo, SpeechMode

    ctx = AppContext.for_tests()
    ctx.settings.ui.theme = theme
    ctx.settings.ui.sounds = False
    ctx.settings.ui.dock_reserve_space = False
    ctx.desktop.display = _Display()
    ctx.desktop.uia.elements = [ElementInfo(r, name, "Button", clickable=True) for name, r in fake_elements()]
    ctx.speech.commands_help = lambda: COMMANDS  # type: ignore[method-assign]
    state = ctx.state

    def stateful(name: str, fn) -> None:  # noqa: ANN001
        ctx.actions.register(name, lambda a: fn(a))

    stateful("pointer.pause", lambda a: state.update(pointer_paused=True))
    stateful("pointer.resume", lambda a: state.update(pointer_paused=False))
    stateful("pointer.toggle", lambda a: state.update(pointer_paused=not state.get("pointer_paused")))
    stateful("dwell.toggle", lambda a: state.update(dwell_enabled=not state.get("dwell_enabled")))
    stateful("dwell.click_type", lambda a: state.update(dwell_click=ClickType(a.arg("type", "left"))))
    stateful("speech.mode", lambda a: state.update(speech_mode=SpeechMode(a.arg("mode", "auto"))))
    for name in ("mouse.scroll", "keyboard.toggle", "key.press"):
        stateful(name, lambda a: None)

    def click(a: Action) -> None:
        from sightline.core.events import ClickPerformed

        x = a.arg("x", ctx.desktop.input.pos[0])
        y = a.arg("y", ctx.desktop.input.pos[1])
        ctx.desktop.input.click(a.arg("button", "left"), a.arg("count", 1), x, y)
        ctx.bus.publish(ClickPerformed(x, y, a.arg("button", "left"), a.arg("count", 1), a.source))

    stateful("mouse.click", click)
    stateful("mouse.move", lambda a: ctx.desktop.input.move_cursor(a.arg("x"), a.arg("y")))
    return ctx


class _Display:
    """One 1920x1080 monitor at 125%, matching the offscreen screen."""

    def __init__(self) -> None:
        from sightline.core.types import Monitor, Rect

        work_h = SCREEN_H - round(TASKBAR_LOGICAL * DPR)
        self._m = Monitor("playground", "Playground", Rect(0, 0, SCREEN_W, SCREEN_H), Rect(0, 0, SCREEN_W, work_h), DPR, True)

    def monitors(self):  # noqa: ANN201
        return [self._m]

    def primary(self):  # noqa: ANN201
        return self._m

    def virtual_rect(self):  # noqa: ANN201
        return self._m.rect

    def monitor_at(self, x: float, y: float):  # noqa: ANN201
        return self._m


def _commands():  # noqa: ANN202
    from sightline.core.types import CommandHelp as C

    rows = [
        ("Mouse", "click", "Left click where the pointer is", ""),
        ("Mouse", "double click", "Double click", ""),
        ("Mouse", "right click", "Right click", ""),
        ("Mouse", "scroll down <number>", "Scroll down, three notches unless you say how many", "scroll down five"),
        ("Mouse", "scroll up <number>", "Scroll up", ""),
        ("Mouse", "start scrolling down", "Keep scrolling until you say stop", ""),
        ("Mouse", "drag", "Press the button; say drop to let go", ""),
        ("Precision", "show numbers", "Number everything you can click", ""),
        ("Precision", "click <number>", "Click a numbered item", "click twelve"),
        ("Precision", "grid", "Show a 3 by 3 grid; say a number to narrow it", ""),
        ("Precision", "zoom", "Magnify around the pointer", ""),
        ("Keys", "press <key>", "Press a key or a combination", "press control s"),
        ("Keys", "enter, tab, escape, backspace", "Press that key", ""),
        ("Text", "type <words>", "Type exactly what follows", "type hello world"),
        ("Text", "scratch that", "Remove the last thing you dictated", ""),
        ("Text", "new line, comma, full stop", "Punctuation while dictating", ""),
        ("Windows and apps", "open <app>", "Start an app", "open chrome"),
        ("Windows and apps", "switch to <window>", "Bring a window to the front", "switch to mail"),
        ("Windows and apps", "close window", "Close the window in front", ""),
        ("Windows and apps", "snap left, snap right", "Put the window on half the screen", ""),
        ("Sightline", "go to sleep", "Stop listening until you say wake up", ""),
        ("Sightline", "pause eyes", "Stop the pointer following your eyes", ""),
        ("Sightline", "what can I say", "Show this sheet", ""),
    ]
    return [C(cat, phrase, desc, ex) for cat, phrase, desc, ex in rows]


COMMANDS = []  # filled in main() once sightline is importable


def fake_elements() -> list[tuple[str, object]]:
    """Clickable rectangles (physical pixels) matching what draw_desktop paints."""
    from sightline.core.types import Rect

    s = DPR
    out = []

    def add(name: str, x: float, y: float, w: float, h: float) -> None:
        out.append((name, Rect(round(x * s), round(y * s), round(w * s), round(h * s))))

    for i, tab in enumerate(("Newsletter", "Mail", "Calendar")):
        add(tab, 8 + i * 186, 6, 180, 30)
    for i, name in enumerate(("Back", "Forward", "Reload")):
        add(name, 10 + i * 36, 44, 30, 30)
    add("Address", 130, 44, 900, 30)
    for i, name in enumerate(("Home", "News", "Events", "Volunteer", "Contact")):
        add(name, 120 + i * 120, 108, 100, 34)
    add("Subscribe", 120, 380, 150, 46)
    add("Read more", 290, 380, 150, 46)
    add("Share", 460, 380, 110, 46)
    add("Search", 880, 108, 300, 34)
    for i in range(6):
        add(f"Story {i + 1}", 120, 470 + i * 44, 520, 26)
    for i in range(4):
        add(f"Link {i + 1}", 760, 470 + i * 44, 300, 26)
    for i in range(7):
        add(f"Taskbar {i + 1}", 580 + i * 48, 864 - 44, 40, 40)
    return out


def _system_fonts() -> None:
    """The offscreen platform does not see Windows fonts; load Segoe UI so the fake page looks like Windows."""
    from PySide6.QtGui import QFontDatabase

    if "Segoe UI" in QFontDatabase.families():
        return
    for name in ("segoeui.ttf", "segoeuib.ttf"):
        path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))


def draw_desktop(dark_page: bool):  # noqa: ANN201 - QImage
    """A believable desktop at 125%: a browser page and a taskbar, drawn in logical units."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter

    img = QImage(SCREEN_W, SCREEN_H, QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(DPR)
    W, H = SCREEN_W / DPR, SCREEN_H / DPR
    _system_fonts()
    page_bg = QColor("#1E2124") if dark_page else QColor("#FFFFFF")
    page_fg = QColor("#E6E6E6") if dark_page else QColor("#202124")
    muted = QColor("#9AA0A6") if dark_page else QColor("#5F6368")
    link = QColor("#8AB4F8") if dark_page else QColor("#1A0DAB")
    chrome = QColor("#2B2E31") if dark_page else QColor("#DEE1E6")
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.fillRect(QRectF(0, 0, W, H), QColor("#3C5A6E"))
    # browser
    p.fillRect(QRectF(0, 0, W, H - TASKBAR_LOGICAL), page_bg)
    p.fillRect(QRectF(0, 0, W, 82), chrome)
    font = QFont("Segoe UI")
    font.setPixelSize(13)
    p.setFont(font)
    for i, tab in enumerate(("Newsletter", "Mail", "Calendar")):
        r = QRectF(8 + i * 186, 6, 180, 30)
        p.fillRect(r, page_bg if i == 0 else chrome.darker(108))
        p.setPen(page_fg)
        p.drawText(r.adjusted(12, 0, 0, 0), int(Qt.AlignmentFlag.AlignVCenter), tab)
    p.fillRect(QRectF(130, 44, 900, 30), page_bg)
    p.setPen(muted)
    p.drawText(QRectF(142, 44, 880, 30), int(Qt.AlignmentFlag.AlignVCenter), "https://gardens.example.org/newsletter/october")
    for i in range(3):
        p.setPen(page_fg)
        p.drawText(QRectF(10 + i * 36, 44, 30, 30), int(Qt.AlignmentFlag.AlignCenter), ("<", ">", "R")[i])
    # site nav
    font.setPixelSize(15)
    p.setFont(font)
    for i, name in enumerate(("Home", "News", "Events", "Volunteer", "Contact")):
        p.setPen(page_fg)
        p.drawText(QRectF(120 + i * 120, 108, 100, 34), int(Qt.AlignmentFlag.AlignCenter), name)
    p.setPen(muted)
    p.drawRect(QRectF(880, 108, 300, 34))
    p.drawText(QRectF(892, 108, 280, 34), int(Qt.AlignmentFlag.AlignVCenter), "Search the site")
    # article
    font.setPixelSize(34)
    font.setWeight(QFont.Weight.Bold)
    p.setFont(font)
    p.setPen(page_fg)
    p.drawText(QRectF(120, 170, 1000, 50), int(Qt.AlignmentFlag.AlignVCenter), "Autumn planting day at the community garden")
    font.setPixelSize(17)
    font.setWeight(QFont.Weight.Normal)
    p.setFont(font)
    body = (
        "Bring gloves and a friend. We will be planting garlic, broad beans and spring bulbs along the north beds, "
        "and the new raised beds by the gate are finally ready for winter greens. Tea and soup from eleven."
    )
    p.drawText(QRectF(120, 236, 980, 120), int(Qt.TextFlag.TextWordWrap), body)
    for i, (name, w) in enumerate((("Subscribe", 150), ("Read more", 150), ("Share", 110))):
        x = (120, 290, 460)[i]
        r = QRectF(x, 380, w, 46)
        if i == 0:
            p.fillRect(r, QColor("#1A73E8"))
            p.setPen(QColor("#FFFFFF"))
        else:
            p.setPen(muted)
            p.drawRect(r)
            p.setPen(link)
        p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), name)
    font.setPixelSize(16)
    p.setFont(font)
    for i in range(6):
        p.setPen(link)
        p.drawText(QRectF(120, 470 + i * 44, 520, 26), int(Qt.AlignmentFlag.AlignVCenter), f"Story {i + 1}: notes from the {['tool shed', 'pond', 'orchard', 'seed swap', 'compost bays', 'bee hotel'][i]}")
        p.setPen(muted)
        p.drawLine(120, 470 + i * 44 + 34, 640, 470 + i * 44 + 34)
    for i in range(4):
        p.setPen(link)
        p.drawText(QRectF(760, 470 + i * 44, 300, 26), int(Qt.AlignmentFlag.AlignVCenter), ["Volunteer rota", "Plot waiting list", "Find us", "Past newsletters"][i])
    p.fillRect(QRectF(1100, 170, 260, 260), QColor("#7FA36B") if not dark_page else QColor("#4E6B44"))
    # taskbar
    p.fillRect(QRectF(0, H - TASKBAR_LOGICAL, W, TASKBAR_LOGICAL), QColor("#202020"))
    for i in range(7):
        r = QRectF(580 + i * 48 + 8, H - TASKBAR_LOGICAL + 12, 24, 24)
        p.fillRect(r, QColor(["#3A96DD", "#F2C811", "#E8E8E8", "#C239B3", "#31A24C", "#E74856", "#8A8A8A"][i]))
    font.setPixelSize(12)
    p.setFont(font)
    p.setPen(QColor("#FFFFFF"))
    p.drawText(QRectF(W - 120, H - TASKBAR_LOGICAL, 100, TASKBAR_LOGICAL), int(Qt.AlignmentFlag.AlignCenter), "10:42\n24/10/2026")
    p.end()
    return img


# =========================================================================== compositing

_Z = {
    "Window": 0,
    "dock": 10,
    "click-type": 12,
    "highlight-edge": 20,
    "labels": 22,
    "grid": 22,
    "resume-zone": 24,
    "zoom-lens": 26,
    "help": 28,
    "click-ripple": 30,
    "pointer-ring": 32,
    "gaze-dot": 33,
    "did-you-mean": 39,
    "captions": 40,
    "toast": 41,
    "hud": 42,
}


def composite(background, path: Path, extra: list | None = None) -> None:  # noqa: ANN001
    """Background plus every visible top-level window, in a sensible stacking order."""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    for _ in range(3):
        app.processEvents()
    img = QImage(background)
    img.setDevicePixelRatio(DPR)
    p = QPainter(img)
    widgets = [w for w in QApplication.topLevelWidgets() if w.isVisible()]
    widgets.sort(key=lambda w: _Z.get(w.objectName(), 5))
    for w in widgets:
        pm = w.grab()
        p.drawPixmap(QPointF(w.x(), w.y()), pm)
    for item in extra or []:
        item(p)
    p.end()
    img.save(str(path))
    print(f"wrote {path.relative_to(ROOT)}")


def draw_cursor(x_phys: float, y_phys: float):  # noqa: ANN201
    """Paint a standard arrow cursor so screenshots show where the pointer is."""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

    def paint(p: QPainter) -> None:
        x, y = x_phys / DPR, y_phys / DPR
        path = QPainterPath(QPointF(x, y))
        for dx, dy in ((0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)):
            path.lineTo(x + dx, y + dy)
        path.closeSubpath()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("#FFFFFF"), 1.2))
        p.setBrush(QColor("#000000"))
        p.drawPath(path)

    return paint


# =========================================================================== scenes


class Playground:
    def __init__(self, theme: str, dark_page: bool) -> None:
        from sightline.ui.shell import UIShell

        self.ctx = build_context(theme)
        self.background = draw_desktop(dark_page)
        from PySide6.QtWidgets import QApplication

        self.app = QApplication.instance()
        self.shell = UIShell(self.ctx, self.app, grabber=self._grab)
        self._simulate_taskbar()
        self.shell.start()
        self.pump()

    def _simulate_taskbar(self) -> None:
        """The offscreen screen has no taskbar; give it the same work area as the drawn one."""
        from dataclasses import replace

        from sightline.core.types import Rect
        from sightline.ui.coords import ScreenMap

        screens = self.shell.screens
        work = Rect(0, 0, SCREEN_W, SCREEN_H - round(TASKBAR_LOGICAL * DPR))
        screens.map = ScreenMap([replace(s, work=work) for s in screens.map.screens])
        screens.changed.emit()

    def _grab(self, source, screen):  # noqa: ANN001, ANN202
        from PySide6.QtCore import QRect

        from sightline.core.types import Rect

        x, y, w, h = round(source.x), round(source.y), round(source.w), round(source.h)
        img = self.background.copy(QRect(x, y, w, h))
        img.setDevicePixelRatio(1.0)
        return img, Rect(x, y, w, h)

    def pump(self, seconds: float = 0.05) -> None:
        from sightline.ui.bridge import process_events_until

        deadline = time.monotonic() + seconds
        process_events_until(lambda: time.monotonic() >= deadline, timeout=seconds + 1)

    def publish(self, *events: object) -> None:
        for e in events:
            self.ctx.bus.publish(e)
        self.pump()

    def pointer(self, x: float, y: float, phase: str = "fine", **kw: object) -> None:
        from sightline.core.events import FaceStatus, PointerSample

        self.ctx.desktop.input.pos = (x, y)
        self.shell.cursor.poll()
        self.publish(FaceStatus(True, 30.0), PointerSample(time.perf_counter(), x, y, phase=phase, **kw))

    def act(self, name: str, **args: object) -> None:
        from sightline.core.types import Action

        self.ctx.actions.execute_now(Action(name, args, source="test"))
        self.pump()

    def shot(self, name: str, cursor: bool = True) -> None:
        extra = [draw_cursor(*self.ctx.desktop.input.pos)] if cursor else []
        composite(self.background, OUT / f"{name}.png", extra)

    def close(self) -> None:
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication

        self.shell.stop()
        for w in QApplication.topLevelWidgets():
            w.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.pump()


def scene_overview(theme: str, dark_page: bool) -> None:
    from sightline.core.events import HoverTarget, IntentResult, Notice, TranscriptFinal
    from sightline.core.types import Rect

    pg = Playground(theme, dark_page)
    s = DPR
    pg.pointer(205 * s, 400 * s)
    pg.publish(HoverTarget(Rect(120 * s, 380 * s, 150 * s, 46 * s), "Subscribe", "Button"))
    pg.publish(TranscriptFinal(1, "scroll down three"), IntentResult(1, "scroll down three", "command", "Scroll down 3"))
    pg.publish(Notice("Microphone: CABLE Output (VB-Audio Point)", "info", key="mic"))
    pg.shot(f"overview_{theme}{'_darkpage' if dark_page else ''}")
    pg.close()


def scene_ring_states(theme: str) -> None:
    """A contact sheet of the pointer ring over black, mid grey and white."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter

    from sightline.core.events import DwellProgress, EyeHold, FaceStatus, StateChanged

    pg = Playground(theme, False)
    ring = pg.shell.ring
    pg.pointer(400, 400)
    states = [
        ("Tracking", lambda: None),
        ("Jumped (warp)", lambda: setattr(ring, "_warp", 0.55)),
        ("Held still", lambda: ring.on_sample(_sample("frozen"))),
        ("Click armed", lambda: ring.on_hold(EyeHold(0, 1))),
        ("Double armed", lambda: ring.on_hold(EyeHold(0, 2))),
        ("Dwell 40%", lambda: ring.on_dwell(DwellProgress(0, 0, 0.4))),
        ("Dwell 80%", lambda: ring.on_dwell(DwellProgress(0, 0, 0.8))),
        ("Eyes paused", lambda: ring.on_state(StateChanged("pointer_paused", False, True))),
        ("No face", lambda: ring.on_face(FaceStatus(False))),
    ]
    cell_w, cell_h = 300, 210
    sheet = QImage(cell_w * len(states), cell_h * 3, QImage.Format.Format_ARGB32_Premultiplied)
    sheet.fill(QColor("#808080"))
    p = QPainter(sheet)
    font = QFont("Segoe UI")
    font.setPixelSize(15)
    for col, (label, setup) in enumerate(states):
        ring._hold = 0
        ring._dwell = 0.0
        ring._warp = 0.0
        ring._paused = False
        ring._face_present = True
        ring._sample_phase = "fine"
        ring._update_phase()
        setup()
        ring.update()
        pg.pump()
        pm = ring.grab()
        for row, bg in enumerate(("#000000", "#7A7A7A", "#FFFFFF")):
            cell = QRectF(col * cell_w, row * cell_h, cell_w, cell_h)
            p.fillRect(cell, QColor(bg))
            p.drawPixmap(QPointF(cell.center().x() - pm.width() / 2, cell.center().y() - pm.height() / 2 - 8), pm)
            p.setFont(font)
            p.setPen(QColor("#FFFFFF") if row < 2 else QColor("#000000"))
            p.drawText(cell.adjusted(0, 0, 0, -8), int(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter), label)
    p.end()
    path = OUT / f"ring_states_{theme}.png"
    sheet.save(str(path))
    print(f"wrote {path.relative_to(ROOT)}")
    pg.close()


def _sample(phase: str):  # noqa: ANN202
    from sightline.core.events import PointerSample

    return PointerSample(time.perf_counter(), 400, 400, phase=phase)


def scene_labels(theme: str, dark_page: bool) -> None:
    pg = Playground(theme, dark_page)
    pg.pointer(980 * DPR, 300 * DPR)
    pg.act("ui.labels.show")
    pg.pump(0.6)
    labels = pg.shell.labels.labels
    if labels:
        # Look at badge 9 to show the armed (hover) state.
        target = next((lb for lb in labels if lb.number == 9), labels[0])
        pg.ctx.desktop.input.pos = target.badge.center
        pg.shell.cursor.poll()
        pg.pump()
    pg.shot(f"labels_{theme}{'_darkpage' if dark_page else ''}")
    pg.close()


def scene_grid(theme: str) -> None:
    pg = Playground(theme, False)
    pg.pointer(700 * DPR, 400 * DPR)
    pg.act("ui.grid.show")
    pg.shot(f"grid_level0_{theme}")
    pg.act("ui.grid.select", cell=4)
    pg.act("ui.grid.select", cell=6)
    pg.shot(f"grid_level2_{theme}")
    pg.close()


def scene_zoom(theme: str) -> None:
    pg = Playground(theme, False)
    pg.pointer(330 * DPR, 402 * DPR)
    pg.act("ui.zoom.show")
    pg.pump(0.25)
    pg.shot(f"zoom_{theme}")
    pg.close()


def scene_help(theme: str) -> None:
    pg = Playground(theme, False)
    pg.pointer(700 * DPR, 400 * DPR)
    pg.act("ui.help.show")
    pg.pump(0.1)
    pg.shot(f"help_{theme}", cursor=False)
    pg.close()


def scene_feedback(theme: str) -> None:
    """Captions while speaking, a rejected phrase, a failed action toast, the ripple, the resume zone."""
    from sightline.core.events import (
        ActionExecuted,
        ClickPerformed,
        GazeZoneProgress,
        IntentResult,
        MicLevel,
        Notice,
        TranscriptFinal,
        TranscriptPartial,
    )
    from sightline.core.types import Action, Rect

    pg = Playground(theme, False)
    pg.pointer(600 * DPR, 300 * DPR)
    pg.publish(MicLevel(0.2, True), TranscriptPartial(3, "open the volunteer"))
    pg.shot(f"captions_partial_{theme}")
    pg.publish(IntentResult(3, "open the volunteer rota", "rejected", "Not understood"))
    pg.publish(ActionExecuted(Action("window.close"), False, "No window to close"))
    pg.publish(Notice("Could not start vision: no camera found", "error", key="start-vision"))
    pg.publish(ClickPerformed(600 * DPR, 300 * DPR, "right"))
    pg.shell.ripple._anim.stop()
    pg.shell.ripple._t = 0.35
    pg.shell.ripple.show()
    pg.shot(f"feedback_{theme}")
    pg.close()

    pg = Playground(theme, False)
    pg.pointer(900 * DPR, 420 * DPR)
    said = "scrol dawn a bit"
    pg.publish(TranscriptFinal(4, said), IntentResult(4, said, "rejected", "Not understood", alternatives=("Scroll down", "Scroll up", "Show desktop")))
    pg.shot(f"did_you_mean_{theme}")
    pg.close()

    pg = Playground(theme, False)
    pg.act("pointer.pause")
    pg.pointer(1300 * DPR, 120 * DPR, phase="paused")
    zone = Rect(SCREEN_W - 260 - round(118 * DPR), 0, 260, 200)
    pg.publish(GazeZoneProgress("resume", 0.6, zone))
    pg.shot(f"paused_resume_{theme}")
    pg.close()


def scene_dock_variants(theme: str) -> None:
    pg = Playground(theme, False)
    pg.pointer(700 * DPR, 400 * DPR)
    pg.act("dwell.toggle")
    pg.act("speech.mode", mode="sleep")
    pg.shell.dock.flyout.open_beside(pg.shell.dock.buttons["click_type"])
    pg.shot(f"dock_states_{theme}")
    pg.close()

    pg = Playground(theme, False)
    pg.ctx.settings.ui.dock_edge = "top"
    pg.ctx.settings_changed("ui")
    pg.pump()
    pg.pointer(700 * DPR, 400 * DPR)
    pg.shot(f"dock_top_{theme}")
    pg.close()

    pg = Playground(theme, False)
    pg.act("ui.dock.toggle")
    pg.pointer(700 * DPR, 400 * DPR)
    pg.shot(f"hud_only_{theme}")
    pg.close()


def _fake_frame():  # noqa: ANN202 - CameraFrame
    """An abstract camera picture (a plain oval and two eyes) with landmark points, for the preview."""
    import numpy as np

    from sightline.core.events import CameraFrame

    h, w = 240, 320
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = (58, 62, 66)
    img[((xx - 160) / 62.0) ** 2 + ((yy - 125) / 82.0) ** 2 <= 1] = (148, 160, 176)
    for ex in (136, 184):
        img[((xx - ex) / 11.0) ** 2 + ((yy - 108) / 5.0) ** 2 <= 1] = (40, 40, 44)
    t = np.linspace(0, 2 * np.pi, 120, endpoint=False)
    outline = np.stack([160 + 62 * np.cos(t), 125 + 82 * np.sin(t)], axis=1)
    eyes = np.concatenate([np.stack([ex + 11 * np.cos(t[::4]), 108 + 5 * np.sin(t[::4])], axis=1) for ex in (136, 184)])
    return CameraFrame(img, np.concatenate([outline, eyes]), time.perf_counter())


def _window_size(widget, w: float = 1040, h: float = 800) -> None:  # noqa: ANN001
    widget.resize(round(w), round(h))


def scene_setup(theme: str) -> None:
    """Every page of the set-up wizard, the calibration dots and the finish page."""
    from sightline.core.events import MicLevel, ModelProgress
    from sightline.core.interfaces import CalibrationReport
    from sightline.core.models import ModelFile, ModelSpec
    from sightline.core.types import Attention
    from sightline.ui.setup import create_setup
    from sightline.ui.windows import targets

    pg = Playground(theme, False)
    for spec in (
        ModelSpec("face_landmarker", "Face and eye tracking model", (ModelFile("https://example.invalid/f", "f.task", "0" * 64, 3_758_596),), "Apache-2.0"),
        ModelSpec("asr", "Speech recogniser", (ModelFile("https://example.invalid/a", "a.tar.bz2", "0" * 64, 61_500_000, extract=True),), "MIT"),
        ModelSpec("embed", "Command matcher", (ModelFile("https://example.invalid/e", "e.onnx", "0" * 64, 30_200_000),), "MIT"),
    ):
        pg.ctx.models.register(spec)
    pg.ctx.models.ensure = lambda **_kw: time.sleep(3600)  # type: ignore[method-assign] - never finishes in a shot
    pg.ctx.vision.attention_value = Attention(1.0, 1.0, 0.6, 30)
    wizard = create_setup(pg.shell.services, lambda: None, lambda _cb: None)
    wizard.open_uri = lambda _uri: None
    _window_size(wizard)
    wizard._go(0)
    save_widget(wizard, f"setup_01_welcome_{theme}")
    wizard.wizard_next()
    pg.pump(0.3)
    save_widget(wizard, f"setup_02_checks_{theme}")
    wizard.wizard_next()
    pg.pump()
    pg.publish(
        ModelProgress("face_landmarker", "ready", 3_758_596, 3_758_596),
        ModelProgress("asr", "downloading", 24_000_000, 61_500_000),
        ModelProgress("embed", "queued", 0, 30_200_000),
    )
    save_widget(wizard, f"setup_03_download_{theme}")
    wizard.models.view.complete = True
    wizard.models_ready()
    wizard.wizard_next()
    pg.pump(0.2)
    pg.publish(_fake_frame())
    save_widget(wizard, f"setup_04_camera_{theme}")
    wizard.wizard_next()
    pg.pump(0.2)
    for _ in range(6):
        pg.publish(MicLevel(0.25, True))
        pg.pump(0.1)
    save_widget(wizard, f"setup_05_microphone_{theme}")
    wizard.wizard_next()
    pg.pump()
    save_widget(wizard, f"setup_06_hand_over_{theme}")
    wizard.wizard_next()
    pg.pump()
    eyes = wizard.page
    eyes.instruction.setText(targets.EYE_STEPS[1].instruction)
    eyes.bar.set_value(0.45)
    save_widget(wizard, f"setup_07_eyes_{theme}")
    eyes.report = CalibrationReport(True, "eyes", "Open, closed and both winks learned.")
    wizard.wizard_next()
    pg.pump()

    gaze = wizard.page
    screen = pg.shell.screens.map.screens[0]
    window = targets.TargetsWindow(pg.shell.services)
    window.cover(screen)
    window.instruction = targets.INSTRUCTIONS["gaze"]
    window.note = "Say cancel to stop."
    window.dot = targets.points_on(screen, targets.GAZE_POINTS)[3]
    window.collecting, window.shrink = True, 0.55
    save_widget(window, f"setup_08_gaze_dots_{theme}")
    window.hide()
    report = CalibrationReport(True, "gaze", "Average error 38 px (about 1.1 degrees)", 38.0, 1.1)
    gaze.report = report
    gaze.message.setText(report.message)
    gaze._show_comfort(report)
    wizard._update_nav()
    save_widget(wizard, f"setup_09_gaze_result_{theme}")
    wizard.wizard_next()
    pg.pump()
    save_widget(wizard, f"setup_10_head_{theme}")
    wizard.wizard_skip()
    pg.pump()
    mouth = wizard.page
    mouth.works = True
    mouth.phase = "done"
    mouth.bar.set_value(1.0)
    mouth.instruction.setText("It works for you")
    mouth.message.setText("Turn on \"Also require your mouth to move\"? Speech then only acts while your mouth moves.")
    mouth.gate.show()
    mouth.gate.set_current(False)
    wizard._update_nav()
    save_widget(wizard, f"setup_11_mouth_{theme}")
    wizard.wizard_next()
    pg.pump()
    save_widget(wizard, f"setup_12_finish_{theme}")
    wizard._finished = True
    wizard.hide()

    from sightline.ui.windows.wizards import CalibrationWizard

    cal = CalibrationWizard(pg.shell.services, "full")
    _window_size(cal)
    cal.note_result("Eye clicks", "Open, closed and both winks learned.")
    cal.note_result("Where you look", "Average error 38 px (about 1.1 degrees)")
    cal.note_result("Comfortable size", gaze.comfort.text())
    cal._go(len(cal.pages) - 1)
    save_widget(cal, f"calibration_done_{theme}")
    cal.hide()

    pg.shell.tray.sync()
    menu = pg.shell.tray.menu
    menu.adjustSize()
    save_widget(menu, f"tray_menu_{theme}")
    pg.close()


def scene_settings(theme: str) -> None:
    """The settings window: most used, presets, one area, and the helper's door opened."""
    pg = Playground(theme, False)
    pg.act("ui.settings.show")
    window = pg.shell._settings_window
    _window_size(window, 1180, 860)
    pg.act("ui.settings.adjust", direction="up")
    pg.pump()
    save_widget(window, f"settings_1_most_used_{theme}")
    window.show_page("Presets")
    save_widget(window, f"settings_2_presets_{theme}")
    window.show_page("Eyes and pointer")
    window.next_setting()
    save_widget(window, f"settings_3_eyes_{theme}")
    window.show_page("For a helper")
    window.toggle_advanced()
    save_widget(window, f"settings_4_helper_{theme}")
    window.hide()
    pg.close()


def scene_practice(theme: str) -> None:
    """The practice room: menu, a target under the pointer, the tapping circle, and the summary."""
    from PySide6.QtCore import QPointF

    from sightline.ui import measures

    pg = Playground(theme, False)
    pg.act("ui.practice.show")
    window = pg.shell._practice_window
    _window_size(window, 1400, 880)
    save_widget(window, f"practice_1_menu_{theme}")
    window.rng.seed(7)
    window.start_targets()
    pg.pump()
    for _ in range(4):
        c, _r = window.canvas.targets[0]
        window.select_at(c)
    window.select_at(QPointF(40, 40))
    c, _r = window.canvas.targets[0]
    window.canvas.hover = c
    save_widget(window, f"practice_2_targets_{theme}")
    window.start_tapping()
    pg.pump()
    for i in range(5):
        c, r = window.canvas.targets[window.canvas.current]
        window.select_at(QPointF(c.x() + (0.3 * r if i % 2 else -0.2 * r), c.y()))
    save_widget(window, f"practice_3_tapping_{theme}")
    window.mode = "menu"
    window.score = measures.TargetScore(shown=12, hits=12, misses=2, skipped=0, times_ms=[1850.0, 1400.0, 2100.0, 1650.0] * 3)
    window.tapping = [
        measures.TappingResult(150, 700, 11, 0, 0.0, 1450.0, 120.0, 705.0, 2.78, 1.92),
        measures.TappingResult(100, 700, 11, 1, 1 / 11, 1700.0, 118.0, 702.0, 2.80, 1.65),
        measures.TappingResult(60, 700, 11, 3, 3 / 11, 2300.0, 131.0, 698.0, 2.65, 1.15),
    ]
    window.show_summary()
    save_widget(window, f"practice_4_summary_{theme}")
    window.hide()
    pg.close()


def save_widget(widget, name: str) -> None:  # noqa: ANN001
    from PySide6.QtWidgets import QApplication

    widget.show()
    for _ in range(3):
        QApplication.instance().processEvents()
    path = OUT / f"{name}.png"
    widget.grab().save(str(path))
    print(f"wrote {path.relative_to(ROOT)}")


SCENES = {
    "overview": lambda: [scene_overview("dark", False), scene_overview("light", False), scene_overview("dark", True)],
    "ring": lambda: [scene_ring_states("dark"), scene_ring_states("light")],
    "labels": lambda: [scene_labels("dark", False), scene_labels("light", True)],
    "grid": lambda: [scene_grid("dark")],
    "zoom": lambda: [scene_zoom("dark")],
    "help": lambda: [scene_help("dark"), scene_help("light")],
    "feedback": lambda: [scene_feedback("dark"), scene_feedback("light")],
    "dock": lambda: [scene_dock_variants("dark"), scene_dock_variants("light")],
    "setup": lambda: [scene_setup("dark"), scene_setup("light")],
    "settings": lambda: [scene_settings("dark"), scene_settings("light")],
    "practice": lambda: [scene_practice("dark"), scene_practice("light")],
}


# =========================================================================== live mode


def run_live(seconds: float, offscreen: bool = False) -> int:
    """Real shell, real screen, fake events.  Takes the desktop lock for the whole run.

    With ``offscreen`` the same event generator drives the shell on Qt's
    offscreen platform (a smoke test: nothing is shown and nothing is heard).
    """
    if offscreen:
        os.environ["QT_QPA_PLATFORM"] = _offscreen_config()
        return _live(seconds, sounds=False)
    sys.path.insert(0, str(ROOT))
    from tools.desktop_lock import desktop_lock

    with desktop_lock("ui-playground"):
        return _live(seconds, sounds=True)


def _live(seconds: float, sounds: bool) -> int:
    import ctypes

    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from sightline.core.events import (
        DwellProgress,
        EyeHold,
        FaceStatus,
        HoverTarget,
        IntentResult,
        MicLevel,
        Notice,
        PointerSample,
        TranscriptFinal,
        TranscriptPartial,
    )
    from sightline.core.types import Rect
    from sightline.ui.shell import UIShell

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    ctx = build_context("system")
    ctx.settings.ui.sounds = sounds

    # The real cursor position, read only (GetCursorPos); nothing here moves it.
    import ctypes.wintypes as wt

    point = wt.POINT()

    def cursor() -> tuple[float, float]:
        ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
        return (float(point.x), float(point.y))

    ctx.desktop.input.cursor_pos = cursor  # type: ignore[method-assign]
    shell = UIShell(ctx, app)
    shell.start()
    t0 = time.monotonic()
    tick = itertools.count()
    phrases = itertools.cycle(
        [
            ("scroll down three", "command", "Scroll down 3", ()),
            ("open the volunteer rota", "rejected", "Not understood", ("Open Volunteer rota.xlsx", "Open volume mixer")),
            ("hello and thank you for the soup", "dictation", "Typed: Hello and thank you for the soup", ()),
            ("show numbers", "command", "Show numbers", ()),
        ]
    )

    def step() -> None:
        n = next(tick)
        x, y = cursor()
        phase = "warp" if n % 90 == 0 else "frozen" if (n // 30) % 7 == 3 else "fine"
        ctx.bus.publish(PointerSample(time.perf_counter(), x, y, x + 25 * math.sin(n / 9), y + 18 * math.cos(n / 7), phase))
        ctx.bus.publish(FaceStatus(True, 30.0))
        ctx.bus.publish(MicLevel(abs(math.sin(n / 11)) * 0.3, (n // 25) % 3 == 0))
        cycle = n % 150
        if cycle == 20:
            ctx.bus.publish(EyeHold(time.perf_counter(), 1))
        elif cycle == 45:
            ctx.bus.publish(EyeHold(time.perf_counter(), 2))
        elif cycle == 60:
            ctx.bus.publish(EyeHold(time.perf_counter(), 0))
        if 80 <= cycle < 110:
            ctx.bus.publish(DwellProgress(x, y, (cycle - 80) / 30.0, 40))
        elif cycle == 110:
            ctx.bus.publish(DwellProgress(x, y, 0.0, 40))
        if n % 10 == 0:
            ctx.bus.publish(HoverTarget(Rect(x - 60, y - 18, 120, 36), "Fake button", "Button"))
        if n % 120 == 0:
            text, kind, display, alternatives = next(phrases)
            uid = n // 120
            words = text.split()
            for i in range(1, len(words) + 1):
                QTimer.singleShot(120 * i, lambda w=" ".join(words[:i]), u=uid: ctx.bus.publish(TranscriptPartial(u, w)))
            QTimer.singleShot(120 * len(words) + 200, lambda u=uid, t=text: ctx.bus.publish(TranscriptFinal(u, t)))
            QTimer.singleShot(120 * len(words) + 260, lambda u=uid, t=text, k=kind, d=display, a=alternatives: ctx.bus.publish(IntentResult(u, t, k, d, alternatives=a)))
        if n % 400 == 200:
            ctx.bus.publish(Notice("This is the playground: events are fake.", "info", key="playground"))
        if time.monotonic() - t0 > seconds:
            app.quit()

    timer = QTimer()
    timer.timeout.connect(step)
    timer.start(33)
    code = app.exec()
    delivered, wakeups = shell.bridge.delivered, shell.bridge.wakeups
    shell.stop()
    print(f"live run: {next(tick)} frames, {delivered} events delivered in {wakeups} GUI wake-ups")
    return code


# =========================================================================== main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sightline UI playground")
    parser.add_argument("--live", action="store_true", help="run on the real desktop with fake events (takes the desktop lock)")
    parser.add_argument("--seconds", type=float, default=120.0, help="how long --live runs")
    parser.add_argument("--offscreen", action="store_true", help="with --live: drive the shell offscreen (smoke test)")
    parser.add_argument("--only", nargs="*", choices=sorted(SCENES), help="render only these scenes")
    args = parser.parse_args(argv)
    # Never write to the user's real settings or data folders from the playground.
    os.environ.setdefault("SIGHTLINE_HOME", str(OUT / "home"))
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false;qt.multimedia.*=false")
    COMMANDS.extend(_commands())
    if args.live:
        return run_live(args.seconds, offscreen=args.offscreen)

    os.environ["QT_QPA_PLATFORM"] = _offscreen_config()
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["sightline-playground"])
    app.setQuitOnLastWindowClosed(False)
    for name in args.only or SCENES:
        SCENES[name]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
