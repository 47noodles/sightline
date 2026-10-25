# The first session on real hardware

Everything in Sightline so far was built and measured without a webcam, a
microphone or a screen anyone sat at (`ARCHITECTURE.md` section 11).  This
file is the plan for the first hands-on session: the order to run things
in, what to look for, and where the results go.

## Before you start

* **The PC's owner is at the computer for the whole session, and has
  agreed to it.**  Most steps below move the real pointer, type real keys,
  open windows, beep, or use the camera and microphone.  Say what is about
  to happen before each part, and stop when asked.
* Save and close anything important.  The live desktop tests refuse to run
  while Notepad or the On-Screen Keyboard is already open, so close those
  too.
* Plug in a webcam and a microphone.  For the mixed-DPI checks, a second
  monitor at a different scale (for example 100% beside 150%) helps.
* Install with the test tools, then run the safe suite and check it passes:

  ```powershell
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -Dev
  $env:QT_QPA_PLATFORM = "offscreen"
  .venv\Scripts\python.exe -m pytest tests -p no:cacheprovider -m "not desktop and not gui and not slow and not models"
  Remove-Item Env:QT_QPA_PLATFORM
  ```

* Start Sightline as `.venv\Scripts\sightline-console.exe` (no watchdog,
  log in the console) for everything except the watchdog checks.  Quit it
  from the tray icon.  Its first run downloads about 620 MB of models.
* Note the setup: Windows version, CPU, camera, microphone, monitors and
  their scaling.  Every result below is worth less without it.

## Order

1. Desktop: automated, about 1 to 2 minutes, no camera needed.  It shows
   that input and UI Automation work on this PC before anything else
   drives them.
2. Vision: the camera, a guided recording, then tuning.
3. Speech: capture, then a real voice.
4. UI: the real-screen checklist.
5. Robustness and security.

## 1. Desktop

From the repository root, with the mouse and keyboard left alone while
they run:

```powershell
.venv\Scripts\python.exe -m pytest tests/desktop -m desktop -s -p no:cacheprovider
.venv\Scripts\python.exe tools\desktop_osk_probe.py
.venv\Scripts\python.exe tools\desktop_bench.py --json out\desktop-bench.json
```

These move the cursor, type, and open Notepad, File Explorer, Chrome,
Edge and the On-Screen Keyboard, then close what they opened.  Chrome and
Edge use a throwaway profile; a browser that is not installed is skipped.

What to look for:

* Every live test passes or skips with a reason.  Note any skip.
* Cursor moves land exactly on every monitor.
* The On-Screen Keyboard probe reports hover mode and the hover time
  Sightline set (800 ms by default), and the keyboard docks at the bottom.
* Timings in the same range as `docs/modules/desktop.md`, or a note of
  what is slower.

Results: the live tests write `out/desktop-live-timings.json` and the bench
writes `out/desktop-bench.json`.  Paste the figures into the Timings table
in `docs/modules/desktop.md`, in place of "from the live run".

## 2. Vision

* **The camera.**  Open a physical webcam through Media Foundation: the
  set-up wizard's Camera page shows the preview, and the log should say
  "Camera ... open with Media Foundation at 1280x720".
  `.venv\Scripts\python.exe tools\vision_record.py --list-cameras` lists
  what Windows offers, physical cameras first.  Check the frame rate in the
  replay report below (20 a second or more).
* **A guided recording.**  `.venv\Scripts\python.exe tools\vision_record.py`
  takes about 2.5 minutes and beeps at each step so the closed-eye steps
  can be followed, so run it only with a person at the camera.  Then
  `.venv\Scripts\python.exe tools\vision_replay.py` reports, per step, how
  often the face was found, eye closure, pointer spread and which gestures
  fired against what was asked.  Try other values without touching the
  saved settings, for example
  `.venv\Scripts\python.exe tools\vision_replay.py --set blink_click_ms=450 --set head_dead_zone=0.4`.
* **Tuning.**  The blink and wink thresholds, the 350 ms long blink
  (`blink_click_ms`), the 180 px and 130 ms warp (`warp_distance_px`,
  `warp_dwell_ms`), and the mouth thresholds (`attention`'s mouth movement,
  and the set-up Mouth check's 0.5, 0.2 and 0.35).  `docs/modules/vision.md`
  ("Tuning on a real webcam") says what to look for in the replay.
* **The cursor pump on the real injector.**  Is the pointer smooth?  Does
  it yield to the real mouse: move the mouse, and eye control should wait
  until 1.5 s after it stops, then carry on from where the mouse left it.
* **A spoken click while the pointer moves.**  The pump should pause
  (150 ms) and the click land where the speech began, not where the
  pointer drifted to.
* **Reading scroll on real windows.**  Say "reading mode" over a long page:
  looking low scrolls down, high scrolls up, the middle stops.
* **The `steered` threshold.**  Does 2 px of head movement
  (`PointerConfig.steer_px`) silence the number chooser too readily?  Try
  gaze clicks near small, close targets.
* **Face gestures**, through the calibration wizard ("calibrate face
  gestures", or "full calibration"): which gestures it offers, and how
  often they misfire while talking and reading.

Results: recordings go to `%LOCALAPPDATA%\Sightline\recordings`.  They
hold face measurements (and video with `--video`), so keep them off GitHub.
Write the findings into `docs/modules/vision.md` ("Defaults and why",
"Known limits"), and change defaults in `vision/config.py` with the
measured reason beside them.

## 3. Speech

* **Live capture.**  `SIGHTLINE_LIVE_AUDIO=1` turns on the opt-in capture
  test:

  ```powershell
  $env:SIGHTLINE_LIVE_AUDIO = "1"
  .venv\Scripts\python.exe -m pytest tests/speech/test_live_audio.py -s -p no:cacheprovider
  Remove-Item Env:SIGHTLINE_LIVE_AUDIO
  ```

  It was first run through a VB-Audio virtual cable and records only from
  that cable, so it skips when the cable is not installed.  With a real
  microphone, the set-up wizard's Microphone page (level meter and spoken
  check) is the real test.
* **Real-voice accuracy.**  Say the commands in the README's cheat sheet
  and the "What can I say" sheet; note what is missed or misheard.  Does
  "tap" (click) get confused with "tab" (the key)?  Start Sightline with
  `--debug` to log each utterance's kind, method, length and latency (never
  the words).
* **The attention thresholds on a real face.**  Talk while looking away:
  the caption should say "Ignored: you were looking away", and only the
  safety commands should run.  Talk while facing the screen: everything
  should work.
* **"cancel" inside the countdown.**  With a throwaway Notepad window in
  front, say "close window", then "cancel".  Does the second word land
  inside the 1.2 s countdown?

Results: `docs/modules/speech.md` ("Known limits", and accuracy on real
voices beside the synthetic figures).  Common mishearings can be fixed in
`speech/normalize.py`.

## 4. UI

Design notes are in `docs/modules/ui.md`.  Check on real screens:

* Click-through overlays stay out of Alt+Tab, and clicks pass through them.
* The dock, the "did you mean" chips and the action menu tiles take clicks
  without stealing keyboard focus from the app in front.
* The dock's AppBar strip: maximised windows stop at the dock's edge, and
  the strip goes away when Sightline quits.
* Mixed DPI: overlays, the dock and windows on each monitor at their own
  scale.
* The zoom lens: a click in the lens lands on the real point under it.
* The tray icon and its menu.
* Sound latency: armed-click ticks and command sounds arrive on time.
* Dark title bars at 125%.
* Calibration dots cover the whole screen, corners and edges included.
* The camera preview in the set-up wizard runs smoothly.
* The "Open settings" buttons on the Windows settings page open the right
  pages.
* "Hand over" in helper mode: after it, everything works by eyes and voice.
* Real eye clicks in the practice room.
* Focus after picking a chooser chip or a menu tile goes back to the app.
* Hit tests at 100, 125, 150 and 200% scaling.
* The countdown chip sits where it can be read and blinked on.

Results: tick them off in `docs/modules/ui.md` ("Known limits"), with
anything that failed.

## 5. Robustness and security

* **The watchdog with a real crash.**  Start `.venv\Scripts\sightline.exe`,
  then end the app's Python process in Task Manager: it should come back
  with "Sightline restarted after a problem".  Quit from the tray: no
  restart.  Sign out of Windows: no restart.
* **The message boxes.**  End it four times within 10 minutes: the
  watchdog gives up and shows a message saying where the log is.  Start a
  second copy while one runs: "Sightline is already running".
* **The On-Screen Keyboard registry.**  Note the values under
  `HKCU\Software\Microsoft\Osk`, say "show keyboard" then "hide keyboard",
  and check they are back as they were.  Also quit Sightline while the
  keyboard is open.
* **Keep-awake and the screen lock with a real camera.**  The screen stays
  on while someone is in front of the camera; once nobody has been seen
  for 5 minutes (`keep_awake_minutes`), Windows may lock as usual.
* **Running as administrator.**  Start `sightline-console` from an
  administrator terminal: a notice should say it does not need to run that
  way.  Quit straight away.
* **Keys stuck after a crash.**  Say "hold shift", end the app in Task
  Manager, and let the watchdog restart it: Shift should no longer be
  held.
* **The Run entry.**  Turn on "Start Sightline when I sign in to Windows"
  (Settings, Start-up page): a value named Sightline appears under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, pointing at
  `.venv\Scripts\sightline.exe`.  Sign out and in: Sightline starts.  Turn
  it off: the value goes.
* **The countdown on real buttons and consoles.**  A gaze click on a real
  Delete or Send button, and "press enter" in a real Command Prompt, should
  each wait for the countdown.
* **`window_at` on awkward windows**: an owned dialog (Notepad's Save As),
  a Chromium popup (a Chrome menu), a UWP app (Settings or Calculator) and
  an elevated window (Task Manager).  This prints what it finds under the
  pointer once a second for 30 seconds:

  ```powershell
  @'
  import ctypes, time
  ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # physical pixels, as in the app
  from sightline.desktop import win32
  from sightline.desktop.windows import Win32WindowManager
  wm = Win32WindowManager()
  for _ in range(30):
      print(wm.window_at(*win32.cursor_pos()), flush=True)
      time.sleep(1)
  '@ | .venv\Scripts\python.exe -
  ```

  A dialog or popup should name its app's window.

Results: the Status paragraphs in
`docs/reviews/2026-10-25-security-review.md` (S-06, S-08, S-12, S-13 and
S-14 each say what was not yet tried), and `docs/modules/desktop.md` for
`window_at`.

## Where results go

| What | Where |
|---|---|
| Desktop timings | `out/desktop-live-timings.json`, `out/desktop-bench.json`, then the Timings table in `docs/modules/desktop.md` |
| Vision recordings | `%LOCALAPPDATA%\Sightline\recordings` (private, never committed); findings in `docs/modules/vision.md` |
| Speech | `docs/modules/speech.md` |
| UI checklist | `docs/modules/ui.md` |
| Robustness and security | `docs/reviews/2026-10-25-security-review.md` |
| What is still open | `docs/ROADMAP.md` |

`out/` and the recordings are ignored by git.  Logs and recordings from
these sessions are personal data (a face, the mistakes a voice makes), so
read anything before sharing it.
