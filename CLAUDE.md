# Sightline: notes for Claude Code

Sightline lets someone use a Windows PC with only their eyes and voice:
webcam gaze and head pointer, blink, wink and face-gesture clicks, offline
speech commands and dictation.  Python 3.13 and PySide6, Apache-2.0.

**Status: alpha.**  Every planned feature is built and the safe test suite
passes (1656 tests).  Nothing has been tuned on a real webcam, microphone
or screen yet; every threshold is a prior.  The next job is the first
session on real hardware.

**Start here with Lachlan:** walk him through `docs/FIRST-TEST.md` (install,
wizard, a twelve-step try-out, how to stop it), collect his notes, then
move on to the full plan in `docs/TESTING.md`.

## Layout and ownership

Five subsystems under `src/sightline/`, each with an owner:

| Path | Owner | What |
|---|---|---|
| `core/`, `settings.py`, `app.py` | lead | types, events, bus, actions, risk policy, settings, models, wiring, watchdog |
| `vision/` | vision | camera, face landmarks, gaze, head pointer, eye and face gestures, calibration |
| `speech/` | speech | microphone, VAD, recognisers, grammar, interpretation, dictation |
| `desktop/` | desktop | SendInput, UI Automation, windows, apps, On-Screen Keyboard, Magnifier |
| `ui/` | ui | Qt shell, overlays, dock, wizards, settings window, sounds |

`ARCHITECTURE.md` is the contract between them: code against its
interfaces, events and action catalogue, not another subsystem's
internals.  Each subsystem's design notes, tests and known limits are in
`docs/modules/<subsystem>.md`.  Tests live in `tests/<subsystem>/`, tools
in `tools/`.  A test parses ARCHITECTURE.md's "## 7. Action catalogue"
section, so keep that heading and table format.

## Set up and run

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Dev   # .venv, pip, pip install -e ".[dev]"; -DryRun only prints
.venv\Scripts\sightline-console.exe   # for testing: no watchdog, log in the console
.venv\Scripts\sightline.exe           # for use: under the watchdog, restarted after a crash
```

Quit from the tray icon.  Ending the app in Task Manager counts as a
crash, and the watchdog restarts it (at most three times in ten minutes).
The first run downloads about 620 MB of models into
`%LOCALAPPDATA%\Sightline\models`; never commit model files.

## Tests

The safe suite, which touches no camera, microphone, window, sound or
input (about 40 seconds on a desktop, about 6 minutes on a laptop):

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider -m "not desktop and not gui and not slow and not models"
```

**`-m desktop`, and `tests/desktop/test_live_desktop.py` in particular,
move the real cursor, type real keys and open windows** (Notepad,
Explorer, Chrome, Edge, the On-Screen Keyboard).  So do the tools in
`docs/TESTING.md`, and some of them beep or use the camera.  Run them only
on a machine whose owner is sitting at it, and warn the owner first.
Leave `SIGHTLINE_LIVE_DESKTOP`, `SIGHTLINE_LIVE_AUDIO` and
`SIGHTLINE_UI_TESTS_ON_SCREEN` unset otherwise.  `models` tests need the
downloaded models; `gui` tests create Qt windows.

## Conventions

* Every user-visible string is plain English: short, no jargon, no emoji,
  no em-dashes.  No text-to-speech anywhere; feedback is earcons and
  captions.
* Coordinates are physical pixels in the virtual desktop; only the UI
  converts to Qt logical pixels.  Times are `time.perf_counter()` seconds.
* Everything that acts goes through the `ActionRegistry` (`core/actions.py`)
  as a named action, so undo, the countdown and logging see it.
* What is hard to undo is decided only in `core/risk.py`; web addresses only
  in `core/urls.py`.  Do not keep another list.
* Safety settings are `setting(protected=True)` (`ui.confirm_risky`,
  `speech.attention_gate`, `speech.mouth_gate`): voice may turn them on,
  never off.
* The log never holds what was said, typed, clicked or visited.
* Docs and comments: explain why, two spaces after a full stop, no
  em-dashes, no marketing words.

## Where to look next

* `docs/ROADMAP.md`: done, next, known gaps.
* `docs/TESTING.md`: the first real-hardware session, in order, and where
  results go.
* `docs/reviews/`: the concept review (item numbers used in the roadmap)
  and the security review, with a status under every finding.

## Open decisions for Lachlan (the owner)

* Left wink as double click instead of middle click.
* Downloading Smart Turn v3 (about 8 MB) for dictation endpoints.
* Keeping both Parakeet models required (about 590 MB at set-up) or making
  the 0.6B optional.
* Whether the set-up wizard should ask about starting with Windows (it now
  defaults to off).
