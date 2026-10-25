"""UI test setup: one QApplication per session on Qt's offscreen platform.

Nothing here touches the real desktop: windows render into memory, the
desktop service is the in-memory fake, and sounds are muted.  The offscreen
platform is given two virtual monitors (1920x1080 at 125% and a 4K screen at
200% to its right, 240 px higher) so mixed-DPI placement is tested for real.
Set SIGHTLINE_UI_TESTS_ON_SCREEN=1 to run on the real desktop instead.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_CONFIG = Path(__file__).with_name("offscreen_screens.json")


def _platform_spec() -> str:
    # Qt splits platform arguments on ':', so a drive letter breaks the path; a relative path avoids it.
    try:
        rel = os.path.relpath(_CONFIG, os.getcwd()).replace("\\", "/")
    except ValueError:  # different drive
        return "offscreen"
    return f"offscreen:configfile={rel}" if ":" not in rel else "offscreen"


if os.environ.get("SIGHTLINE_UI_TESTS_ON_SCREEN") != "1":
    os.environ["QT_QPA_PLATFORM"] = _platform_spec()
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false;qt.multimedia.*=false")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["sightline-tests"])
    app.setQuitOnLastWindowClosed(False)
    yield app


@pytest.fixture
def ui_ctx(qapp):
    """A test context with the sounds muted and the dock's app bar off."""
    from sightline.core.context import AppContext

    ctx = AppContext.for_tests()
    ctx.settings.ui.sounds = False
    ctx.settings.ui.dock_reserve_space = False
    return ctx


@pytest.fixture
def shell(ui_ctx, qapp):
    """A started UIShell on the fake context; stopped and cleaned up afterwards."""
    from sightline.ui.shell import UIShell

    s = UIShell(ui_ctx, qapp)
    s.start()
    yield s
    s.stop()
    for widget in qapp.topLevelWidgets():
        widget.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


@pytest.fixture
def recorder(ui_ctx):
    """Registers stand-ins for the desktop and vision actions the UI chains into, recording each call."""
    calls: list = []
    for name in ("mouse.click", "mouse.move", "mouse.scroll", "key.press", "pointer.pause", "pointer.resume", "pointer.toggle", "dwell.toggle", "dwell.click_type", "keyboard.toggle", "speech.mode"):
        if not ui_ctx.actions.has(name):
            ui_ctx.actions.register(name, calls.append)
    return calls
