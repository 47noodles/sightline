"""The crash watchdog behind the ``sightline`` entry point.

Every test runs a fake child: a function standing in for the app, or a tiny script
that stands in for its process.  No test starts a real Sightline, and the scripts
run under ``pythonw`` with no console, so nothing appears on screen.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from sightline import app
from sightline.app import RESTARTED_NOTICE, Watchdog
from sightline.core.bus import EventBus
from sightline.core.events import Notice

CRASH = 0xC0000005  # an access violation, as Windows reports a crashed process


class FakeApp:
    """Plays the app: each run returns the next exit code, and may leave the quit file first."""

    def __init__(self, *codes: int, quits: tuple[int, ...] = ()) -> None:
        self.codes = list(codes)
        self.quits = quits  # 1-based runs in which the user quits before the exit code comes
        self.runs: list[list[str]] = []

    def __call__(self, command) -> int:
        command = list(command)
        self.runs.append(command)
        if len(self.runs) in self.quits:
            app.note_quit(command[command.index("--quit-file") + 1])
        return self.codes[len(self.runs) - 1]


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def watchdog(tmp_path: Path, fake: FakeApp, *, clock: Clock | None = None, ending: bool = False, step_s: float = 0.0) -> Watchdog:
    clock = clock or Clock()
    slept: list[float] = []

    def spawn(command) -> int:
        code = fake(command)
        clock.t += step_s  # how long that run lasted
        return code

    dog = Watchdog(["app"], quit_file=tmp_path / "quit.flag", spawn=spawn, session_ending=lambda: ending, clock=clock, sleep=slept.append)
    dog.slept = slept  # type: ignore[attr-defined]
    return dog


# =========================================================================== deciding when to restart


def test_a_normal_quit_never_restarts(tmp_path):
    fake = FakeApp(0)
    assert watchdog(tmp_path, fake).run(["--debug"]) == 0
    assert len(fake.runs) == 1
    assert fake.runs[0] == ["app", "--debug", "--quit-file", str(tmp_path / "quit.flag")]


def test_a_crash_starts_it_again_and_says_so(tmp_path):
    fake = FakeApp(CRASH, 1, 0)
    dog = watchdog(tmp_path, fake)
    assert dog.run(["--no-speech", "--setup"]) == 0
    assert len(fake.runs) == 3
    first, second, third = fake.runs
    assert "--restarted" not in first and first[1:3] == ["--no-speech", "--setup"]
    assert second[-1] == "--restarted" and third[-1] == "--restarted", "the app shows a toast when it sees this"
    assert "--no-speech" in second, "the user's own choices carry over"
    assert "--setup" not in second, "finished set-up is not run again (unfinished set-up runs anyway)"
    assert dog.slept == [dog.delay_s, dog.delay_s]


def test_at_most_three_restarts_in_ten_minutes(tmp_path):
    fake = FakeApp(*[1] * 10)
    dog = watchdog(tmp_path, fake, step_s=60.0)  # a crash a minute
    assert dog.run() == 1
    assert len(fake.runs) == 4, "the first run and three restarts, then it gives up"


def test_restarts_further_apart_than_that_carry_on(tmp_path):
    fake = FakeApp(1, 1, 1, 1, 1, 0)
    dog = watchdog(tmp_path, fake, step_s=240.0)  # a crash every four minutes
    assert dog.run() == 0
    assert len(fake.runs) == 6, "the oldest restart leaves the ten minutes before the fourth"


def test_quitting_is_never_a_crash_even_when_closing_goes_wrong(tmp_path):
    fake = FakeApp(CRASH, quits=(1,))  # the user quit, then the teardown crashed
    dog = watchdog(tmp_path, fake)
    assert dog.run() == 0
    assert len(fake.runs) == 1
    dog.clean_up()
    assert not (tmp_path / "quit.flag").exists()


def test_a_quit_file_left_from_an_earlier_run_does_not_stop_a_restart(tmp_path):
    (tmp_path / "quit.flag").write_text("quit", "utf-8")
    fake = FakeApp(CRASH, 0)
    assert watchdog(tmp_path, fake).run() == 0
    assert len(fake.runs) == 2


def test_windows_signing_out_is_not_a_crash(tmp_path):
    fake = FakeApp(1, 0)
    assert watchdog(tmp_path, fake, ending=True).run() == 1
    assert len(fake.runs) == 1


def test_an_app_that_cannot_start_is_not_retried_forever(tmp_path):
    def missing(_command) -> int:
        raise FileNotFoundError("no python")

    dog = Watchdog(["app"], quit_file=tmp_path / "quit.flag", spawn=missing, session_ending=lambda: False, sleep=lambda s: None)
    assert dog.run() == 1


def test_the_child_cannot_be_stood_in_for_by_the_working_folder(tmp_path):
    # Review S-10: "python -m sightline" put the working folder first on sys.path; -P leaves it out.
    dog = Watchdog(quit_file=tmp_path / "quit.flag")
    assert dog.command == [sys.executable, "-P", "-m", "sightline"]


def test_giving_up_says_so_on_screen_with_where_the_log_is(tmp_path):
    # Review S-06: the watchdog stopped silently, and someone who uses only eyes and voice could not tell why.
    from sightline.core import paths

    fake = FakeApp(CRASH, CRASH, CRASH, CRASH)
    shown: list[str] = []
    dog = watchdog(tmp_path, fake)
    dog.message = shown.append
    assert dog.run() == CRASH
    assert len(shown) == 1
    assert "has not been started again" in shown[0] and str(paths.logs_dir()) in shown[0]


def test_a_start_that_fails_says_so_too(tmp_path):
    def missing(_command) -> int:
        raise FileNotFoundError("no python")

    shown: list[str] = []
    dog = Watchdog(["app"], quit_file=tmp_path / "quit.flag", spawn=missing, session_ending=lambda: False, sleep=lambda s: None, message=shown.append)
    assert dog.run() == 1
    assert len(shown) == 1 and shown[0].startswith("Sightline could not be started.")


def test_restarts_and_quits_say_nothing(tmp_path):
    shown: list[str] = []
    dog = watchdog(tmp_path, FakeApp(CRASH, 0))
    dog.message = shown.append
    assert dog.run() == 0
    assert shown == []


def test_the_watchdog_shows_its_messages_in_a_message_box(monkeypatch, message_boxes):
    made: dict = {}

    class Dog:
        def __init__(self, *args, **kwargs) -> None:
            made.update(kwargs)

        def run(self, args) -> int:
            made["message"]("Sightline stopped")
            return 0

        def clean_up(self) -> None:
            pass

    monkeypatch.setattr(app, "Watchdog", Dog)
    monkeypatch.setattr(app, "_watchdog_logging", lambda: None)
    assert app.supervise([]) == 0
    assert made["message"] is app.show_message
    assert message_boxes == ["Sightline stopped"]


def test_a_message_box_that_fails_is_only_logged(monkeypatch):
    def broken(text, title):
        raise OSError("no desktop")

    monkeypatch.setattr(app, "_message_box", broken)
    app.show_message("hello")  # does not raise


def test_help_version_and_mistyped_options_never_start_the_app(monkeypatch, capsys):
    class NoChild:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("no child process should start")

    monkeypatch.setattr(app, "Watchdog", NoChild)
    monkeypatch.setattr(app, "_watchdog_logging", lambda: None)
    assert app.supervise(["--version"]) == 0
    assert "Sightline" in capsys.readouterr().out
    assert app.supervise(["--no-such-option"]) == 2, "argparse's usage error, not three restarts of it"
    assert "--no-such-option" in capsys.readouterr().err


def test_exit_codes_read_as_windows_shows_them():
    assert app._exit_text(1) == "1"
    assert app._exit_text(CRASH) == "0xC0000005"
    assert app._exit_text(-1073741819) == "0xC0000005"


# =========================================================================== the app's side


def test_the_app_takes_the_watchdogs_flags_without_showing_them():
    args = app._parse_args(["--restarted", "--quit-file", r"C:\x\quit.flag", "--debug"])
    assert args.restarted and args.quit_file == r"C:\x\quit.flag" and args.debug
    plain = app._parse_args([])
    assert not plain.restarted and plain.quit_file is None


def test_quitting_leaves_the_quit_file(tmp_path):
    flag = tmp_path / "quit.flag"
    app.note_quit(str(flag))
    assert flag.read_text("utf-8") == "quit"
    app.note_quit(None)  # not started by the watchdog: nothing to tell
    app.note_quit(tmp_path / "missing" / "quit.flag")  # a folder that went away is logged, not raised


def test_a_second_copy_says_so_instead_of_vanishing(message_boxes):
    # Review S-12: a taken name, or one Windows will not let us open, means "not alone": say so and stop.
    closed: list[int] = []
    taken = app._SingleInstance(create=lambda name: (77, app.ERROR_ALREADY_EXISTS), close=closed.append)
    assert not taken.acquire() and closed == [77]
    refused = app._SingleInstance(create=lambda name: (0, 5), close=closed.append)  # 5: access denied
    assert not refused.acquire()
    first = app._SingleInstance(create=lambda name: (88, 0), close=closed.append)
    assert first.acquire()
    first.release()
    first.release()
    assert closed == [77, 88]
    app.already_running()
    assert message_boxes == [app.ALREADY_RUNNING_MESSAGE]


def test_the_app_shows_the_message_when_another_copy_runs(monkeypatch, message_boxes):
    from types import SimpleNamespace

    monkeypatch.setattr(app, "_SingleInstance", lambda: SimpleNamespace(acquire=lambda: False, release=lambda: None))
    monkeypatch.setattr(app, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(app, "_enable_dpi_awareness", lambda: None)
    assert app.main([]) == 0
    assert message_boxes == [app.ALREADY_RUNNING_MESSAGE]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows mutexes")
def test_the_mutex_reports_a_name_that_is_already_taken():
    import uuid

    name = f"Local\\SightlineTests.{uuid.uuid4().hex}"
    first, error = app._create_mutex(name)
    try:
        assert first and error != app.ERROR_ALREADY_EXISTS
        second, error = app._create_mutex(name)
        assert second and error == app.ERROR_ALREADY_EXISTS
        app._close_handle(second)
    finally:
        app._close_handle(first)


def test_start_up_problems_become_notices():
    # Review S-14 and S-06: running as administrator, and a settings file that had to be set aside.
    from types import SimpleNamespace

    from sightline.settings import BROKEN_SETTINGS_NOTICE, Settings

    bus = EventBus()
    seen: list[Notice] = []
    bus.subscribe(Notice, seen.append)
    damaged = Settings()
    damaged.load_problem = BROKEN_SETTINGS_NOTICE
    app.announce_start_problems(bus, damaged, SimpleNamespace(guard=SimpleNamespace(is_elevated_self=lambda: True)))
    assert [(n.key, n.level) for n in seen] == [("settings-damaged", "warn"), ("app-elevated", "warn")]
    assert seen[0].text == BROKEN_SETTINGS_NOTICE and "does not need to" in seen[1].text
    seen.clear()
    app.announce_start_problems(bus, Settings(), SimpleNamespace(guard=SimpleNamespace(is_elevated_self=lambda: False)))

    def broken() -> bool:
        raise OSError("no token")

    app.announce_start_problems(bus, Settings(), SimpleNamespace(guard=SimpleNamespace(is_elevated_self=broken)))
    app.announce_start_problems(bus, Settings(), None)
    assert seen == []


def test_starting_with_windows_follows_the_setting(run_key):
    command = r'"C:\Sightline\Scripts\sightline.exe"'
    assert app.sync_autostart(True, command=command) == "added"
    assert run_key == {"Sightline": command}
    assert app.sync_autostart(True, command=command) == "unchanged"
    assert app.sync_autostart(True, command=r'"D:\Moved\sightline.exe"') == "updated"
    assert app.sync_autostart(False) == "removed" and run_key == {}
    assert app.sync_autostart(False) == "unchanged"


def test_a_first_run_adds_no_run_entry(run_key):
    """Starting with Windows is off until someone turns it on, so a first run on someone's PC never adds one."""
    from sightline.settings import Settings

    settings = Settings()
    assert settings.general.start_with_windows is False
    app._sync_autostart_quietly(settings)
    assert run_key == {}


def test_the_run_entry_is_the_supervised_program_never_python_m(monkeypatch, tmp_path, run_key):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    monkeypatch.setattr(app.sysconfig, "get_path", lambda name: str(scripts))
    assert app.autostart_command() is None, "no sightline program there"
    assert app.sync_autostart(True) == "unavailable" and run_key == {}
    (scripts / "sightline.exe").write_bytes(b"")
    assert app.autostart_command() == f'"{scripts / "sightline.exe"}"'
    assert app.sync_autostart(True) == "added"
    assert "python" not in run_key["Sightline"].lower() and "-m" not in run_key["Sightline"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\Sightline\Sightline.exe")
    assert app.autostart_command() == r'"C:\Program Files\Sightline\Sightline.exe"'


def test_the_restarted_app_says_so():
    bus = EventBus()
    seen: list[Notice] = []
    bus.subscribe(Notice, seen.append)
    app.announce_restart(bus)
    assert [n.text for n in seen] == [RESTARTED_NOTICE] == ["Sightline restarted after a problem."]
    assert seen[0].level == "warn" and seen[0].key == "app-restarted"


# =========================================================================== with a real child process

# A stand-in for the app's process: notes its arguments, then exits as the plan says.
CHILD = """
import json, sys
from pathlib import Path

here = Path(__file__).parent
with (here / "runs.jsonl").open("a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
runs = (here / "runs.jsonl").read_text("utf-8").splitlines()
step = json.loads((here / "plan.json").read_text("utf-8"))[len(runs) - 1]
if step.get("quit"):
    args = sys.argv[1:]
    Path(args[args.index("--quit-file") + 1]).write_text("quit", "utf-8")
sys.exit(step["code"])
"""


def _windowless_python() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return str(pythonw) if pythonw.exists() else sys.executable


@pytest.mark.skipif(sys.platform != "win32", reason="the watchdog runs on Windows")
def test_a_real_child_process_is_restarted_and_its_quit_is_respected(tmp_path):
    script = tmp_path / "fake_sightline.py"
    script.write_text(CHILD, "utf-8")
    # Crash, then quit and crash while closing: two runs, no third.
    (tmp_path / "plan.json").write_text(json.dumps([{"code": 7}, {"code": 3, "quit": True}, {"code": 0}]), "utf-8")
    dog = Watchdog([_windowless_python(), str(script)], quit_file=tmp_path / "quit.flag", delay_s=0.0, session_ending=lambda: False)
    assert dog.run(["--no-vision"]) == 0
    runs = [json.loads(line) for line in (tmp_path / "runs.jsonl").read_text("utf-8").splitlines()]
    assert len(runs) == 2
    assert runs[0] == ["--no-vision", "--quit-file", str(tmp_path / "quit.flag")]
    assert runs[1] == ["--no-vision", "--quit-file", str(tmp_path / "quit.flag"), "--restarted"]
