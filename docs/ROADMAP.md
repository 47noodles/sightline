# Roadmap

Where Sightline stands, what comes next, and what is known to be missing.
Item numbers such as "item 12" refer to the concept review,
[`reviews/2026-10-24-concept-review.md`](reviews/2026-10-24-concept-review.md),
which holds the reasons and the evidence.  `ARCHITECTURE.md` stays the
contract; this file says what gets built when.

## Status

Alpha, for first hands-on testing.  Every feature planned for this version
is built, and the safe automated suite passes (1656 tests; 262 more need the
live desktop, Qt windows on screen, downloaded models, or are slow).
Nothing has been tuned on a real webcam, microphone or screen yet: every
timing and threshold is a prior from research and synthetic tests.

## Done in this version

**Core.**  Event bus, action registry with undo and fail-closed guards, one
risk policy for what is hard to undo (`core/risk.py`), one web address
policy (`core/urls.py`), settings with protected and read-only fields,
model downloads pinned by SHA-256, and a crash watchdog that restarts the
app at most three times in ten minutes and says on screen when it gives
up.

**Vision.**  Hybrid pointer (gaze jumps, head steers) plus head-only and
gaze-only modes; blink and wink clicks; dwell; the resume zone; eye, gaze
and head calibration; "fix pointer" for drift; an accuracy map that sizes
snapping, the chooser and targets from the real error; face-gesture clicks
with a trainer (off until trained); the reading hold; jumping only on
intent (optional); reading scroll; learning from clicks; attention
(facing the screen, mouth movement) for speech; recovery from its own
failures and "restart tracking".

**Speech.**  Offline recognition (Parakeet 0.6B, falling back to 110M on
slower computers), voice activity detection, endpointing that adapts to
the command, about 190 built-in commands with chains and repeats, "did you
mean" suggestions, paraphrase matching tuned for precision, the attention
gate (only the safety commands run while the user looks away), personal
aliases learned from corrections, a per-person command pause, vocabulary
biasing, dictation formatting, "scratch that", privacy in password fields,
and the user's own `commands.toml`.

**Desktop.**  Exact input injection with scan codes and Unicode typing,
UI Automation clickables and snapping, the hover target, editing text by
content, window and app control, the On-Screen Keyboard in hover mode
(the user's own keyboard settings put back), Magnifier, read-only checks of
the Windows settings Sightline depends on, keeping the screen on while
someone is there, undo for its actions, and plain messages for
administrator windows and the secure desktop.

**UI.**  The dock, pointer ring, captions, toasts, numbers, grid, zoom
lens, help sheet, "did you mean" chips, the number chooser for unsure gaze
clicks, the action menu at the pointer, the countdown before hard-to-undo
actions, the set-up wizard with helper mode and the Windows checks page,
the calibration wizard, a settings window built for gaze and voice (no
sliders, no typing, presets), the practice room with a tapping test and a
one-page summary for a therapist, the tray icon and earcons.

**Security review.**  An independent review found 0 critical, 2 high, 2
medium, 12 low and 6 information items
([`reviews/2026-10-25-security-review.md`](reviews/2026-10-25-security-review.md)).
Fixed: S-01 (a, b), S-02 to S-10, S-13, S-15, S-16, I-01 and I-05.  Fixed in
part: S-11, S-12 and S-14.  `SECURITY.md` covers the same-user boundary
(I-02).

## Next

1. **The first session on real hardware**, in the order in
   [`TESTING.md`](TESTING.md): the live desktop tests, a physical webcam,
   a real microphone and voice, the real-screen UI checklist, and the
   robustness and security checks.
2. **Tune from what that session measures**: the blink and wink timings,
   the long blink, the jump distance and delay, the mouth and attention
   thresholds, the `steered` threshold, snap and dwell sizes.  Record the
   reasons in the module docs.
3. **Decisions for the project owner:**
   * left wink as double click instead of middle click (item 4: middle
     click would move to the one-second blink hold and to voice);
   * whether speech may download Smart Turn v3 (an ONNX model of about
     8 MB, BSD-2-Clause) for smarter dictation endpoints;
   * keeping both Parakeet models required (about 590 MB at set-up) or
     making the 0.6B optional, at some cost in accuracy;
   * whether the set-up wizard should ask about starting with Windows.  It
     is off by default and can be turned on in Settings, on the Start-up
     page.

## Smaller follow-ups

* Speech: an echo check that holds speech overlapping what the PC itself
  is playing (WASAPI loopback, review S-01 c); a one-time warning when
  there is no camera, since the attention gate then lets everything
  through.
* The tray's Quit, and "hold alt" then "press f4", outside the countdown.
* A "copy log for a bug report" action.
* Model files: a size check or re-hash at start-up (S-11 b).  Point the
  downloader's User-Agent at this repository.  A way to download the
  optional Moonshine Base recogniser: choosing it in Settings today only
  says it is not downloaded and keeps a Parakeet recogniser in use.
* A second copy could signal the first instead of only saying so (S-12);
  optionally switch off shell and registry actions while elevated (S-14).
* Keep profile data in `%LOCALAPPDATA%` rather than the roaming profile
  (I-03); publish a lock file with hashes and run `pip-audit` (I-04); pass
  the launched process's id to `AllowSetForegroundWindow` (I-06).
* Desktop: the snap radius from `error_px`.
* Vision: a cursor move that happens only if no other input came first,
  under the injector lock; a "reading" pointer phase; face-gesture steps in
  `tools/vision_record.py`.
* UI: optionally a gesture (such as the blink hold) for the action menu;
  keep menu clicks out of click learning; `ui.labels.show` for an area, so
  the menu can offer "Numbers here".

## Known gaps

* Nothing is tuned on a real face, voice or screen yet.  Webcam gaze is
  typically 100 to 200 px off, against 16 to 45 px on synthetic faces.
* Speech accuracy comes from synthetic voices.  Real voices, accents,
  dysarthric speech and real microphones are still to be measured;
  isolated one-word commands are the weakest spot, and "tap" (click) and
  "tab" (the key) can be confused.
* English only: the vocabulary, grammar and models.
* Facing the screen is not proof that the owner spoke: a video or a call
  can still reach the microphone.  The safety-only rule limits what such
  speech can do.
* Apps running as administrator cannot be controlled: that needs a signed
  UIAccess build (item 12).  Windows Voice Access can handle those windows
  and the sign-in screen.
* No installer and no releases: Sightline runs from a Python virtual
  environment, and set-up downloads about 620 MB of models.
* Mixed-DPI and multi-monitor placement, click-through and no-activate
  window styles are checked offscreen and in code, not yet on real
  screens.  The zoom lens is a still image.
* If Sightline is killed rather than quit, Windows may keep the dock's
  reserved strip until Explorer notices, and On-Screen Keyboard settings it
  changed are not put back.

## Later

* Item 12: a signed UIAccess build with an installer, so administrator
  windows work without running Sightline as administrator.  Needs a first
  release.
* Item 17: pilot users through MND and ALS associations, spinal units and
  similar groups.
* Items 16 c to e: a recogniser check at set-up, noise suppression, an
  atypical-speech test set.
* Item 2: a voiceprint check.  18: posture profiles.  20: app-aware command
  packs.  21: fatigue-aware adaptation.  23: tremor auto-tune.  24:
  mouth-sound triggers.  25: a fallback ladder ending in switch scanning.
  26: Sightline's own keyboard with prediction.  7b: numbered keys on the
  On-Screen Keyboard.
* Multi-camera support, a game mode, other languages.
* Lip reading is left out of this version.

## Not copied

* Actigaze's confirm buttons (patented) and Microsoft's dynamic dwell
  patents: design around them.
* OptiKey (GPL-3.0) and Dasher (GPL): separate programs only.
* Project GameFace was archived on 5 September 2025: ideas only.
