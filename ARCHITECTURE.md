# Sightline architecture

Sightline lets someone use a Windows PC with only their eyes and voice.  A
webcam tracks the face; gaze moves the pointer and small head movements
fine-tune it; deliberate blinks and winks click.  Speech runs commands or
types, depending on context.  Everything runs locally on the CPU.

This file is the contract between the four subsystems.  Code against the
interfaces here, never against another subsystem's internals.

## 1. Layout and ownership

| Path | Owner | Contents |
|---|---|---|
| `src/sightline/core/` | lead | types, events, bus, actions, state, settings helpers, paths, models, interfaces, fakes |
| `src/sightline/settings.py`, `app.py` | lead | settings composition, entry point and wiring |
| `src/sightline/vision/` | vision | camera, face landmarks, gaze model, head pointer, blink and wink, dwell, calibration API |
| `src/sightline/speech/` | speech | microphone, VAD, ASR, endpointing, grammar and intents, dictation formatting |
| `src/sightline/desktop/` | desktop | SendInput, UI Automation, windows, apps, Windows on-screen keyboard and Magnifier, desktop actions |
| `src/sightline/ui/` | ui | Qt shell, overlays, dock, captions, labels, grid, zoom lens, setup and calibration wizards, settings, sounds |
| `src/sightline/assets/` | ui (sounds, fonts, icons); others may add subfolders | packaged read-only files |
| `tests/<subsystem>/` | that subsystem | pytest tests |
| `tools/` | anyone, own files only | dev harnesses, benchmarks; `desktop_lock.py` is shared |
| `docs/modules/<subsystem>.md` | that subsystem | design notes, how to test, known limits |

Rules for working in parallel (several people or agents in one checkout,
one of them the lead):

* Edit only files you own.  If you need a change in `core/`, send the lead
  the exact request (file, what, why); do not edit core.  The one
  exception: you may append dependencies to `pyproject.toml`
  (`[project].dependencies`, re-read the file right before editing, one
  small edit).
* Each subsystem may add private events in `<subsystem>/events.py`.
* Install packages only into `.venv` (`.venv\Scripts\python.exe -m pip install ...`),
  never globally, and add them to `pyproject.toml`.
* Only the lead commits.  Others do not run `git commit`, `git checkout`,
  `git stash` or anything else that changes git state in the shared
  working tree.
* Anything that moves the real mouse, types real keys, opens focus-stealing
  windows or screenshots the desktop must hold the desktop lock
  (`tools/desktop_lock.py`, pytest fixture `desktop_lock`, marker `desktop`).

## 2. Process and threads

One Python process, per-monitor DPI aware v2 (set before Qt starts).

| Thread | Owner | Work |
|---|---|---|
| Qt GUI | ui | all widgets, painting, sounds |
| `sightline-actions` | core | runs Actions in order (input injection, UIA calls) |
| camera + vision | vision | capture, MediaPipe, gaze, gestures; publishes events at camera rate |
| cursor pump | vision | eases the real cursor toward the pointer target at ~120 Hz |
| audio callback + speech | speech | capture, VAD, ASR, intents |
| `sightline-uia` | desktop | quick UI Automation questions (focus, element at a point, text before the caret), focus and hover polling |
| `sightline-uia-scan` | desktop | slow clickable scans (20-300 ms), kept apart so they never stall hover or speech |
| `sightline-winevents` | desktop | WinEvent hooks (foreground and focus changes), keep-awake |
| `sightline-scroll` | desktop | continuous scrolling while it runs |
| `sightline-apps` | desktop | Start menu and app list refresh |

MediaPipe, ONNX Runtime and NumPy release the GIL, so inference does not
stall the GUI.  Keep per-frame Python work small.

## 3. Coordinates and time

* Every coordinate in events, actions and interfaces is a **physical pixel
  in the virtual desktop**.  Only the UI converts to Qt logical pixels, at
  draw time, per screen (`QScreen.devicePixelRatio`, native geometry).
* Every timestamp is `time.perf_counter()` seconds.

## 4. Shared services (`sightline.core`)

* `EventBus` (`core/bus.py`): `subscribe(Type, fn)`, `publish(event)`.
  Callbacks run synchronously in the publisher's thread and must return in
  well under a millisecond.  Qt code subscribes through `ui/bridge.py`,
  which re-emits on the GUI thread.
* `ActionRegistry` (`core/actions.py`): `register(name, handler, thread="worker"|"ui", description=...)`,
  `dispatch(Action)` (never blocks), `execute_now(Action)` (synchronous,
  for chaining), `add_interceptor(fn, priority, owner, fail_closed=False)`.
  Raise `ActionError("plain English reason")` for expected failures; the UI
  shows the message.  An interceptor that raises is logged and skipped,
  unless it was added with `fail_closed=True`: then the action is dropped
  with `GUARD_FAILED_MESSAGE` ("a safety check failed").  Guards use it (the
  countdown does), so a broken guard never lets through what it guards.
* Risk policy (`core/risk.py`): the one list of what is hard to undo.
  `assess(action, focus, target=...) -> Risk | None` and `risk_of(...) ->
  str | None` (a plain description, "Closing this tab").  Key chords are
  normalised however they are spelled, with extra keys or modifiers still
  counted (Alt+F4+A is Alt+F4).  Speech keeps these actions away from
  paraphrase matches and the countdown holds them; nothing else keeps a
  list.  `core/urls.py`: `web_address(url)` allows only http, https and
  mailto (no scheme means https); everything that opens an address uses it.
* `AppState` (`core/state.py`): modes and toggles, `state.update(...)`
  publishes `StateChanged`.
* Settings: each subsystem's dataclass in `<subsystem>/config.py`, composed
  in `sightline/settings.py`.  Declare fields with
  `core.settings_base.setting(default, label=..., help=..., group=..., minimum=..., maximum=..., choices=...)`.
  The settings window is generated from this metadata.  After changing
  settings call `ctx.settings_changed("<section>")`; subsystems react to
  `SettingsChanged`.  `readonly=True` marks a value Sightline learns and
  shows but nobody sets.  `protected=True` marks a safety setting
  (`ui.confirm_risky`, `speech.attention_gate`, `speech.mouth_gate`): a
  voice-sourced action may turn it on but never off or weaker; the settings
  window refuses and says to blink or dwell on it instead.  User files are
  read with `settings_base.loads_strict` (no NaN, Infinity, 1e999 or nesting
  deeper than 32); a file that cannot be used is set aside as
  `*.broken.json` and the defaults are used, with a notice once the UI is
  up, so a damaged file never stops Sightline starting.
* `ModelManager` (`core/models.py`): declare `ModelSpec`s (URL, SHA-256,
  size, licence) in `<subsystem>/models.py`, register them in your
  `create()`.  The setup wizard downloads every required model with progress.
  Files live in `%LOCALAPPDATA%\Sightline\models\<id>\`.  Libraries that
  download their own files must be pointed at that folder.
* `AppContext` (`core/context.py`): what each `create(ctx)` receives: bus,
  actions, state, settings, models, and the other subsystems once built
  (`ctx.desktop` exists before vision and speech are created; `ctx.vision`
  and `ctx.speech` exist before the UI is created).
* Paths (`core/paths.py`), logging (`core/log.py`, use `logging.getLogger(__name__)`, never print).
  The log never holds what was said, typed, clicked or visited: no
  recognised words at INFO (at DEBUG only their kind and length, unless a
  developer turns on `general.log_speech`), no window titles, element
  names, queries or addresses.  `Action.describe()` writes `text`, `query`,
  `url`, `phrase`, `start`, `end` and `name` as their length, and an action
  marked `private=True` (typed or pressed into a password field) as
  `name(<private>)`.

## 5. Subsystem entry points

```python
sightline.desktop.create(ctx) -> Desktop        # core/interfaces.py: Desktop
sightline.vision.create(ctx)  -> VisionAPI      # core/interfaces.py: VisionAPI, CalibrationAPI
sightline.speech.create(ctx)  -> SpeechAPI      # core/interfaces.py: SpeechAPI
sightline.ui.create(ctx, qt_app) -> UIShell     # below
```

`Desktop.windows.window_at(x, y) -> WindowInfo | None` answers "which app
window is under this point" with one lookup, safe from any thread: the root
owner (a dialog or popup an app owns stands for the app's window), with
Sightline's own windows looked through, and None over the desktop or the
taskbar.  `list_windows()` walks every window; use it only for the whole list.

`UIShell` (only `app.py` calls it):

```python
class UIShell:
    def start(self) -> None            # tray, overlays, dock, HUD, ui.* actions, installs the UI invoker
    def run_setup(self, on_complete: Callable[[], None]) -> None   # first-run wizard, then on_complete()
    def ensure_models(self, on_ready: Callable[[], None]) -> None  # downloads missing models with a small progress window
    def stop(self) -> None
```

Start order in `app.py`: desktop.create, vision.create, speech.create,
ui.create, actions.start, desktop.start, shell.start, then (after setup or
model check) vision.start and speech.start on a background thread.
`desktop.start` first lets go of any key or mouse button Windows reports
held (key-ups and button-ups only), so a restart after a crash does not
inherit a held Shift or drag.  Notices about start-up (restarted after a
problem, a damaged settings file, running as administrator) are published
after `shell.start`, since nothing shows toasts before it.

The `sightline` entry point is a watchdog (`app.supervise`): it runs the app
as `python -P -m sightline` from the data folder (the working folder is
never on the import path) and starts it again after an abnormal exit, at
most three times in ten minutes.  When it gives up, or cannot start the app
at all, it shows a plain Windows message box saying so and where the log
is, since the person may have no other way to learn why Sightline is gone.
A second copy that finds the single-instance mutex taken (or cannot open
it) shows a message box too instead of closing silently.  Both go through
`app.show_message`, which every test replaces.

## 6. Events (`core/events.py`)

| Event | Publisher | Rate | Main readers |
|---|---|---|---|
| `FaceStatus` | vision | 2-5 Hz and on change | HUD, setup wizard |
| `CameraFrame` | vision | ~15 Hz while preview on | setup wizard, settings |
| `PointerSample` | vision | camera rate | pointer ring, gaze dot; `error_px` sizes snapping, the chooser and targets; `steered` (the head, a nudge or the mouse has moved the pointer since gaze last placed it) tells the chooser a click is where the user put it |
| `FaceGesture` | vision | on gesture | sounds, diagnostics (brow raise, smile, mouth open, cheek puff) |
| `CalibrationActive` | vision | calibration start and end | wizards (end on an unexpected False: a pipeline rebuild cancels calibration) |
| `GazeModelChanged` | vision | new gaze model | wizards, toasts (accuracy after calibration, drift fix or learning) |
| `EyeState` | vision | camera rate | eye calibration page, diagnostics |
| `EyeGesture` | vision | on gesture | sounds, diagnostics |
| `EyeHold` | vision | stage changes | sounds (ticks while eyes are shut), pointer ring |
| `DwellProgress` | vision | while dwelling | pointer ring |
| `GazeZoneProgress` | vision | while looking at the resume zone | resume indicator |
| `MicLevel` | speech | 10-20 Hz | HUD |
| `TranscriptPartial` / `TranscriptFinal` | speech | per utterance | captions |
| `IntentResult` | speech | per utterance | captions, sounds; `alternatives` become numbered "did you mean" chips |
| `SpeechStatus` | speech | on change | HUD, setup wizard |
| `FocusChanged` | desktop | on change | speech (context), diagnostics |
| `HoverTarget` | desktop | ~10 Hz when it changes | highlight box |
| `ClickPerformed` | desktop | per click | click ripple, sounds, vision (learning from clicks, anchored at `aim_t` when set) |
| `ActionExecuted` | core | per action | toasts on failure, diagnostics |
| `StateChanged`, `SettingsChanged`, `Notice`, `ModelProgress`, `AppLifecycle` | core and anyone | as needed | UI |

## 7. Action catalogue

Names, arguments and owners.  Coordinates are optional physical pixels.
Without them, an action that has an `aim_t` uses
`VisionAPI.aim_point(aim_t)`, and falls back to the cursor when that is
None (no vision, paused, yielding to a real mouse).  Speech produces most
of these and sets `aim_t` to the speech onset; eye gestures and dwell
produce the `mouse.*` ones with explicit coordinates (the pointer position
from just before the eyes closed).  `element.click` ranks duplicate names
by distance to the aim point.

### desktop

| Action | Arguments | Notes |
|---|---|---|
| `mouse.click` | `button="left"`, `count=1`, `x`, `y`, `snap=True` | Snaps to the nearest clickable within `snap_radius_px` when not already on one; publishes `ClickPerformed` |
| `mouse.down` / `mouse.up` | `button="left"`, `x`, `y` | Drag; sets `state.drag_active` |
| `mouse.drag_toggle` | `x`, `y` | Press if not dragging, else release |
| `mouse.scroll` | `direction`, `amount` (notches, default `scroll_step`), `x`, `y` | One-shot |
| `mouse.scroll_start` / `mouse.scroll_stop` | `direction`, `speed=1.0` | Continuous until stopped |
| `mouse.move` | `x`, `y` or `dx`, `dy` or `to` in center/top/bottom/left/right | On the current monitor |
| `key.press` | `combo`, `repeat=1`, `hwnd` | e.g. `ctrl+shift+t`; with `hwnd`, that window is brought forward first |
| `key.hold` / `key.release` | `key` | |
| `key.release_all` | | Releases keys, buttons, stops scrolling |
| `text.type` | `text` | Types exactly this (speech has already formatted it) |
| `text.select` | `phrase` | Select the occurrence of a phrase nearest the caret in the focused field ("select quick brown") |
| `text.select_range` | `start`, `end` | Select from one phrase to another ("select from quick to lazy") |
| `text.caret` | `where` in before/after, `phrase` | Move the caret next to a phrase ("move after brown") |
| `login.fill` | | Press the user's password manager autofill shortcut ("fill login"), so sign-ins need no typing |
| `element.click` | `name`, `button="left"`, `count=1` | Click a named element ("click Save") |
| `window.switch` | `name` or `hwnd` | Fuzzy over titles and process names |
| `window.close` / `.minimize` / `.maximize` / `.restore` | `hwnd` | Default: foreground |
| `window.snap` | `side` in left/right/up/down | |
| `window.place` | `hwnd`, `placement` | Internal (history=False): restores a saved placement; the reversal for snap, minimise and maximise |
| `window.next` / `window.previous` | | |
| `desktop.show` | | |
| `app.open` | `name` | Fuzzy over the Start menu |
| `system.volume` | `direction` in up/down/mute, `amount=2` | |
| `system.media` | `key` in play_pause/next/previous/stop | |
| `web.go` / `web.search` | `url` / `query` | Address bar of the focused browser |
| `keyboard.show` / `.hide` / `.toggle` | | Windows On-Screen Keyboard in hover mode |
| `magnifier.toggle` / `magnifier.zoom` | `direction` in in/out | Windows Magnifier |

### vision

| Action | Arguments |
|---|---|
| `pointer.pause` / `.resume` / `.toggle` | |
| `pointer.mode` | `mode` in hybrid/head/gaze |
| `pointer.recenter` | |
| `pointer.nudge` | `direction`, `amount=10` (px) |
| `pointer.restart` | (restart eye and head tracking with fresh state; "restart tracking") |
| `pointer.reading_scroll` | `on` (toggles when omitted): looking low on the page scrolls down, high scrolls up |
| `dwell.on` / `dwell.off` / `dwell.toggle` | |
| `dwell.click_type` | `type` in left/right/middle/double/drag |

### speech

| Action | Arguments |
|---|---|
| `speech.mode` | `mode` in auto/command/dictation/sleep |
| `speech.scratch` | (undo the last dictated phrase) |
| `speech.listen` | `on` (mute or unmute the microphone) |
| `speech.choose` | `index` (1-based): run a "did you mean" alternative from the last rejected utterance |

### ui

| Action | Arguments |
|---|---|
| `ui.labels.show` / `ui.labels.hide` | |
| `ui.labels.select` | `number`, `button="left"`, `count=1` |
| `ui.grid.show` / `ui.grid.hide` / `ui.grid.back` | |
| `ui.grid.select` | `cell` (1-9) |
| `ui.grid.click` | `button="left"`, `count=1` |
| `ui.zoom.show` / `ui.zoom.hide` | Lens at the pointer; the next click lands at the magnified spot |
| `ui.help.show` / `ui.help.hide` | "What can I say" sheet |
| `ui.settings.show` | |
| `ui.settings.next` / `ui.settings.previous` | Move between settings while the settings window is open |
| `ui.settings.adjust` | `direction` in up/down: one step of the current setting |
| `ui.settings.set` | `value`: a spoken number or choice for the current setting |
| `ui.settings.preset` | `name`, e.g. "steady hands", "tremor", "tired eyes" |
| `ui.practice.show` | The practice room (targets, tapping test, summary) |
| `ui.chooser.select` / `ui.chooser.cancel` | `number`, `button`, `count`, `token` (from a chip): pick a candidate when a gaze click was ambiguous (overlay `"chooser"`) |
| `ui.menu.show` / `ui.menu.hide` / `ui.menu.select` | The action menu at `x`, `y` or the aim point; select by `number` or `item`, `token` from a tile (overlay `"menu"`) |

Argument markers: `chosen=True` on a click the chooser re-runs, and
`confirmed=True` on an action the countdown releases, tell their own
interceptors to let it through; `private=True` on text or keys typed into a
password field keeps them out of the log.  Handlers ignore them.
| `ui.calibrate` | `kind` in eyes/gaze/head/gestures/drift/full; "fix pointer" is `drift` (look at one dot for 1.5 s) |
| `ui.dock.toggle` | |
| `ui.wizard.next` / `ui.wizard.back` / `ui.wizard.skip` | Setup and calibration wizards |
| `ui.cancel` | Close whatever overlay is open |
| `app.quit` | (registered by `app.py`) |

### core

| Action | Arguments |
|---|---|
| `action.undo` | ("undo that") reverses the last action the user caused, using the `undo` function its handler registered, or says plainly that it can't.  Handlers register `undo=` and `history=False` (see `core/actions.py`) |

While an overlay is open, `state.active_overlay` names it, and speech maps
words to it: bare numbers while `"labels"` or `"grid"` is open ("12" means
`ui.labels.select number=12`); "next", "continue", "done", "back", "skip"
while `"setup"` or `"calibration"` is open; "next", "back", "up", "down",
"set to <value>" and preset names while `"settings"` is open; bare numbers
and item names while `"chooser"` or `"menu"` is open; "cancel" or "stop"
while `"confirm"` (the countdown before a hard-to-reverse action) is open.

## 8. Interaction design (shared understanding)

* **Pointer (hybrid, default).**  Gaze is accurate to roughly 1-3 degrees
  with a webcam.  When the user fixates further than `warp_distance_px` from
  the pointer, the pointer jumps there; inside that radius, head rotation
  moves it relatively and precisely.  Head-only and gaze-only modes exist.
  Before gaze calibration the pointer runs head-only, so setup is usable
  with no calibration.
* **Eye clicks.**  Both eyes closed for `blink_click_ms` arms a left click
  (a tick sound), kept closed to `blink_double_ms` arms a double click (a
  second tick), longer than `blink_cancel_ms` cancels (resting).  The action
  fires when the eyes open, at the pointer position from just before they
  closed.  Right-eye wink is right click, left-eye wink is middle click
  (`gesture_actions`).  Natural blinks never click.
* **What you see is what you click.**  Desktop publishes `HoverTarget` with
  the element a click would hit after snapping; the UI outlines it.
* **Precision aids.**  Snapping, the zoom lens, numbered labels ("show
  numbers", then "click 12" or look at a number and blink), the 3x3 grid,
  `pointer.nudge`, and head fine-tuning.
* **Speech context.**  In auto mode an utterance that is entirely a command
  runs as a command; otherwise, if a text field has focus, it is typed; if
  not, it is reported as not understood.  "type ..." always types, "press
  ..." always presses keys.  Sleep mode ignores everything except the wake
  phrase.
* **Spoken commands aim where the user looked when they began to speak.**
  People fixate a target about 0.6 s before they say the verb, and by the
  time "click" has been recognised the pointer has often moved on.  Speech
  stamps every action with `aim_t` (the speech onset); handlers that act
  "here" use `aim_point(aim_t)`.  Click learning anchors there too.
* **Only speech meant for the computer acts.**  Speech asks
  `VisionAPI.attention(onset, end)`.  When the user was not facing the
  screen (or, where measured at set-up, their mouth was not moving), only
  the safety commands run, and only when said exactly: sleep and wake,
  stop, cancel, pause and resume eye control, "undo that" and help
  (`speech.interpret.SAFE_WHEN_AWAY`).  Nothing is typed and nothing else
  runs, because a voice on a call or in a video can say "open command
  prompt" exactly.  The caption says it was ignored, so the user knows why.
  Attention None (no vision) means no gate: voice-only users need it.  No
  command in `commands.toml` may take a safety command's words.
* **Recover, don't guess.**  A rejected utterance offers up to three
  numbered "did you mean" alternatives (`IntentResult.alternatives`);
  saying the number or blinking on the chip runs it.  Actions that are hard
  to reverse never run from a paraphrase match; they go to "did you mean"
  instead.
* **Hard to undo means a moment to stop it.**  What is hard to undo is
  decided in one place, `core/risk.py`: closing a window or tab, quitting,
  Delete and the shortcuts that clear, send or quit, Windows+R, opening a
  command window, PowerShell, Terminal, the registry editor or Run, Enter or
  a typed line into a command window, and clicking a control named Delete,
  Remove, Discard, Send, Submit, Pay, Buy or Empty Recycle Bin.  From voice,
  the eyes or dwell these wait for the countdown (`ui/overlays/confirm.py`,
  `confirm_risky`), which "cancel", "stop", "undo that" or a blink on its
  chip stops.  The countdown's interceptor is fail-closed.
* **Safety settings are changed with the eyes.**  Voice can turn
  `confirm_risky`, `attention_gate` and `mouth_gate` on but not off: a voice
  from a video must not be able to switch off the checks that stand between
  it and the computer.
* **Tracking survives its own bugs.**  A failure inside the vision pipeline
  costs one frame, not the session: it is logged, the filters reset, and
  repeated failures rebuild the pipeline.  "Restart tracking"
  (`pointer.restart`) does the same on request.
* **Eyes-only access.**  The dock's large buttons (pause, click type, scroll,
  keyboard, numbers, zoom, sleep, settings) work by blink or dwell.  While
  eye control is paused, looking at the resume zone resumes it.  The
  Windows On-Screen Keyboard in hover mode covers typing without voice
  (passwords included); the user's own keyboard settings are put back when
  Sightline closes it or quits.
* **The screen stays on while someone is there.**  `desktop.keep_awake`
  stops the screen turning off (and Windows locking a PC whose owner cannot
  type a password), but only while the camera has seen a face in the last
  `keep_awake_minutes`; an empty room locks as usual.  Without a camera the
  setting alone decides.
* **Passwords stay private.**  In a password field nothing heard is shown,
  and whatever was typed or pressed there (dictation, spelled keys, a
  snippet) shows as bullets and is never logged.
* **Feedback.**  Sounds for armed clicks, clicks, recognised and rejected
  commands, sleep and wake; captions for what was heard and done; a toast
  for every failed action.  No text-to-speech.

## 9. Reused open-source work

Prefer adapting existing permissively licensed code to writing new code.
When you copy or adapt code, keep its licence header, add a line to
`THIRD_PARTY_NOTICES.md` (lead merges these from your report if you cannot
edit it), and say in a comment where it came from.

| Source | Licence | Use |
|---|---|---|
| MediaPipe Face Landmarker | Apache-2.0 | 478 landmarks incl. iris, 52 blendshapes, head transform |
| EyeTrax (github.com/ck-zhang/EyeTrax) | MIT | gaze features, ridge model, filters, calibration patterns (vendored and adapted: it pins numpy<2) |
| Google Project GameFace (github.com/google/project-gameface) | Apache-2.0 | head-mouse smoothing and gesture binding ideas and code |
| shaal/eye-tracker | MIT | wink-mode and blink-rejection logic (TypeScript/Rust, port ideas) |
| moonshine-voice, sherpa-onnx | MIT, Apache-2.0 | streaming ASR and VAD |
| hassil | Apache-2.0 | sentence templates with slots for the command grammar |
| talonhub/community | MIT | command vocabulary: alphabet, keys, symbols, formatters |
| text2num | MIT | spoken numbers |
| rapidfuzz | MIT | fuzzy matching |
| model2vec / fastembed | MIT / Apache-2.0 | small sentence embeddings for paraphrased commands |
| pynput, uiautomation, pywinctl, comtypes | LGPL-3.0, Apache-2.0, BSD-3, MIT | input, UI Automation, windows |
| PySide6 | LGPL-3.0 | UI |
| Atkinson Hyperlegible font | OFL-1.1 | UI typeface (designed for low vision) |

No GPL or AGPL code is copied into this repository (Sightline is
Apache-2.0).  Running a GPL program as a separate process is fine.

## 10. Quality bar

* Correct under real conditions: lost face, camera unplugged, no microphone,
  elevated windows (input is blocked by Windows; say so in a toast), multiple
  monitors, 125-200% scaling.
* Responsive: no GUI-thread work over ~8 ms; vision under 20 ms per frame on
  a 6-core desktop CPU; speech end-of-speech to action under ~300 ms for
  short commands, with the model's share under 200 ms.
* Every user-visible string is plain English, short, no jargon, no emoji,
  no em-dashes.
* Tests: unit tests for logic that does not need hardware (filters, gesture
  state machines, grammar, formatting, geometry, settings).  Hardware or
  desktop tests carry a marker (`models`, `gui`, `desktop`) and skip
  cleanly when the thing is missing.
* Comments and docs: explain why, not what.  Two spaces after a full stop.
  No em-dashes.  Avoid marketing words ("robust", "seamless", "leverage").

## 11. Test hardware so far

Everything so far was built and measured on one desktop PC, used over
Remote Desktop: Intel Core i5-12400 (6 cores), 32 GB RAM, Intel UHD 730
graphics, Windows 11, Python 3.13.  It had **no webcam** and **no physical
microphone**.

* Speech was measured with two SAPI voices (David and Zira) rendered to
  WAV files.  Live capture was checked through a virtual audio cable
  (VB-Audio Point: play into "Output (VB-Audio Point)", record from
  "CABLE Output (VB-Audio Point)", WDM-KS host API).
* Vision was measured on six photos and on synthetic faces built from
  them; no physical camera has been opened yet.
* Timings there: Face Landmarker 10.8 ms a frame at 1280x720 and the rest
  of the vision pipeline under 0.7 ms; Parakeet 0.6B 156 ms median model
  time after a pause, and about 200 ms from the end of speech to a
  decision through the whole speech pipeline.  The desktop timings in
  `docs/modules/desktop.md` are from the same machine at 1920x1080 and
  125% scaling.

Gaze, blink and wink tuning on real faces, real voices and real screens
has not happened yet, so the vision code ships with defaults, diagnostics
and a recording tool (`tools/vision_record.py`, `tools/vision_replay.py`).
`docs/TESTING.md` is the plan for the first session on real hardware.
