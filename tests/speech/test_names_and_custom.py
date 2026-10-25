"""Spoken names of apps, windows and buttons, and the user's own commands file."""

from __future__ import annotations

from pathlib import Path

import pytest

from sightline.speech import custom
from sightline.speech.names import DynamicNames, window_forms


@pytest.fixture
def dyn() -> DynamicNames:
    d = DynamicNames()
    d.set_apps(["Google Chrome", "Microsoft Word", "Microsoft Excel", "Visual Studio Code", "Notepad", "Notepad++", "Calculator", "Spotify"])
    d.set_windows([("Inbox - Outlook", "olk.exe", 1), ("Budget.xlsx - Excel", "EXCEL.EXE", 2), ("YouTube - Google Chrome", "chrome.exe", 3)])
    d.set_elements(["Save", "Save as", "Sign in", "Cancel", "Next page"])
    return d


@pytest.mark.parametrize(
    ("spoken", "app"),
    [
        ("chrome", "Google Chrome"),
        ("google chrome", "Google Chrome"),
        ("word", "Microsoft Word"),
        ("excel", "Microsoft Excel"),
        ("visual studio code", "Visual Studio Code"),
        ("notepad", "Notepad"),
        ("calculator", "Calculator"),
        ("spotify", "Spotify"),
    ],
)
def test_apps(dyn: DynamicNames, spoken: str, app: str) -> None:
    hit = dyn.resolve_app(spoken)
    assert hit is not None and hit[0] == app


@pytest.mark.parametrize("spoken", ["zoom", "photoshop", "the shops", ""])
def test_unknown_apps(dyn: DynamicNames, spoken: str) -> None:
    assert dyn.resolve_app(spoken) is None


@pytest.mark.parametrize(
    ("spoken", "hwnd"),
    [("outlook", 1), ("inbox", 1), ("excel", 2), ("budget", 2), ("youtube", 3), ("chrome", 3)],
)
def test_windows(dyn: DynamicNames, spoken: str, hwnd: int) -> None:
    hit = dyn.resolve_window(spoken)
    assert hit is not None and hit[1] == hwnd


def test_elements_prefer_the_exact_name(dyn: DynamicNames) -> None:
    assert dyn.resolve_element("save")[0] == "Save"
    assert dyn.resolve_element("save as")[0] == "Save as"
    assert dyn.resolve_element("sign in")[0] == "Sign in"
    assert dyn.resolve_element("next page")[0] == "Next page"
    assert dyn.resolve_element("delete everything") is None


def test_window_forms() -> None:
    forms = window_forms("Budget.xlsx - Excel", "EXCEL.EXE")
    assert "excel" in forms and any("budget" in f for f in forms)


# ---------------------------------------------------------------------- commands.toml


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "commands.toml"
    path.write_text(text, "utf-8")
    return path


def test_example_file_has_no_live_commands(tmp_path: Path) -> None:
    path = tmp_path / "commands.toml"
    assert custom.write_example(path)
    assert not custom.write_example(path)  # never overwrites
    commands, problems = custom.load(path)
    assert commands == [] and problems == []


def test_valid_commands(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
[[command]]
phrase = "sign off"
text = "Kind regards,\\\\nSam"

[[command]]
phrase = "(open|start) music"
app = "Spotify"

[[command]]
phrase = "news"
url = "https://www.bbc.co.uk/news"

[[command]]
phrase = "Private Window!"
keys = "ctrl+shift+n"
""",
    )
    commands, problems = custom.load(path)
    assert problems == []
    assert [c.phrase for c in commands] == ["sign off", "(open|start) music", "news", "Private Window!"]
    specs = custom.to_specs(commands)
    assert all(s.priority == 10 and s.category == "My commands" for s in specs)
    assert custom.template_for("Private Window!") == "private window"


def test_problems_are_plain_and_specific(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
[[command]]
text = "no phrase"

[[command]]
phrase = "two things"
keys = "ctrl+a"
text = "hello"

[[command]]
phrase = "bad (template"
keys = "ctrl+a"
""",
    )
    commands, problems = custom.load(path)
    assert commands == []
    assert len(problems) == 3
    assert "no phrase" in problems[0]
    assert "exactly one" in problems[1]


def test_broken_toml(tmp_path: Path) -> None:
    commands, problems = custom.load(write(tmp_path, "[[command]\nphrase = "))
    assert commands == [] and problems and problems[0].startswith("Your commands file has a mistake")


def test_user_commands_win(tmp_path: Path) -> None:
    from sightline.speech.grammar import BuildContext, build_commands
    from sightline.speech.interpret import Interpreter, Situation
    from sightline.speech.matcher import Grammar

    commands, _ = custom.load(write(tmp_path, '[[command]]\nphrase = "copy"\nkeys = "ctrl+insert"\n'))
    interp = Interpreter(Grammar(build_commands() + custom.to_specs(commands)))
    d = interp.decide("copy", Situation(mode="command"), BuildContext())
    assert dict(d.outcomes[0].actions[0].args) == {"combo": "ctrl+insert"}


# ---------------------------------------------------------------------- limits and safety words (review S-07)


def test_stacked_optionals_are_refused_quickly(tmp_path: Path) -> None:
    import time

    from sightline.speech.grammar import BuildContext, build_commands
    from sightline.speech.interpret import Interpreter, Situation
    from sightline.speech.matcher import Grammar

    phrase = " ".join(["[a]"] * 18)
    path = write(tmp_path, f'[[command]]\nphrase = "{phrase}"\nkeys = "ctrl+shift+n"\n')
    started = time.perf_counter()
    commands, problems = custom.load(path)
    assert time.perf_counter() - started < 1.0
    assert commands == [] and len(problems) == 1 and "optional parts" in problems[0]
    # Matching an ordinary sentence stays quick (with the phrase loaded it took about 3 s).
    interp = Interpreter(Grammar(build_commands() + custom.to_specs(commands)))
    started = time.perf_counter()
    interp.decide(" ".join(["a"] * 17) + " b", Situation(mode="command"), BuildContext(), semantic=False)
    assert time.perf_counter() - started < 1.0


def test_a_few_optionals_are_still_fine(tmp_path: Path) -> None:
    commands, problems = custom.load(write(tmp_path, '[[command]]\nphrase = "[please] (sign|send) off [now] [thanks]"\ntext = "Bye"\n'))
    assert problems == [] and len(commands) == 1


def test_phrases_with_too_many_ways_of_saying_them_are_refused(tmp_path: Path) -> None:
    many = " ".join("(a|b|c)" for _ in range(7))  # 3 ** 7 = 2187 ways
    commands, problems = custom.load(write(tmp_path, f'[[command]]\nphrase = "{many}"\ntext = "x"\n'))
    assert commands == [] and "ways" in problems[0]
    assert custom.template_size(custom.parse_sentence("(a|b) [c] (d;e;f)")) == (1, 2 * 2 * 6)


def test_the_file_the_commands_and_their_texts_are_capped(tmp_path: Path) -> None:
    big = write(tmp_path, "# " + "x" * (custom.MAX_FILE_BYTES + 10) + "\n")
    commands, problems = custom.load(big)
    assert commands == [] and "larger than 256 KB" in problems[0]
    many = "".join(f'[[command]]\nphrase = "word number {i}"\ntext = "{i}"\n' for i in range(custom.MAX_COMMANDS + 5))
    commands, problems = custom.load(write(tmp_path, many))
    assert len(commands) == custom.MAX_COMMANDS and any(f"first {custom.MAX_COMMANDS}" in p for p in problems)
    long_text = "y" * (custom.MAX_TEXT_CHARS + 1)
    commands, problems = custom.load(write(tmp_path, f'[[command]]\nphrase = "essay"\ntext = "{long_text}"\n'))
    assert commands == [] and "longer than 2000 characters" in problems[0]


@pytest.mark.parametrize("url", ["file:///c:/windows/system32/calc.exe", "ms-settings:privacy", "search-ms:query=x", "javascript:alert(1)"])
def test_a_command_url_must_be_a_web_page(tmp_path: Path, url: str) -> None:
    # Review S-09: with no browser open, web.go handed the address to the Windows shell.
    commands, problems = custom.load(write(tmp_path, f'[[command]]\nphrase = "open it"\nurl = "{url}"\n'))
    assert commands == [] and "must be a web page" in problems[0]


def test_web_pages_and_mail_links_are_fine(tmp_path: Path) -> None:
    text = '[[command]]\nphrase = "news"\nurl = "bbc.co.uk/news"\n[[command]]\nphrase = "write to sam"\nurl = "mailto:sam@example.com"\n'
    commands, problems = custom.load(write(tmp_path, text))
    assert problems == [] and len(commands) == 2


def test_commands_may_not_take_a_safety_word(tmp_path: Path) -> None:
    from sightline.speech.grammar import BuildContext, build_commands, sleep_and_wake
    from sightline.speech.interpret import Interpreter, Situation
    from sightline.speech.matcher import Grammar

    text = (
        '[[command]]\nphrase = "cancel"\nkeys = "enter"\n'
        '[[command]]\nphrase = "stop"\nkeys = "enter"\n'
        '[[command]]\nphrase = "go to sleep"\ntext = "x"\n'
        '[[command]]\nphrase = "(halt|freeze) [now]"\nkeys = "enter"\n'
        '[[command]]\nphrase = "Undo that!"\nkeys = "enter"\n'
        '[[command]]\nphrase = "cancel my order"\ntext = "fine"\n'
    )
    commands, problems = custom.load(write(tmp_path, text))
    assert [c.phrase for c in commands] == ["cancel my order"], "a longer phrase that starts with one is fine"
    assert len(problems) == 5 and all("safety commands" in p for p in problems)
    assert '"stop" is one of' in problems[1]
    interp = Interpreter(Grammar(build_commands() + custom.to_specs(commands) + sleep_and_wake("wake up", "go to sleep")))
    for words, action in (("cancel", "ui.cancel"), ("stop", "key.release_all"), ("halt", "key.release_all"), ("go to sleep", "speech.mode"), ("undo that", "action.undo")):
        d = interp.decide(words, Situation(mode="auto"), BuildContext(), semantic=False)
        assert action in [a.name for o in d.outcomes for a in o.actions], words


def test_the_users_own_sleep_and_wake_phrases_are_protected_too(tmp_path: Path) -> None:
    protected = custom.safety_phrases("hello computer", "good night")
    assert {"hello computer", "good night", "stop", "cancel", "undo that", "help", "pause eyes", "resume eyes"} <= protected
    commands, problems = custom.load(write(tmp_path, '[[command]]\nphrase = "good night"\ntext = "x"\n'), protected=protected)
    assert commands == [] and problems
    commands, problems = custom.load(write(tmp_path, '[[command]]\nphrase = "good night"\ntext = "x"\n'))
    assert len(commands) == 1, "with the usual phrases it is an ordinary command"


def test_a_list_in_a_phrase_cannot_cover_a_safety_word(tmp_path: Path) -> None:
    commands, problems = custom.load(write(tmp_path, '[[command]]\nphrase = "{app_name}"\ntext = "x"\n'))
    assert commands == [] and "safety commands" in problems[0]

