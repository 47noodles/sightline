"""The bundle of shared services handed to each subsystem's ``create`` function."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .actions import ActionRegistry
from .bus import EventBus
from .events import SettingsChanged
from .models import ModelManager
from .state import AppState

if TYPE_CHECKING:
    from sightline.settings import Settings

    from .interfaces import Desktop, SpeechAPI, VisionAPI


@dataclass
class AppContext:
    bus: EventBus
    actions: ActionRegistry
    state: AppState
    settings: Settings
    models: ModelManager
    desktop: Desktop | None = None
    vision: VisionAPI | None = None
    speech: SpeechAPI | None = None
    extras: dict = field(default_factory=dict)

    def settings_changed(self, section: str, *, save: bool = True) -> None:
        """Call after changing a settings section: saves to disk and tells every subsystem."""
        if save:
            self.settings.save()
        self.bus.publish(SettingsChanged(section))

    @classmethod
    def for_tests(cls, settings: Settings | None = None) -> AppContext:
        """A context with real core services and fake subsystems."""
        from sightline.settings import Settings as _Settings

        from .fakes import FakeDesktop, FakeSpeech, FakeVision

        bus = EventBus()
        return cls(
            bus=bus,
            actions=ActionRegistry(bus),
            state=AppState(bus),
            settings=settings or _Settings(),
            models=ModelManager(bus),
            desktop=FakeDesktop(),
            vision=FakeVision(),
            speech=FakeSpeech(),
        )
