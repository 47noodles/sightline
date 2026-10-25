"""Finding spoken phrases in text near the caret, and the arrow-key walks that reach them.  Pure logic."""

from __future__ import annotations

from sightline.desktop.textedit import (
    MAX_WALK_STEPS,
    Span,
    TextQuery,
    find_all,
    key_steps,
    locate,
    nearest,
    same_text,
    walk_plan,
)

TEXT = "The quick brown fox jumps over the lazy dog.  The quick, brown cat sleeps."


def words(text: str, span: Span) -> str:
    return text[span.start : span.end]


def test_matching_ignores_case_and_punctuation_between_words():
    spans = find_all(TEXT, "QUICK brown")
    assert [words(TEXT, s) for s in spans] == ["quick brown", "quick, brown"]


def test_only_whole_words_match():
    assert find_all("concatenate the cat", "cat") == [Span(16, 19)]
    assert find_all("cats and dogs", "cat") == []


def test_apostrophes_match_either_way():
    assert [words("I don’t know", s) for s in find_all("I don’t know", "don't")] == ["don’t"]
    assert [words("I don't know", s) for s in find_all("I don't know", "don’t")] == ["don't"]


def test_a_phrase_without_words_finds_nothing():
    assert find_all(TEXT, "  ,, ") == []


def test_nearest_prefers_the_occurrence_before_the_caret_on_a_tie():
    spans = [Span(0, 3), Span(10, 13)]
    assert nearest(spans, 6) == Span(0, 3)  # three characters each way
    assert nearest(spans, 7) == Span(10, 13)  # the later one is nearer
    assert nearest([Span(4, 9)], 6) == Span(4, 9)  # the caret inside a match


def test_select_picks_the_nearest_occurrence():
    caret = len(TEXT)  # dictating at the end
    assert words(TEXT, locate(TEXT, caret, TextQuery("select", "the quick brown"))) == "The quick, brown"
    assert words(TEXT, locate(TEXT, 0, TextQuery("select", "the quick brown"))) == "The quick brown"
    assert locate(TEXT, caret, TextQuery("select", "purple")) == "purple"


def test_select_range_spans_both_phrases():
    caret = len(TEXT)
    span = locate(TEXT, caret, TextQuery("range", "jumps", end_phrase="dog"))
    assert words(TEXT, span) == "jumps over the lazy dog"
    # Said the wrong way round, it still selects between them.
    span = locate(TEXT, caret, TextQuery("range", "lazy", end_phrase="fox"))
    assert words(TEXT, span) == "fox jumps over the lazy"
    assert locate(TEXT, caret, TextQuery("range", "jumps", end_phrase="unicorn")) == "unicorn"
    assert locate(TEXT, caret, TextQuery("range", "unicorn", end_phrase="dog")) == "unicorn"


def test_caret_goes_before_or_after_the_phrase():
    caret = len(TEXT)
    after = locate(TEXT, caret, TextQuery("caret", "lazy dog", where="after"))
    before = locate(TEXT, caret, TextQuery("caret", "lazy dog", where="before"))
    assert after == Span(TEXT.index("lazy dog") + 8, TEXT.index("lazy dog") + 8)
    assert before == Span(TEXT.index("lazy dog"), TEXT.index("lazy dog"))


def test_walk_selects_text_before_the_caret_from_its_end():
    text = "The quick brown fox"
    span = locate(text, len(text), TextQuery("select", "quick"))
    assert walk_plan(text, len(text), span, select=True, collapse=False) == (("left", 10), ("shift+left", 5))


def test_walk_selects_text_after_the_caret_and_around_it():
    text = "The quick brown fox"
    assert walk_plan(text, 0, Span(4, 9), select=True, collapse=False) == (("right", 4), ("shift+right", 5))
    assert walk_plan(text, 6, Span(4, 9), select=True, collapse=True) == (("left", 1), ("left", 2), ("shift+right", 5))


def test_walk_moves_the_caret_only():
    text = "The quick brown fox"
    assert walk_plan(text, 19, Span(9, 9), select=False, collapse=False) == (("left", 10),)
    assert walk_plan(text, 2, Span(9, 9), select=False, collapse=False) == (("right", 7),)
    assert walk_plan(text, 9, Span(9, 9), select=False, collapse=False) == ()


def test_a_crlf_pair_is_one_arrow_press():
    text = "one\r\ntwo three"
    assert key_steps(text, 0, len(text)) == len(text) - 1
    span = locate(text, len(text), TextQuery("select", "one"))
    assert walk_plan(text, len(text), span, select=True, collapse=False) == (("left", 10), ("shift+left", 3))


def test_walks_longer_than_the_limit_are_refused():
    text = "word " + "x" * (MAX_WALK_STEPS + 10)
    span = locate(text, len(text), TextQuery("select", "word"))
    assert walk_plan(text, len(text), span, select=True, collapse=False) is None


def test_same_text_ignores_line_break_style():
    assert same_text("a\r\nb  c", "a\nb c")
    assert not same_text("abc", "abd")
