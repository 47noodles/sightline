"""Which first-run window ``UIShell.run_setup`` opens: the set-up wizard in ``windows/wizards.py``.

It satisfies ``sightline.ui.wizard.Wizard`` and calls ``on_complete`` (start
the engines) exactly once: as soon as the downloads are ready, because the
camera, microphone and calibration pages need the engines running, or when
the window is closed early.  Finishing sets
``settings.general.first_run_complete`` and opens the practice room.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QWidget

if TYPE_CHECKING:
    from .services import UiServices


def create_setup(
    services: UiServices,
    on_complete: Callable[[], None],
    ensure_models: Callable[[Callable[[], None]], None],
) -> QWidget:
    from .windows.wizards import SetupWizard

    return SetupWizard(services, on_complete, ensure_models)
