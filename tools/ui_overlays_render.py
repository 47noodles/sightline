"""Render the number chooser, the action menu and the countdown to PNGs, in light and dark.

Reuses the playground's synthetic desktop (a browser page and a taskbar at 125%) and its
fake context, installs the three overlays as the shell will, drives them with the same
actions the app uses, and writes ``out/ui/overlays/*.png``.  Qt runs on its offscreen
platform: nothing appears on screen, the real cursor never moves and no sound plays::

    .venv\\Scripts\\python.exe tools\\ui_overlays_render.py
    .venv\\Scripts\\python.exe tools\\ui_overlays_render.py --only chooser --themes dark
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "ui" / "overlays"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ui_playground as playground  # noqa: E402 - the synthetic desktop, fake context and compositor

S = playground.DPR  # logical page coordinates to physical pixels

# Stacking for the compositor: the chooser and menu under the pointer ring, the countdown over the captions.
playground._Z.update(  # noqa: SLF001 - the playground's own table, extended for these windows
    {
        "chooser-marks": 29,
        "action-menu-mark": 29,
        "chooser-chip": 31,
        "action-menu-tile": 31,
        "confirm-countdown": 43,
        "confirm-notice": 43,
    }
)

# Actions the menu and the countdown lead to, which the playground's context does not register.
STAND_INS = ("mouse.drag_toggle", "mouse.scroll_start", "mouse.scroll_stop", "keyboard.show", "window.close", "app.quit", "key.release_all")


def _offscreen_config() -> str:
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = OUT / "offscreen_screen.json"
    cfg.write_text(
        '{"synchronousWindowSystemEvents": true, "windowFrameMargins": false, "screens": ['
        f'{{"name": "overlays", "x": 0, "y": 0, "width": {playground.SCREEN_W}, "height": {playground.SCREEN_H}, '
        f'"logicalDpi": {round(96 * S)}, "logicalBaseDpi": 96, "dpr": 1}}]}}',
        "utf-8",
    )
    rel = os.path.relpath(cfg, os.getcwd()).replace("\\", "/")
    return f"offscreen:configfile={rel}"


class Scene(playground.Playground):
    """The playground with the chooser, the action menu and the countdown installed."""

    def __init__(self, theme: str, dark_page: bool = False) -> None:
        super().__init__(theme, dark_page)
        from sightline.ui.overlays import action_menu, chooser, confirm

        for name in STAND_INS:
            if not self.ctx.actions.has(name):
                self.ctx.actions.register(name, lambda _a: None)
        self._uninstalls = []
        self.confirm = self._install(confirm)
        self.menu = self._install(action_menu)
        self.chooser = self._install(chooser)

    def _install(self, module):  # noqa: ANN001, ANN202
        undo = module.install(self.ctx, self.shell.services)
        self._uninstalls.append(undo)
        return undo.overlay

    def run(self, name: str, source: str = "voice", utterance: str | None = None, **args: object) -> None:
        from sightline.core.types import Action

        self.ctx.actions.execute_now(Action(name, args, source=source, utterance=utterance))
        self.pump(0.12)

    def gaze_jump(self, x: float, y: float, error_px: float) -> None:
        """Gaze puts the pointer here and the head has not steered it since."""
        self.pointer(x, y, phase="warp", error_px=error_px)
        self.pointer(x, y, phase="fine", error_px=error_px)

    def look_at(self, point: tuple[float, float]) -> None:
        self.ctx.desktop.input.pos = (round(point[0]), round(point[1]))
        self.shell.cursor.poll()
        self.pump()

    def shot(self, name: str, cursor: bool = True) -> None:
        extra = [playground.draw_cursor(*self.ctx.desktop.input.pos)] if cursor else []
        playground.composite(self.background, OUT / f"{name}.png", extra)

    def close(self) -> None:
        for undo in reversed(self._uninstalls):
            undo()
        super().close()


# =========================================================================== scenes


def _chooser_shot(theme: str, name: str, point: tuple[float, float], error_px: float, dark_page: bool = False) -> None:
    sc = Scene(theme, dark_page)
    x, y = point[0] * S, point[1] * S
    sc.gaze_jump(x, y, error_px)
    sc.run("mouse.click", source="eye", button="left", count=1, x=x, y=y)
    chip = sc.chooser.chip_window(2)
    if chip is None:
        print(f"{name}: the chooser did not open ({len(sc.chooser.spots)} chips)")
    else:
        sc.look_at(chip.physical.center)  # resting on chip 2 arms it and its outline
    sc.shot(f"{name}_{theme}")
    sc.close()


def scene_chooser(theme: str) -> None:
    # Three taskbar buttons in a row: a row of chips above them, numbered left to right.
    _chooser_shot(theme, "chooser_row", (580 + 2 * 48 + 20, 840), 60)
    # Stories in a list: a column of chips beside them, numbered top to bottom.
    _chooser_shot(theme, "chooser_column", (200, 545), 90, dark_page=theme == "dark")
    # Back, Forward, Reload and the first tab, in the corner: an arc that stays on screen.
    _chooser_shot(theme, "chooser_corner", (61, 59), 60)


def scene_menu(theme: str) -> None:
    sc = Scene(theme)
    x, y = 760 * S, 300 * S  # on the article text
    sc.pointer(x, y)
    sc.run("ui.menu.show", utterance="menu", x=x, y=y)
    sc.look_at(sc.menu.windows[1].physical.center)  # resting on "Right click" arms it
    sc.shot(f"menu_{theme}")
    sc.run("ui.menu.select", utterance="scroll here", item="scroll here")
    sc.look_at((x, y + 140 * S))
    sc.shot(f"menu_scroll_{theme}")
    sc.close()

    sc = Scene(theme)
    x, y = 25 * S, 59 * S  # the Back button, in the top left corner
    sc.pointer(x, y)
    sc.run("ui.menu.show", utterance="menu", x=x, y=y)
    sc.shot(f"menu_corner_{theme}")
    sc.close()


def scene_confirm(theme: str) -> None:
    from sightline.core.events import IntentResult, TranscriptFinal
    from sightline.core.types import Action, Rect, WindowInfo

    sc = Scene(theme)
    sc.ctx.settings.ui.confirm_delay_ms = 3000
    sc.ctx.desktop.windows.windows = [WindowInfo(101, "Autumn planting day - Gardens newsletter", "msedge.exe", Rect(0, 0, 1920, 1020))]
    sc.pointer(700 * S, 300 * S)
    sc.publish(TranscriptFinal(1, "close window"), IntentResult(1, "close window", "command", "Close window", actions=(Action("window.close"),)))
    sc.run("window.close", utterance="close window")
    sc.pump(1.0)  # part way through the countdown
    sc.shot(f"confirm_{theme}")
    sc.publish(TranscriptFinal(2, "cancel"), IntentResult(2, "cancel", "command", "Cancelled", actions=(Action("ui.cancel"),)))
    sc.run("ui.cancel", utterance="cancel")
    sc.shot(f"confirm_cancelled_{theme}")
    sc.close()


SCENES = {"chooser": scene_chooser, "menu": scene_menu, "confirm": scene_confirm}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the chooser, action menu and countdown overlays offscreen")
    parser.add_argument("--only", nargs="*", choices=sorted(SCENES), help="render only these overlays")
    parser.add_argument("--themes", nargs="*", choices=("light", "dark"), default=["light", "dark"])
    args = parser.parse_args(argv)
    # Offscreen always, and never the user's real settings or data folders.
    os.environ["QT_QPA_PLATFORM"] = _offscreen_config()
    os.environ["SIGHTLINE_HOME"] = str(OUT / "home")
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false;qt.multimedia.*=false")
    playground.COMMANDS.extend(playground._commands())  # noqa: SLF001 - the help sheet's rows

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["sightline-overlays-render"])
    if app.platformName() != "offscreen":
        print(f"Refusing to render on the {app.platformName()!r} platform: this tool only runs offscreen.")
        return 2
    app.setQuitOnLastWindowClosed(False)
    for name in args.only or SCENES:
        for theme in args.themes:
            SCENES[name](theme)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
