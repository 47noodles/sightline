"""The shared risk policy (``core.risk``) and which web addresses may be opened (``core.urls``).

Review S-04, S-15 and S-09: one list of what is hard to undo, used by speech (no paraphrase
runs it) and by the countdown (it waits), and only web pages and e-mail links ever reach
the browser or the Windows shell.
"""

from __future__ import annotations

import pytest

from sightline.core.risk import RISKY_KEYS, app_risk, assess, click_risk, key_risk, normalize_combo, pretty_combo, risk_of, split_combo
from sightline.core.types import Action, FocusInfo
from sightline.core.urls import REFUSED_MESSAGE, has_scheme, web_address

CONSOLE = FocusInfo(hwnd=5, process="cmd.exe", editable=True)
TERMINAL = FocusInfo(hwnd=5, process=r"C:\Program Files\WindowsApps\Microsoft.WindowsTerminal\WindowsTerminal.exe", editable=True)
NOTEPAD = FocusInfo(hwnd=6, process="notepad.exe", editable=True)

# =========================================================================== key combos


@pytest.mark.parametrize(
    "spoken, combo",
    [
        ("Alt + F4", "alt+f4"),
        ("f4+alt", "alt+f4"),
        ("alt f4", "alt+f4"),
        ("Control+W", "ctrl+w"),
        ("left control+w", "ctrl+w"),
        ("shift+ctrl+w", "ctrl+shift+w"),
        ("Del", "delete"),
        ("shift+del", "shift+delete"),
        ("ctrl+Return", "ctrl+enter"),
        ("windows+r", "win+r"),
        ("alt+f4+a", "alt+f4+a"),
        ("", ""),
    ],
)
def test_one_spelling_for_every_combo(spoken, combo):
    assert normalize_combo(spoken) == combo


def test_combos_split_into_modifiers_and_keys():
    assert split_combo("shift + Control + W") == (("ctrl", "shift"), ("w",))
    assert split_combo("alt+f4+a") == (("alt",), ("f4", "a"))
    assert pretty_combo("ctrl+shift+delete") == "Control+Shift+Delete"


@pytest.mark.parametrize(
    "combo, doing",
    [
        ("alt+f4", "Closing this window"),
        ("ctrl+w", "Closing this tab"),
        ("ctrl+f4", "Closing this tab"),
        ("ctrl+shift+w", "Closing every tab in this window"),
        ("delete", "Pressing Delete"),
        ("shift+delete", "Pressing Shift+Delete"),
        ("ctrl+shift+delete", "Pressing Control+Shift+Delete"),
        ("ctrl+enter", "Pressing Control+Enter"),
        ("alt+s", "Pressing Alt+S"),
        ("ctrl+q", "Pressing Control+Q"),
        ("ctrl+shift+q", "Pressing Control+Shift+Q"),
        ("win+r", "Opening the Run box"),
        ("ctrl+shift+escape", "Opening Task Manager"),
        ("win+x", "Opening the Start button's menu"),
        # Extra keys or modifiers do not slip past (alt+f4+a pressed all of them together).
        ("alt+f4+a", "Closing this window"),
        ("a+alt+f4", "Closing this window"),
        ("ctrl+alt+w", "Closing this tab"),
        ("ctrl+alt+shift+w", "Closing every tab in this window"),
        ("Control + W", "Closing this tab"),
        ("Del", "Pressing Delete"),
    ],
)
def test_risky_keys(combo, doing):
    risk = key_risk(combo)
    assert risk is not None and risk.doing == doing, combo


@pytest.mark.parametrize("combo", ["ctrl+a", "ctrl+c", "ctrl+v", "alt+tab", "win+d", "enter", "shift+enter", "backspace", "ctrl+delete", "tab", "f5", "q", "w"])
def test_everyday_keys_are_not_risky(combo):
    assert key_risk(combo) is None, combo


def test_enter_is_risky_only_in_a_command_window():
    assert key_risk("enter", CONSOLE).kind == "console"
    assert key_risk("shift+enter", CONSOLE) is not None
    assert key_risk("enter", TERMINAL) is not None, "a full path names the process too"
    assert key_risk("enter", NOTEPAD) is None and key_risk("enter") is None


def test_every_listed_combo_is_in_its_own_spelling():
    assert all(normalize_combo(combo) == combo for combo in RISKY_KEYS)


# =========================================================================== other actions


@pytest.mark.parametrize("name", ["Command Prompt", "cmd", "cmd.exe", "Windows PowerShell", "PowerShell 7", "pwsh", "Terminal", "Windows Terminal",
                                  "wt", "Registry Editor", "regedit", "Run", "Task Manager", "bash", "WSL", "Services", "Computer Management"])
def test_shells_and_system_tools_are_risky_to_open(name):
    risk = app_risk(name)
    assert risk is not None and risk.kind == "shell" and risk.doing == f"Opening {name}", name


@pytest.mark.parametrize("name", ["Notepad", "Spotify", "Temple Run", "PowerToys", "Microsoft Word", "Calculator", "", None])
def test_ordinary_apps_are_not(name):
    assert app_risk(name) is None, name


@pytest.mark.parametrize("name", ["Delete", "Remove", "Discard draft", "Send", "Submit", "Pay now", "Buy now", "Purchase", "Checkout",
                                  "Empty Recycle Bin", "Remove from list", "Place order", "Erase"])
def test_buttons_that_delete_send_or_pay_are_risky(name):
    risk = click_risk(name)
    assert risk is not None and risk.doing == f"Clicking “{name}”", name


@pytest.mark.parametrize("name", ["Deleted Items", "Sent", "Sent Items", "Payment", "Payments", "Recycle Bin", "Sender", "Save", "", None])
def test_other_buttons_are_not(name):
    assert click_risk(name) is None, name


def test_assess_covers_every_kind_of_action():
    def risk(action_name, focus=None, target=None, **args):
        return risk_of(Action(action_name, args), focus, target=target)

    assert risk("window.close") == "Closing this window"
    assert risk("app.quit") == "Quitting Sightline"
    assert risk("key.press", combo="ctrl+w") == "Closing this tab"
    assert risk("key.press", key="Delete") == "Pressing Delete"
    assert risk("text.type", CONSOLE, text="del *.*\n") == "Typing a line into a command window"
    assert risk("text.type", CONSOLE, text="del *.*\r") is not None
    assert risk("text.type", CONSOLE, text="dir") is None
    assert risk("text.type", NOTEPAD, text="Dear Sam,\n") is None
    assert risk("app.open", name="Command Prompt") == "Opening Command Prompt"
    assert risk("element.click", name="Send") == "Clicking “Send”"
    assert risk("mouse.click", target="Delete") == "Clicking “Delete”"
    assert risk("mouse.click") is None and risk("mouse.click", target="Sent Items") is None
    assert risk("window.minimize") is None and risk("web.go", url="bbc.co.uk") is None


def test_odd_arguments_never_raise():
    for args in ({"combo": None}, {"combo": 5}, {"key": ["alt", "f4"]}, {"text": None}, {"text": 7}, {"name": None}, {"name": 3.5}):
        for name in ("key.press", "text.type", "app.open", "element.click", "mouse.click"):
            assess(Action(name, args), CONSOLE, target=args.get("name"))  # returns without raising, whatever it says


def test_speech_and_the_countdown_share_the_list():
    from sightline.speech import interpret

    try:
        from sightline.ui.overlays import confirm
    except ImportError:  # pragma: no cover - Qt missing
        confirm = None
    assert interpret.RISKY_KEYS is RISKY_KEYS and interpret.normalize_combo is normalize_combo
    if confirm is not None:
        assert confirm.RISKY_KEYS is RISKY_KEYS and confirm.risk_of is risk_of


# =========================================================================== web addresses (review S-09)


@pytest.mark.parametrize(
    "url, opened",
    [
        ("bbc.co.uk", "https://bbc.co.uk"),
        ("bbc.co.uk/news", "https://bbc.co.uk/news"),
        ("https://www.bbc.co.uk/news", "https://www.bbc.co.uk/news"),
        ("http://example.com", "http://example.com"),
        ("HTTPS://EXAMPLE.COM", "HTTPS://EXAMPLE.COM"),
        ("mailto:sam@example.com", "mailto:sam@example.com"),
        ("localhost:3000", "https://localhost:3000"),
        ("example.com:8080/x", "https://example.com:8080/x"),
        ("//example.com", "https://example.com"),
    ],
)
def test_web_pages_and_mail_links_may_be_opened(url, opened):
    assert web_address(url) == opened


@pytest.mark.parametrize(
    "url",
    [
        "file:///c:/windows/system32/calc.exe",
        "ms-settings:privacy",
        "search-ms:query=x",
        "javascript:alert(1)",
        "data:text/html,hi",
        "ftp://example.com/x",
        "shell:startup",
        r"c:\windows\system32\calc.exe",
        r"\\server\share\run.exe",
        "https://",
        "http:example.com",
        "mailto:",
        "https://example.com/\nmore",
        "",
        None,
    ],
)
def test_everything_else_is_refused(url):
    assert web_address(url) is None, url


def test_scheme_detection_and_the_message():
    assert has_scheme("https://x.example") and has_scheme("mailto:a@b.c") and has_scheme("ms-settings:x")
    assert not has_scheme("bbc.co.uk") and not has_scheme("localhost:3000")
    assert REFUSED_MESSAGE == "Sightline only opens web pages and e-mail links."
