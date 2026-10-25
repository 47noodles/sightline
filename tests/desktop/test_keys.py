from __future__ import annotations

import pytest

from sightline.desktop import keys as k
from sightline.desktop.keys import KeyComboError, UsKeyResolver, parse_combo, parse_key, resolve_chord


def names(combo: str) -> tuple[list[str], list[str]]:
    chord = parse_combo(combo)
    return [m.name for m in chord.modifiers], [x.name for x in chord.keys]


@pytest.mark.parametrize(
    ("combo", "mods", "mains"),
    [
        ("ctrl+shift+t", ["ctrl", "shift"], ["t"]),
        ("Ctrl + Shift + T", ["ctrl", "shift"], ["t"]),
        ("win+d", ["win"], ["d"]),
        ("alt+f4", ["alt"], ["f4"]),
        ("f5", [], ["f5"]),
        ("F24", [], ["f24"]),
        ("page down", [], ["pagedown"]),
        ("Page-Down", [], ["pagedown"]),
        ("pgup", [], ["pageup"]),
        ("volume up", [], ["volumeup"]),
        ("volume mute", [], ["volumemute"]),
        ("play pause", [], ["playpause"]),
        ("next track", [], ["nexttrack"]),
        ("a", [], ["a"]),
        ("Z", [], ["z"]),
        ("7", [], ["7"]),
        ("seven", [], ["7"]),
        ("ctrl++", ["ctrl"], ["plus"]),
        ("ctrl+plus", ["ctrl"], ["plus"]),
        ("+", [], ["plus"]),
        ("ctrl+-", ["ctrl"], ["minus"]),
        ("ctrl+,", ["ctrl"], ["comma"]),
        ("ctrl+/", ["ctrl"], ["/"]),
        ("shift page down", ["shift"], ["pagedown"]),
        ("control shift escape", ["ctrl", "shift"], ["escape"]),
        ("numpad 7", [], ["numpad7"]),
        ("numpad seven", [], ["numpad7"]),
        ("keypad enter", [], ["numpadenter"]),
        ("numpad plus", [], ["numpadadd"]),
        ("left bracket", [], ["leftbracket"]),
        ("backtick", [], ["backtick"]),
        ("windows", ["win"], []),
        ("right alt", ["ralt"], []),
        ("context menu", [], ["apps"]),
        ("print screen", [], ["printscreen"]),
        ("escape", [], ["escape"]),
        ("esc", [], ["escape"]),
        ("ctrl+ctrl+c", ["ctrl"], ["c"]),
        ("up arrow", [], ["up"]),
    ],
)
def test_parse_combo(combo, mods, mains):
    assert names(combo) == (mods, mains)


@pytest.mark.parametrize("bad", ["", "   ", "ctrl+blorp", "hyper+q", "page sideways", "sleep", "power"])
def test_unknown_keys_raise_plain_english(bad):
    with pytest.raises(KeyComboError) as info:
        parse_combo(bad)
    message = str(info.value)
    assert message and "—" not in message
    assert message.startswith(("I do not know the key", "No key was given"))


def test_extended_flags_and_scan_codes_for_navigation_keys():
    mods, mains = resolve_chord(parse_combo("ctrl+shift+left"), UsKeyResolver())
    assert [(s.vk, s.scan, s.extended) for s in mods] == [(k.VK_LCONTROL, 0x1D, False), (k.VK_LSHIFT, 0x2A, False)]
    assert [(s.vk, s.scan, s.extended) for s in mains] == [(k.VK_LEFT, 0x4B, True)]
    for name, vk, scan in [
        ("delete", k.VK_DELETE, 0x53),
        ("home", k.VK_HOME, 0x47),
        ("page down", k.VK_NEXT, 0x51),
        ("win", k.VK_LWIN, 0x5B),
        ("right ctrl", k.VK_RCONTROL, 0x1D),
        ("volume up", k.VK_VOLUME_UP, 0x30),
        ("play pause", k.VK_MEDIA_PLAY_PAUSE, 0x22),
        ("numpad enter", k.VK_RETURN, 0x1C),
        ("numpad slash", k.VK_DIVIDE, 0x35),
    ]:
        key = parse_key(name)
        assert (key.vk, key.scan, key.extended) == (vk, scan, True), name


def test_non_extended_keys():
    for name in ("enter", "escape", "tab", "space", "backspace", "f5", "numpad 7", "shift", "ctrl", "alt"):
        assert parse_key(name).extended is False, name
    # Numpad digits keep their own virtual key, so NumLock state cannot turn them into Home or arrows.
    assert parse_key("numpad 7").vk == k.VK_NUMPAD0 + 7


def test_letters_and_digits_take_scan_codes_from_the_layout():
    _mods, mains = resolve_chord(parse_combo("ctrl+a"), UsKeyResolver())
    assert (mains[0].vk, mains[0].scan) == (ord("A"), 0x1E)
    _mods, mains = resolve_chord(parse_combo("1"), UsKeyResolver())
    assert (mains[0].vk, mains[0].scan) == (ord("1"), 0x02)


def test_symbols_add_shift_when_the_layout_needs_it():
    mods, mains = resolve_chord(parse_combo("ctrl+?"), UsKeyResolver())
    assert [m.name for m in mods] == ["ctrl", "shift"]
    assert mains[0].vk == k.VK_OEM_2
    mods, _ = resolve_chord(parse_combo("ctrl+shift+?"), UsKeyResolver())
    assert [m.name for m in mods] == ["ctrl", "shift"]  # shift not added twice
    mods, mains = resolve_chord(parse_combo("exclamation"), UsKeyResolver())
    assert [m.name for m in mods] == ["shift"] and mains[0].vk == ord("1")


def test_plus_means_the_plus_key_without_shift():
    mods, mains = resolve_chord(parse_combo("ctrl+plus"), UsKeyResolver())
    assert [m.name for m in mods] == ["ctrl"]
    assert mains[0].vk == k.VK_OEM_PLUS


class _GermanLike(UsKeyResolver):
    """A layout where "/" is shift+7, like German."""

    def vk_for_char(self, ch):
        if ch == "/":
            return (ord("7"), True, False, False)
        return super().vk_for_char(ch)


def test_symbols_follow_the_active_layout():
    mods, mains = resolve_chord(parse_combo("ctrl+/"), _GermanLike())
    assert [m.name for m in mods] == ["ctrl", "shift"]
    assert mains[0].vk == ord("7")


def test_key_names_lists_every_family():
    all_names = k.key_names()
    for expected in ("a", "0", "enter", "pagedown", "f24", "volumeup", "numpad5", "slash", "plus"):
        assert expected in all_names
