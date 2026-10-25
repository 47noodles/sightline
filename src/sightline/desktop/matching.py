"""Fuzzy matching of spoken names against apps, windows and on-screen elements (rapidfuzz).

Speech recognisers get names roughly right ("visual studio code", "files",
"word"), so matching is generous with aliases and token overlap but strict
with short queries, where fuzzy scores are noisy.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from sightline.core.types import AppInfo, ElementInfo, WindowInfo

T = TypeVar("T")

# Spoken name -> Start menu names to try, best first.
APP_ALIASES: dict[str, tuple[str, ...]] = {
    "word": ("Word", "Microsoft Word"),
    "microsoft word": ("Word", "Microsoft Word"),
    "excel": ("Excel", "Microsoft Excel"),
    "microsoft excel": ("Excel", "Microsoft Excel"),
    "powerpoint": ("PowerPoint", "Microsoft PowerPoint"),
    "power point": ("PowerPoint", "Microsoft PowerPoint"),
    "outlook": ("Outlook (new)", "Outlook", "Outlook (classic)", "Microsoft Outlook"),
    "email": ("Outlook (new)", "Outlook", "Outlook (classic)", "Mail", "Thunderbird"),
    "mail": ("Outlook (new)", "Outlook", "Outlook (classic)", "Mail", "Thunderbird"),
    "onenote": ("OneNote", "OneNote for Windows 10"),
    "one note": ("OneNote", "OneNote for Windows 10"),
    "chrome": ("Google Chrome",),
    "google chrome": ("Google Chrome",),
    "google": ("Google Chrome",),
    "edge": ("Microsoft Edge",),
    "microsoft edge": ("Microsoft Edge",),
    "firefox": ("Firefox", "Mozilla Firefox"),
    "browser": ("Google Chrome", "Microsoft Edge", "Firefox", "Mozilla Firefox", "Brave"),
    "web browser": ("Google Chrome", "Microsoft Edge", "Firefox", "Mozilla Firefox", "Brave"),
    "internet": ("Google Chrome", "Microsoft Edge", "Firefox", "Mozilla Firefox", "Brave"),
    "files": ("File Explorer",),
    "file explorer": ("File Explorer",),
    "explorer": ("File Explorer",),
    "my files": ("File Explorer",),
    "file manager": ("File Explorer",),
    "folders": ("File Explorer",),
    "windows explorer": ("File Explorer",),
    "settings": ("Settings",),
    "windows settings": ("Settings",),
    "pc settings": ("Settings",),
    "control panel": ("Control Panel",),
    "calculator": ("Calculator",),
    "calc": ("Calculator",),
    "notepad": ("Notepad",),
    "note pad": ("Notepad",),
    "terminal": ("Terminal", "Windows Terminal", "Windows PowerShell", "Command Prompt"),
    "windows terminal": ("Terminal", "Windows Terminal"),
    "command line": ("Terminal", "Windows Terminal", "Command Prompt"),
    "command prompt": ("Command Prompt", "Terminal"),
    "cmd": ("Command Prompt", "Terminal"),
    "powershell": ("Windows PowerShell", "PowerShell", "PowerShell 7 (x64)", "Terminal"),
    "power shell": ("Windows PowerShell", "PowerShell", "PowerShell 7 (x64)", "Terminal"),
    "teams": ("Microsoft Teams", "Microsoft Teams (work or school)", "Teams", "Microsoft Teams classic"),
    "microsoft teams": ("Microsoft Teams", "Microsoft Teams (work or school)", "Teams"),
    "spotify": ("Spotify",),
    "music": ("Spotify", "Media Player", "Windows Media Player"),
    "store": ("Microsoft Store",),
    "app store": ("Microsoft Store",),
    "microsoft store": ("Microsoft Store",),
    "paint": ("Paint",),
    "photos": ("Photos",),
    "pictures": ("Photos",),
    "camera": ("Camera",),
    "snipping tool": ("Snipping Tool",),
    "screenshot": ("Snipping Tool",),
    "task manager": ("Task Manager",),
    "code": ("Visual Studio Code",),
    "vs code": ("Visual Studio Code",),
    "vscode": ("Visual Studio Code",),
    "visual studio code": ("Visual Studio Code",),
    "clock": ("Clock",),
    "alarms": ("Clock",),
    "timer": ("Clock",),
    "calendar": ("Calendar", "Outlook (new)"),
    "maps": ("Maps",),
    "weather": ("Weather",),
    "news": ("Microsoft News", "News"),
    "whatsapp": ("WhatsApp",),
    "whats app": ("WhatsApp",),
    "zoom": ("Zoom Workplace", "Zoom"),
    "discord": ("Discord",),
    "slack": ("Slack",),
    "vlc": ("VLC media player",),
    "media player": ("Media Player", "Windows Media Player Legacy", "Windows Media Player"),
    "sound recorder": ("Sound Recorder", "Voice Recorder"),
    "voice recorder": ("Sound Recorder", "Voice Recorder"),
    "magnifier": ("Magnifier",),
    "on screen keyboard": ("On-Screen Keyboard",),
    "onscreen keyboard": ("On-Screen Keyboard",),
    "wordpad": ("WordPad",),
    "word pad": ("WordPad",),
    "copilot": ("Copilot", "Microsoft Copilot"),
    "claude": ("Claude",),
    "docker": ("Docker Desktop",),
}

# Executable -> names people use for it ("switch to word").
PROCESS_NAMES: dict[str, tuple[str, ...]] = {
    "chrome.exe": ("chrome", "google chrome", "browser"),
    "msedge.exe": ("edge", "microsoft edge", "browser"),
    "firefox.exe": ("firefox", "browser"),
    "brave.exe": ("brave", "browser"),
    "opera.exe": ("opera", "browser"),
    "vivaldi.exe": ("vivaldi", "browser"),
    "winword.exe": ("word", "microsoft word"),
    "excel.exe": ("excel", "microsoft excel"),
    "powerpnt.exe": ("powerpoint", "microsoft powerpoint"),
    "outlook.exe": ("outlook", "mail", "email"),
    "olk.exe": ("outlook", "mail", "email"),
    "onenote.exe": ("onenote",),
    "explorer.exe": ("file explorer", "files", "explorer", "folder"),
    "notepad.exe": ("notepad",),
    "notepad++.exe": ("notepad plus plus",),
    "windowsterminal.exe": ("terminal", "windows terminal", "command line"),
    "cmd.exe": ("command prompt", "cmd"),
    "powershell.exe": ("powershell",),
    "pwsh.exe": ("powershell",),
    "code.exe": ("vs code", "visual studio code", "code"),
    "devenv.exe": ("visual studio",),
    "spotify.exe": ("spotify", "music"),
    "ms-teams.exe": ("teams", "microsoft teams"),
    "teams.exe": ("teams", "microsoft teams"),
    "slack.exe": ("slack",),
    "discord.exe": ("discord",),
    "zoom.exe": ("zoom",),
    "whatsapp.exe": ("whatsapp",),
    "telegram.exe": ("telegram",),
    "systemsettings.exe": ("settings",),
    "calculatorapp.exe": ("calculator",),
    "calculator.exe": ("calculator",),
    "mspaint.exe": ("paint",),
    "photos.exe": ("photos",),
    "snippingtool.exe": ("snipping tool",),
    "taskmgr.exe": ("task manager",),
    "vlc.exe": ("vlc", "video player"),
    "acrobat.exe": ("acrobat", "pdf"),
    "acrord32.exe": ("acrobat reader", "pdf"),
    "thunderbird.exe": ("thunderbird", "mail"),
    "claude.exe": ("claude",),
}

BROWSER_PROCESSES = frozenset(
    {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "arc.exe", "iexplore.exe"}
)

_JUNK_APP = re.compile(
    r"\b(uninstall\w*|un-install|readme|read me|release notes|documentation|licen[cs]e|changelog|"
    r"what'?s new|user guide|manual|faq)\b|(\bhelp|website|web site|on the web|home ?page)$",
    re.IGNORECASE,
)


def is_junk_app_name(name: str) -> bool:
    """Start menu entries nobody means when they say "open ..." (uninstallers, readmes, help links)."""
    return bool(_JUNK_APP.search(name))


def normalize(text: str) -> str:
    text = text.lower().replace("&", " and ").replace("’", "'")
    text = re.sub(r"[®™©]", "", text)  # (R), TM, (C)
    text = re.sub(r"[^\w' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> list[str]:
    return [t for t in normalize(text).split() if t]


@dataclass(frozen=True, slots=True)
class Match(Generic[T]):
    item: T
    score: float


def _name_score(query: str, name: str) -> float:
    """0-100: how well a spoken query matches a name."""
    from rapidfuzz import fuzz  # imported on first use: it costs 75 ms, and construction must stay cheap

    q, n = normalize(query), normalize(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 100.0
    q_tokens, n_tokens = q.split(), n.split()
    if n.startswith(q + " ") or n_tokens[: len(q_tokens)] == q_tokens:
        return 94.0 - min(len(n_tokens) - len(q_tokens), 4)
    if all(t in n_tokens for t in q_tokens):
        return 90.0 - min(len(n_tokens) - len(q_tokens), 6)
    if len(q) <= 3:
        return 0.0  # "ok", "go": fuzzy scores on two letters mean nothing
    score = fuzz.WRatio(q, n)
    # Long names contain many short queries by accident; WRatio's partial matching rewards that.
    if len(n) > 2 * len(q) + 8:
        score = min(score, fuzz.token_set_ratio(q, n))
    return float(score) * 0.92


def match_apps(query: str, apps: Sequence[AppInfo], *, min_score: float = 72.0) -> list[Match[AppInfo]]:
    """Apps for "open <query>", best first."""
    q = normalize(query)
    for prefix in ("the ", "my ", "app ", "application "):
        if q.startswith(prefix):
            q = q[len(prefix) :]
    for suffix in (" app", " application", " program"):
        if q.endswith(suffix):
            q = q[: -len(suffix)]
    if not q:
        return []
    by_name: dict[str, AppInfo] = {}
    for app in apps:
        by_name.setdefault(normalize(app.name), app)
    results: dict[str, Match[AppInfo]] = {}
    # Aliases outrank even an exact name: "files" means File Explorer although an app called Files exists.
    for rank, alias in enumerate(APP_ALIASES.get(q, ())):
        app = by_name.get(normalize(alias))
        if app is not None:
            key = normalize(app.name)
            results.setdefault(key, Match(app, 101.0 - rank * 0.5))
    for app in apps:
        key = normalize(app.name)
        if key in results:
            continue
        score = _name_score(q, app.name)
        if score >= min_score:
            results[key] = Match(app, score)
    return sorted(results.values(), key=lambda m: (-m.score, len(m.item.name)))


def match_windows(query: str, windows: Sequence[WindowInfo], *, min_score: float = 70.0) -> list[Match[WindowInfo]]:
    """Windows for "switch to <query>", best first; ties keep front-to-back order."""
    from rapidfuzz import fuzz

    q = normalize(query)
    for suffix in (" window", " app"):
        if q.endswith(suffix) and len(q) > len(suffix):
            q = q[: -len(suffix)]
    if not q:
        return []
    out: list[Match[WindowInfo]] = []
    for win in windows:
        exe = win.process.lower()
        names = PROCESS_NAMES.get(exe, ())
        stem = exe[:-4] if exe.endswith(".exe") else exe
        process_score = 0.0
        if q in names or q == normalize(stem):
            process_score = 96.0
        elif names and max(fuzz.ratio(q, n) for n in names) >= 88:
            process_score = 88.0
        title_score = _name_score(q, win.title)
        # A title that merely contains the query ("Inbox - me@x.com - Outlook") is still a strong hit.
        if len(q) >= 4 and q in normalize(win.title):
            title_score = max(title_score, 86.0)
        score = max(process_score, title_score)
        if score >= min_score:
            out.append(Match(win, score))
    out.sort(key=lambda m: -m.score)  # stable: front windows stay first among equals
    return out


_PREFERRED_TYPES = {"Button": 3.0, "MenuItem": 3.0, "Hyperlink": 2.5, "TabItem": 2.0, "SplitButton": 2.0, "CheckBox": 1.0}


def match_elements(query: str, elements: Iterable[ElementInfo], *, min_score: float = 72.0) -> list[Match[ElementInfo]]:
    """Elements for "click <query>", best first."""
    q = normalize(query)
    if not q:
        return []
    out: list[Match[ElementInfo]] = []
    for el in elements:
        if not el.name:
            continue
        score = _name_score(q, el.name)
        if score < min_score:
            continue
        out.append(Match(el, score + _PREFERRED_TYPES.get(el.control_type, 0.0)))
    out.sort(key=lambda m: -m.score)
    return out
