# First test: set up and try Sightline in about 30 minutes

This is the short version, for the person sitting at the PC.  The full
hardware test plan, with the automated tools, is `docs/TESTING.md`.

Keep your real mouse and keyboard within reach the whole time.  The real
mouse always wins: move it and Sightline's pointer waits.

## 1. Before you start (2 minutes)

* Save and close anything important.
* Close video calls and the Camera app, so the webcam is free.
* Sit 35 to 90 cm from the screen with light on your face, not a window
  behind you.

## 2. Install (10 minutes, once)

You need 64-bit Python 3.13.  If you don't have it, run
`winget install --id Python.Python.3.13` first, then open a new
PowerShell window.

In PowerShell:

```powershell
git clone https://github.com/47noodles/sightline.git
cd sightline
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

It ends with "Sightline is installed."  Nothing outside the folder is
changed.  If you already have the folder, skip straight to step 3.

## 3. Start it

From the Sightline folder:

```powershell
.venv\Scripts\sightline-console.exe
```

A console window shows the log, and the set-up wizard opens.  Sightline
does not add itself to start-up.

## 4. The set-up wizard (10 to 15 minutes)

Use the mouse and keyboard for the first pages.

| Page | What to do |
|---|---|
| Welcome | Leave **Helper mode** on and go on. |
| Windows settings | Anything marked as a problem has an **Open settings** button.  Fix it, then come back. |
| Download | Wait for the models (about 620 MB, once). |
| Camera | Pick your webcam.  You should see yourself. |
| Microphone | Pick your microphone, talk, and watch the meter move. |
| Hand over | Press **Hand over**.  From here, use only your eyes and voice.  Say "next", "back" or "skip" to move on. |
| Eye clicks | Follow the prompts for blinks and winks.  If you can't wink, choose "I can't wink". |
| Where you look | Look at each of the 13 dots as it appears. |
| Head pointer | Follow the prompts. |
| Mouth check | Optional.  Say "skip" if you like. |
| You are ready | Opens the practice room. |

## 5. Try these (10 minutes)

Open an empty Notepad window first ("open notepad"), so stray clicks and
typing land somewhere harmless.  Tick each one that works, and note the
ones that don't.

| # | Do or say | What should happen |
|---|---|---|
| 1 | Look around the screen | The pointer follows your eyes; small head moves fine-tune it. |
| 2 | Blink normally for a while | Nothing clicks. |
| 3 | Close both eyes for about a third of a second | A click where you were looking. |
| 4 | Wink your right eye | A right click (a menu opens).  Say "cancel" or press Esc. |
| 5 | Look at Notepad and say "type hello world" | "hello world" is typed. |
| 6 | Say "new line", then "scratch that" | A new line, then it is removed. |
| 7 | Say "select all", then "copy" | The text is selected and copied. |
| 8 | Say "show numbers", then a number | Numbers appear on everything clickable; saying one clicks it. |
| 9 | Say "close window", then "cancel" straight away | A 1.2 second countdown appears and stops.  Notepad stays open. |
| 10 | Turn your head away from the screen and say "type test" | Nothing is typed.  The caption says you were looking away. |
| 11 | Say "pause eyes", then "resume eyes" | The pointer stops following you, then starts again. |
| 12 | Say "what can I say" | The full list of commands. |

If the pointer drifts, say "fix pointer" and look at the dot.  If blinks
click too easily or too hard, say "calibrate eyes".

## 6. Stop it

Click the Sightline icon beside the clock (it may be under the ^ arrow)
and choose **Quit Sightline**.  Or say "quit Sightline".  Closing the
console window also stops it at once.

## 7. If something goes wrong

* **It won't stop.**  Close the console window.
* **A key seems stuck** (after closing the console).  Press and release
  that key.
* **No camera.**  Close other apps using it, and check Settings, Privacy &
  security, Camera.
* **Voice does nothing.**  Say "wake up", or pick the microphone again in
  settings ("settings", Voice page).
* **Run the wizard again:** `.venv\Scripts\sightline-console.exe --setup`

The log is in `%LOCALAPPDATA%\Sightline\logs\sightline.log`.  It never
holds what you said or typed, but read it before sharing it anyway.

## 8. After the test

Write down what worked, what didn't, and what felt wrong (too slow, too
twitchy, misheard words).  Then open a Claude Code chat in the Sightline
folder and paste your notes.  It can run the deeper checks in
`docs/TESTING.md` with you, and tune the settings from what you found.
