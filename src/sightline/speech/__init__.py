"""Microphone, speech recognition, command grammar and dictation."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sightline.core.context import AppContext
    from sightline.core.interfaces import SpeechAPI


def create(ctx: AppContext) -> SpeechAPI:
    """Build the speech service: registers its models and actions, opens nothing until ``start()``."""
    from .service import SpeechService

    return SpeechService(ctx)
