"""The contract between the shell and a wizard window (setup, calibration).

Voice and the dock drive wizards through ``ui.wizard.next``, ``ui.wizard.back``
and ``ui.wizard.skip``; the shell forwards them to whichever wizard is open.
While a wizard is open ``state.active_overlay`` is its ``overlay_name``, so
speech maps "next", "continue", "done", "back" and "skip" to those actions.

To replace the minimal first-run window with the full setup wizard, make a
window that satisfies ``Wizard`` and return it from
``sightline.ui.setup.create_setup``; nothing else in the shell changes.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Wizard(Protocol):
    overlay_name: str  # "setup" or "calibration"

    def wizard_next(self) -> None: ...

    def wizard_back(self) -> None: ...

    def wizard_skip(self) -> None: ...
