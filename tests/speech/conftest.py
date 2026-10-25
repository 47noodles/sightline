from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from speech_helpers import bag_of_words_index

from sightline.speech.grammar import BuildContext, build_commands, sleep_and_wake
from sightline.speech.interpret import Decision, Interpreter, Situation
from sightline.speech.matcher import Grammar
from sightline.speech.names import DynamicNames

APPS = ["Google Chrome", "Notepad", "File Explorer", "Calculator", "Microsoft Word", "Visual Studio Code", "Spotify", "Microsoft Edge"]
WINDOWS = [
    ("Untitled - Notepad", "notepad.exe", 101),
    ("New Tab - Google Chrome", "chrome.exe", 102),
    ("Inbox - Outlook", "olk.exe", 103),
    ("Downloads", "explorer.exe", 104),
]
ELEMENTS = ["Save", "File", "Edit", "View", "Documents", "Back", "Reload", "Sign in", "Submit", "Cancel"]


@pytest.fixture(scope="session")
def grammar() -> Grammar:
    return Grammar(build_commands() + sleep_and_wake("wake up", "go to sleep"))


@pytest.fixture(scope="session")
def names() -> DynamicNames:
    names = DynamicNames()
    names.set_apps(APPS)
    names.set_windows(WINDOWS)
    names.set_elements(ELEMENTS)
    return names


@pytest.fixture
def bctx(names: DynamicNames) -> BuildContext:
    ctx = BuildContext()
    ctx.resolve_app = names.resolve_app
    ctx.resolve_window = names.resolve_window
    ctx.resolve_element = names.resolve_element
    return ctx


@pytest.fixture
def interp(grammar: Grammar) -> Interpreter:
    return Interpreter(grammar)


Result = list[tuple[str, dict[str, Any]]]


def decision_actions(decision: Decision) -> Result:
    """Actions a decision leads to, with typed text shown as ("type", text) before formatting."""
    out: Result = []
    for outcome in decision.outcomes:
        if outcome.type_request is not None:
            out.append(("type", {"text": outcome.type_request.text}))
        out.extend((a.name, dict(a.args)) for a in outcome.actions)
    return out


@pytest.fixture
def run(interp: Interpreter, bctx: BuildContext) -> Callable[..., Result]:
    """Interpret ``text`` and return its actions; [] when it is not a command."""

    def _run(text: str, *, mode: str = "command", overlay: str | None = None, editable: bool = False, apply: bool = True) -> Result:
        decision = interp.decide(text, Situation(mode=mode, overlay=overlay, editable=editable), bctx, semantic=False)
        if decision.kind != "command":
            return []
        if apply:
            for outcome in decision.outcomes:
                bctx.apply(outcome)
        return decision_actions(decision)

    return _run


@pytest.fixture
def decide(interp: Interpreter, bctx: BuildContext) -> Callable[..., Decision]:
    def _decide(
        text: str,
        *,
        mode: str = "auto",
        overlay: str | None = None,
        editable: bool = False,
        password: bool = False,
        audio_ms: float | None = None,
        away: str = "",
        choices: int = 0,
        offer: bool = False,
    ) -> Decision:
        situation = Situation(mode=mode, overlay=overlay, editable=editable, password=password, away=away, choices=choices, offer=offer)
        return interp.decide(text, situation, bctx, audio_ms=audio_ms, semantic=False)

    return _decide


@pytest.fixture
def semantic_interp(grammar: Grammar) -> Interpreter:
    return Interpreter(grammar, bag_of_words_index(grammar))
