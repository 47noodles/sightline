# Sightline

Use a Windows computer with only your eyes and voice.

Sightline turns an ordinary webcam and microphone into hands-free control
of a Windows PC.  Look at something and the pointer goes there; small head
movements fine-tune it.  Close your eyes for a moment to click, or wink to
right-click.  Say "click save", "scroll down" or "switch to chrome", or
just talk to type.  Everything runs on your own computer: no account, no
cloud service.

It is meant for people who cannot use a mouse and keyboard, such as people
living with motor neurone disease (ALS), a spinal cord injury or cerebral
palsy, and for the helpers who set it up with them.

> **Alpha, for first hands-on testing.**  Every feature below is built, and
> 1656 automated tests pass.  But nothing has been tuned on a real webcam,
> microphone or screen yet: the timings and thresholds are starting points
> from research and synthetic tests, not measurements with real people.
> For the first sessions, keep a helper, or a mouse and keyboard, within
> reach.

**Trying it for the first time?**  Follow
[`docs/FIRST-TEST.md`](docs/FIRST-TEST.md): install, the set-up wizard,
twelve things to try and how to stop it, in about 30 minutes.

## Features

* **Eye and head pointer.**  The pointer jumps to where you look, and small
  head movements steer it the last few pixels.  There are head-only and
  eyes-only modes too.  Until your eyes are calibrated it follows your head
  alone, so set-up works from the start.
* **Clicking with your eyes.**  Close both eyes briefly to click, keep them
  closed a little longer to double-click, wink your right eye to
  right-click.  Natural blinks never click, and resting your eyes never
  clicks.  Dwell clicking (hold your gaze still) and face gestures (raised
  eyebrows, a smile, an open mouth, puffed cheeks) are there for people who
  prefer them.
* **Voice commands, offline.**  About 190 built-in commands for clicking,
  scrolling, keys, windows, apps, the web, editing and media.  A spoken
  click lands where you were looking when you started to speak.  You can
  add your own commands and words.
* **Dictation.**  Talk into any text box: spoken punctuation ("comma", "new
  line"), "scratch that", spelling with plain letters or the NATO alphabet,
  and editing by content ("select quick brown", "move after brown").
* **Precision aids.**  Snapping to the nearest button or link, a zoom lens,
  numbers on everything clickable ("show numbers", then "12"), a
  nine-square grid, a menu at the pointer (right click, drag, scroll and
  more), and a quick numbered choice when a click could hit several small
  targets.
* **Safety nets.**  A short countdown before anything hard to undo
  (closing, deleting, sending, opening a command window), "undo that", "did
  you mean" suggestions when you are not understood, and speech ignored
  while you are not facing the screen.
* **Eyes-only access.**  A dock of large buttons that work by blinking or
  dwelling, the Windows On-Screen Keyboard set up for typing by looking, and
  a spot to look at to switch eye control back on.
* **For every day.**  It starts itself again after a crash and keeps the
  screen on while you are at the computer.  The settings window works with
  your eyes or voice (no sliders, no typing) and has presets such as
  "steady hands", "tremor" and "tired eyes".  A practice room has a
  pointing test and a one-page summary for a therapist.

## Requirements

* Windows 10 or 11, 64-bit.
* 64-bit Python 3.13 with the `py` launcher.  The standard installer from
  [python.org](https://www.python.org/downloads/windows/) includes the
  launcher.  `install.ps1` checks for it and says how to get it.
* A webcam, built in or USB.  Sightline asks it for 1280x720 at 30 frames
  a second.
* A microphone.
* Disk space: about 1.2 GB for the Python packages, and about 0.8 GB for
  the speech and face models once unpacked (a 620 MB download).  Allow
  3 GB free during set-up.
* An internet connection to install and for the first-run download.  After
  that, Sightline works offline.
* A reasonably recent multi-core processor; no graphics card is needed.  It
  was developed on a 6-core desktop processor.  On slower computers it
  picks the smaller speech model by itself.
* English: the commands and speech models are English only for now.

## Quick start

In PowerShell:

```powershell
git clone https://github.com/47noodles/sightline.git
cd sightline
powershell -ExecutionPolicy Bypass -File .\install.ps1
.venv\Scripts\sightline-console.exe
```

No git?  On the repository page choose **Code**, then **Download ZIP**,
unzip it, and open PowerShell in that folder.

`install.ps1` does four things, all inside the Sightline folder:

1. finds 64-bit Python 3.13 through the `py` launcher;
2. creates a private Python environment in `.venv`;
3. upgrades pip in it;
4. installs Sightline and its packages into it (`pip install -e .`).

Add `-DryRun` to see the steps without running them.
`-ExecutionPolicy Bypass` applies to this one run only and changes no
settings.  The same by hand:

```powershell
py -V:3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e .
```

### Starting Sightline

| Command | What it does | Use it for |
|---|---|---|
| `.venv\Scripts\sightline-console.exe` | Runs Sightline with its log in a console window.  Nothing starts it again if it stops. | First tests and development |
| `.venv\Scripts\sightline.exe` | Runs Sightline under a watchdog that starts it again after a crash.  No console window. | Everyday use |

Both take `--setup` (run the set-up wizard again), `--no-vision` (voice
only, no camera), `--no-speech` (eyes only, no microphone) and `--debug`
(a more detailed log).

**Starting with Windows** is off until you turn it on.  Open Sightline's
settings (say "settings", or use Settings on the dock or in the tray
menu), go to the **Start-up** page and turn on **Start Sightline when I
sign in to Windows**.  Sightline then adds an entry for the watchdog
(`sightline.exe`) to your user's Run list, and removes it again when you
turn the setting off.

## The first run

The set-up wizard opens.  Its pages are Welcome, Windows settings,
Download, Camera, Microphone, Hand over, Eye clicks, Where you look, Head
pointer, Mouth check and You are ready.

* **Helper mode** is the default.  A helper does the first steps with the
  mouse and keyboard while eye control is paused, then presses the large
  **Hand over** button, which is on every page.  From then on everything
  works by eyes and voice: say "next", "back" or "skip" to move through the
  pages.
* **Windows settings** checks the switches Sightline depends on: camera and
  microphone privacy, whether apps can share the camera, administrator
  prompts, Voice Access at sign-in, and the screen lock.  Each has an
  **Open settings** button for the helper.  Sightline only reads these; it
  changes none of them.
* **Download** fetches the models below, once.
* **Camera** and **Microphone**: choose them, with a live preview and a
  level meter.
* **Eye clicks**, **Where you look** and **Head pointer** calibrate blinks
  and winks, gaze (13 dots, corners included) and the head pointer.  "I
  can't wink" turns wink clicks off.  The optional **Mouth check** can turn
  on a check that your mouth moved when you spoke.
* **You are ready** opens the practice room.

Run the wizard again with `--setup`, or recalibrate by saying "calibrate",
"calibrate eyes", "calibrate head" or "fix pointer".

### Model downloads

| Model | Used for | Size | Downloaded from | Licence |
|---|---|---|---|---|
| MediaPipe Face Landmarker, float16 v1 | face, eye and head tracking | 3.8 MB | storage.googleapis.com | Apache-2.0 |
| Silero VAD v6.2.3 | telling speech from silence | 2.3 MB | raw.githubusercontent.com (snakers4/silero-vad) | MIT |
| NVIDIA Parakeet TDT 0.6B v2, int8 (sherpa-onnx export) | speech recognition, most accurate | 482 MB | github.com (k2-fsa/sherpa-onnx releases) | CC BY 4.0 |
| NVIDIA Parakeet TDT-CTC 110M, int8 (sherpa-onnx export) | speech recognition, faster | 108 MB | github.com (k2-fsa/sherpa-onnx releases) | CC BY 4.0 |
| all-MiniLM-L6-v2, quantised ONNX, pinned commit | understanding reworded commands | 23.5 MB | huggingface.co (sentence-transformers) | Apache-2.0 |

That is about 620 MB in all.  Every file is pinned by SHA-256 in the
source (`src/sightline/speech/models.py`, `src/sightline/vision/models.py`),
fetched over https only (and redirected only to https), and refused if its
hash or size is wrong.  The files are kept in
`%LOCALAPPDATA%\Sightline\models`.  Nothing is downloaded while Sightline
runs.  If a download fails, the window retries and offers **Try again**.

Settings also lists a faster, less accurate recogniser, Moonshine Base
(111 MB, MIT).  Set-up does not download it and Sightline cannot fetch it
by itself yet, so choosing it keeps a Parakeet recogniser in use.

## Stopping Sightline safely

* **Quit from the tray.**  Click the Sightline icon beside the clock (it may
  be under the arrow that shows hidden icons) and choose **Quit Sightline**.
  It quits at once, and the watchdog does not start it again.  Saying "quit
  Sightline" does the same after a 1.2 second countdown, which "cancel"
  stops.
* **Or pause instead.**  "pause eyes" (or **Pause eyes** on the dock or in
  the tray menu) stops the pointer following your eyes.  "go to sleep"
  stops Sightline listening for anything except "wake up".  The real mouse
  and keyboard keep working throughout.
* **Ending it in Task Manager counts as a crash.**  The watchdog sees it as
  one and starts Sightline again, up to three times in 10 minutes, then
  gives up with a message.  To end everything from Task Manager, use **End
  process tree** on `sightline.exe` in the Details tab.
* **Use `sightline-console` for first tests.**  It runs without the
  watchdog, so nothing comes back after it stops: quit from the tray, or
  close its console window to end it at once.

After an abrupt stop (Task Manager, or closing the console window), a key
or mouse button Sightline was holding may stay held until Sightline next
starts, which lets go of them first; pressing and releasing the key also
works.  The strip Windows keeps clear for the dock may stay until Windows
notices, and On-Screen Keyboard settings Sightline changed are not put
back.  Quitting from the tray avoids all of this.

## Safety

Sightline moves the real mouse pointer, and clicks and presses real keys
in whatever app is in front, just as if you had.

* **The physical mouse always wins.**  When someone moves a real mouse,
  eye control waits until 1.5 seconds after it stops, then carries on from
  where the mouse left the pointer.  The real keyboard works as normal the
  whole time.
* **Start somewhere harmless**, such as an empty Notepad window or the
  practice room (say "practice room"), until clicks land where you expect.
* **Hard-to-undo actions wait.**  Closing a window or tab, quitting,
  Delete, sending, opening a command window, PowerShell or the registry
  editor, Enter in a command window, and clicking a button named Delete,
  Remove, Send, Submit, Pay or Buy all get a 1.2 second countdown that says
  what is about to happen.  Say "cancel", or blink on the countdown, to
  stop it.
* **"stop"** lets go of every held key and button and stops scrolling.
  **"undo that"** reverses the last thing Sightline did where it can, and
  says so when it can't.
* **Speech meant for someone else is ignored.**  While you are not facing
  the screen, only the safety commands work (sleep and wake, stop, cancel,
  pause and resume eyes, "undo that" and help), and nothing is typed.
  Voice cannot turn the safety settings off; change those with your eyes or
  by dwelling.  Without a camera this check is off, and a video or a call
  playing while you face the screen can still be heard as commands.
* **Administrator windows** cannot be controlled (see Troubleshooting).
  Sightline does not need to run as administrator, and says so if it does.
* **Sightline is not a medical device or an alarm.**  Do not rely on it as
  the only way to call for help.

## Privacy

* Everything runs on your computer.  Camera pictures and sound are
  processed in memory and never saved or sent anywhere.  Your profile keeps
  measurements of your eyes and face from calibration and from clicks
  (numbers, not pictures), so the gaze model can be fitted again.
* The only network use is downloading the models at set-up, from the
  servers in the table above, which see your IP address as any download
  does.  There is no account, no telemetry and no update check.
* The log never contains what you said, typed, clicked or visited: no
  recognised words, window titles, button names, web addresses or
  searches.  With detailed logging on, it notes only the kind and length
  of each thing you said.
* In a password field nothing heard is shown, and what was typed shows as
  bullets.
* Your settings, calibration, commands, aliases and vocabulary are plain
  files in `%APPDATA%\Sightline`.  Models, logs and recordings are in
  `%LOCALAPPDATA%\Sightline`.  `%APPDATA%` is the roaming part of a Windows
  profile, so on a work or school PC those files may travel with your
  account.
* The developer tool `tools/vision_record.py` saves face measurements (and
  video, only if asked) to the recordings folder.  Sightline itself never
  records.

## Cheat sheet

Say "what can I say" at any time for the full list.

### Eyes

| Do this | It does |
|---|---|
| Close both eyes for about a third of a second (350 ms), then open them | Click (left, or the click type chosen on the dock) |
| Keep both eyes closed for a second | Double click |
| Wink your right eye | Right click |
| Wink your left eye | Middle click |
| Keep your eyes closed for more than 2.5 seconds | Nothing: resting your eyes never clicks |
| Look at a dock button, then click with your eyes | Pause, click type, dwell, scroll, keyboard, numbers, zoom, menu, sleep, settings and more |
| While eye control is paused, look at the top middle of the screen for 1.5 seconds | Turn eye control back on |
| With dwell on ("dwell on"), hold the pointer still for 0.9 seconds | Click |

Clicks land where the pointer was just before your eyes closed, and a tick
tells you a click is ready.  Natural blinks (100 to 300 ms) are ignored.
Face gestures stay off until you train them ("calibrate face gestures").

### Voice

| Say | What happens |
|---|---|
| "click", "double click", "right click" | Click where you were looking |
| "tap" | Click straight away, without waiting for a name |
| "click save" | Click the button or link called Save |
| "scroll down 3", "keep scrolling down", "stop" | Scroll where the pointer is; "stop" ends it |
| "drag" ... "drop" | Hold the mouse button down, then let go |
| "show numbers", then "12" | Number everything clickable, then click number 12 |
| "show grid", "zoom", "menu" | The precision aids: the grid, the zoom lens, the menu at the pointer |
| "press control shift t", "control c", "tab 3 times" | Press keys and shortcuts |
| "type see you tomorrow", "new line", "scratch that" | Type words; start a new line; remove what you just dictated |
| "spell alpha bravo charlie" | Type letters one by one |
| "select all", "copy", "paste", "undo" | Edit in the app (undo is the app's own) |
| "undo that" | Reverse the last thing Sightline did |
| "select quick brown", "move after brown" | Select words, or move the cursor, by saying them |
| "open notepad", "switch to chrome", "close window", "snap left" | Apps and windows |
| "go to wikipedia dot org", "search for weather in london", "new tab", "go back" | The web browser |
| "pause eyes", "resume eyes" | Stop or start the pointer following your eyes |
| "go to sleep", "wake up" | Stop or start listening |
| "dictation mode", "normal mode" | Type everything you say, or go back to commands and typing |
| "cancel" | Close numbers, grid, zoom or help, or stop a countdown |
| "settings", "calibrate", "fix pointer", "restart tracking" | Settings, calibration, a quick drift fix, restart eye tracking |
| "quit Sightline" | Close Sightline, after a short countdown |

After "did you mean", say "one", "two" or "three".  "tap" (click) and "tab"
(the key) can sound alike to the recogniser.

## Troubleshooting

* **"Another app is using the camera" or "Camera not found".**  Close other
  apps that use the camera, such as video calls or the Camera app.  On
  Windows 11 24H2 and later, a camera's own page in Settings can allow
  several apps to use it at once; the set-up wizard's Windows settings page
  opens it.  Also check Settings, Privacy & security, Camera: "Camera
  access", "Let apps access your camera" and "Let desktop apps access your
  camera" must be on.  Sightline keeps trying to reopen the camera by
  itself.
* **The pointer does not move, or the ring says "No face".**  Put more
  light in front of you rather than a window behind you, sit 35 to 90 cm
  from the camera and face the screen.  "recenter" treats your current head
  position as straight ahead; "restart tracking" starts eye and head
  tracking again.
* **Voice does nothing.**  Choose the microphone in Settings (Voice page)
  or in the set-up wizard, and check Settings, Privacy & security,
  Microphone (the same three switches).  If voice is asleep, say "wake up".
  "microphone off" turns it off; turn it back on from the dock.  The
  caption "Ignored: you were looking away" means: face the screen while you
  speak.
* **Nothing happens in some windows.**  Windows does not let ordinary apps
  control apps running as administrator (Task Manager, installers, some
  settings pages), and nothing can control permission prompts or the lock
  screen.  Sightline says "Windows blocks Sightline from controlling apps
  running as administrator."  Use Windows 11's built-in Voice Access
  (Settings, Accessibility, Speech) for those windows, which can also run
  on the sign-in screen, or ask a helper.  Do not run Sightline as
  administrator.  The On-Screen Keyboard can still type into such windows.
* **Natural blinks click, or clicking is hard.**  Say "calibrate eyes", or
  change "Close both eyes this long to click" in Settings.
* **The pointer drifts.**  Say "fix pointer" and look at the dot, or
  "calibrate".
* **Sightline closed and did not come back.**  The watchdog starts it
  again at most three times in 10 minutes; if it stops once more, the
  watchdog gives up and shows a message.  The logs are in
  `%LOCALAPPDATA%\Sightline\logs` (`sightline.log`, `watchdog.log`).
* **"Your settings file was damaged".**  Sightline moved it aside as
  `settings.broken.json` and started with its usual settings.

## Uninstalling

1. If you turned on **Start Sightline when I sign in to Windows**, turn it
   off in Settings (Start-up page).  That removes its Run entry.
2. Quit Sightline from the tray icon.
3. Delete the Sightline folder.  That removes `.venv` and every package
   Sightline installed.
4. Delete `%APPDATA%\Sightline` (settings, profiles and calibration, your
   commands, aliases and vocabulary) and `%LOCALAPPDATA%\Sightline`
   (models, logs and recordings).  You can paste either name into File
   Explorer's address bar, or in PowerShell:

   ```powershell
   Remove-Item -Recurse -Force "$env:APPDATA\Sightline", "$env:LOCALAPPDATA\Sightline"
   ```

5. If the Run entry is still there (for example because the folder was
   deleted first):

   ```powershell
   Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name Sightline
   ```

Sightline installs no services or drivers and nothing in Program Files.
Python stays installed; remove it in Settings, Apps if you no longer need
it.

## Development

* **Layout.**  Five subsystems in `src/sightline/`: `core`, `vision`,
  `speech`, `desktop` and `ui`.  [`ARCHITECTURE.md`](ARCHITECTURE.md) is
  the contract between them; each has design notes in
  [`docs/modules/`](docs/modules/), tests in `tests/<subsystem>/`, and
  tools in `tools/`.
* **Test tools.**  `install.ps1 -Dev` also installs pytest (or
  `.venv\Scripts\python.exe -m pip install -e ".[dev]"`).
* **The safe test suite** touches no camera, microphone, window, sound or
  input, and takes about 1 to 6 minutes depending on the PC:

  ```powershell
  $env:QT_QPA_PLATFORM = "offscreen"
  .venv\Scripts\python.exe -m pytest tests -m "not desktop and not gui and not slow and not models"
  ```

  In Git Bash: `QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests -m "not desktop and not gui and not slow and not models"`.

* **Warning: `-m desktop` moves the real cursor**, types real keys and opens
  Notepad, File Explorer, Chrome, Edge and the On-Screen Keyboard.  Run it
  only at a PC you are sitting at, with your work saved.  `models` tests
  need the downloaded models, `gui` tests open Qt windows, and `slow` tests
  take a while.  Leave `SIGHTLINE_LIVE_DESKTOP`, `SIGHTLINE_LIVE_AUDIO` and
  `SIGHTLINE_UI_TESTS_ON_SCREEN` unset unless you mean to run live tests.
* **Tools.**  `tools/vision_record.py` and `tools/vision_replay.py` record
  and replay face data for tuning; `tools/speech_bench.py` measures the
  recognisers; `tools/desktop_bench.py` times UI Automation;
  `tools/ui_playground.py` draws every screen offscreen into `out/ui/`.
* **More.**  [`docs/ROADMAP.md`](docs/ROADMAP.md) (done, next, known gaps),
  [`docs/TESTING.md`](docs/TESTING.md) (the first session on real
  hardware), [`docs/reviews/`](docs/reviews/) (the concept and security
  reviews) and [`CLAUDE.md`](CLAUDE.md) (notes for coding agents).
* **Style.**  Every user-visible string is plain English.  No
  text-to-speech, only short sounds.  Coordinates are physical pixels and
  times are `time.perf_counter()` seconds.  Everything that acts goes
  through the action registry, and what is hard to undo is decided only in
  `core/risk.py`.

Bug reports and ideas are welcome as GitHub issues.  For anything
security-related, please read [`SECURITY.md`](SECURITY.md) and report it
privately.

## Licence and acknowledgements

Sightline is licensed under the [Apache License 2.0](LICENSE).  Code
adapted from other projects keeps its own licence; those projects, and the
libraries, models, fonts and icons Sightline uses, are listed in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).  The downloaded models
have their own licences (the table above); the Parakeet models are
NVIDIA's, under CC BY 4.0.

Sightline stands on other people's work: MediaPipe Face Landmarker
(Google), EyeTrax, Project GameFace (Google), the One Euro filter (Géry
Casiez and Nicolas Roussel), shaal/eye-tracker, talonhub/community, Silero
VAD, NVIDIA's Parakeet models and the sherpa-onnx project, Moonshine,
all-MiniLM-L6-v2 from sentence-transformers, hassil, text2num, RapidFuzz,
OpenCV, ONNX Runtime, NumPy, Qt for Python (PySide6), comtypes,
sounddevice, cv2-enumerate-cameras, Microsoft PowerToys, the Atkinson
Hyperlegible Next typeface (Braille Institute) and Lucide icons.  The
design also draws on the eye-tracking and accessibility research cited in
[`docs/reviews/2026-10-24-concept-review.md`](docs/reviews/2026-10-24-concept-review.md).
