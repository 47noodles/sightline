"""Turning recogniser output into the plain form the grammar matches."""

from __future__ import annotations

import pytest

from sightline.speech.normalize import clean_transcript, is_probably_noise, normalize_command, spoken_forms, tokens


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("Scroll down three.", "scroll down 3"),
        ("SCROLL DOWN", "scroll down"),
        ("Um, click.", "click"),
        ("uh scroll up", "scroll up"),
        ("Right-click.", "right click"),
        ("Write click", "right click"),
        ("rite click", "right click"),
        ("Press Control+Shift+T.", "press control shift t"),
        ("Go to tab two", "go to tab 2"),
        ("Tab four times", "tab 4 times"),
        ("Alt-Tab", "alt tab"),
        ("New tap", "new tab"),
        ("close tap", "close tab"),
        ("Dictation mod", "dictation mode"),
        ("Re-center.", "recenter"),
        ("recentre", "recenter"),
        ("Scrolled up", "scroll up"),
        ("OK.", "okay"),
        ("Undo 3 times.", "undo 3 times"),
        ("Volume up twenty", "volume up 20"),
        ("Twenty-one", "21"),
        ("Hello, world!", "hello world"),
        ("go to wikipedia.org", "go to wikipedia.org"),
        ("  Click   on   Save  ", "click on save"),
        ("[MUSIC]", ""),
        ("(laughs)", ""),
        ("", ""),
        ("...", ""),
    ],
)
def test_normalize_command(heard: str, expected: str) -> None:
    assert normalize_command(heard) == expected


def test_normalize_is_idempotent() -> None:
    for text in ("Scroll down three.", "Press Control+Shift+T.", "go to wikipedia.org", "Right-click."):
        once = normalize_command(text)
        assert normalize_command(once) == once


def test_clean_transcript_removes_annotations() -> None:
    assert clean_transcript("Hello [MUSIC] there") == "Hello there"
    assert clean_transcript("<unk> click") == "click"
    assert clean_transcript("*cough* save") == "save"


def test_tokens() -> None:
    assert tokens(normalize_command("Scroll down three.")) == ["scroll", "down", "3"]


@pytest.mark.parametrize(
    ("name", "forms"),
    [
        ("Google Chrome", {"google chrome", "chrome"}),
        ("Microsoft Visual Studio Code", {"microsoft visual studio code", "visual studio code"}),
        ("Notepad++", {"notepad"}),
        ("OBS Studio 30.1", {"obs studio"}),
        ("WinRAR (x64)", {"winrar", "win rar"}),
        ("FileZilla", {"filezilla", "file zilla"}),
    ],
)
def test_spoken_forms(name: str, forms: set[str]) -> None:
    assert forms <= set(spoken_forms(name))


@pytest.mark.parametrize("text", ["you", "thank you", "bye", "the"])
def test_short_hallucinations_are_noise(text: str) -> None:
    assert is_probably_noise(text, 300)


def test_noise_only_when_short() -> None:
    # The same words said deliberately and at length are kept.
    assert not is_probably_noise("thank you", 1200)
    assert not is_probably_noise("scroll down", 300)
    assert is_probably_noise("", None)
