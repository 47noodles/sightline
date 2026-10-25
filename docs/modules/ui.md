# UI shell (`sightline.ui`)

Everything the user sees and hears: the pointer ring and the other marks on
the desktop, captions, toasts, the status strip, the dock, the tray icon,
the precision overlays (numbers, grid, zoom lens, help), the first-run
window, model downloads and the sounds.  `app.py` builds it with
`sightline.ui.create(ctx, qt_app)` and calls `start`, `run_setup` or
`ensure_models`, and `stop`.

## Design system

Defined in `ui/theme.py`, with the reusable widgets in `ui/widgets.py`.

### Rules

* Near-monochrome: ink on ground.  **One accent, amber `#FFB000`, only where
  it means something**: a click is armed (eyes held shut), a dwell is
  filling, a numbered badge is under the pointer, the microphone is hearing
  speech, a command was carried out (the caption marker).  Nothing else is
  amber.
* Hairline rules and ruled tables, not cards.  Panels are square; controls
  and badges have a 2 px radius at most.
* No shadows, gradients, glows, blur or translucency for style, no pill
  tags, emoji, all-caps labels, icon-topped cards, coloured side stripes or
  tinted boxes.  The only transparency is a fade-out animation and dimming
  an overlay while the pointer is over it, so you can see what is beneath.
* Anything drawn over the desktop is legible on any background: panels are
  an opaque fill with a contrasting 1 px border, and every line or ring is
  stroked twice, a wide halo then the ink (or amber with near-black edges).
* Readable text is near-white on near-black or the reverse.  The secondary
  ink is still above 7:1 contrast; nothing that must be read is mid-grey.
* Dark and light themes; `ui.theme` "system" follows the Windows app mode
  and changes live.

### Palette

| Token | Dark | Light | Use |
|---|---|---|---|
| ground | `#111214` | `#F5F5F2` | window background |
| surface | `#17181B` | `#FFFFFF` | panels, dock, badges |
| raised | `#202226` | `#ECECE8` | button fill |
| ink | `#F4F4F1` | `#121315` | text, strokes, borders that must show |
| ink_2 | `#C3C6CB` | `#3A3D42` | secondary text |
| rule | `#34373C` | `#D6D7D9` | hairline dividers |
| rule_strong | `#6E737A` | `#878B91` | control borders at rest |
| halo | `#08090A` | `#FFFFFF` | the contrast edge around strokes on the desktop |
| accent | `#FFB000` | `#FFB000` | armed and listening only |
| on_accent | `#111214` | `#121315` | text and edges on amber |

### Type

Atkinson Hyperlegible Next (Braille Institute, SIL OFL 1.1), Regular,
Medium, SemiBold and Bold, shipped in `assets/fonts/`.  Sizes are logical
pixels at `ui.scale` 1.0 and scale with it.

| Role | Size | Weight | Where |
|---|---|---|---|
| display | 30 | 600 | wizard page titles |
| title | 22 | 600 | window and sheet titles |
| heading | 18 | 600 | section headings in tables |
| body | 16 | 400 | running text (never below 14) |
| label | 15 | 500 | buttons, dock labels, table headers |
| small | 14 | 400 | secondary lines; the floor |
| status | 14 | 500 | status strip, ring tags |
| caption | 22 | 500 | live captions |
| badge | 15 | 700 | numbered labels |
| grid | 26 (down to 14) | 700 | grid numbers |

### Space and lines

Spacing steps 4, 8, 12, 16, 24, 32, 48 (`theme.space(n)`).  Hairlines are
one device pixel (`fill_hairline_h`), so they stay sharp at 125% and 150%.
Overlay strokes are 2.5 px ink on a 1.5 px halo.

### Components (`ui/widgets.py`)

* `GazeButton`: large flat button, icon beside (`row`) or above (`stack`)
  the label, at least 64 px high.  Hover thickens the border to 2.5 px;
  press and "on" invert the colours (dwell on, eyes paused, voice asleep).
  `flat=True` makes it a cell in a ruled column (the dock).
* `RuledTable`: a painted table with an optional header, section heading
  rows and a secondary line per cell; fast with hundreds of rows.
* `ProgressLine`: hairline-framed bar with a solid ink fill.
* `Rule`, `text(content, role)`, `SightlineWindow` (ordinary window with a
  theme-matched native title bar).
* Icons: Lucide line icons (ISC) tinted to the ink colour (`ui/icons.py`);
  the Sightline mark is drawn in code.

## Architecture

| File | What |
|---|---|
| `shell.py` | `UIShell`: builds everything, wires events, registers the `ui.*` actions, settings |
| `bridge.py` | bus to GUI thread; coalesces high-rate events; the registry's UI invoker |
| `coords.py` | physical to logical pixels per screen (pure, tested) |
| `geometry.py` | label placement, grid maths, zoom lens mapping (pure, tested) |
| `services.py` | shared bits: screens, cursor tracker, `UiServices` |
| `overlay_state.py` | keeps `state.active_overlay` right |
| `winapi.py` | window styles, dark title bar, square corners, the dock's app bar |
| `theme.py`, `widgets.py`, `icons.py` | the design system |
| `sounds.py` | earcons and the event-to-sound mapping |
| `dock.py`, `tray.py` | the dock (with the click type flyout) and the tray icon |
| `overlays/` | pointer ring and gaze dot, highlight, ripple, resume zone, status, captions, toasts, labels, grid, zoom, help |
| `windows/` | model download, minimal first-run window, settings and calibration stand-ins |
| `setup.py`, `wizard.py` | the hook and the protocol the full setup wizard plugs into |

Threads: every widget lives on the GUI thread.  Bus callbacks only store
the event and post one wake-up; `PointerSample`, `EyeState`, `MicLevel`,
`CameraFrame`, `DwellProgress`, `GazeZoneProgress`, `HoverTarget` and
`FaceStatus` keep only their latest value, everything else is delivered
in order.  `ui.*` actions are registered with `thread="ui"`.  The numbers
overlay fetches `uia.clickables()` and lays out badges on a worker thread.
Interceptors (numbers, grid, zoom, help) run on the action thread and only
read immutable snapshots; anything they change on screen is posted.

Coordinates: events are physical pixels.  `ScreenMap` mirrors Qt's own
per-screen mapping (`logical = origin + (physical - origin) / dpr`, with
the screen's native top-left as origin), so a window centred on a point is
always scaled by that point's screen.  Built from the HMONITOR rectangles
on Windows, rebuilt when screens change.

The ring follows the real cursor (`desktop.input.cursor_pos()`, polled at
60 Hz while it moves and 10 Hz after a second of stillness), not the
pointer engine's target, so it never runs ahead of the arrow.

## On the desktop

* **Pointer ring**: plain while tracking; snaps in from wide after a jump
  (warp); four ticks when held still (frozen); dashed with a pause tag when
  eye control is paused; dashed with "No face" when the face is lost; amber
  when a click is armed and a second amber ring for a double click; an
  amber arc sweeping clockwise for dwell.  Shown only while vision is
  publishing (and `show_pointer_ring`).  Tags sit left of the ring because
  the arrow extends down and right.
* **Gaze dot** (`show_gaze_dot`): the filtered gaze point.
* **Target outline** (`HoverTarget`): four thin windows around the element a
  click would hit, so a big element costs nothing to draw.  Hidden while
  numbers, grid or zoom are open.
* **Click ripple** (`ClickPerformed`): a widening ring; right, middle and
  double clicks are named.
* **Resume zone** (`GazeZoneProgress`): outline of the zone vision reports,
  a "Resume" tag and an amber bar filling as the gaze holds.  Remembered and
  shown while eye control is paused.
* **Status strip**: voice (mic, moon or mic-off icon, mode text, level meter
  in amber while speech is heard) and eyes (Tracking, Paused, No face).  In
  the dock's header, or alone top right when the dock is hidden.
* **Captions**: bottom centre of the pointer's screen, partial text in the
  secondary ink, final text in ink, then the outcome with a marker (amber
  for a command carried out, hollow for not understood).  Fades 3.5 s
  (`caption_seconds`) plus a little per character after the last update.
  Silent while voice is asleep, except for the command that wakes it.
* **Did you mean** (`IntentResult.alternatives`): up to three large
  numbered choices above the caption area, in a window that takes clicks
  but never focus (like the dock), so looking at one and blinking picks it.
  A pick dispatches `speech.choose index=n` (source "ui"); saying the
  number is speech's part.  They go after 8 s (later while the pointer
  rests on them, at most 30 s) or with the next result; an utterance judged
  not meant for the computer ("ignored") leaves them up.  They never move
  once shown.  `ChoiceBar` in `overlays/choices.py` is the reusable part,
  and `GazeButton(badge="1")` draws the numbered button.  An offer to
  remember a mishearing (a "command" result with exactly one alternative,
  speech's "Say yes to remember ...") is not a guess, so it is titled
  "Remember this?" with "Say yes, or look at it and blink".
* **Toasts**: top centre; `Notice` and every failed `ActionExecuted`.
  `ActionError` text is shown as written; unexpected exceptions become
  "Something went wrong. The log has the details."  Same key replaces, at
  most three.
* Captions, toasts and the strip fade almost away while the pointer is over
  them, so they never hide what you are trying to look at.

## Precision overlays and `ui.*` actions

| Action | Behaviour |
|---|---|
| `ui.labels.show` / `hide` / `select number button count` | Badges next to every clickable element (UIA, fetched off the GUI thread), in reading order, never overlapping, at most `max_numbers` (150).  Placement prefers the margin beside an element, then inside its corner, and avoids covering other elements.  Select clicks the element centre with `snap=False`.  Looking at a badge and blinking (any `mouse.click` on or within 10 px of a badge, or "click" while the pointer is on one) selects it; any other click goes through and closes the numbers. |
| `ui.grid.show` / `select cell` / `back` / `click button count` / `hide` | 3x3 grid on the pointer's monitor, keypad numbering.  Each step moves the pointer to the cell centre.  An eye click inside the grid picks that cell; when cells get too small to divide, it clicks there.  A plain "click" clicks the current centre.  Numbers move to cell corners when cells are small, so the click point stays visible. |
| `ui.zoom.show x y` / `hide` | Copies the area around the point (our marks hidden for 60 ms first), shows it 3x (`zoom_factor`) with crisp pixels and a crosshair, and installs an interceptor: the next click inside the lens lands on the real point under that spot (`snap=False`); a click outside goes through.  Either closes the lens.  A spoken "zoom" aims where you looked as you began to speak; from the dock it aims where you looked before looking at the dock. |
| `ui.chooser.select` / `cancel` (`overlays/chooser.py`) | The number chooser for an unsure gaze click: two to six clickables within the gaze error get numbered chips (more than six open the lens).  A click goes straight through when the pointer is trusted: vision is not placing it, the pointer is head-only, or vision's `PointerSample.steered` says the head (or a nudge, or the mouse) has moved it since gaze last placed it.  Gaze-only pointing is never trusted. |
| `ui.menu.show x y` / `hide` / `select number item` (`overlays/action_menu.py`) | The action menu at the aim point: eight numbered tiles around the target.  "Scroll here" turns it into Stop, Up and Down; "stop scrolling", "up" or "down" said when those are not showing are ignored without a message (logged only).  From the dock it opens where you looked before looking at the dock. |
| `ui.help.show` / `hide` | "What you can say": eye gestures (from the vision settings), then `speech.commands_help()` by category.  Never takes focus; while open, scrolling and page keys scroll the sheet. |
| `ui.dock.toggle` | Hide or show the dock for this session (`show_dock` is the default). |
| `ui.cancel` | Closes whichever overlay is open (and the click type flyout). |
| `ui.wizard.next` / `back` / `skip` | Forwarded to the open wizard; nothing otherwise. |
| `ui.settings.show`, `ui.settings.next` / `previous` / `adjust direction` / `set value` / `preset name` | The settings window, and voice control of it (see below). |
| `ui.calibrate kind` | `eyes`, `gaze`, `head`, `gestures` or `full` open the calibration wizard; `drift` is "fix pointer": one dot in the middle of the current monitor for 1.5 s. |
| `ui.practice.show` | The practice room. |

**Where "here" is.**  Handlers that act at the pointer (the zoom lens, the
monitor for the grid and the help sheet, and a spoken "click" while the
numbers or the lens are up) use the action's own `x` and `y`, else
`VisionAPI.aim_point(aim_t)` for a spoken command (where the pointer rested
when the user began to speak), else the cursor.  Vision being absent or
returning None falls back to the cursor.  `UiServices.aim(action)` and
`UiServices.here(action)` implement this.

`state.active_overlay` is "labels", "grid", "zoom" or "help" while one is
open (one at a time), "setup" or "calibration" while a wizard is open
underneath, and None otherwise.  A value set by another subsystem is left
alone and restored when our overlay closes.  The "did you mean" choices do
not set it: speech remembers the last rejected utterance itself.

## Dock

A ruled column on the `dock_edge` (right by default; left and top work
too): the status strip, then Pause eyes, click type (opens a flyout of Left,
Double, Right, Middle and Drag), Dwell, Scroll up, Scroll down, Keyboard,
Numbers, Zoom, Menu, Sleep or Wake voice, Help, Settings, Calibrate (gaze),
Restart tracking (`pointer.restart`).  Cells are at least
64 px; when the screen is too short they page with a More button.  States
show by inverting the cell.  The window is `WS_EX_NOACTIVATE`, so clicking
it never moves keyboard focus, and it registers as an app bar
(`dock_reserve_space`) so maximised windows stop at its edge.  Scroll, zoom
and the action menu (`ui.menu.show`) act at the last pointer position
outside the dock.  Buttons whose action nobody registered are disabled.

## Sounds

`assets/sounds/*.wav`, synthesised by `tools/ui_make_sounds.py`, played
with `QSoundEffect`, `sounds` and `sound_volume` respected.  The effects
live on their own thread with its own Qt event loop: on this development
machine (Remote Desktop audio) Qt Multimedia blocked the thread that owns
the effects for up to 7 s while loading, which froze the pointer ring when
that was the GUI thread.  A request that waited more than 0.35 s, or a clip
not loaded yet, is dropped rather than played late.  The thread starts only
once sounds are on, and is a daemon `threading.Thread`, so a stuck audio
stack never holds up the exit.  Wood taps for
the pointer (armed ticks, clicks, pause and resume), soft bells for the
voice (recognised, not understood, sleep, wake, success); rising means on,
falling means off.  A command whose own effect makes a sound (a click, a
mode change) gets no extra chime.  No text-to-speech anywhere.  The
calibration wizard plays `calibration_point` and `success` through
`services.sounds.play(name)`.

## Start-up windows

* `ensure_models(on_ready)`: calls `on_ready` at once when nothing is
  missing; otherwise a small window downloads on a worker thread, shows
  each file and one overall bar from `ModelProgress`, retries after 5, 15,
  30 and 60 s, and always offers "Try again".
* `run_setup(on_complete)`: the set-up wizard (below).  `on_complete`
  starts the engines; it runs once, as soon as the downloads are ready.

## Set-up wizard (`windows/wizards.py`)

Pages: Welcome (who is doing the first steps), Windows settings, Download,
Camera, Microphone, Hand over, Eye clicks, Where you look, Head pointer,
Mouth check, You are ready.  The camera and microphone come after the
download, not before, because a camera preview and a level meter need the
engines, and the engines need the downloaded models; the wizard starts
them the moment the download is ready.

* **Helper mode** (the default): a helper does the first steps with mouse
  and keyboard while eye control is paused (`pointer.pause`), then presses
  the large Hand over button (in the header on every page, and as its own
  page).  From then on everything works by eyes and voice: Next doubles as
  Start on pages that run something, "next", "back" and "skip" arrive as
  `ui.wizard.*`, and every button is at least 64 px.  Without a helper, the
  download page moves on by itself.
* **Windows settings**: `desktop.system_checks()` off the GUI thread, each
  shown as Fine, Needs attention or Check this by hand (when Windows does
  not say), with Open settings for the helper (`fix_uri`).
* **Camera**: the camera list as buttons, a live preview from
  `CameraFrame` with the landmarks, and plain advice on placement, light
  and a second camera.  **Microphone**: the list, a level meter, and a
  spoken check that passes on half a second of heard speech or a
  transcript.
* **Calibration pages** drive `CalibrationAPI` through `windows/targets.py`:
  `LabelRun` for eyes (open, closed, left wink, right wink; "I can't wink"
  skips winks and turns wink clicks off) and face gestures, `DotRun` for
  gaze (13 dots including corners and edges), validation (5 dots), head (5
  dots, longer holds) and drift.  Dots animate between positions, shrink
  while collecting, and a tick sounds after each.  `finish()` runs on a
  worker thread.  A `CalibrationActive(False)` the wizard did not ask for
  (a pipeline rebuild) ends the run with "Tracking restarted"; "cancel"
  stops a run.  After gaze and validation the page shows the comfort line:
  vision's own sentence from the validation report (`details["comfort"]`,
  measured on the fresh dots) when it gives one, otherwise one worked out
  from the validation error and the monitor's `size_mm` (or degrees at
  arm's length): "At your accuracy, controls smaller than about 2 cm are
  hard to hit by eyes alone.  Setting Windows text and app size to 150%
  will help."
* **Mouth check** (optional, hidden when `vision.attention` is missing or
  says nothing about the mouth): read a sentence aloud, then stay quiet;
  when the mouth score is at least 0.5 while reading, at most 0.2 while
  quiet and 0.35 apart, it offers to turn on `speech.mouth_gate`.
* **Finish** sets `general.first_run_complete` and opens the practice room
  (or "Finish without practice").  Closing early starts the engines without
  marking set-up done.

The calibration wizard (`ui.calibrate`, the dock and the tray) reuses the
same pages: `eyes`, `gaze`, `head` and `gestures` alone, and `full` as
camera, eye clicks, gaze, head, face gestures and mouth check, each ending
on a summary.
`gestures` asks for neutral, brow raise, smile, mouth open and cheek puff,
then half a minute each of talking and reading, and offers the best two or
three from the report's scores as clicks.

## Settings window (`windows/settings_window.py`)

No sliders, spin boxes or text fields.  Numbers are large minus and plus
buttons around the value in words ("0.35 seconds", "60%"); choices and on
or off are rows of large buttons; the current choice is inverted.  Pages:

* **Most used**: pointer speed, blink to click, voice sensitivity, the
  pause that ends a command, dwell time, each as Slower, A bit slower, As
  is, A bit faster, Faster (or Shorter and Longer) around the value it had
  when the window opened, with a live preview (a dot moving at the pointer
  speed, a filling bar or ring, "Hearing you").
* **Presets**: Steady hands, Tremor, Tired eyes and Sightline defaults, each
  listing what it changes ("Dwell time: 1.2 seconds (now 0.9 seconds)").
* One page per area, generated from the `setting()` metadata in each
  subsystem's `config.py` (`ui/settings_model.py`).  What Sightline learns
  by itself (`readonly`, such as speech's "Your pauses inside commands")
  shows as words with nothing to press; voice gets "Sightline learns this
  one by itself, so there is nothing to change."
* **For a helper**: the advanced settings behind one door, and the settings
  file for the few values that need typing (the wake phrase).

Voice: `ui.settings.next` and `previous` move a framed current setting
(and on to the next page at the end); `adjust direction=up|down` steps it;
`set value=...` takes a number (seconds for times, percent for fractions),
a choice, on or off, or a front-page word; `preset name=...`.  While open,
`state.active_overlay` is "settings".  Every change saves at once with
`ctx.settings_changed(section)`.

Safety settings (`setting(protected=True)`: "Count down before closing,
deleting or sending", "Ignore speech when you're not facing the screen",
"Also require your mouth to move") can be turned on by voice but not off
(review S-01).  The shell passes each action's `source` to the window, and
`settings_model.voice_may_set` refuses a voice change that weakens one:
"This one keeps you safe, so voice can't turn it off.  Look at Off and
blink, or dwell on it."  A preset from voice leaves such a value as it is.
The buttons themselves, pressed with the eyes or dwell, are not limited.

## Countdown before hard-to-undo actions (`overlays/confirm.py`)

An interceptor at priority 120, installed fail-closed, holds what the shared
risk policy (`core/risk.py`) counts as hard to undo when it comes from
voice, the eyes or dwell: closing a window or tab, quitting, Delete and the
shortcuts that clear, send or quit, Windows+R, opening a command window,
PowerShell, Terminal, the registry editor or Run, Enter or a typed line
into a command window, and `element.click` or an eye or dwell click on a
control named Delete, Remove, Discard, Send, Submit, Pay, Buy or Empty
Recycle Bin (the name of a click's target comes from the clickable cache,
with the snap radius, so it never blocks).  A chip above the captions says
what is about to happen ("Clicking “Send” in 0.8 s") for
`confirm_delay_ms`; "cancel", "stop", "undo that" or a blink on the chip
stops it with a plain caption ("Cancelled. Nothing was clicked.").
Otherwise it runs, aimed where it was asked for: a close or key press at
the window that was in front, a click at the point it was aimed at.  If the
interceptor itself fails, the action is dropped with "Sightline stopped
that because a safety check failed" rather than run (review S-05).  The log
names the action and the kind of risk, never a window title or a button's
name.  The tray's Quit (source `ui`) is not held.

## Practice room (`windows/practice.py`, `measures.py`)

* **Hit the targets**: twelve large targets (160, 120 and 90 px), one at a
  time, scoring hits, clicks that missed, targets not reached in 20 s, and
  the time to each hit.  The target turns amber under the pointer.
* **Tapping test**: ISO 9241-9 style multidirectional selection, 11
  targets on a circle at three sizes; effective width is 4.133 times the
  spread of the selections along the movement, throughput is the effective
  index of difficulty over the movement time, errors are selections
  outside the target.  Targets are painted, not buttons, so snapping does
  not help: it measures pointing.
* **Unwanted actions**: the shell counts, from `ActionExecuted`, actions
  undone with `action.undo` or cancelled (`ui.cancel`, or a reported
  cancellation) within 3 s, and gives a rate per hour after a quarter of an
  hour of use.
* **Summary**: those numbers on screen, and "Save for a therapist" writes
  `practice-YYYY-MM-DD-HHMM.html` to the profile folder: plain words first,
  the method underneath, printable.

## Settings (`ui/config.py`)

`theme`, `scale`, `show_captions`, `caption_seconds`, `show_pointer_ring`,
`show_gaze_dot`, `sounds`, `sound_volume`, `show_dock`, `dock_edge`,
`dock_reserve_space`, `zoom_factor`, `max_numbers`, `confirm_risky`
(protected: voice can turn it on, not off) and `confirm_delay_ms`.  All
apply live on `SettingsChanged("ui")`; the target outline follows the
desktop's `highlight_target`.

## Playground and screenshots

    .venv\Scripts\python.exe tools\ui_playground.py            # all scenes, offscreen
    .venv\Scripts\python.exe tools\ui_playground.py --only ring labels
    .venv\Scripts\python.exe tools\ui_playground.py --live     # real desktop, fake events, takes the desktop lock
    .venv\Scripts\python.exe tools\ui_playground.py --live --offscreen --seconds 5   # the same events, offscreen

The default mode never touches the real screen: Qt runs on its offscreen
platform with a virtual 1920x1080 screen at 125%, a synthetic browser page
and taskbar are drawn as the background, and each scene composites every
visible Sightline window into `out/ui/`:

| File | Shows |
|---|---|
| `overview_dark.png`, `overview_light.png`, `overview_dark_darkpage.png` | dock, ring, target outline, caption with outcome, toast |
| `ring_states_dark.png`, `ring_states_light.png` | every ring state over black, grey and white |
| `labels_dark.png`, `labels_light_darkpage.png` | numbers, one armed under the pointer |
| `grid_level0_dark.png`, `grid_level2_dark.png` | the grid at the start and two steps in |
| `zoom_dark.png` | the zoom lens |
| `help_dark.png`, `help_light.png` | the help sheet |
| `captions_partial_*.png`, `feedback_*.png` | partial caption; not understood, failed action toasts, right-click ripple |
| `did_you_mean_*.png` | numbered "did you mean" choices above the caption |
| `paused_resume_*.png` | eyes paused: ring tag, dock state, resume zone filling |
| `dock_states_*.png`, `dock_top_*.png`, `hud_only_*.png` | dwell on, voice asleep and the click type flyout; top edge dock; status strip alone |
| `setup_01_welcome_*.png` to `setup_12_finish_*.png` | every set-up page: welcome, Windows settings, download, camera, microphone, hand over, eye clicks, gaze dots, gaze result with the comfort line, head, mouth check, finish |
| `calibration_done_*.png`, `tray_menu_*.png` | the calibration summary, the tray menu |
| `settings_1_most_used_*.png` to `settings_4_helper_*.png` | most used, presets, eyes and pointer, the helper's page opened |
| `practice_1_menu_*.png` to `practice_4_summary_*.png` | the practice room: menu, a target under the pointer, the tapping circle, the summary |

## Tests

`.venv\Scripts\python.exe -m pytest tests/ui -q` (250 tests, about 30 s).  Wizard tests shrink every wait
with `windows.targets.TIME_SCALE`.
`tests/ui/conftest.py` puts Qt on the offscreen platform with two virtual
monitors (1920x1080 at 125%, and a 4K screen at 200% to its right and 240
px higher), so mixed-DPI window placement is checked against Qt itself;
nothing appears on screen and sounds are muted.  Set
`SIGHTLINE_UI_TESTS_ON_SCREEN=1` to run them on the real desktop instead.

## Known limits

* Window styles (click-through, no-activate, Alt+Tab exclusion), the app
  bar and mixed-DPI placement are verified offscreen and in code, not yet
  on a real multi-monitor desktop.
* The zoom lens is a still image; video under it does not move.
* Scale changes resize overlays and the dock live; ordinary windows (setup,
  models, the settings stand-in) and the help sheet keep their layout until
  Sightline restarts.
* Where Qt Multimedia is slow to load (seen over Remote Desktop), the first
  sounds after start can be missing: late sounds are dropped on purpose.
* If Sightline is killed without `stop`, Windows may keep the dock's
  reserved strip until Explorer notices the window is gone.
* Without eye tracking (`--no-vision`, or vision not created) a calibration
  page says "Eye tracking is not running."  When vision refuses to begin a
  calibration (the camera just went away, say), it says "This calibration
  is not available yet."
* The mouth-check thresholds (0.5, 0.2, 0.35 apart) and the comfort-size
  rule (twice the error across; typical controls 32 px at 100%) are priors,
  to be checked with real users.
