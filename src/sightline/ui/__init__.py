"""Everything the user sees and hears: overlays, dock, captions, setup and calibration, settings, sounds."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication

    from sightline.core.context import AppContext

    from .shell import UIShell


def create(ctx: AppContext, qt_app: QApplication) -> UIShell:
    """Build the UI shell.  Must be called on the GUI thread after QApplication exists."""
    from .shell import UIShell

    return UIShell(ctx, qt_app)
