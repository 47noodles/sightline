"""SendInputInjector event building, with SendInput replaced by a recorder (see conftest)."""

from __future__ import annotations

import pytest

from sightline.desktop import keys as k
from sightline.desktop import win32
from sightline.desktop.input import normalized_coordinate

TAG = win32.SIGHTLINE_INPUT_TAG


def flat(injector):
    return [event for batch in injector.sent for event in batch]


def test_normalised_coordinates_hit_every_pixel_exactly():
    # Windows maps n back to floor(n * extent / 65536); the pixel-centre formula survives that for any size.
    for left, extent in ((0, 1920), (-1280, 3200), (0, 7680), (-3840, 1366 + 3840), (0, 1)):
        for pixel in range(left, left + extent, max(1, extent // 997)):
            n = normalized_coordinate(pixel, left, extent)
            assert 0 <= n <= 65535
            assert left + (n * extent) // 65536 == pixel


def test_naive_formula_would_miss():
    # The common x * 65536 / width formula lands a pixel short; this is why the centre is used.
    misses = sum(1 for x in range(1920) if (int(x * 65536 / 1920) * 1920) // 65536 != x)
    assert misses > 0


def test_move_cursor_uses_absolute_virtual_desk_and_needs_no_fallback(recorded_input):
    recorded_input.move_cursor(1234, 567)
    (event,) = flat(recorded_input)
    assert event[0] == "mouse"
    assert event[4] == win32.MOUSEEVENTF_MOVE | win32.MOUSEEVENTF_ABSOLUTE | win32.MOUSEEVENTF_VIRTUALDESK
    assert event[5] == TAG
    assert tuple(recorded_input.cursor) == (1234, 567)
    assert recorded_input.set_cursor_calls == []
    assert recorded_input.last_injected() > 0


def test_move_cursor_clamps_and_falls_back_when_blocked(recorded_input, monkeypatch):
    recorded_input.move_cursor(99999, -50)
    assert tuple(recorded_input.cursor) == (1919, 0)
    # Simulate Windows dropping the injected move (an administrator window under the pointer).
    monkeypatch.setattr(recorded_input, "_send", lambda inputs: len(inputs))
    recorded_input.move_cursor(10, 20)
    assert recorded_input.set_cursor_calls == [(10, 20)]


def test_click_moves_then_presses_and_releases(recorded_input):
    recorded_input.click("left", 1, 300, 400)
    events = flat(recorded_input)
    assert events[0][4] & win32.MOUSEEVENTF_ABSOLUTE
    assert [e[4] for e in events[1:]] == [win32.MOUSEEVENTF_LEFTDOWN, win32.MOUSEEVENTF_LEFTUP]
    assert recorded_input.buttons_held() == set()


def test_double_click_and_right_click(recorded_input):
    recorded_input.click("left", 2)
    assert [e[4] for e in flat(recorded_input)] == [
        win32.MOUSEEVENTF_LEFTDOWN, win32.MOUSEEVENTF_LEFTUP, win32.MOUSEEVENTF_LEFTDOWN, win32.MOUSEEVENTF_LEFTUP,
    ]  # fmt: skip
    recorded_input.sent.clear()
    recorded_input.click("right")
    assert [e[4] for e in flat(recorded_input)] == [win32.MOUSEEVENTF_RIGHTDOWN, win32.MOUSEEVENTF_RIGHTUP]
    recorded_input.sent.clear()
    recorded_input.click("middle")
    assert [e[4] for e in flat(recorded_input)] == [win32.MOUSEEVENTF_MIDDLEDOWN, win32.MOUSEEVENTF_MIDDLEUP]


def test_swapped_buttons_keep_left_meaning_primary(recorded_input, monkeypatch):
    monkeypatch.setattr(win32, "GetSystemMetrics", lambda i: 1)
    recorded_input.click("left")
    assert [e[4] for e in flat(recorded_input)] == [win32.MOUSEEVENTF_RIGHTDOWN, win32.MOUSEEVENTF_RIGHTUP]


def test_unknown_button_is_rejected(recorded_input):
    with pytest.raises(ValueError):
        recorded_input.click("thumb")


def test_scroll_whole_notches_one_event_each(recorded_input):
    recorded_input.scroll(3)
    events = flat(recorded_input)
    assert [(e[3], e[4]) for e in events] == [(-120, win32.MOUSEEVENTF_WHEEL)] * 3
    recorded_input.sent.clear()
    recorded_input.scroll(-1)
    assert [e[3] for e in flat(recorded_input)] == [120]


def test_scroll_fractions_accumulate(recorded_input):
    for _ in range(9):
        recorded_input.scroll(0.1)
    assert flat(recorded_input) == []
    recorded_input.scroll(0.1)
    assert [e[3] for e in flat(recorded_input)] == [-120]


def test_smooth_scroll_sends_thirds_and_horizontal(recorded_input):
    recorded_input.scroll(0.0, 0.5, smooth=True)
    events = flat(recorded_input)
    assert [(e[3], e[4]) for e in events] == [(40, win32.MOUSEEVENTF_HWHEEL)]
    recorded_input.sent.clear()
    recorded_input.scroll(0.0, 0.5, smooth=True)  # 0.5 notch = 60; 20 was left over, so 80 is due
    assert [e[3] for e in flat(recorded_input)] == [80]


def test_key_combo_presses_in_order_and_releases_in_reverse(recorded_input):
    recorded_input.key_combo("ctrl+shift+t")
    keys_ = [(e[1], e[2], e[3]) for e in flat(recorded_input)]
    up = win32.KEYEVENTF_KEYUP
    assert keys_ == [
        (k.VK_LCONTROL, 0x1D, 0),
        (k.VK_LSHIFT, 0x2A, 0),
        (ord("T"), 0x14, 0),
        (ord("T"), 0x14, up),
        (k.VK_LSHIFT, 0x2A, up),
        (k.VK_LCONTROL, 0x1D, up),
    ]
    assert all(e[4] == TAG for e in flat(recorded_input))


def test_extended_keys_carry_the_flag(recorded_input):
    recorded_input.key_combo("win+left")
    events = flat(recorded_input)
    assert all(e[3] & win32.KEYEVENTF_EXTENDEDKEY for e in events)
    assert not any(e[3] & win32.KEYEVENTF_SCANCODE for e in events)  # vk + scan, so NumLock cannot change it


def test_repeat_holds_modifiers_once(recorded_input):
    recorded_input.key_combo("shift+down", 3)
    vks = [(e[1], bool(e[3] & win32.KEYEVENTF_KEYUP)) for e in flat(recorded_input)]
    assert vks.count((k.VK_LSHIFT, False)) == 1 and vks.count((k.VK_LSHIFT, True)) == 1
    assert vks.count((k.VK_DOWN, False)) == 3


def test_held_modifier_is_not_released_by_a_combo(recorded_input):
    recorded_input.key_down("shift")
    recorded_input.sent.clear()
    recorded_input.key_combo("shift+a")
    assert all(e[1] != k.VK_LSHIFT for e in flat(recorded_input))
    assert recorded_input.keys_held() == ["shift"]
    recorded_input.sent.clear()
    recorded_input.release_all()
    assert [(e[1], bool(e[3] & win32.KEYEVENTF_KEYUP)) for e in flat(recorded_input)] == [(k.VK_LSHIFT, True)]
    assert recorded_input.keys_held() == []


def test_release_all_lets_go_of_buttons_and_stuck_modifiers(recorded_input, monkeypatch):
    recorded_input.button_down("left")
    recorded_input.sent.clear()
    monkeypatch.setattr(win32, "GetAsyncKeyState", lambda vk: 0x8000 if vk == k.VK_RCONTROL else 0)
    recorded_input.release_all()
    events = flat(recorded_input)
    assert ("key", k.VK_RCONTROL, 0x1D, win32.KEYEVENTF_KEYUP | win32.KEYEVENTF_EXTENDEDKEY, TAG) in events
    assert any(e[0] == "mouse" and e[4] == win32.MOUSEEVENTF_LEFTUP for e in events)
    assert recorded_input.buttons_held() == set()


def test_type_text_unicode_with_surrogates_and_newlines(recorded_input):
    recorded_input.type_text("hi \U0001f600\r\nx\ty")
    events = flat(recorded_input)
    unicode_units = [e[2] for e in events if e[3] & win32.KEYEVENTF_UNICODE and not e[3] & win32.KEYEVENTF_KEYUP]
    assert unicode_units == [ord("h"), ord("i"), ord(" "), 0xD83D, 0xDE00, ord("x"), ord("y")]
    # Each unit is a down/up pair and the pairs keep their order (high surrogate first).
    pairs = [(e[2], bool(e[3] & win32.KEYEVENTF_KEYUP)) for e in events if e[3] & win32.KEYEVENTF_UNICODE]
    assert pairs[6:10] == [(0xD83D, False), (0xD83D, True), (0xDE00, False), (0xDE00, True)]
    # Newline is a real Enter key and tab a real Tab key.
    named = [e[1] for e in events if not e[3] & win32.KEYEVENTF_UNICODE and not e[3] & win32.KEYEVENTF_KEYUP]
    assert named == [k.VK_RETURN, k.VK_TAB]


def test_type_text_is_chunked(recorded_input):
    recorded_input.type_chunk = 10
    recorded_input.type_text("a" * 35)
    sizes = [len(batch) for batch in recorded_input.sent]
    assert sizes == [20, 20, 20, 10]


def test_bad_combo_does_not_press_anything(recorded_input):
    with pytest.raises(k.KeyComboError):
        recorded_input.key_combo("ctrl+nonsense")
    assert recorded_input.sent == []
