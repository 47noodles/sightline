from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # so tests can import tools.desktop_lock


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own settings and data folders.

    Tests that need real downloaded models read them from the developer's
    models folder through ``real_models_dir`` instead.
    """
    import os

    real = Path(os.environ.get("LOCALAPPDATA", "")) / "Sightline" / "models"
    monkeypatch.setenv("SIGHTLINE_REAL_MODELS", str(real))
    monkeypatch.setenv("SIGHTLINE_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture(autouse=True)
def message_boxes(monkeypatch):
    """No test shows a real Windows message box (this desktop is live): each message is recorded instead."""
    from sightline import app

    shown: list[str] = []
    monkeypatch.setattr(app, "_message_box", lambda text, title: shown.append(text))
    return shown


@pytest.fixture(autouse=True)
def run_key(monkeypatch):
    """No test writes the real Run key: Sightline's start-with-Windows entry goes to a dict instead."""
    from sightline import app

    values: dict[str, str] = {}

    class FakeRunKey:
        def get(self, name: str) -> str | None:
            return values.get(name)

        def set(self, name: str, command: str) -> None:
            values[name] = command

        def delete(self, name: str) -> None:
            values.pop(name, None)

    monkeypatch.setattr(app, "RunKey", FakeRunKey)
    return values


@pytest.fixture
def real_models_dir() -> Path:
    import os

    return Path(os.environ["SIGHTLINE_REAL_MODELS"])


@pytest.fixture
def ctx():
    from sightline.core.context import AppContext

    return AppContext.for_tests()


@pytest.fixture
def desktop_lock():
    """Hold the shared desktop lock for tests that touch the live desktop."""
    from tools.desktop_lock import desktop_lock as _lock

    with _lock("pytest"):
        yield
