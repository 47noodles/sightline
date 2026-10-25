"""Windows integration: input injection, UI Automation, windows, apps, built-in accessibility tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sightline.core.context import AppContext
    from sightline.core.interfaces import Desktop


def create(ctx: AppContext) -> Desktop:
    """Build the desktop services and register the desktop actions.  Threads start in ``start()``."""
    from .service import WindowsDesktop

    return WindowsDesktop(ctx)
