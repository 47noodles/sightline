from __future__ import annotations

import pytest

from sightline.core.types import AppInfo, ElementInfo, Rect, WindowInfo
from sightline.desktop.matching import is_junk_app_name, match_apps, match_elements, match_windows, normalize

APPS = [
    AppInfo(name, f"shell:AppsFolder\\{name}", "uwp")
    for name in [
        "Word", "Excel", "PowerPoint", "Outlook", "Outlook (classic)", "Google Chrome", "Microsoft Edge",
        "File Explorer", "Files", "Settings", "WSL Settings", "Calculator", "Notepad", "Notepad++", "Terminal",
        "Windows PowerShell", "Command Prompt", "Microsoft Teams", "Steam", "Spotify", "Visual Studio Code",
        "Visual Studio Installer", "Microsoft Store", "Snipping Tool", "Task Manager", "Paint", "Photos",
    ]
]  # fmt: skip


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("word", "Word"),
        ("Word", "Word"),
        ("excel", "Excel"),
        ("chrome", "Google Chrome"),
        ("google chrome", "Google Chrome"),
        ("edge", "Microsoft Edge"),
        ("files", "File Explorer"),
        ("file explorer", "File Explorer"),
        ("settings", "Settings"),
        ("calculator", "Calculator"),
        ("notepad", "Notepad"),
        ("terminal", "Terminal"),
        ("outlook", "Outlook"),
        ("teams", "Microsoft Teams"),
        ("spotify", "Spotify"),
        ("the spotify app", "Spotify"),
        ("vs code", "Visual Studio Code"),
        ("visual studio code", "Visual Studio Code"),
        ("store", "Microsoft Store"),
        ("snipping tool", "Snipping Tool"),
        ("wsl settings", "WSL Settings"),
        ("power point", "PowerPoint"),
        ("calculater", "Calculator"),  # recogniser spelling slip
    ],
)
def test_app_matching(spoken, expected):
    matches = match_apps(spoken, APPS)
    assert matches, spoken
    assert matches[0].item.name == expected


@pytest.mark.parametrize("spoken", ["xyzzy", "ok", "", "   "])
def test_app_matching_rejects_nonsense(spoken):
    assert match_apps(spoken, APPS) == []


def test_alias_only_used_when_the_app_exists():
    apps = [AppInfo("Firefox", "x", "lnk")]
    assert match_apps("browser", apps)[0].item.name == "Firefox"
    assert match_apps("spotify", apps) == []


@pytest.mark.parametrize(
    ("name", "junk"),
    [
        ("Uninstall Spotify", True),
        ("Python 3.13 Documentation", True),
        ("Read Me", True),
        ("Release Notes", True),
        ("VLC Website", True),
        ("Firefox", False),
        ("Help and Support Center", False),
        ("License Manager", True),
        ("Calculator", False),
    ],
)
def test_junk_apps(name, junk):
    assert is_junk_app_name(name) is junk


WINDOWS = [
    WindowInfo(1, "Inbox - me@example.com - Outlook", "olk.exe", Rect(0, 0, 10, 10)),
    WindowInfo(2, "Budget 2026.xlsx - Excel", "EXCEL.EXE", Rect(0, 0, 10, 10)),
    WindowInfo(3, "YouTube - Google Chrome", "chrome.exe", Rect(0, 0, 10, 10)),
    WindowInfo(4, "Report - Word", "WINWORD.EXE", Rect(0, 0, 10, 10)),
    WindowInfo(5, "Downloads - File Explorer", "explorer.exe", Rect(0, 0, 10, 10)),
    WindowInfo(6, "Gmail - Google Chrome", "chrome.exe", Rect(0, 0, 10, 10)),
    WindowInfo(7, "Untitled - Notepad", "Notepad.exe", Rect(0, 0, 10, 10)),
]


@pytest.mark.parametrize(
    ("spoken", "hwnd"),
    [
        ("outlook", 1),
        ("inbox", 1),
        ("mail", 1),
        ("excel", 2),
        ("budget", 2),
        ("chrome", 3),
        ("youtube", 3),
        ("gmail", 6),
        ("word", 4),
        ("report", 4),
        ("files", 5),
        ("downloads", 5),
        ("file explorer", 5),
        ("notepad", 7),
        ("notepad window", 7),
    ],
)
def test_window_matching(spoken, hwnd):
    matches = match_windows(spoken, WINDOWS)
    assert matches, spoken
    assert matches[0].item.hwnd == hwnd


def test_window_matching_keeps_front_to_back_order_among_equals():
    matches = match_windows("chrome", WINDOWS)
    assert [m.item.hwnd for m in matches[:2]] == [3, 6]


def test_window_matching_rejects_unknown():
    assert match_windows("spotify", WINDOWS) == []


def element(name, kind="Button"):
    return ElementInfo(Rect(0, 0, 10, 10), name, kind, clickable=True)


def test_element_matching():
    elements = [element("Save"), element("Save as", "MenuItem"), element("Look at this book", "Text"), element("Settings"), element("")]
    assert [m.item.name for m in match_elements("save", elements)][:2] == ["Save", "Save as"]
    assert match_elements("save as", elements)[0].item.name == "Save as"
    assert match_elements("ok", elements) == []  # two letters never fuzzy-match inside "book"
    assert match_elements("setings", elements)[0].item.name == "Settings"


def test_normalize():
    assert normalize("  Microsoft® Word  ") == "microsoft word"
    assert normalize("Tom & Jerry") == "tom and jerry"
