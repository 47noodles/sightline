"""Webcam eye and head tracking: gaze pointer, blink and wink gestures, dwell, calibration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sightline.core.context import AppContext
    from sightline.core.interfaces import VisionAPI


def create(ctx: AppContext) -> VisionAPI:
    """Build the vision service: registers its models and actions, opens nothing until ``start()``."""
    from .service import VisionService

    return VisionService(ctx)
