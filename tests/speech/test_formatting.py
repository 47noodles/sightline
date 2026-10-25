"""Dictation formatting: punctuation words, capitals, spacing, code formatters and spelling."""

from __future__ import annotations

import pytest

from sightline.speech.formatting import (
    apply_formatters,
    auto_capitalize,
    dictation_text,
    needs_space_between,
    place_text,
    spell,
    state_after,
)


def typed(raw: str, before: str | None = None, **kwargs: bool) -> str:
    """What dictating ``raw`` after ``before`` types."""
    return place_text(dictation_text(raw, **kwargs), before)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("hello world", "hello world"),
        ("Hello world.", "hello world"),  # recogniser full stops are dropped by default
        ("Hello comma world full stop how are you question mark", "hello, world. how are you?"),
        ("I have twenty three apples", "I have 23 apples"),
        ("new paragraph Thanks", "\n\nThanks"),
        ("open quote hello close quote", '"hello"'),
        ("Comma.", ","),
        ("smiley face", ":-)"),
        ("Paris is lovely", "Paris is lovely"),  # names keep their capital
        ("The cat sat", "the cat sat"),  # a common word capitalised only because it came first
        ("NASA launched it", "NASA launched it"),
        ("[MUSIC]", ""),
    ],
)
def test_dictation_text(raw: str, expected: str) -> None:
    assert dictation_text(raw) == expected


def test_recogniser_punctuation_can_be_kept() -> None:
    assert dictation_text("Hello, world.", keep_recogniser_punctuation=True) == "hello, world."


def test_spoken_punctuation_can_be_off() -> None:
    assert dictation_text("full stop", spoken_punctuation=False) == "full stop"


def test_numbers_can_stay_words() -> None:
    assert dictation_text("twenty three", convert_numbers=False) == "twenty three"


@pytest.mark.parametrize(
    ("raw", "before", "expected"),
    [
        ("hello", None, "Hello"),
        ("hello", "", "Hello"),
        ("hello", "Dear Sam", " hello"),
        ("hello", "Dear Sam ", "hello"),
        ("hello", "Hi.", " Hello"),
        ("comma", "Dear Sam", ","),
        ("thanks", "Dear Sam,\n", "Thanks"),
        ("i think so", "Yes.", " I think so"),
        ("world", "(", "world"),
        ("okay", "e.g.", " okay"),
        ("question mark", "really", "?"),
        ("hello comma world full stop how are you question mark", None, "Hello, world. How are you?"),
        ("new line thanks", "Dear Sam,", "\nThanks"),
    ],
)
def test_place_text(raw: str, before: str | None, expected: str) -> None:
    assert typed(raw, before) == expected


def test_spacing_rules() -> None:
    assert needs_space_between("hello", "world")
    assert not needs_space_between("hello", ",")
    assert not needs_space_between("(", "x")
    assert not needs_space_between("hello ", "world")
    assert not needs_space_between("", "world")


def test_auto_capitalize_state() -> None:
    text, state = auto_capitalize("hello. world", "sentence start")
    assert text == "Hello. World"
    assert state is None
    _, state = auto_capitalize("done.", None)
    assert state == "sentence start"
    assert state_after(None) == "sentence start"
    assert state_after("Hi there") is None
    assert state_after("Hi there.") == "sentence start"


def test_no_capital_after_abbreviation() -> None:
    assert typed("such as cats", "e.g.") == " such as cats"


@pytest.mark.parametrize(
    ("formatter", "expected"),
    [
        ("SNAKE_CASE", "user_name_list"),
        ("PRIVATE_CAMEL_CASE", "userNameList"),
        ("PUBLIC_CAMEL_CASE", "UserNameList"),
        ("DASH_SEPARATED", "user-name-list"),
        ("ALL_CAPS,SNAKE_CASE", "USER_NAME_LIST"),
        ("DOT_SEPARATED", "user.name.list"),
        ("NO_SPACES", "usernamelist"),
        ("DOUBLE_QUOTED_STRING", '"user name list"'),
        ("DOUBLE_UNDERSCORE", "user__name__list"),
        ("CAPITALIZE_ALL_WORDS", "User Name List"),
        ("CAPITALIZE", "User name list"),
        ("ALL_CAPS", "USER NAME LIST"),
        ("ALL_LOWERCASE", "user name list"),
    ],
)
def test_formatters(formatter: str, expected: str) -> None:
    assert apply_formatters("user name list", formatter) == expected


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("alpha bravo charlie", "abc"),
        ("a b c", "abc"),
        ("air bat cap", "abc"),
        ("capital alpha bravo", "Ab"),
        ("h e l l o", "hello"),
        ("bee sea", "bc"),
        ("alpha dash bravo", "a-b"),
        ("1 2 3", "123"),
        ("hello world", None),
        ("", None),
    ],
)
def test_spell(words: str, expected: str | None) -> None:
    assert spell(words) == expected
