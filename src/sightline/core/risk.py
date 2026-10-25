"""One policy for actions that are hard to reverse (review findings S-04, S-15).

Speech never runs these from a paraphrase or an alias (they become "Say 1 to
confirm"), and the countdown (``ui/overlays/confirm.py``) holds them for a
moment when they come from the eyes, dwell or voice.  ``risk_of`` says what
makes an action risky in the words the countdown shows ("Closing this
window"), or None when it is not risky.

What counts:

* closing windows and quitting: ``window.close``, ``app.quit``;
* keys that close, delete, send or quit: Alt+F4, Ctrl+W, Ctrl+Shift+W,
  Ctrl+F4, Delete, Shift+Delete, Ctrl+Shift+Delete (clears browsing data),
  Ctrl+Enter, Alt+S, Ctrl+Q, and Windows+R (the Run box);
* opening a shell or a system tool by name (``app.open`` of Command Prompt,
  PowerShell, Terminal, the Registry Editor, Run, Task Manager and the like);
* Enter into a console: ``text.type`` with a new line, or a key press with
  Enter, while the focused process is a command window;
* a click on something whose name means delete, remove, discard, send,
  submit, pay, buy or empty the recycle bin: ``element.click`` by name, or
  any click whose target the caller names (``target``).

Key combos are normalised first, however they are spelled: "Control + W",
"f4+alt" and "alt f4" are the same keys.  Extra keys do not hide a risky
chord ("alt+f4+a" still presses Alt+F4), and nor does an extra modifier on a
risky shortcut ("ctrl+alt+w" still holds Ctrl+W).  A modifier added to a
single risky key makes a different shortcut ("ctrl+delete" deletes a word).

Everything here is pure and safe on any thread.  It never raises for odd
argument values: the countdown runs it on every action, and fails closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .types import Action, FocusInfo

# =========================================================================== key combos

MODIFIER_ORDER = {"ctrl": 0, "alt": 1, "shift": 2, "win": 3}
_ALIASES = {
    **dict.fromkeys(("control", "ctl", "lctrl", "rctrl", "leftctrl", "rightctrl", "lcontrol", "rcontrol", "leftcontrol", "rightcontrol"), "ctrl"),
    **dict.fromkeys(("lalt", "ralt", "leftalt", "rightalt", "option", "altgr", "altgraph"), "alt"),
    **dict.fromkeys(("lshift", "rshift", "leftshift", "rightshift"), "shift"),
    **dict.fromkeys(("windows", "windowskey", "winkey", "lwin", "rwin", "leftwin", "rightwin", "leftwindows", "rightwindows", "super", "meta", "cmd", "command", "start"), "win"),
    **dict.fromkeys(("del", "deletekey", "forwarddelete"), "delete"),
    **dict.fromkeys(("return", "enterkey", "numpadenter", "keypadenter", "numenter"), "enter"),
    **dict.fromkeys(("esc", "escapekey"), "escape"),
    **dict.fromkeys(("bksp", "backspacekey"), "backspace"),
    "pgup": "pageup",
    "pgdn": "pagedown",
    "pgdown": "pagedown",
}
_NAMED = frozenset(
    {"delete", "enter", "escape", "backspace", "tab", "space", "insert", "home", "end", "pageup", "pagedown", "up", "down", "left", "right", "capslock", "printscreen", "apps"}
)


def _canonical(word: str) -> str | None:
    squashed = re.sub(r"[\s_-]+", "", word)
    name = _ALIASES.get(squashed, squashed)
    if name in MODIFIER_ORDER or name in _NAMED or len(name) == 1 or re.fullmatch(r"f([1-9]|1\d|2[0-4])", name):
        return name
    return None


def split_combo(combo: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(modifiers in a fixed order, the other keys in the order given) for a combo however it is spelled.

    The desktop's parser takes these spellings and more; this only has to recognise the
    risky ones, so a word it does not know is kept as it is.
    """
    text = str(combo or "").strip().lower()
    if not text:
        return (), ()
    tokens: list[str] = []
    for part in re.split(r"\s*\+\s*", text):
        words = part.split()
        i = 0
        while i < len(words):
            for j in range(len(words), i, -1):  # longest first: "left control" before "left"
                name = _canonical("".join(words[i:j]))
                if name is not None:
                    tokens.append(name)
                    i = j
                    break
            else:
                tokens.append(words[i])
                i += 1
    modifiers = tuple(sorted({t for t in tokens if t in MODIFIER_ORDER}, key=MODIFIER_ORDER.__getitem__))
    keys = tuple(dict.fromkeys(t for t in tokens if t not in MODIFIER_ORDER))
    return modifiers, keys


def normalize_combo(combo: object) -> str:
    """A key combo in one spelling: modifiers first (ctrl, alt, shift, win), then the keys, lower case.

    "Alt + F4", "f4+alt", "alt f4", "Control+W", "left control+w", "Del" and "shift+del"
    become "alt+f4", "alt+f4", "alt+f4", "ctrl+w", "ctrl+w", "delete" and "shift+delete".
    """
    modifiers, keys = split_combo(combo)
    return "+".join(modifiers + keys)


_PRETTY = {"ctrl": "Control", "alt": "Alt", "shift": "Shift", "win": "Windows", "pageup": "Page Up", "pagedown": "Page Down", "printscreen": "Print Screen", "capslock": "Caps Lock"}


def pretty_combo(combo: object) -> str:
    """"ctrl+shift+delete" as people read it: "Control+Shift+Delete"."""
    modifiers, keys = split_combo(combo)
    return "+".join(_PRETTY.get(k, k.upper() if len(k) <= 3 else k.capitalize()) for k in modifiers + keys)


# =========================================================================== the policy


@dataclass(frozen=True, slots=True)
class Risk:
    """Why an action is hard to reverse."""

    kind: str  # "close-window", "quit", "close-tab", "delete", "keys", "shell", "console" or "click"
    doing: str  # what is about to happen, for the countdown's chip: "Closing this window"
    cancelled: str  # the caption when it is cancelled: "Cancelled. The window stays open."


_NOT_PRESSED = "Cancelled. The keys were not pressed."
_NOT_DELETED = "Cancelled. Nothing was deleted."
_CLOSE_WINDOW = Risk("close-window", "Closing this window", "Cancelled. The window stays open.")

# Exact chords (normalised) and what pressing them does.
RISKY_KEYS: dict[str, Risk] = {
    "alt+f4": _CLOSE_WINDOW,
    "ctrl+w": Risk("close-tab", "Closing this tab", "Cancelled. The tab stays open."),
    "ctrl+f4": Risk("close-tab", "Closing this tab", "Cancelled. The tab stays open."),
    "ctrl+shift+w": Risk("close-tab", "Closing every tab in this window", "Cancelled. The tabs stay open."),
    "delete": Risk("delete", "Pressing Delete", _NOT_DELETED),
    "shift+delete": Risk("delete", "Pressing Shift+Delete", _NOT_DELETED),
    "ctrl+shift+delete": Risk("delete", "Pressing Control+Shift+Delete", "Cancelled. Nothing was cleared."),
    "ctrl+enter": Risk("keys", "Pressing Control+Enter", _NOT_PRESSED),
    "alt+s": Risk("keys", "Pressing Alt+S", _NOT_PRESSED),
    "ctrl+q": Risk("quit", "Pressing Control+Q", _NOT_PRESSED),
    "win+r": Risk("shell", "Opening the Run box", "Cancelled. Nothing was opened."),
    # Not in the review's list, but the same door as opening Task Manager or a terminal by name.
    "ctrl+shift+escape": Risk("shell", "Opening Task Manager", "Cancelled. Nothing was opened."),
    "win+x": Risk("shell", "Opening the Start button's menu", "Cancelled. Nothing was opened."),
}
RISKY_ACTIONS: dict[str, Risk] = {
    "window.close": _CLOSE_WINDOW,
    "app.quit": Risk("quit", "Quitting Sightline", "Cancelled. Sightline keeps running."),
}

# Command windows, where a new line runs whatever was typed.
CONSOLE_PROCESSES = frozenset(
    {
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "powershell_ise.exe",
        "windowsterminal.exe",
        "wt.exe",
        "openconsole.exe",
        "conhost.exe",
        "wsl.exe",
        "wslhost.exe",
        "bash.exe",
        "mintty.exe",
    }
)
# Apps that run commands or change the system, named exactly or by a phrase inside the name.
SHELL_APP_NAMES = frozenset({"run", "services", "computer management", "group policy", "local group policy editor", "local security policy", "system configuration"})
SHELL_APP_WORDS = (
    "command prompt",
    "cmd",
    "powershell",
    "pwsh",
    "terminal",
    "wt",
    "regedit",
    "registry editor",
    "bash",
    "wsl",
    "task manager",
    "taskmgr",
    "mmc",
    "gpedit",
    "msconfig",
)
# Controls whose click deletes, sends or pays.
RISKY_CLICK_WORDS = frozenset({"delete", "remove", "discard", "erase", "send", "submit", "pay", "buy", "purchase", "checkout"})
RISKY_CLICK_PHRASES = ("empty recycle bin", "empty the recycle bin", "empty bin", "empty trash", "empty the trash", "place order", "check out")


def _words(text: object) -> list[str]:
    return re.sub(r"[^\w]+", " ", str(text or "").lower()).split()


def _has_phrase(words: list[str], phrase: str) -> bool:
    target = phrase.split()
    n = len(target)
    return any(words[i : i + n] == target for i in range(len(words) - n + 1))


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _in_console(focus: FocusInfo | None) -> bool:
    process = str(getattr(focus, "process", "") or "").strip().lower()
    return process.rsplit("\\", 1)[-1] in CONSOLE_PROCESSES


def key_risk(combo: object, focus: FocusInfo | None = None) -> Risk | None:
    """What pressing these keys risks, however they are spelled, or None."""
    modifiers, keys = split_combo(combo)
    for key in keys:
        exact = "+".join((*modifiers, key))
        if exact in RISKY_KEYS:
            return RISKY_KEYS[exact]
    held = set(modifiers)
    best: tuple[int, Risk] | None = None
    for chord, risk in RISKY_KEYS.items():
        r_mods, r_keys = split_combo(chord)
        # An extra modifier on a risky shortcut is still that shortcut (the closest one: Ctrl+Alt+Shift+W is
        # Ctrl+Shift+W, not Ctrl+W); a modifier on a bare risky key is another shortcut altogether.
        if r_mods and set(r_mods) < held and r_keys[0] in keys and (best is None or len(r_mods) > best[0]):
            best = (len(r_mods), risk)
    if best is not None:
        risk = best[1]
        if risk.kind in ("close-window", "close-tab"):
            return risk
        return Risk(risk.kind, f"Pressing {pretty_combo(combo)}", risk.cancelled)
    if "enter" in keys and _in_console(focus):
        return Risk("console", "Pressing Enter in a command window", _NOT_PRESSED)
    return None


def app_risk(name: object) -> Risk | None:
    """Opening this app by name: a shell or a system tool is risky."""
    words = [w for w in _words(str(name or "").lower().replace(".exe", " ")) if w]
    if not words:
        return None
    joined = " ".join(words)
    if joined in SHELL_APP_NAMES or any(_has_phrase(words, phrase) for phrase in SHELL_APP_WORDS):
        return Risk("shell", f"Opening {_clip(str(name).strip(), 40)}", "Cancelled. Nothing was opened.")
    return None


def click_risk(name: object) -> Risk | None:
    """Clicking a control with this name: delete, remove, discard, send, submit, pay, buy and the like."""
    words = _words(name)
    if not words:
        return None
    if any(w in RISKY_CLICK_WORDS for w in words) or any(_has_phrase(words, p) for p in RISKY_CLICK_PHRASES):
        return Risk("click", f"Clicking “{_clip(str(name).strip(), 30)}”", "Cancelled. Nothing was clicked.")
    return None


def assess(action: Action, focus: FocusInfo | None = None, *, target: object = None) -> Risk | None:
    """Why ``action`` is hard to reverse, or None.

    ``focus`` is what has keyboard focus (for Enter into a console).  ``target`` is the
    name of the control a click will land on, when the caller knows it.
    """
    name = action.name
    if name in RISKY_ACTIONS:
        return RISKY_ACTIONS[name]
    if name == "key.press":
        return key_risk(action.arg("combo") or action.arg("key") or "", focus)
    if name == "text.type":
        text = action.arg("text")
        if isinstance(text, str) and ("\n" in text or "\r" in text) and _in_console(focus):
            return Risk("console", "Typing a line into a command window", "Cancelled. Nothing was typed.")
        return None
    if name == "app.open":
        return app_risk(action.arg("name"))
    if name == "element.click":
        return click_risk(action.arg("name"))
    if name == "mouse.click" and target:
        return click_risk(target)
    return None


def risk_of(action: Action, focus: FocusInfo | None = None, *, target: object = None) -> str | None:
    """What makes ``action`` hard to reverse, in plain words ("Closing this tab"); None when it is not."""
    risk = assess(action, focus, target=target)
    return risk.doing if risk is not None else None


__all__ = [
    "CONSOLE_PROCESSES",
    "RISKY_ACTIONS",
    "RISKY_KEYS",
    "Risk",
    "app_risk",
    "assess",
    "click_risk",
    "key_risk",
    "normalize_combo",
    "pretty_combo",
    "risk_of",
    "split_combo",
]
