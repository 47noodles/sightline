from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))  # for vision_synth


class Recorder:
    """Collects bus events by type and dispatched actions, for assertions."""

    def __init__(self, ctx) -> None:
        self.events: list[object] = []
        self.actions: list = []
        ctx.bus.subscribe(object, self.events.append)
        ctx.actions.dispatch = self.actions.append  # tests inspect actions instead of running them

    def of(self, cls) -> list:
        return [e for e in self.events if isinstance(e, cls)]

    def clear(self) -> None:
        self.events.clear()
        self.actions.clear()


@pytest.fixture
def vctx():
    """A test context whose dispatched actions are recorded, never run."""
    from sightline.core.context import AppContext

    ctx = AppContext.for_tests()
    return ctx


@pytest.fixture
def rec(vctx):
    return Recorder(vctx)


@pytest.fixture
def user():
    from vision_synth import SyntheticUser

    return SyntheticUser(seed=1)


@pytest.fixture
def face_model(real_models_dir):
    path = real_models_dir / "face_landmarker" / "face_landmarker.task"
    if not path.exists():
        pytest.skip("face landmarker model not downloaded")
    return path
