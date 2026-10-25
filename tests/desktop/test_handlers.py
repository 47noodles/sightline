"""Desktop action handlers against the in-memory fakes: argument handling, snapping, state, messages."""

from __future__ import annotations

import dataclasses
import threading

import pytest

from sightline.core.actions import ActionError
from sightline.core.events import ActionExecuted, ClickPerformed
from sightline.core.fakes import FakeApps, FakeDisplay, FakeInput, FakeUIA, FakeWindows
from sightline.core.types import Action, AppInfo, ElementInfo, FocusInfo, Rect, WindowInfo
from sightline.desktop.config import DesktopConfig
from sightline.desktop.elevation import ELEVATED_MESSAGE, SECURE_DESKTOP_MESSAGE
from sightline.desktop.handlers import (
    LOGIN_NO_FIELD_MESSAGE,
    LOGIN_NO_MANAGER_MESSAGE,
    OSK_BLOCKED_MESSAGE,
    PASSWORD_MANAGER_KEYS,
    PASSWORD_MANAGER_NAMES,
    PASSWORD_TEXT_MESSAGE,
    SEARCH_URL,
    TEXT_UNREADABLE_MESSAGE,
    DesktopActions,
)
from sightline.desktop.keys import KeyComboError, parse_combo
from sightline.desktop.textedit import TextEditResult, TextQuery
from sightline.desktop.uia import ElementRef

ARCHITECTURE_DESKTOP_ACTIONS = {
    "mouse.click", "mouse.down", "mouse.up", "mouse.drag_toggle", "mouse.scroll", "mouse.scroll_start",
    "mouse.scroll_stop", "mouse.move", "key.press", "key.hold", "key.release", "key.release_all", "text.type",
    "text.select", "text.select_range", "text.caret", "login.fill",
    "element.click", "window.switch", "window.close", "window.minimize", "window.maximize", "window.restore",
    "window.snap", "window.next", "window.previous", "desktop.show", "app.open", "system.volume", "system.media",
    "web.go", "web.search", "keyboard.show", "keyboard.hide", "keyboard.toggle", "magnifier.toggle", "magnifier.zoom",
}  # fmt: skip


class Guard:
    def __init__(self) -> None:
        self.point = False
        self.foreground = False
        self.secure = False

    def point_blocked(self, x, y):
        return self.point

    def foreground_blocked(self):
        return self.foreground

    def window_blocked(self, hwnd):
        return False

    def secure_desktop_active(self):
        return self.secure


class Scroller:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.running = False

    def start(self, direction, speed=1.0):
        self.calls.append(("start", direction, speed))
        self.running = True

    def stop(self):
        self.calls.append(("stop",))
        was = self.running
        self.running = False
        return was


class Keyboard:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[str] = []
        self.showing = False

    def show(self):
        self.calls.append("show")
        self.showing = self.ok
        return self.ok

    def hide(self):
        self.calls.append("hide")
        self.showing = False
        return True

    def toggle(self):
        self.calls.append("toggle")
        self.showing = not self.showing
        return self.showing


class Magnifier:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def toggle(self):
        self.calls.append("toggle")
        return True

    def zoom(self, direction):
        self.calls.append(direction)


SAVE = ElementInfo(Rect(100, 100, 80, 30), "Save", "Button", clickable=True, handle=ElementRef("scan", (1, 0), 77))
OPEN = ElementInfo(Rect(300, 100, 80, 30), "Open", "Button", clickable=True, handle=ElementRef("scan", (1, 1), 77))


@pytest.fixture
def env(ctx):
    inp = FakeInput()
    uia = FakeUIA(elements=[SAVE, OPEN])
    windows = FakeWindows(
        windows=[
            WindowInfo(11, "Report - Word", "WINWORD.EXE", Rect(0, 0, 800, 600)),
            WindowInfo(12, "YouTube - Google Chrome", "chrome.exe", Rect(0, 0, 800, 600)),
            WindowInfo(13, "Gmail - Google Chrome", "chrome.exe", Rect(0, 0, 800, 600)),
            WindowInfo(14, "Untitled - Notepad", "Notepad.exe", Rect(0, 0, 800, 600)),
        ]
    )
    apps = FakeApps(items=[AppInfo("Google Chrome", "c.lnk"), AppInfo("Notepad", "n", "uwp"), AppInfo("File Explorer", "e.lnk")])
    opened: list[str] = []
    guard = Guard()
    scroller = Scroller()
    keyboard = Keyboard()
    magnifier = Magnifier()
    covered: dict[str, int] = {"root": 77}
    actions = DesktopActions(
        ctx,
        input=inp,
        uia=uia,
        windows=windows,
        apps=apps,
        display=FakeDisplay(),
        guard=guard,
        keyboard=keyboard,
        magnifier=magnifier,
        scroller=scroller,
        open_url=lambda url: opened.append(url) or True,
        window_at=lambda x, y: covered["root"],
        is_osk_window=lambda hwnd: hwnd == 999,
    )
    actions.register(ctx.actions)
    clicks: list[ClickPerformed] = []
    ctx.bus.subscribe(ClickPerformed, clicks.append)

    class Env:
        pass

    e = Env()
    e.ctx, e.input, e.uia, e.windows, e.apps, e.opened, e.guard = ctx, inp, uia, windows, apps, opened, guard
    e.scroller, e.keyboard, e.magnifier, e.clicks, e.actions, e.covered = scroller, keyboard, magnifier, clicks, actions, covered

    def run(action_name, /, source="voice", **args):
        return ctx.actions.execute_now(Action(action_name, args, source))

    e.run = run
    return e


def test_every_desktop_action_in_the_architecture_is_registered(env):
    assert ARCHITECTURE_DESKTOP_ACTIONS <= set(env.ctx.actions.names())


# ------------------------------------------------------------------ clicking


def test_click_inside_an_element_clicks_the_point(env):
    result = env.run("mouse.click", source="eye", x=110, y=105)
    assert env.input.calls[-1] == ("click", ("left", 1, 110, 105))
    assert result["element"] == "Save" and not result["snapped"]
    assert env.clicks == [ClickPerformed(110, 105, "left", 1, "eye")]


def test_click_near_an_element_snaps_to_it(env):
    result = env.run("mouse.click", source="dwell", x=200, y=110)
    assert env.input.calls[-1] == ("click", ("left", 1, 140.0, 115.0))
    assert result["snapped"] and result["element"] == "Save"
    assert env.clicks[-1].x == 140.0 and env.clicks[-1].source == "dwell"


def test_snap_can_be_turned_off_per_click_and_in_settings(env):
    env.run("mouse.click", x=200, y=110, snap=False)
    assert env.input.calls[-1] == ("click", ("left", 1, 200, 110))
    env.ctx.settings.desktop.snap_to_targets = False
    env.run("mouse.click", x=200, y=110)
    assert env.input.calls[-1] == ("click", ("left", 1, 200, 110))


def test_click_without_coordinates_uses_the_cursor_and_counts(env):
    env.input.pos = (500, 600)
    env.run("mouse.click", button="right", count=2)
    assert env.input.calls[-1] == ("click", ("right", 2, 500, 600))
    env.run("mouse.click", button="middle", count="3")
    assert env.input.calls[-1] == ("click", ("middle", 3, 500, 600))


@pytest.mark.parametrize(("args", "message"), [({"button": "thumb"}, "left, right or middle"), ({"count": "lots"}, "1, 2 or 3"), ({"x": "far"}, "x must be a number")])
def test_bad_click_arguments(env, args, message):
    with pytest.raises(ActionError, match=message):
        env.run("mouse.click", **args)


def test_click_on_an_administrator_window_explains_why(env):
    env.guard.point = True
    with pytest.raises(ActionError) as info:
        env.run("mouse.click", x=10, y=10)
    assert str(info.value) == ELEVATED_MESSAGE
    assert "click" not in env.input.names()
    assert env.clicks == []


def test_click_on_the_on_screen_keyboard_explains_hover(env):
    env.guard.point = True
    env.covered["root"] = 999
    with pytest.raises(ActionError, match="rest the pointer") as info:
        env.run("mouse.click", x=10, y=10)
    assert str(info.value) == OSK_BLOCKED_MESSAGE


def test_secure_desktop_blocks_everything_with_a_message(env):
    env.guard.secure = True
    for name, args in (("mouse.click", {}), ("key.press", {"combo": "enter"}), ("text.type", {"text": "hi"})):
        with pytest.raises(ActionError) as info:
            env.run(name, **args)
        assert str(info.value) == SECURE_DESKTOP_MESSAGE


# ------------------------------------------------------------------ dragging


def test_down_up_and_drag_toggle_keep_drag_state(env):
    env.run("mouse.down", x=10, y=20)
    assert env.ctx.state.drag_active
    assert env.input.calls[-2:] == [("move_cursor", (10.0, 20.0)), ("button_down", ("left",))]
    env.run("mouse.up", x=50, y=60)
    assert not env.ctx.state.drag_active
    assert env.input.calls[-1] == ("button_up", ("left",))
    env.run("mouse.drag_toggle", x=1, y=2)
    assert env.ctx.state.drag_active
    env.run("mouse.drag_toggle", x=300, y=400)
    assert not env.ctx.state.drag_active
    assert env.input.calls[-2:] == [("move_cursor", (300.0, 400.0)), ("button_up", ("left",))]


def test_click_while_dragging_drops(env):
    env.run("mouse.drag_toggle", x=1, y=2)
    result = env.run("mouse.click", x=700, y=500)
    assert result["dropped"]
    assert not env.ctx.state.drag_active
    assert "click" not in env.input.names()


# ------------------------------------------------------------------ scrolling and moving


def test_scroll_defaults_and_directions(env):
    env.run("mouse.scroll", direction="down")
    assert env.input.calls[-1] == ("scroll", (3.0, 0.0))  # scroll_step
    env.run("mouse.scroll", direction="up", amount=5)
    assert env.input.calls[-1] == ("scroll", (-5.0, -0.0))
    env.run("mouse.scroll", direction="right", amount=2, x=400, y=300)
    assert env.input.calls[-2:] == [("move_cursor", (400.0, 300.0)), ("scroll", (0.0, 2.0))]
    with pytest.raises(ActionError, match="up, down, left or right"):
        env.run("mouse.scroll", direction="sideways")


def test_continuous_scroll_and_what_stops_it(env):
    env.run("mouse.scroll_start", direction="up", speed=2)
    assert env.scroller.calls[-1] == ("start", "up", 2.0)
    env.run("mouse.scroll_stop")
    assert env.scroller.calls[-1] == ("stop",)
    env.run("mouse.scroll_start", direction="down")
    env.run("mouse.click", x=1, y=1)
    assert ("stop",) in env.scroller.calls[-2:]
    env.run("mouse.scroll_start", direction="down")
    env.run("key.release_all")
    assert env.scroller.calls[-1] == ("stop",)
    assert env.input.calls[-1] == ("release_all", ())
    assert not env.ctx.state.drag_active


def test_mouse_move_variants(env):
    env.input.pos = (100, 100)
    env.run("mouse.move", x=5, y=6)
    assert env.input.pos == (5.0, 6.0)
    env.run("mouse.move", dx=10, dy=-3)
    assert env.input.pos == (15.0, 3.0)
    env.run("mouse.move", to="center")
    assert env.input.pos == (960.0, 540.0)
    env.run("mouse.move", to="top left")
    assert env.input.pos == (192.0, 108.0)
    env.run("mouse.move", to="bottom")
    assert env.input.pos == (960.0, 972.0)
    with pytest.raises(ActionError):
        env.run("mouse.move", to="the moon")
    with pytest.raises(ActionError):
        env.run("mouse.move")


# ------------------------------------------------------------------ keys and text


def test_key_press_and_repeat(env):
    env.run("key.press", combo="ctrl+shift+t")
    assert env.input.calls[-1] == ("key_combo", ("ctrl+shift+t", 1))
    env.run("key.press", combo="down", repeat=4)
    assert env.input.calls[-1] == ("key_combo", ("down", 4))
    with pytest.raises(ActionError, match="which key"):
        env.run("key.press")


def test_unknown_key_becomes_a_plain_message(env, monkeypatch):
    def boom(combo, repeat=1):
        raise KeyComboError("I do not know the key “blorp”")

    monkeypatch.setattr(env.input, "key_combo", boom)
    with pytest.raises(ActionError, match="I do not know the key"):
        env.run("key.press", combo="blorp")


def test_keys_to_an_administrator_window_explain_why(env):
    env.guard.foreground = True
    for name, args in (("key.press", {"combo": "enter"}), ("text.type", {"text": "x"}), ("key.hold", {"key": "shift"})):
        with pytest.raises(ActionError) as info:
            env.run(name, **args)
        assert str(info.value) == ELEVATED_MESSAGE
    assert env.input.calls == []


def test_hold_release_and_type(env):
    env.run("key.hold", key="shift")
    env.run("key.release", key="shift")
    env.run("text.type", text="Hello, world.\nBye \U0001f600")
    env.run("text.type", text="")
    assert env.input.calls == [("key_down", ("shift",)), ("key_up", ("shift",)), ("type_text", ("Hello, world.\nBye \U0001f600",))]


# ------------------------------------------------------------------ elements


def test_element_click_by_name(env):
    result = env.run("element.click", name="open")
    assert result["element"] == "Open"
    assert env.input.calls[-1] == ("click", ("left", 1, 340.0, 115.0))
    assert env.clicks[-1].source == "voice"


def test_element_click_not_found_message(env):
    with pytest.raises(ActionError) as info:
        env.run("element.click", name="Publish")
    assert str(info.value) == "I could not find “Publish” on the screen"


def test_covered_element_is_invoked_instead(env):
    env.covered["root"] = 5  # another window sits over the element's centre
    result = env.run("element.click", name="save")
    assert result == {"element": "Save", "invoked": True}
    assert env.uia.invoked == [SAVE]
    assert "click" not in env.input.names()
    env.uia.invoke = lambda element, timeout=0.5: False
    with pytest.raises(ActionError, match="could not click it"):
        env.run("element.click", name="save")


# ------------------------------------------------------------------ windows


def test_window_switch_fuzzy_and_messages(env):
    env.run("window.switch", name="notepad")
    assert env.windows.activated[-1] == 14
    env.run("window.switch", name="gmail")
    assert env.windows.activated[-1] == 13
    with pytest.raises(ActionError) as info:
        env.run("window.switch", name="spotify")
    assert str(info.value) == "I could not find a window called Spotify"
    env.run("window.switch", hwnd=12)
    assert env.windows.activated[-1] == 12


def test_switching_to_the_current_app_moves_to_its_next_window(env):
    env.windows.windows.insert(0, env.windows.windows.pop(1))  # YouTube Chrome window in front
    env.run("window.switch", name="chrome")
    assert env.windows.activated[-1] == 13


def test_window_buttons_pass_the_handle(env):
    env.run("window.close")
    env.run("window.minimize", hwnd=12)
    env.run("window.maximize", hwnd=13)
    env.run("window.restore")
    assert env.windows.calls == [("close", None), ("minimize", 12), ("maximize", 13), ("restore", None)]


def test_window_snap(env):
    env.run("window.snap", side="left")
    assert env.input.calls[-1] == ("key_combo", ("win+left", 1))
    with pytest.raises(ActionError):
        env.run("window.snap", side="diagonal")


def test_next_and_previous_window_cycle(env):
    env.run("window.next")
    env.run("window.next")
    env.run("window.previous")
    assert env.windows.activated == [12, 13, 12]


def test_show_desktop(env):
    env.run("desktop.show")
    assert env.input.calls[-1] == ("key_combo", ("win+d", 1))


# ------------------------------------------------------------------ apps


def test_app_open(env):
    assert env.run("app.open", name="chrome") == {"app": "Google Chrome"}
    assert env.apps.launched[-1].name == "Google Chrome"
    env.run("app.open", name="files")
    assert env.apps.launched[-1].name == "File Explorer"


def test_app_open_messages(env):
    with pytest.raises(ActionError) as info:
        env.run("app.open", name="spotify")
    assert str(info.value) == "I could not find an app called Spotify"
    env.apps.launch = lambda app: False
    with pytest.raises(ActionError) as info:
        env.run("app.open", name="notepad")
    assert str(info.value) == "Notepad did not start"


# ------------------------------------------------------------------ system


def test_volume_and_media(env):
    env.run("system.volume", direction="up")
    assert env.input.calls[-1] == ("key_combo", ("volume up", 1))
    env.run("system.volume", direction="down", amount=10)
    assert env.input.calls[-1] == ("key_combo", ("volume down", 5))
    env.run("system.volume", direction="mute")
    assert env.input.calls[-1] == ("key_combo", ("volume mute", 1))
    for key, expected in (("play_pause", "play pause"), ("next", "next track"), ("previous", "previous track"), ("stop", "media stop")):
        env.run("system.media", key=key)
        assert env.input.calls[-1] == ("key_combo", (expected, 1))
    with pytest.raises(ActionError):
        env.run("system.volume", direction="sideways")
    with pytest.raises(ActionError):
        env.run("system.media", key="rewind")


# ------------------------------------------------------------------ web


def test_web_go_uses_the_focused_browser(env):
    env.windows.windows.insert(0, env.windows.windows.pop(1))  # Chrome in front
    env.run("web.go", url="bbc.co.uk")
    assert [c for c in env.input.calls if c[0] in ("key_combo", "type_text")] == [
        ("key_combo", ("ctrl+l", 1)),
        ("type_text", ("bbc.co.uk",)),
        ("key_combo", ("enter", 1)),
    ]


def test_web_go_brings_a_background_browser_forward(env):
    env.run("web.go", url="example.com")
    assert env.windows.activated == [12]


def test_web_without_a_browser_opens_the_default_one(env):
    env.windows.windows = [w for w in env.windows.windows if w.process != "chrome.exe"]
    env.run("web.go", url="example.com")
    env.run("web.search", query="weather in perth")
    assert env.opened == ["https://example.com", SEARCH_URL + "weather+in+perth"]


@pytest.mark.parametrize(
    "url",
    ["file:///c:/windows/system32/calc.exe", "ms-settings:privacy", "search-ms:query=x", "javascript:alert(1)", r"\\server\share", r"c:\windows", "shell:startup"],
)
@pytest.mark.parametrize("browser_open", [False, True])
def test_web_go_opens_only_web_pages_and_mail_links(env, url, browser_open):
    # Review S-09: with no browser open the address went to ShellExecute, which hands any scheme to its handler.
    if not browser_open:
        env.windows.windows = [w for w in env.windows.windows if w.process != "chrome.exe"]
    with pytest.raises(ActionError, match="only opens web pages and e-mail links"):
        env.run("web.go", url=url)
    assert env.opened == []
    assert not [c for c in env.input.calls if c[0] == "type_text"]


def test_web_go_still_opens_mail_links_and_full_addresses(env):
    env.windows.windows = [w for w in env.windows.windows if w.process != "chrome.exe"]
    env.run("web.go", url="mailto:sam@example.com")
    env.run("web.go", url="http://example.com/x")
    assert env.opened == ["mailto:sam@example.com", "http://example.com/x"]


def test_web_search_forces_search_for_address_like_queries(env):
    env.windows.windows.insert(0, env.windows.windows.pop(1))
    env.run("web.search", query="python.org")
    assert ("type_text", ("? python.org",)) in env.input.calls
    env.run("web.search", query="how tall is everest")
    assert ("type_text", ("how tall is everest",)) in env.input.calls


# ------------------------------------------------------------------ Windows accessibility tools


def test_keyboard_actions(env):
    env.run("keyboard.show")
    env.run("keyboard.hide")
    assert env.run("keyboard.toggle") == {"showing": True}
    assert env.keyboard.calls == ["show", "hide", "toggle"]
    env.keyboard.ok = False
    with pytest.raises(ActionError, match="did not open"):
        env.run("keyboard.show")


def test_magnifier_actions(env):
    assert env.run("magnifier.toggle") == {"on": True}
    env.run("magnifier.zoom", direction="in")
    env.run("magnifier.zoom", direction="out")
    assert env.magnifier.calls == ["toggle", "in", "out"]
    with pytest.raises(ActionError):
        env.run("magnifier.zoom", direction="sideways")


def test_messages_have_no_em_dashes(env):
    messages = []
    for name, args in (
        ("mouse.click", {"button": "x"}),
        ("app.open", {"name": "zzz"}),
        ("window.switch", {"name": "zzz"}),
        ("element.click", {"name": "zzz"}),
        ("mouse.scroll", {"direction": "zzz"}),
        ("window.snap", {"side": "zzz"}),
    ):
        with pytest.raises(ActionError) as info:
            env.run(name, **args)
        messages.append(str(info.value))
    assert all("—" not in m and "–" not in m for m in messages)


# ------------------------------------------------------------------ aim: acting where the user looked when they began to speak


def aimed(action_name, /, aim_t=5.0, **args):
    return Action(action_name, args, "voice", aim_t=aim_t)


def test_spoken_click_acts_where_the_user_looked(env):
    env.ctx.vision.aim = (305.4, 110.2)
    env.input.pos = (900, 900)
    result = env.ctx.actions.execute_now(aimed("mouse.click"))
    assert env.ctx.vision.aim_calls == [5.0]
    assert env.input.calls[0] == ("move_cursor", (305.0, 110.0))  # the highlight shows the target first
    assert env.input.calls[-1] == ("click", ("left", 1, 305.0, 110.0))
    assert result["element"] == "Open"
    assert env.clicks[-1] == ClickPerformed(305.0, 110.0, "left", 1, "voice", aim_t=5.0)


def test_aim_falls_back_to_the_cursor(env):
    class Broken:
        def aim_point(self, t):
            raise RuntimeError("a vision bug")

    env.input.pos = (500, 600)
    env.ctx.vision.aim = None
    for vision in (env.ctx.vision, None, object(), Broken()):
        env.ctx.vision = vision
        env.input.calls.clear()
        env.ctx.actions.execute_now(aimed("mouse.click"))
        assert env.input.calls == [("click", ("left", 1, 500, 600))]
    assert env.clicks[-1].aim_t == 5.0


def test_explicit_coordinates_win_over_the_aim(env):
    env.ctx.vision.aim = (305, 110)
    env.ctx.actions.execute_now(Action("mouse.click", {"x": 110, "y": 105}, "eye", aim_t=5.0))
    assert env.ctx.vision.aim_calls == []
    assert env.input.calls == [("click", ("left", 1, 110, 105))]


def test_scroll_here_scrolls_where_the_user_looked(env):
    env.ctx.vision.aim = (640, 360)
    env.ctx.actions.execute_now(aimed("mouse.scroll", direction="down"))
    assert env.input.calls == [("move_cursor", (640.0, 360.0)), ("scroll", (3.0, 0.0))]


def test_duplicate_names_go_to_the_one_nearest_the_aim(env):
    lower = ElementInfo(Rect(100, 700, 80, 30), "Save", "Button", clickable=True, handle=ElementRef("scan", (1, 2), 77))
    env.uia.elements.append(lower)
    env.ctx.vision.aim = (150, 690)
    result = env.ctx.actions.execute_now(aimed("element.click", name="save"))
    assert (result["x"], result["y"]) == (140.0, 715.0)
    assert env.clicks[-1].aim_t == 5.0
    env.ctx.vision.aim = None  # no aim: nearest the cursor
    env.input.pos = (120, 90)
    result = env.ctx.actions.execute_now(aimed("element.click", name="save"))
    assert (result["x"], result["y"]) == (140.0, 115.0)


# ------------------------------------------------------------------ undo


@pytest.fixture
def queued(env):
    """Run actions through the real queue, which is where core remembers them for "undo that"."""
    results: list[ActionExecuted] = []
    changed = threading.Condition()

    def on(event):
        with changed:
            results.append(event)
            changed.notify_all()

    env.ctx.bus.subscribe(ActionExecuted, on)
    env.ctx.actions.start()

    def run(name, /, source="voice", **args) -> ActionExecuted:
        with changed:
            start = len(results)
        env.ctx.actions.dispatch(Action(name, args, source))
        with changed:
            assert changed.wait_for(lambda: len(results) > start, timeout=2.0)
            return results[start]

    yield run
    env.ctx.actions.stop()


class PlacingWindows(FakeWindows):
    def placement(self, hwnd):
        return {"show": 1, "flags": 0, "min": [-1, -1], "max": [-1, -1], "normal": [10, 10, 810, 610], "rect": [3, 10, 817, 617]}

    def place(self, hwnd, placement):
        self.calls.append(("place", hwnd))


def test_undo_minimise_brings_the_window_back(env, queued):
    assert queued("window.minimize").ok
    assert queued("action.undo").ok
    assert env.windows.calls == [("minimize", None)]
    assert env.windows.activated == [11]  # Word was in front


def test_undo_maximise_and_restore_return_to_the_previous_state(env, queued):
    queued("window.maximize", hwnd=13)
    queued("action.undo")
    assert env.windows.calls[-2:] == [("maximize", 13), ("restore", 13)]
    env.windows.windows[3] = dataclasses.replace(env.windows.windows[3], maximized=True)
    queued("window.restore", hwnd=14)
    queued("action.undo")
    assert env.windows.calls[-2:] == [("restore", 14), ("maximize", 14)]


def test_undo_snap_puts_the_window_back(env, queued):
    placing = PlacingWindows(windows=list(env.windows.windows))
    env.actions.windows = placing
    queued("window.snap", side="left")
    assert env.input.calls[-1] == ("key_combo", ("win+left", 1))
    assert queued("action.undo").ok
    assert placing.calls == [("place", 11)]
    placing.windows[0] = dataclasses.replace(placing.windows[0], maximized=True)
    queued("window.snap", side="right")
    queued("action.undo")
    assert placing.calls[-1] == ("maximize", 11)


def test_undo_scroll_scrolls_back_at_the_same_point(env, queued):
    queued("mouse.scroll", direction="down", amount=4, x=400, y=300)
    env.input.pos = (10, 10)  # the pointer has moved on since
    assert queued("action.undo").ok
    assert env.input.calls[-2:] == [("move_cursor", (400.0, 300.0)), ("scroll", (-4.0, 0.0))]


def test_undo_closing_a_browser_tab_reopens_it(env, queued):
    env.windows.windows.insert(0, env.windows.windows.pop(1))  # Chrome in front
    queued("key.press", combo="ctrl+w")
    assert queued("action.undo").ok
    assert env.windows.activated[-1] == 12
    assert env.input.calls[-1] == ("key_combo", ("ctrl+shift+t", 1))


def test_other_key_presses_cannot_be_undone(env, queued):
    queued("key.press", combo="ctrl+w")  # Word is in front: no browser tab to reopen
    event = queued("action.undo")
    assert not event.ok and "can't undo" in event.error


def test_undo_dictation_asks_speech_to_scratch_it(env, queued):
    scratched: list[str] = []
    env.ctx.actions.register("speech.scratch", lambda a: scratched.append(a.source))
    queued("text.type", text="hello there")
    assert queued("action.undo").ok
    assert scratched == ["voice"]
    queued("text.type", source="ui", text="typed from the dock")
    assert not queued("action.undo").ok


def test_moving_the_pointer_and_the_keyboard_are_not_the_last_thing_done(env, queued):
    queued("mouse.scroll", direction="down", x=400, y=300)
    queued("mouse.move", x=5, y=6)
    queued("keyboard.toggle")
    queued("key.release_all")
    queued("action.undo")
    assert env.input.calls[-1] == ("scroll", (-3.0, 0.0))


def test_undo_switch_volume_and_media(env, queued):
    queued("window.switch", name="notepad")
    queued("action.undo")
    assert env.windows.activated == [14, 11]
    queued("system.volume", direction="up", amount=10)
    queued("action.undo")
    assert env.input.calls[-1] == ("key_combo", ("volume down", 5))
    queued("system.media", key="play")
    queued("action.undo")
    assert env.input.calls[-1] == ("key_combo", ("play pause", 1))


# ------------------------------------------------------------------ editing text by content


def test_select_walks_with_arrow_keys_when_the_field_cannot_select_itself(env):
    env.uia.caret_text = "The quick brown fox"
    assert env.run("text.select", phrase="quick") == {"method": "arrow keys"}
    assert env.input.calls == [("key_combo", ("left", 10)), ("key_combo", ("shift+left", 5))]
    env.input.calls.clear()
    env.run("text.select_range", start="quick", end="brown")
    assert env.input.calls == [("key_combo", ("left", 4)), ("key_combo", ("shift+left", 11))]
    env.input.calls.clear()
    env.run("text.caret", where="before", phrase="brown")
    assert env.input.calls == [("key_combo", ("left", 9))]


def test_the_ui_automation_path_is_used_when_the_field_supports_it(env):
    seen: list[TextQuery] = []

    def edit_text(query, timeout=1.0):
        seen.append(query)
        return TextEditResult("done")

    env.uia.edit_text = edit_text
    assert env.run("text.select", phrase="  quick   brown ") == {"method": "text range"}
    assert env.run("text.caret", phrase="fox") == {"method": "text range"}
    assert seen == [TextQuery("select", "quick brown"), TextQuery("caret", "fox", where="after")]
    assert env.input.calls == []
    env.uia.edit_text = lambda q, timeout=1.0: TextEditResult("walk", keys=(("left", 1), ("shift+right", 3)))
    env.run("text.select", phrase="x")
    assert env.input.calls == [("key_combo", ("left", 1)), ("key_combo", ("shift+right", 3))]
    env.uia.edit_text = lambda q, timeout=1.0: TextEditResult("too_far")
    with pytest.raises(ActionError, match="too far"):
        env.run("text.select", phrase="x")
    env.uia.edit_text = lambda q, timeout=1.0: None  # UI Automation did not answer: try the keys
    env.uia.caret_text = "abc xyz"
    env.input.calls.clear()
    env.run("text.select", phrase="abc")
    assert env.input.calls == [("key_combo", ("left", 4)), ("key_combo", ("shift+left", 3))]


def test_text_messages(env):
    env.uia.caret_text = "The quick brown fox"
    with pytest.raises(ActionError) as info:
        env.run("text.select", phrase="purple")
    assert str(info.value) == "I could not find “purple” in this text"
    with pytest.raises(ActionError) as info:
        env.run("text.select_range", start="quick", end="zebra")
    assert str(info.value) == "I could not find “zebra” near “quick”"
    with pytest.raises(ActionError, match="where the selection ends"):
        env.run("text.select_range", start="quick")
    with pytest.raises(ActionError, match="before or after"):
        env.run("text.caret", where="inside", phrase="fox")
    env.uia.caret_text = None
    with pytest.raises(ActionError) as info:
        env.run("text.select", phrase="fox")
    assert str(info.value) == TEXT_UNREADABLE_MESSAGE
    env.uia.focus = FocusInfo(editable=True, password=True)
    with pytest.raises(ActionError) as info:
        env.run("text.select", phrase="fox")
    assert str(info.value) == PASSWORD_TEXT_MESSAGE
    env.uia.focus = FocusInfo(editable=True)
    env.guard.foreground = True
    with pytest.raises(ActionError) as info:
        env.run("text.caret", phrase="fox")
    assert str(info.value) == ELEVATED_MESSAGE
    assert env.input.calls == []


# ------------------------------------------------------------------ sign-ins


def test_every_password_manager_choice_has_keys_or_says_why_not():
    choices = next(f for f in dataclasses.fields(DesktopConfig) if f.name == "password_manager").metadata["choices"]
    for value in choices:
        if value in ("", "custom"):
            continue
        assert value in PASSWORD_MANAGER_KEYS or value in PASSWORD_MANAGER_NAMES
    for keys in PASSWORD_MANAGER_KEYS.values():
        parse_combo(keys)  # every default is a combo the injector understands


def test_fill_login_presses_the_password_managers_keys(env):
    env.uia.focus = FocusInfo(editable=True, process="chrome.exe")
    with pytest.raises(ActionError) as info:
        env.run("login.fill")
    assert str(info.value) == LOGIN_NO_MANAGER_MESSAGE
    for manager, keys in PASSWORD_MANAGER_KEYS.items():
        env.ctx.settings.desktop.password_manager = manager
        assert env.run("login.fill") == {"manager": manager}
        assert env.input.calls[-1] == ("key_combo", (keys, 1))
    env.ctx.settings.desktop.password_manager_keys = "ctrl+alt+l"  # the user changed the shortcut
    env.run("login.fill")
    assert env.input.calls[-1] == ("key_combo", ("ctrl+alt+l", 1))


def test_fill_login_messages(env, monkeypatch):
    desktop = env.ctx.settings.desktop
    desktop.password_manager = "bitwarden"
    env.uia.focus = FocusInfo(editable=False)
    with pytest.raises(ActionError) as info:
        env.run("login.fill")
    assert str(info.value) == LOGIN_NO_FIELD_MESSAGE
    env.uia.focus = FocusInfo(editable=True)
    desktop.password_manager = "protonpass"
    with pytest.raises(ActionError, match="Proton Pass has no keys"):
        env.run("login.fill")
    desktop.password_manager = "custom"
    with pytest.raises(ActionError, match="Enter the keys"):
        env.run("login.fill")
    desktop.password_manager_keys = "ctrl+blorp"

    def boom(combo, repeat=1):
        raise KeyComboError("I do not know the key “blorp”")

    monkeypatch.setattr(env.input, "key_combo", boom)
    with pytest.raises(ActionError, match="are not keys I know"):
        env.run("login.fill")
    desktop.password_manager_keys = ""
    desktop.password_manager = "bitwarden"
    env.guard.foreground = True
    with pytest.raises(ActionError) as info:
        env.run("login.fill")
    assert str(info.value) == ELEVATED_MESSAGE
    assert env.input.calls == []

@pytest.mark.parametrize("keys", ["alt+f4", "Alt + F4", "delete", "ctrl+w", "ctrl+shift+delete", "ctrl+q", "win+r", "alt+f4+a"])
def test_fill_login_never_presses_keys_that_close_or_delete(env, keys):
    # Review S-04: these keys are pressed here, not through key.press, so the countdown never sees them.
    env.uia.focus = FocusInfo(editable=True, process="chrome.exe")
    env.ctx.settings.desktop.password_manager = "custom"
    env.ctx.settings.desktop.password_manager_keys = keys
    with pytest.raises(ActionError, match="would close or delete something"):
        env.run("login.fill")
    assert not [c for c in env.input.calls if c[0] == "key_combo"]


def test_undo_right_after_selecting_text_refuses_instead_of_scratching(env, queued):
    scratched: list[Action] = []
    env.ctx.actions.register("speech.scratch", scratched.append)
    env.uia.caret_text = "hello there"
    queued("text.type", text="hello there")
    assert queued("text.select", phrase="there").ok
    event = queued("action.undo")
    assert not event.ok and "can't undo" in event.error
    assert scratched == []  # a scratch now would backspace over the selected word first