"""Spoken commands to actions: the grammar, numbers, homophones, chaining and repeats."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from sightline.speech.grammar import build_commands, help_rows, parse_key_sequence, sleep_and_wake, spoken_url


def A(name: str, /, **args: Any) -> tuple[str, dict[str, Any]]:
    return (name, args)


def K(combo: str, repeat: int = 1) -> tuple[str, dict[str, Any]]:
    return ("key.press", {"combo": combo} if repeat == 1 else {"combo": combo, "repeat": repeat})


def T(text: str) -> tuple[str, dict[str, Any]]:
    return ("type", {"text": text})


CASES: list[tuple[str, list]] = [
    # ------------------------------------------------------------------ mouse
    ("click", [A("mouse.click", button="left")]),
    ("left click", [A("mouse.click", button="left")]),
    ("click it", [A("mouse.click", button="left")]),
    ("Click.", [A("mouse.click", button="left")]),
    ("tap", [A("mouse.click", button="left")]),
    ("double click", [A("mouse.click", button="left", count=2)]),
    ("double-click that", [A("mouse.click", button="left", count=2)]),
    ("triple click", [A("mouse.click", button="left", count=3)]),
    ("right click", [A("mouse.click", button="right")]),
    ("right-click here", [A("mouse.click", button="right")]),
    ("write click", [A("mouse.click", button="right")]),
    ("middle click", [A("mouse.click", button="middle")]),
    ("click save", [A("element.click", name="Save")]),
    ("click on the file", [A("element.click", name="File")]),
    ("click sign in", [A("element.click", name="Sign in")]),
    ("double click documents", [A("element.click", name="Documents", count=2)]),
    ("right click documents", [A("element.click", name="Documents", button="right")]),
    ("drag", [A("mouse.down", button="left")]),
    ("start dragging", [A("mouse.down", button="left")]),
    ("drop", [A("mouse.up", button="left")]),
    ("let go", [A("mouse.up", button="left")]),
    ("scroll down", [A("mouse.scroll", direction="down")]),
    ("scroll up", [A("mouse.scroll", direction="up")]),
    ("scroll left", [A("mouse.scroll", direction="left")]),
    ("scroll right", [A("mouse.scroll", direction="right")]),
    ("scroll down three", [A("mouse.scroll", direction="down", amount=9)]),
    ("Scroll down 3.", [A("mouse.scroll", direction="down", amount=9)]),
    ("scroll up two times", [A("mouse.scroll", direction="up", amount=6)]),
    ("scroll down twice", [A("mouse.scroll", direction="down", amount=6)]),
    ("scroll the page down", [A("mouse.scroll", direction="down")]),
    ("scroll down a bit", [A("mouse.scroll", direction="down", amount=1)]),
    ("scroll up a little", [A("mouse.scroll", direction="up", amount=1)]),
    ("scrolled down", [A("mouse.scroll", direction="down")]),
    ("keep scrolling down", [A("mouse.scroll_start", direction="down", speed=1.0)]),
    ("start scrolling up", [A("mouse.scroll_start", direction="up", speed=1.0)]),
    ("scroll down slowly", [A("mouse.scroll_start", direction="down", speed=0.5)]),
    ("stop scrolling", [A("mouse.scroll_stop")]),
    ("move mouse to the center", [A("mouse.move", to="center")]),
    ("move the pointer to the top", [A("mouse.move", to="top")]),
    ("mouse to the middle", [A("mouse.move", to="center")]),
    ("center the mouse", [A("mouse.move", to="center")]),
    ("move mouse left fifty", [A("mouse.move", dx=-50, dy=0)]),
    ("move the pointer down 20 pixels", [A("mouse.move", dx=0, dy=20)]),
    ("nudge left", [A("pointer.nudge", direction="left", amount=10)]),
    ("nudge up three", [A("pointer.nudge", direction="up", amount=30)]),
    ("nudge the mouse right", [A("pointer.nudge", direction="right", amount=10)]),
    # ------------------------------------------------------------------ keys
    ("press enter", [K("enter")]),
    ("press return", [K("enter")]),
    ("hit escape", [K("escape")]),
    ("press control shift t", [K("ctrl+shift+t")]),
    ("press control c", [K("ctrl+c")]),
    ("press alt f4", [K("alt+f4")]),
    ("press alt f four", [K("alt+f4")]),
    ("press windows d", [K("win+d")]),
    ("press control alt delete", [K("ctrl+alt+delete")]),
    ("press page down", [K("pagedown")]),
    ("press tab three times", [K("tab", 3)]),
    ("press alpha", [K("a")]),
    ("press capital a", [K("shift+a")]),
    ("press f5", [K("f5")]),
    ("press control plus", [K("ctrl+plus")]),
    ("press space", [K("space")]),
    ("press control shift escape", [K("ctrl+shift+escape")]),
    ("press down down enter", [K("down", 2), K("enter")]),
    ("press down enter", [K("down"), K("enter")]),
    ("control c", [K("ctrl+c")]),
    ("control v", [K("ctrl+v")]),
    ("Control-Z", [K("ctrl+z")]),
    ("alt tab", [K("alt+tab")]),
    ("Alt-Tab.", [K("alt+tab")]),
    ("shift tab", [K("shift+tab")]),
    ("shift tab twice", [K("shift+tab", 2)]),
    ("control shift tab", [K("ctrl+shift+tab")]),
    ("windows e", [K("win+e")]),
    ("alt f four", [K("alt+f4")]),
    ("enter", [K("enter")]),
    ("Enter.", [K("enter")]),
    ("tab", [K("tab")]),
    ("tab three times", [K("tab", 3)]),
    ("tab four", [K("tab", 4)]),
    ("tab for times", [K("tab", 4)]),
    ("escape", [K("escape")]),
    ("backspace", [K("backspace")]),
    ("backspace five", [K("backspace", 5)]),
    ("delete", [K("delete")]),
    ("space", [K("space")]),
    ("home", [K("home")]),
    ("end", [K("end")]),
    ("page down", [K("pagedown")]),
    ("page up twice", [K("pageup", 2)]),
    ("down", [K("down")]),
    ("up three", [K("up", 3)]),
    ("left", [K("left")]),
    ("right twice", [K("right", 2)]),
    ("hold shift", [A("key.hold", key="shift")]),
    ("release shift", [A("key.release", key="shift")]),
    ("release all", [A("key.release_all")]),
    # ------------------------------------------------------------------ text editing
    ("select all", [K("ctrl+a")]),
    ("select everything", [K("ctrl+a")]),
    ("select word", [K("ctrl+left"), K("ctrl+shift+right")]),
    ("select line", [K("home"), K("shift+end")]),
    ("select word left", [K("ctrl+shift+left")]),
    ("select previous word three times", [K("ctrl+shift+left", 3)]),
    ("select left five", [K("shift+left", 5)]),
    ("select to end of line", [K("shift+end")]),
    ("select to the start of the line", [K("shift+home")]),
    ("delete word", [K("ctrl+backspace")]),
    ("delete word three times", [K("ctrl+backspace", 3)]),
    ("delete three words", [K("ctrl+backspace", 3)]),
    ("delete next word", [K("ctrl+delete")]),
    ("delete line", [K("home"), K("shift+end"), K("backspace")]),
    ("delete that", [K("delete")]),
    ("go to end of line", [K("end")]),
    ("end of line", [K("end")]),
    ("go to start of line", [K("home")]),
    ("go to the beginning of the line", [K("home")]),
    ("go to top", [K("ctrl+home")]),
    ("go to the top of the page", [K("ctrl+home")]),
    ("go to bottom", [K("ctrl+end")]),
    ("end of the document", [K("ctrl+end")]),
    ("word left", [K("ctrl+left")]),
    ("next word three times", [K("ctrl+right", 3)]),
    ("undo", [K("ctrl+z")]),
    ("undo that", [A("action.undo")]),
    ("fill login", [A("login.fill")]),
    ("fill in my password", [A("login.fill")]),
    ("restart tracking", [A("pointer.restart")]),
    ("restart eye tracking", [A("pointer.restart")]),
    ("undo three times", [K("ctrl+z", 3)]),
    ("undo 3", [K("ctrl+z", 3)]),
    ("redo", [K("ctrl+y")]),
    ("copy", [K("ctrl+c")]),
    ("copy that", [K("ctrl+c")]),
    ("cut", [K("ctrl+x")]),
    ("paste", [K("ctrl+v")]),
    ("paste it", [K("ctrl+v")]),
    ("paste as plain text", [K("ctrl+shift+v")]),
    ("copy all", [K("ctrl+a"), K("ctrl+c")]),
    ("save", [K("ctrl+s")]),
    ("save the file", [K("ctrl+s")]),
    ("save as", [K("ctrl+shift+s")]),
    ("find", [K("ctrl+f")]),
    ("find on page", [K("ctrl+f")]),
    ("find invoice", [K("ctrl+f"), A("text.type", text="invoice")]),
    ("bold", [K("ctrl+b")]),
    ("make that bold", [K("ctrl+b")]),
    ("italic", [K("ctrl+i")]),
    ("underline", [K("ctrl+u")]),
    ("new document", [K("ctrl+n")]),
    ("print this", [K("ctrl+p")]),
    # ------------------------------------------------------------------ dictation commands
    ("type hello world", [T("hello world")]),
    ("type see you tomorrow", [T("see you tomorrow")]),
    ("type scroll down", [T("scroll down")]),
    ("spell alpha bravo charlie", [T("abc")]),
    ("spell h e l l o", [T("hello")]),
    ("spell capital alpha bravo", [T("Ab")]),
    ("snake user name", [T("user_name")]),
    ("camel user name", [T("userName")]),
    ("hammer user name", [T("UserName")]),
    ("kebab main menu", [T("main-menu")]),
    ("constant max size", [T("MAX_SIZE")]),
    ("all caps hello", [T("hello")]),
    ("new line", [T("\n")]),
    ("new paragraph", [T("\n\n")]),
    ("scratch that", [A("speech.scratch")]),
    ("strike that", [A("speech.scratch")]),
    # ------------------------------------------------------------------ windows and apps
    ("open notepad", [A("app.open", name="Notepad")]),
    ("open chrome", [A("app.open", name="Google Chrome")]),
    ("launch calculator", [A("app.open", name="Calculator")]),
    ("open word", [A("app.open", name="Microsoft Word")]),
    ("open visual studio code", [A("app.open", name="Visual Studio Code")]),
    ("start spotify", [A("app.open", name="Spotify")]),
    ("switch to chrome", [A("window.switch", name="New Tab - Google Chrome", hwnd=102)]),
    ("switch to notepad", [A("window.switch", name="Untitled - Notepad", hwnd=101)]),
    ("switch to outlook", [A("window.switch", name="Inbox - Outlook", hwnd=103)]),
    ("bring up downloads", [A("window.switch", name="Downloads", hwnd=104)]),
    ("close window", [A("window.close")]),
    ("close the window", [A("window.close")]),
    ("close this app", [A("window.close")]),
    ("minimise", [A("window.minimize")]),
    ("minimize window", [A("window.minimize")]),
    ("maximise window", [A("window.maximize")]),
    ("maximize", [A("window.maximize")]),
    ("restore window", [A("window.restore")]),
    ("snap left", [A("window.snap", side="left")]),
    ("snap the window to the right", [A("window.snap", side="right")]),
    ("next window", [A("window.next")]),
    ("previous window", [A("window.previous")]),
    ("show desktop", [A("desktop.show")]),
    ("minimise everything", [A("desktop.show")]),
    ("start menu", [K("win")]),
    ("open the start menu", [K("win")]),
    ("task view", [K("win+tab")]),
    ("file explorer", [K("win+e")]),
    ("windows settings", [K("win+i")]),
    # ------------------------------------------------------------------ web browser
    ("go back", [K("alt+left")]),
    ("go back twice", [K("alt+left", 2)]),
    ("go forward", [K("alt+right")]),
    ("new tab", [K("ctrl+t")]),
    ("new tap", [K("ctrl+t")]),
    ("close tab", [K("ctrl+w")]),
    ("next tab", [K("ctrl+tab")]),
    ("next tab two times", [K("ctrl+tab", 2)]),
    ("previous tab", [K("ctrl+shift+tab")]),
    ("reopen tab", [K("ctrl+shift+t")]),
    ("go to tab three", [K("ctrl+3")]),
    ("go to tab 2", [K("ctrl+2")]),
    ("refresh", [K("f5")]),
    ("reload the page", [K("f5")]),
    ("address bar", [K("ctrl+l")]),
    ("go to wikipedia dot org", [A("web.go", url="wikipedia.org")]),
    ("go to bbc dot co dot uk", [A("web.go", url="bbc.co.uk")]),
    ("go to github.com", [A("web.go", url="github.com")]),
    ("open the website youtube dot com", [A("web.go", url="youtube.com")]),
    ("search for weather in london", [A("web.search", query="weather in london")]),
    ("google cheap flights", [A("web.search", query="cheap flights")]),
    ("bookmark this", [K("ctrl+d")]),
    ("zoom in", [K("ctrl+=")]),
    ("zoom out", [K("ctrl+-")]),
    ("reset zoom", [K("ctrl+0")]),
    # ------------------------------------------------------------------ sound and media
    ("volume up", [A("system.volume", direction="up", amount=10)]),
    ("volume up 20 percent", [A("system.volume", direction="up", amount=20)]),
    ("turn the volume down by five percent", [A("system.volume", direction="down", amount=5)]),
    ("volume down three", [A("system.volume", direction="down", amount=6)]),
    ("turn the volume up", [A("system.volume", direction="up", amount=10)]),
    ("louder", [A("system.volume", direction="up", amount=10)]),
    ("quieter", [A("system.volume", direction="down", amount=10)]),
    ("mute", [A("system.volume", direction="mute")]),
    ("unmute the sound", [A("system.volume", direction="mute")]),
    ("play", [A("system.media", key="play_pause")]),
    ("pause the music", [A("system.media", key="play_pause")]),
    ("next track", [A("system.media", key="next")]),
    ("previous song", [A("system.media", key="previous")]),
    ("stop the music", [A("system.media", key="stop")]),
    # ------------------------------------------------------------------ Sightline
    ("pause eyes", [A("pointer.pause")]),
    ("eye control off", [A("pointer.pause")]),
    ("resume eyes", [A("pointer.resume")]),
    ("toggle eyes", [A("pointer.toggle")]),
    ("eyes only", [A("pointer.mode", mode="gaze")]),
    ("head only", [A("pointer.mode", mode="head")]),
    ("eyes and head", [A("pointer.mode", mode="hybrid")]),
    ("recenter", [A("pointer.recenter")]),
    ("re-centre", [A("pointer.recenter")]),
    ("dwell on", [A("dwell.on")]),
    ("dwell off", [A("dwell.off")]),
    ("toggle dwell", [A("dwell.toggle")]),
    ("dwell right click", [A("dwell.click_type", type="right")]),
    ("dwell double", [A("dwell.click_type", type="double")]),
    ("show numbers", [A("ui.labels.show")]),
    ("numbers", [A("ui.labels.show")]),
    ("hide numbers", [A("ui.labels.hide")]),
    ("show grid", [A("ui.grid.show")]),
    ("mouse grid", [A("ui.grid.show")]),
    ("hide grid", [A("ui.grid.hide")]),
    ("zoom", [A("ui.zoom.show")]),
    ("zoom here", [A("ui.zoom.show")]),
    ("show keyboard", [A("keyboard.show")]),
    ("hide keyboard", [A("keyboard.hide")]),
    ("magnifier", [A("magnifier.toggle")]),
    ("magnify more", [A("magnifier.zoom", direction="in")]),
    ("what can i say", [A("ui.help.show")]),
    ("What can I say?", [A("ui.help.show")]),
    ("help", [A("ui.help.show")]),
    ("settings", [A("ui.settings.show")]),
    ("open settings", [A("ui.settings.show")]),
    ("calibrate", [A("ui.calibrate", kind="gaze")]),
    ("calibrate gaze", [A("ui.calibrate", kind="gaze")]),
    ("calibrate eyes", [A("ui.calibrate", kind="eyes")]),
    ("calibrate my eyes", [A("ui.calibrate", kind="eyes")]),
    ("calibrate blinks", [A("ui.calibrate", kind="eyes")]),
    ("calibrate head", [A("ui.calibrate", kind="head")]),
    ("calibrate face gestures", [A("ui.calibrate", kind="gestures")]),
    ("full calibration", [A("ui.calibrate", kind="full")]),
    ("fix pointer", [A("ui.calibrate", kind="drift")]),
    ("fix the mouse", [A("ui.calibrate", kind="drift")]),
    ("practice", [A("ui.practice.show")]),
    ("practice room", [A("ui.practice.show")]),
    ("open the practice room", [A("ui.practice.show")]),
    ("reading mode", [A("pointer.reading_scroll", on=True)]),
    ("reading mode on", [A("pointer.reading_scroll", on=True)]),
    ("reading mode off", [A("pointer.reading_scroll", on=False)]),
    ("stop reading mode", [A("pointer.reading_scroll", on=False)]),
    ("menu", [A("ui.menu.show")]),
    ("action menu", [A("ui.menu.show")]),
    ("open the menu", [A("ui.menu.show")]),
    ("close the menu", [A("ui.menu.hide")]),
    ("show dock", [A("ui.dock.toggle")]),
    ("dictation mode", [A("speech.mode", mode="dictation")]),
    ("start dictation", [A("speech.mode", mode="dictation")]),
    ("command mode", [A("speech.mode", mode="command")]),
    ("normal mode", [A("speech.mode", mode="auto")]),
    ("stop dictation", [A("speech.mode", mode="auto")]),
    ("go to sleep", [A("speech.mode", mode="sleep")]),
    ("stop listening", [A("speech.mode", mode="sleep")]),
    ("microphone off", [A("speech.listen", on=False)]),
    ("stop", [A("key.release_all")]),
    ("cancel", [A("ui.cancel"), A("key.release_all")]),
    ("never mind", [A("ui.cancel"), A("key.release_all")]),
    ("quit sightline", [A("app.quit")]),
    # ------------------------------------------------------------------ chains
    ("select all copy", [K("ctrl+a"), K("ctrl+c")]),
    ("select all and copy", [K("ctrl+a"), K("ctrl+c")]),
    ("copy then switch to notepad", [K("ctrl+c"), A("window.switch", name="Untitled - Notepad", hwnd=101)]),
    ("go to end of line new line", [K("end"), T("\n")]),
    ("scroll down three and click", [A("mouse.scroll", direction="down", amount=9), A("mouse.click", button="left")]),
    ("control a control c", [K("ctrl+a"), K("ctrl+c")]),
    ("save and close window", [K("ctrl+s"), A("window.close")]),
]


@pytest.mark.parametrize(("utterance", "expected"), CASES, ids=[c[0] for c in CASES])
def test_command(run, utterance: str, expected: list) -> None:
    assert run(utterance) == expected


FIELD_CASES = [
    ("select quick brown", [A("text.select", phrase="quick brown")]),
    ("select from quick to lazy", [A("text.select_range", start="quick", end="lazy")]),
    ("move after brown", [A("text.caret", where="after", phrase="brown")]),
    ("put the cursor before the fox", [A("text.caret", where="before", phrase="the fox")]),
    ("select all", [K("ctrl+a")]),
    ("select all and copy", [K("ctrl+a"), K("ctrl+c")]),
    ("select word", [K("ctrl+left"), K("ctrl+shift+right")]),
]


@pytest.mark.parametrize(("utterance", "expected"), FIELD_CASES, ids=[c[0] for c in FIELD_CASES])
def test_text_field_commands(run, utterance: str, expected: list) -> None:
    assert run(utterance, editable=True) == expected


def test_phrase_selection_needs_a_text_field(run) -> None:
    assert run("select quick brown") == []
    assert run("move after brown") == []


NOT_COMMANDS = [
    "hello world",
    "i think we should meet tomorrow",
    "the weather is nice today",
    "can you send me the report",
    "blah",
    "open the pod bay doors please hal",
    "my name is sam",
    "scroll of the dead sea",
    "press release from the company",
    "type",
    "spell",
    "go to the shops",
    "search",
]


@pytest.mark.parametrize("utterance", NOT_COMMANDS)
def test_not_a_command(decide, utterance: str) -> None:
    decision = decide(utterance, mode="command")
    assert decision.kind in ("rejected", "ignored"), decision


def test_unknown_short_names_go_to_the_desktop(decide) -> None:
    # "open zoom" when Zoom is not in the app list yet: the desktop looks it up and says if it cannot.
    decision = decide("open zoom", mode="command")
    assert decision.kind == "command" and decision.method == "names"
    assert decision.outcomes[0].actions[0].args == {"name": "zoom"}
    assert decision.confidence < 0.99
    # Sentences are not names.
    assert decide("open the pod bay doors please hal", mode="command").kind == "rejected"
    # Inside a text field an unknown name is typed instead.
    assert decide("open zoom", mode="auto", editable=True).kind == "dictation"


def test_every_spoken_number_up_to_twenty(run) -> None:
    words = "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
    for n, word in enumerate(words, 1):
        assert run(f"tab {word} times") == [K("tab", n)] if n > 1 else [K("tab")]


@pytest.mark.parametrize(("utterance", "n"), [("tab to times", 2), ("tab too times", 2), ("tab for times", 4), ("scroll down won", 1), ("undo tree times", 3)])
def test_number_homophones(run, utterance: str, n: int) -> None:
    result = run(utterance)
    assert len(result) == 1
    name, args = result[0]
    assert args.get("repeat", args.get("amount", 1) // (3 if name == "mouse.scroll" else 1)) == n


# ---------------------------------------------------------------------- overlays


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("12", [A("ui.labels.select", number=12)]),
        ("twelve", [A("ui.labels.select", number=12)]),
        ("click 7", [A("ui.labels.select", number=7)]),
        ("number 31", [A("ui.labels.select", number=31)]),
        ("pick four", [A("ui.labels.select", number=4)]),
        ("double click 3", [A("ui.labels.select", number=3, count=2)]),
        ("right click 9", [A("ui.labels.select", number=9, button="right")]),
        ("hide numbers", [A("ui.labels.hide")]),
        ("cancel", [A("ui.cancel"), A("key.release_all")]),
        ("scroll down", [A("mouse.scroll", direction="down")]),  # global commands still work
    ],
)
def test_labels_overlay(run, utterance: str, expected: list) -> None:
    assert run(utterance, overlay="labels") == expected


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("5", [A("ui.grid.select", cell=5)]),
        ("five", [A("ui.grid.select", cell=5)]),
        ("square 3", [A("ui.grid.select", cell=3)]),
        ("click", [A("ui.grid.click")]),
        ("double click", [A("ui.grid.click", count=2)]),
        ("right click", [A("ui.grid.click", button="right")]),
        ("back", [A("ui.grid.back")]),
        ("hide grid", [A("ui.grid.hide")]),
    ],
)
def test_grid_overlay(run, utterance: str, expected: list) -> None:
    assert run(utterance, overlay="grid") == expected


@pytest.mark.parametrize("overlay", ["setup", "calibration"])
@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("next", [A("ui.wizard.next")]),
        ("continue", [A("ui.wizard.next")]),
        ("done", [A("ui.wizard.next")]),
        ("okay", [A("ui.wizard.next")]),
        ("OK.", [A("ui.wizard.next")]),
        ("back", [A("ui.wizard.back")]),
        ("go back", [A("ui.wizard.back")]),
        ("skip", [A("ui.wizard.skip")]),
        ("skip this", [A("ui.wizard.skip")]),
    ],
)
def test_wizard_overlay(run, overlay: str, utterance: str, expected: list) -> None:
    assert run(utterance, overlay=overlay) == expected


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("next", [A("ui.settings.next")]),
        ("next setting", [A("ui.settings.next")]),
        ("back", [A("ui.settings.previous")]),
        ("previous", [A("ui.settings.previous")]),
        ("go back", [A("ui.settings.previous")]),
        ("more", [A("ui.settings.adjust", direction="up")]),
        ("up", [A("ui.settings.adjust", direction="up")]),
        ("faster", [A("ui.settings.adjust", direction="up")]),
        ("a bit more", [A("ui.settings.adjust", direction="up")]),
        ("less", [A("ui.settings.adjust", direction="down")]),
        ("down", [A("ui.settings.adjust", direction="down")]),
        ("slower", [A("ui.settings.adjust", direction="down")]),
        ("up three times", [A("ui.settings.adjust", direction="up")] * 3),
        ("set to three", [A("ui.settings.set", value=3)]),
        ("Set it to 0.5 seconds.", [A("ui.settings.set", value=0.5)]),
        ("set it to one second", [A("ui.settings.set", value=1)]),
        ("set to half a second", [A("ui.settings.set", value=0.5)]),
        ("set to sixty percent", [A("ui.settings.set", value=60)]),
        ("set to off", [A("ui.settings.set", value="off")]),
        ("set it to a bit faster", [A("ui.settings.set", value="a bit faster")]),
        ("steady hands", [A("ui.settings.preset", name="steady hands")]),
        ("tremor", [A("ui.settings.preset", name="tremor")]),
        ("tired eyes", [A("ui.settings.preset", name="tired eyes")]),
        ("defaults", [A("ui.settings.preset", name="defaults")]),
        ("use the tremor preset", [A("ui.settings.preset", name="tremor")]),
        ("cancel", [A("ui.cancel"), A("key.release_all")]),
        ("scroll down", [A("mouse.scroll", direction="down")]),  # global commands still work
    ],
)
def test_settings_window(run, utterance: str, expected: list) -> None:
    assert run(utterance, overlay="settings") == expected


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("2", [A("ui.chooser.select", number=2)]),
        ("two", [A("ui.chooser.select", number=2)]),
        ("number 4", [A("ui.chooser.select", number=4)]),
        ("click 3", [A("ui.chooser.select", number=3)]),
        ("pick one", [A("ui.chooser.select", number=1)]),
        ("choose 6", [A("ui.chooser.select", number=6)]),
        ("double click 2", [A("ui.chooser.select", number=2, count=2)]),
        ("right click number three", [A("ui.chooser.select", number=3, button="right")]),
        ("middle click 1", [A("ui.chooser.select", number=1, button="middle")]),
        ("cancel", [A("ui.chooser.cancel")]),
        ("none", [A("ui.chooser.cancel")]),
        ("none of them", [A("ui.chooser.cancel")]),
        ("scroll down", [A("mouse.scroll", direction="down")]),
    ],
)
def test_number_chooser(run, utterance: str, expected: list) -> None:
    assert run(utterance, overlay="chooser") == expected


def test_overlay_numbers_stop_at_their_last_item(decide) -> None:
    # Six targets at most in the chooser, eight tiles in the menu: a bigger number is not theirs.
    assert decide("7", mode="command", overlay="chooser").kind == "rejected"
    assert decide("9", mode="command", overlay="menu").kind == "rejected"
    assert decide("8", mode="command", overlay="menu").kind == "command"


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("3", [A("ui.menu.select", number=3)]),
        ("number two", [A("ui.menu.select", number=2)]),
        ("double click", [A("ui.menu.select", item="double click")]),
        ("right click", [A("ui.menu.select", item="right click")]),
        ("middle click", [A("ui.menu.select", item="middle click")]),
        ("context menu", [A("ui.menu.select", item="context menu")]),
        ("drag", [A("ui.menu.select", item="drag")]),
        ("drag from here", [A("ui.menu.select", item="drag from here")]),
        ("drop", [A("ui.menu.select", item="drop")]),
        ("drop here", [A("ui.menu.select", item="drop here")]),
        ("scroll", [A("ui.menu.select", item="scroll")]),
        ("scroll here", [A("ui.menu.select", item="scroll here")]),
        ("zoom", [A("ui.menu.select", item="zoom")]),
        ("zoom here", [A("ui.menu.select", item="zoom here")]),
        ("show numbers", [A("ui.menu.select", item="show numbers")]),
        ("keyboard", [A("ui.menu.select", item="keyboard")]),
        ("show keyboard", [A("ui.menu.select", item="show keyboard")]),
        ("select the right click", [A("ui.menu.select", item="right click")]),
        # The scroll controls the menu shows once scrolling ("stop" stops scrolling everywhere).
        ("stop scrolling", [A("ui.menu.select", item="stop scrolling")]),
        ("up", [A("ui.menu.select", item="up")]),
        ("Scroll up.", [A("ui.menu.select", item="scroll up")]),
        ("down", [A("ui.menu.select", item="down")]),
        ("scroll down", [A("ui.menu.select", item="scroll down")]),
        ("8", [A("ui.menu.select", number=8)]),
        ("close the menu", [A("ui.menu.hide")]),
        ("cancel", [A("ui.cancel"), A("key.release_all")]),
        ("click", [A("mouse.click", button="left")]),  # not an item: the global command
    ],
)
def test_action_menu(run, utterance: str, expected: list) -> None:
    assert run(utterance, overlay="menu") == expected


@pytest.mark.parametrize("utterance", ["cancel", "no", "No!", "don't", "Don't.", "do not", "wait"])
def test_confirm_countdown_is_cancelled(run, utterance: str) -> None:
    assert run(utterance, overlay="confirm") == [A("ui.cancel")]


def test_confirm_countdown_takes_nothing_else(run) -> None:
    # "stop" and "never mind" cancel through their usual commands; everything else means what it always does.
    assert run("stop", overlay="confirm") == [A("key.release_all")]
    assert run("never mind", overlay="confirm") == [A("ui.cancel"), A("key.release_all")]
    assert run("scroll down", overlay="confirm") == [A("mouse.scroll", direction="down")]


def test_overlay_words_keep_their_meaning_elsewhere(run, decide) -> None:
    # "up", "faster" and "right click" only change meaning while their window or menu is open.
    assert run("up") == [K("up")]
    assert run("right click") == [A("mouse.click", button="right")]
    assert run("stop") == [A("key.release_all")]
    assert decide("faster", mode="command").reason == "Not scrolling"
    for word in ("tremor", "more", "set to three"):
        assert decide(word, mode="command").kind == "rejected", word


def test_bare_numbers_need_an_overlay(decide) -> None:
    assert decide("12", mode="command").kind == "rejected"
    assert decide("next", mode="command").kind == "rejected"


# ---------------------------------------------------------------------- state: repeat, scrolling speed, wake


def test_repeat_last_command(run, bctx) -> None:
    from sightline.core.types import Action

    assert run("again") == []  # nothing to repeat yet
    bctx.last_command = (Action("key.press", {"combo": "ctrl+z"}, source="voice"),)
    assert run("again") == [K("ctrl+z")]
    assert run("repeat three times") == [K("ctrl+z")] * 3
    assert run("do that again") == [K("ctrl+z")]


def test_scroll_speed_needs_scrolling(run, decide) -> None:
    decision = decide("scroll faster", mode="command")
    assert decision.kind == "rejected"
    assert decision.reason == "Not scrolling"
    run("keep scrolling down")
    assert run("faster") == [A("mouse.scroll_start", direction="down", speed=1.5)]
    assert run("slower") == [A("mouse.scroll_start", direction="down", speed=1.0)]
    run("stop scrolling")
    assert run("faster") == []


def test_wake_returns_to_previous_mode(run, bctx) -> None:
    bctx.current_mode = "sleep"
    bctx.previous_mode = "dictation"
    assert run("wake up", mode="sleep") == [A("speech.mode", mode="dictation")]


def test_wake_when_awake_does_nothing(run, bctx) -> None:
    bctx.current_mode = "auto"
    assert run("wake up") == []


def test_custom_wake_phrase() -> None:
    from sightline.speech.interpret import Interpreter, Situation
    from sightline.speech.matcher import Grammar
    from sightline.speech.grammar import BuildContext

    grammar = Grammar(build_commands() + sleep_and_wake("hey computer", "goodnight computer"))
    interp = Interpreter(grammar)
    ctx = BuildContext()
    ctx.current_mode = "sleep"
    decision = interp.decide("Hey, computer!", Situation(mode="sleep"), ctx)
    assert decision.kind == "command"
    assert decision.outcomes[0].actions[0].args == {"mode": "auto"}
    ctx.current_mode = "auto"
    decision = interp.decide("goodnight computer", Situation(mode="auto"), ctx)
    assert decision.outcomes[0].actions[0].args == {"mode": "sleep"}


# ---------------------------------------------------------------------- pieces


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("enter", [("enter", 1)]),
        ("control shift t", [("ctrl+shift+t", 1)]),
        ("down down enter", [("down", 2), ("enter", 1)]),
        ("tab 3 times", [("tab", 3)]),
        ("down down down", [("down", 3)]),
        ("alt f 4", [("alt+f4", 1)]),
        ("bravo", [("b", 1)]),
        ("capital b", [("shift+b", 1)]),
        ("comma", [(",", 1)]),
        ("not a key", None),
        ("", None),
    ],
)
def test_parse_key_sequence(words: str, expected) -> None:
    assert parse_key_sequence(words) == expected


@pytest.mark.parametrize(
    ("words", "url"),
    [
        ("wikipedia dot org", "wikipedia.org"),
        ("bbc dot co dot uk", "bbc.co.uk"),
        ("github.com", "github.com"),
        ("www dot google dot com", "www.google.com"),
        ("docs dot python dot org slash 3", "docs.python.org/3"),
        ("the shops", None),
        ("bed", None),
        # Only web pages (review S-09): a scheme must be said in full, and other schemes never pass.
        ("http colon slash slash example dot com", "http://example.com"),
        ("https colon slash slash example dot com slash news", "https://example.com/news"),
        ("httpfoo", None),
        ("file colon slash slash slash c", None),
        ("ms-settings colon privacy dot com", None),
        ("search-ms colon query dot com", None),
    ],
)
def test_spoken_url(words: str, url: str | None) -> None:
    assert spoken_url(words) == url


# ---------------------------------------------------------------------- coverage of the action catalogue


def _catalogue_actions() -> set[str]:
    text = (Path(__file__).resolve().parents[2] / "ARCHITECTURE.md").read_text("utf-8")
    section = text.split("## 7. Action catalogue", 1)[1].split("## 8.", 1)[0]
    names: set[str] = set()
    for cell in re.findall(r"^\| `([^|]+)` \|", section, re.MULTILINE):
        parts = [p.strip(" `") for p in cell.split("/")]
        base = parts[0]
        names.add(base)
        prefix = base.rsplit(".", 1)[0]
        for extra in parts[1:]:
            names.add(prefix + extra if extra.startswith(".") else extra)
    return names


def _produced_actions(grammar) -> set[str]:
    import ast
    import inspect

    import sightline.speech.grammar as g

    source = inspect.getsource(g)
    produced = set(re.findall(r'_a\(\s*"([a-z_.]+)"', source))
    produced |= set(re.findall(r'_simple\(\s*"([a-z_.]+)"', source))
    if "_keys(" in source or "_seq(" in source:
        produced.add("key.press")
    produced.add("text.type")  # dictation
    ast.parse(source)
    return produced


# Every catalogue action that has a voice command (ARCHITECTURE.md section 7: wave 1, the review
# changes and wave 2).  Actions added to the catalogue later show up as a warning in the test
# summary until they get commands.
VOICE_ACTIONS = frozenset(
    """
    mouse.click mouse.down mouse.up mouse.scroll mouse.scroll_start mouse.scroll_stop mouse.move
    key.press key.hold key.release key.release_all text.type text.select text.select_range text.caret
    login.fill element.click window.switch window.close window.minimize window.maximize window.restore
    window.snap window.next window.previous desktop.show app.open system.volume system.media web.go
    web.search keyboard.show keyboard.hide keyboard.toggle magnifier.toggle magnifier.zoom
    pointer.pause pointer.resume pointer.toggle pointer.mode pointer.recenter pointer.nudge pointer.restart
    pointer.reading_scroll
    dwell.on dwell.off dwell.toggle dwell.click_type speech.mode speech.scratch speech.listen speech.choose
    ui.labels.show ui.labels.hide ui.labels.select ui.grid.show ui.grid.hide ui.grid.back ui.grid.select
    ui.grid.click ui.zoom.show ui.zoom.hide ui.help.show ui.help.hide ui.settings.show ui.calibrate
    ui.settings.next ui.settings.previous ui.settings.adjust ui.settings.set ui.settings.preset
    ui.practice.show ui.chooser.select ui.chooser.cancel ui.menu.show ui.menu.hide ui.menu.select
    ui.dock.toggle ui.wizard.next ui.wizard.back ui.wizard.skip ui.cancel app.quit action.undo
    """.split()
)
# Only from the eyes, the dock or another subsystem; window.place is internal (the reversal of snap,
# minimise and maximise for "undo that").
NOT_BY_VOICE = frozenset({"mouse.drag_toggle", "window.place"})


def test_speech_reaches_every_voice_action(grammar) -> None:
    import warnings

    catalogue = _catalogue_actions()
    assert "mouse.click" in catalogue and "speech.scratch" in catalogue
    produced = _produced_actions(grammar)
    missing = VOICE_ACTIONS - produced
    assert not missing, f"no voice command produces: {sorted(missing)}"
    assert VOICE_ACTIONS <= catalogue, f"renamed or removed from the catalogue: {sorted(VOICE_ACTIONS - catalogue)}"
    unknown = produced - catalogue
    assert not unknown, f"speech produces actions missing from the catalogue: {sorted(unknown)}"
    newer = catalogue - produced - NOT_BY_VOICE
    if newer:
        warnings.warn(f"catalogue actions with no voice command yet: {', '.join(sorted(newer))}", stacklevel=1)


def test_help_rows_cover_every_category(grammar) -> None:
    rows = help_rows(list(grammar.specs.values()))
    categories = {r.category for r in rows}
    assert {"Mouse", "Keys", "Text editing", "Dictation", "Windows and apps", "Web browser", "Sound and media", "Sightline", "Numbers and grid", "Setup"} <= categories
    for row in rows:
        assert row.phrase and row.description
        assert "\u2014" not in row.description and "\u2014" not in row.phrase  # no em-dashes in user text
    assert len(rows) >= 100


def _with_history(bctx) -> None:
    from sightline.core.types import Action

    bctx.last_command = (Action("key.press", {"combo": "ctrl+z"}, source="voice"),)
    bctx.continuous_scroll = ("down", 1.0)


def test_help_examples_actually_work(grammar, run, bctx) -> None:
    _with_history(bctx)
    rows = help_rows(list(grammar.specs.values()))
    for row in rows:
        if not row.example:
            continue
        overlay = {"Numbers and grid": "labels", "Settings window": "settings", "Action menu": "menu"}.get(row.category)
        works = run(row.example, overlay=overlay, apply=False) or run(row.example, overlay=overlay, editable=True, apply=False)
        assert works, f"help example does not work: {row.example!r}"


def test_every_command_has_help_or_shares_a_row(grammar) -> None:
    # Commands without their own row are variants listed under another (e.g. "zoom out" under "zoom in").
    with_help = sum(1 for s in grammar.specs.values() if s.help)
    assert with_help >= 0.6 * len(grammar.specs)


def test_test_set_commands_all_parse(run, bctx) -> None:
    from tools.speech_testset import COMMANDS, OVERLAY_COMMANDS

    _with_history(bctx)
    for item in COMMANDS:
        text = item if isinstance(item, str) else item[0]
        assert run(text, apply=False), f"test-set command not understood: {text!r}"
    for item in OVERLAY_COMMANDS:
        text, overlay = item if isinstance(item, tuple) else (item, "labels")
        assert run(text, overlay=overlay, apply=False), f"overlay command not understood: {text!r}"


# ---------------------------------------------------------------------- what Sightline learns: vocabulary and alias offers


@pytest.mark.parametrize(
    ("utterance", "words"),
    [
        ("add that to vocabulary", ""),
        ("Add it to my vocabulary.", ""),
        ("add Kubernetes to the vocabulary", "kubernetes"),
        ("add shiv on to vocabulary", "shiv on"),
    ],
)
def test_add_to_vocabulary_works_while_dictating_too(decide, utterance: str, words: str) -> None:
    for mode, editable in (("command", False), ("auto", True), ("dictation", True)):
        d = decide(utterance, mode=mode, editable=editable)
        assert d.kind == "command", (mode, d.kind)
        match = d.parse.matches[0]
        assert match.spec.id == "speech.vocabulary" and str(match.slots.get("words", "")) == words


def test_yes_only_answers_an_offer(decide) -> None:
    assert decide("yes", mode="command", offer=True).parse.matches[0].spec.id == "speech.remember"
    assert decide("yes", mode="command").kind == "rejected"
