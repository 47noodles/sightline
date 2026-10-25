# Sightline: conceptual design review

Reviewed on 24 October 2026 against `ARCHITECTURE.md` (sections 7 and 8 closely), the four `config.py` files, and a skim of the code that exists now (`vision/pointer.py`, `eyes.py`, `head.py`, `learning.py`, `gaze_model.py`, `speech/grammar.py`, `matcher.py`, `semantic.py`, `asr.py`, `desktop/handlers.py`, `spatial.py`, `accessibility.py`, `elevation.py`, `ui/dock.py`, `overlays/labels.py`, `grid.py`, `zoom.py`).  I did not run the app, play audio or open any window.

How to read it.  Evidence is tagged (paper), (vendor) for product documentation and support pages, or (my reasoning).  Where I could only read an abstract or a secondary summary I say so.  Numbers I worked out myself are marked "my estimate".  Impact: High means it decides whether the product is usable or trusted day to day for many users, Medium means a clear gain for many users or a large gain for some, Low is polish.  Effort: Small is days, Medium is one to three weeks for one engineer, Large is longer.

One framing point before the list.  Nothing I found suggests a webcam gaze model will reach the 0.5 to 1 degree of infrared trackers.  The newest in-browser work I found (WebEyeTrack, August 2025) reports 2.32 cm error on GazeCapture after personalisation with as few as nine samples (paper; GazeCapture is a phone and tablet dataset, so I cannot convert it to degrees).  So the precision has to come from interaction design, and most of the items below are about that.

---

## 1. What is already right (keep it)

* Hybrid pointer: gaze jumps, head steers.  This is the structure the research supports.  MAGIC pointing (Zhai, Morimoto and Ihde, CHI 1999) is the origin.  In Look and Lean (Spakov, Isokoski and Majaranta, ETRA 2014) head assistance in some conditions brought the pointer to within about 7 px of the target centre against about 19 px for eyes alone, with no loss of time (paper abstract).  GazeSwitch (Hou et al., PACM HCI 2024) is the current refinement.
* Natural blinks rejected by duration, winks switched off per user when they cannot be told from blinks, the pointer frozen with a 120 ms lookback when a lid moves, the click firing on re-open, ticks to confirm with the eyes shut.
* The fixation clamp and the post-jump anchor, which stop a slightly wrong estimate from pulling the pointer back.
* Learning from clicks with two guards, so drift is corrected without a recalibration.
* "What you see is what you click": one snapping rule shared by the hover highlight and the click, and `snap_point`, which avoids clicking the middle of a large row.
* Prefix-aware endpointing, a closed grammar first with paraphrase matching tuned for precision, chaining, and the Talon-derived vocabulary.
* A dock that never takes focus, resume-by-gaze, yielding to a real mouse, UIPI detection with a plain message.
* Plain-English strings, no text-to-speech, high-contrast two-tone feedback.

On the Midas-touch problem specifically: the eye path is handled well (duration rule, per-user calibration, freeze and lookback, a cancel at 2.5 s, dwell off by default).  The weaker side is voice and mode state, which is where most of the risks below sit.

## 2. Mistakes and risks in the current design

In order of how much I would worry.

**R1. Voice commands act where the cursor is when the command finishes, not where the user was looking when they began to speak.**  `desktop/handlers.py` takes `cursor_pos()` when a spoken click has no coordinates.  With a 250 ms command endpoint, recogniser time and dispatch, that is roughly 600 to 800 ms after the word started (my estimate), and people look at a target about 630 ms before they speak (Kaur et al., ICMI 2003).  In hybrid mode the pointer has often warped to the next fixation by then.  The blink path already solves this with a lookback.  Fix: item 1.  It is cheapest to fix now, while the speech service is still being written.

**R2. An open microphone, type-everything-when-a-text-field-has-focus, and paraphrase matching, with no check that the speech is for the computer or from the user.**  A side conversation, a carer or a television becomes typed text or a command ("go to sleep" and "close this" are ordinary phrases).  Fix: items 2 and 14.

**R3. The answer to a blocked window is "Run Sightline as administrator" (`ELEVATED_MESSAGE`), and there is no plan for lock-out.**  Running the whole app, with a camera and microphone, as administrator is the wrong fix.  Microsoft's mechanism for assistive technology is UIAccess, and `elevation.py` already recognises it.  The plan also says nothing about the UAC prompt, the sign-in screen, a screen lock that arrives while someone reads, a video call taking the camera, or recovery after a crash inside the app.  Fix: items 3 and 12.

**R4. The dock is on a screen edge, where webcam gaze is least accurate, and it works as "choose the action, then the target".**  That needs a trip to the edge and back for every non-default action, and the scroll and zoom buttons need a "last target" memory to cope.  Tobii's own Gaze Selection mode has the same shape and their support page says the user must return to the toolbar before every click (vendor).  Fix: item 19.

**R5. The only eyes-only typing route is the Windows On-Screen Keyboard in hover mode at 800 ms a key, set up by writing registry values that osk.exe reads.**  800 ms a key is an upper bound of about 15 words a minute before any pointer travel or errors (my arithmetic), and it is also the only route for passwords.  Fix: items 7 and 26.

**R6. The default gestures spend the easy actions on rare things.**  Left wink is middle click, which is rarely needed, while a double click needs a 1.0 s closure, the longest and most tiring gesture in the set.  Fix: item 4.

**R7. Several constants are smaller than the measured error across much of the screen.**  Snap radius 48 px, dwell tolerance 40 px, resume-zone margin 60 px, and dock buttons at the edge, against webcam error of 60 to 230 px.  A vendor test of a 5-point webcam calibration reported about 93 px near the centre and 148 to 170 px at the bottom corners (vendor blog, indicative only).  The calibration already measures held-out error per target, and nothing uses it after set-up.  Fix: item 6.

**R8. Nothing has been tried on a real face or voice.**  The development machine has no camera or microphone, so 350 ms, the 0.6 and 0.4 closure levels, the 180 px warp, the 130 ms warp dwell and the 250 ms endpoint are priors, not measurements.  The studies cited here used lab trackers and, mostly, healthy students (Actigaze: 25 volunteers aged 18 to 45).  Fix: item 17.

**R9. One camera, shared.**  A video call can take it.  Until Windows 11 24H2, one app at a time could open a webcam; 24H2 added a per-camera switch, "Allow multiple apps to use camera at the same time", off by default (third-party guides; confirm in Microsoft's release notes).  `camera.py` already reports a "busy" error; the guidance around it is missing.  Fix: item 3.

**R10. Click learning anchors voice clicks at "now minus 0.1 s".**  By then the gaze has usually left the target.  Use the speech onset (item 1).

**R11. Reuse and maintenance traps.**  Project GameFace was archived by Google on 5 September 2025.  OptiKey is GPL-3.0 and Dasher is GPL, so both can only be run as separate programs.  The Actigaze confirm-button scheme is patented (the University of Bath record mentions a patent filing) and Microsoft holds patents titled "Dynamic eye-gaze dwell times", which cover cascading dwell.  Most public gaze datasets are research-only (I did not verify each licence), so do not train a bundled model on them without checking.

---

## 3. Improvements, ordered by impact divided by effort

Where the time goes today, from the configured numbers (my estimates, to be replaced by measurements in item 17):

* Left click by blink: 130 ms warp dwell and 80 ms jump, plus 0.3 to 1 s of head steering when the jump lands a little off, plus a 350 ms closure and re-open detection: about 1.0 to 2.0 s.
* Double click by the 1.0 s closure: about 0.65 s more than a single click.
* Spoken "click": about 350 ms for the word, 250 ms endpoint and about 100 ms recogniser and dispatch: about 0.7 s after speech starts, and the pointer is read at the end of that.
* "Click save": the same plus up to 1 s to find the element, but no aiming at all.
* "Show numbers", then a number: two utterances plus a scan that can take 1.5 s: about 2.5 to 3.5 s.
* Hover typing: 0.8 s a key plus pointer travel: a few words a minute in practice.

The items below take time off these where they can (face gestures, single-utterance sequences, correct targets the first time) and, where they add a step (the chooser, confirmations), they add it only when the system is unsure.

| # | Change | Impact | Effort | Touches |
|---|---|---|---|---|
| 1 | Aim voice commands at where the user looked when they began to speak | High | Small | speech, vision, desktop |
| 2 | Only act on speech that is for the computer | High | Small to Medium | vision, speech, UI |
| 3 | Make set-up and the worst day survivable without a helper | High | Small to Medium | UI, desktop |
| 4 | Spend the easy gestures on frequent actions; give "click" a fast spelling | Medium | Small | vision, speech |
| 5 | Tame the warp | Medium | Small | vision |
| 6 | Size every precision aid from the calibration's own error map | Medium | Small | vision, desktop, UI |
| 7 | Passwords and sign-ins without typing | Medium to High | Small to Medium | speech, desktop, UI |
| 8 | Settings you can change with the product's own input | Medium to High | Small | UI |
| 9 | Let the user or a helper add commands, snippets and words | Medium to High | Small to Medium | speech, UI |
| 10 | Ambiguity-aware targeting with a local number chooser | High | Medium | desktop, UI, speech |
| 11 | Face-gesture clicks beside blinks and winks | High | Medium | vision, UI |
| 12 | A UIAccess build: signed, installed under Program Files | High | Medium | desktop, packaging |
| 13 | A practice room and a gentle ramp | High | Medium | UI |
| 14 | Recovery and trust: undo, did-you-mean, graded confirmation | High | Medium | speech, desktop, UI |
| 15 | Edit and correct text by content | High | Medium | speech, desktop, UI |
| 16 | Atypical speech and noise: aliases, adaptive endpoints, test set | High for affected users | Medium | speech |
| 17 | Measure with real people early | High | Medium | process, UI |
| 18 | Posture profiles and a five-second drift fix | Medium to High | Medium | vision, UI |
| 19 | A pointer-anchored action menu instead of dock round trips | Medium to High | Medium | UI |
| 20 | App-aware command packs mined from the Talon community | Medium | Medium | speech, desktop |
| 21 | Fatigue-aware adaptation | Medium | Small to Medium | vision, UI |
| 22 | Gaze-enhanced scrolling | Medium | Small to Medium | vision, desktop |
| 23 | Auto-tune for tremor and involuntary movement | Medium to High | Medium | vision |
| 24 | Mouth-sound triggers | Medium to High | Medium | speech |
| 25 | A fallback ladder ending in switch scanning | Medium to High | Medium to Large | vision, UI, desktop |
| 26 | Own large-key keyboard with prediction | Medium to High | Large | UI, desktop, speech |

### 1. Aim voice commands at where the user looked when they began to speak
**Impact High, effort Small.  Touches speech, vision, desktop; one core change request.**

**Change.**  Add `aim_t`, the VAD onset time, to speech-made `Action`s (a core change request).  Pointer-bound commands (click, double click, right click, scroll here, zoom, numbers here, drag, mark) ask the pointer engine for the last resting point at or just after `aim_t`, using the one-second history `PointerEngine.position_at` already keeps, instead of reading the cursor at dispatch.  If the pointer was still moving at onset because the user was steering with the head, use the first resting point after onset.  Use `aim_t` as the anchor for click learning and to rank duplicate names in `element.click` ("Save" twice) by distance to that point.  Once the pointer is latched, add the deictic family at almost no cost: "select this word" (double click), "this line" (triple click), "link in new tab" (control click), "shift click", "control click".

**Why.**  People look at the target before they speak.  The one study that measured it found the key fixation on average 630 ms before the start of the spoken verb (range 150 to 1200 ms across people), and choosing the fixation nearest the verb onset gave about 95% correct targets (Kaur et al., "Where is 'it'?", ICMI 2003, abstract-level source).  The blink path already uses a lookback for the same reason (R1).

**Reuse.**  None needed; the history buffer exists.

### 2. Only act on speech that is for the computer
**Impact High, effort Small (face and head gate) to Medium (mouth motion, voiceprint).  Touches vision, speech, UI.**

**Change.**  Give each utterance a "for me" score from three signals.  First, a face is present and the head is turned no more than about 25 degrees from the screen (already measured).  Second, the mouth moved during the speech, from the jaw and lip blendshapes the Face Landmarker already returns (`jawOpen`, `mouthClose`, `mouthFunnel`, `mouthPucker`).  Third, optionally, a voiceprint check with a small speaker-embedding model enrolled from three sentences at set-up.  Use the score to grade trust rather than to mute: a low score allows only exact command matches, no paraphrase matching and no free-text typing; a high score allows everything.  Use hysteresis so a glance at the keyboard does not silence commands.  Show a plain "Not listening to this" state in the dock.

**Why.**  Google's Look and Talk (2022) decides that speech is for the device from proximity, face match, head orientation, gaze direction, lip movement and voice match together, all on the device (vendor description).  Sightline already has the first four at 30 frames a second for free.  Without a check, `auto` mode types anything said near a focused text field.  Limits: people with weak jaw movement, masks or ventilator interfaces that hide the mouth.  So every signal must be optional and measured at set-up.

**Reuse.**  MediaPipe blendshapes (Apache-2.0, in use).  sherpa-onnx speaker embeddings (Apache-2.0 library); the 3D-Speaker CAM++ and ERes2Net models are Apache-2.0 per their repository, but check each model card (NeMo TitaNet has its own licence).  Silero VAD (MIT) if the onset needs a second opinion.

### 3. Make set-up and the worst day survivable without a helper
**Impact High, effort Small to Medium.  Touches UI, desktop.**

**Change.**  (a) A set-up check screen that reads and explains the Windows switches Sightline depends on: camera and microphone privacy, "Allow multiple apps to use camera at the same time", the UAC level, and whether Voice Access starts before sign-in, each with a button that opens the right Settings page for the helper.  (b) Start at sign-in, plus a tiny watchdog that restarts Sightline after a crash and shows a large "Restarting, say wake up" banner.  Inside the process, a lost camera is handled (`CameraSupervisor`), the microphone has its own watchdog (`speech/audio.py`), and the bus and the action registry already isolate failures in subscribers and handlers.  But as `vision/service.py` reads today, an exception in the pipeline's own code (measure, closure, gestures, engine, dwell, learner, calibration) ends the vision thread for good, with a one-line notice and no way back by voice.  Catch the failure per frame (log it, reset the filters, carry on), restart the thread after repeated failures, and add a "restart tracking" action to the registry and the dock.  (c) Keep the display awake while a face is present (`SetThreadExecutionState`) and release it when the face has been gone for a few minutes; a person who reads without moving is otherwise idle to Windows (my reasoning).  (d) Camera advice: a camera under the screen looking slightly up sees irises and lids better than one above it (my reasoning; eye-tracker bars sit under the display), a lamp in front rather than a window behind, and a second cheap USB camera if the first is needed for calls.  (e) A "helper mode" so the first ten minutes (install, permissions, camera angle) can be done with mouse and keyboard, then handed over.

**Why.**  Lock-out is the worst failure.  Tobii's support pages say the UAC prompt cannot be operated by gaze and advise turning off desktop dimming or UAC (vendor; the article is old but the principle holds).  Microsoft documents Voice Access on the lock screen and during Windows set-up from Windows 11 22H2, with the setting "Start voice access before sign-in" (vendor); a third-party guide says an administrator must switch it on.  That is the one supported route to a sign-in screen.  Windows 11 24H2 added the multi-camera switch (R9).

**Reuse.**  Windows `ms-settings:` links and `SetThreadExecutionState`.  Nothing to install.

### 4. Spend the easy gestures on frequent actions; give "click" a fast spelling
**Impact Medium, effort Small.  Touches vision defaults, speech grammar.**

**Change.**  Default map: long blink is left click, right wink is right click, left wink is double click, and the 1.0 s hold opens the action menu (item 19), or is double click until that exists.  Middle click and drag move to voice and the menu.  Everything stays editable (`gesture_actions`).  In the grammar, split "tap" (and a short word such as "go") from "click <name>", so the bare words can end after about 150 ms, and keep `wait_after` only on the "click" that may be followed by a name.  Today `mouse.click` carries `wait_after=True` for the whole spec, so every plain "click" pays the extra wait.

**Why.**  By frequency, left click far exceeds right click, which exceeds double click, which far exceeds middle click (my reasoning).  The 1.0 s closure costs about 650 ms more than a single click; a wink needs only 180 ms.  "Click" is the most common spoken pointer command.

**Reuse.**  None.

### 5. Tame the warp
**Impact Medium, effort Small.  Touches vision.**

**Change.**  (a) After a jump, ignore head movement for about 200 ms: people turn the head part of the way during a large saccade, and the pointer then overshoots the target it just reached.  (b) A "reading calm" state: while fixations are short, run along lines and the head is still, hold the pointer instead of chasing each fixation; it moves again on the first deliberate head movement or on "there".  (c) Offer MAGIC's conservative variant as a setting: jump only on an intent signal (first head movement, a click gesture, or a spoken command).  (d) Later, replace the distance threshold with a learned eye-or-head switch.

**Why.**  Zhai et al. separate liberal warping on every large saccade (early, possibly distracting) from conservative warping only after the manual device is actuated (from their slides; I could not retrieve the full results).  The eye-head switching literature describes exactly the overshoot in (a), and GazeSwitch (2024) found a learned switch needs less head movement than a threshold rule (abstract).  A pointer that chases reading also triggers hover side effects such as tooltips, hover menus and video controls (my reasoning).  The post-jump anchor in `pointer.py` stops gaze pulling the pointer back; this stops the head pushing it on.

**Reuse.**  None.

### 6. Size every precision aid from the calibration's own error map
**Impact Medium, effort Small.  Touches vision, desktop, UI.**

**Change.**  Turn the held-out per-target errors the gaze fit already stores into `error_px(x, y)`, interpolated across each monitor, and publish it with the pointer sample.  Use it for the snap and chooser radius (replacing the fixed 48 px), the margins of the dock and resume zone, the minimum size of number badges and chooser chips, and a plain-words "comfort size" line at the end of calibration ("At your accuracy, controls smaller than about 2.5 cm are hard to hit by eyes alone.  Setting Windows text and app size to 150 percent will help.").  Add calibration dots near the corners and edges, because the Start button, tray, close button and dock live there.  Treat dock buttons, number badges and chooser chips as expanding targets: grow the hit area (not necessarily the drawing) by the local error once the pointer is near.

**Why.**  Webcam error is not uniform (R7).  Fixed constants are right in one place and wrong in another.  Larger on-screen targets remain the cheapest precision gain, but only if the user is told.  Target expansion helps pointing even when it starts after 90% of the distance has been covered, though neighbouring expanding widgets need care to avoid overlap (McGuffin and Balakrishnan, TOCHI 2005, abstract).

**Reuse.**  None.

### 7. Passwords and sign-ins without typing
**Impact Medium to High, effort Small (commands) to Medium (numbered keyboard).  Touches speech, desktop, UI.**

**Change.**  (a) "Fill login" and "fill password" commands that fire the user's password-manager shortcut (a setting; browsers and the common managers all have one) after focusing the field, so most sign-ins need no typing at all.  (b) Put numbers on the keys of the On-Screen Keyboard so a whole run of keys can be said in one utterance ("twelve four nine") instead of one 800 ms hover each, and nothing shows which keys were pressed.  The labels overlay scans the foreground window and taskbar only, so include osk.exe's window.  (c) Keep password fields out of dictation and captions, as planned.

**Why.**  Hover typing is bounded at about 15 words a minute (R5) and shows the password to anyone watching the pointer.  Microsoft's Voice Access offers the same idea on the sign-in screen: say "show keyboard", then speak the number labels, which keeps typed characters from being overheard (vendor).  Password managers remove the problem for most sites.

**Reuse.**  `uiautomation` (Apache-2.0, already a dependency) to enumerate OSK keys.

### 8. Settings you can change with the product's own input
**Impact Medium to High, effort Small.  Touches UI.**

**Change.**  Build the settings window so that nothing needs a precise drag or typing.  Sliders become large minus and plus steppers that say the value aloud on screen.  Each setting previews its effect live (the pointer-speed setting moves a sample dot).  The five most used (pointer speed, click time, voice sensitivity, pause time, dwell time) sit on one front page as plain choices ("slower, a bit slower, as is, faster").  Free-text fields such as the wake phrase and microphone name become choices or a voice-entry flow.  The whole window works with "next setting" and "set to three".  Offer named presets such as "steady hands", "tremor" and "tired eyes" that set groups of values, and keep the dozens of advanced values behind one door a helper can open.

**Why.**  The settings window is generated from field metadata with a minimum, maximum and step (`ARCHITECTURE.md` section 4).  A generated slider is a drag target and a spin box is two tiny buttons, which are hard to hit with gaze error of 60 to 230 px.  The people who most need to tune Sightline are the ones least able to operate default controls.  Ability-based design (Wobbrock et al., TACCESS 2011) argues for adapting the system to the user, and presets are the cheap form of that (my reasoning).  The window is not written yet, so this costs little now.

**Reuse.**  None; `GazeButton` already exists.

### 9. Let the user or a helper add commands, snippets and words
**Impact Medium to High, effort Small to Medium.  Touches speech, UI.**

**Change.**  Load a user file of hassil sentences mapped to actions (key sequence, text, open app or URL, chains), plus snippets ("insert my address") and a word list passed to the recogniser's key-term biasing (`set_keyterms` is already wrapped).  Add a voice flow, "new command", that asks for the phrase and offers actions from a short numbered list, and an "add to vocabulary" command.  Ship examples for email sign-offs and addresses.

**Why.**  Personal commands and vocabulary are what keep long-term users.  Windows Voice Access has "Add to vocabulary" and voice shortcuts, and Apple's iOS 18 added Vocal Shortcuts for custom phrases (vendor).  Names, street addresses and email addresses are what a small recogniser gets wrong most.

**Reuse.**  hassil (Apache-2.0, already used).  talonhub/community (MIT) for example snippets.

### 10. Ambiguity-aware targeting with a local number chooser
**Impact High, effort Medium.  Touches desktop, UI, speech.**

**Change.**  Make the click rule depend on how much the user has verified the pointer.  If the head has steered since the last jump (phase "fine"), trust the pointer and click where it is, as now.  If the pointer came straight from gaze (phase "warp" or "gaze", or a gaze-only user), take every clickable that touches a circle of radius `error_px` around it (a filter on the existing snapshot, no new UI Automation calls).  One candidate: click it.  Two to six: show large numbered chips beside them in a fixed angular order and accept "two", or a blink while looking at a chip.  More than six: offer the zoom lens or grid.  Later, weight candidates by a Gaussian over each rectangle plus a prior from past clicks.  Also let the numbers and the grid take a whole sequence in one utterance ("five two eight" narrows three levels), as Voice Access does with "mouse grid 1 2 3" (vendor).

**Why.**  Gaze error of 60 to 230 px is several times the height of a toolbar button, so "nearest" is often wrong.  The literature's answer is to carry the uncertainty and decide late (Schwarz, Hudson, Mankoff and Wilson, UIST 2010), to choose targets by probability rather than by hit (Bi and Zhai, "Bayesian Touch", UIST 2013, abstract), or to give each target its own region (the bubble cursor, Grossman and Balakrishnan, CHI 2005, which depends on target density).  A confirm step has a measured cost.  Actigaze (Lutteroth, Penkar and Weber, UIST 2015), with a lab tracker, had median click times of 1.46 to 1.67 s against 1.1 s for a mouse, and 2.1 to 3.5% misclicks against 1.9%.  An earlier spoken-colour chooser (Miniotas et al. 2006, as reported by Spakov et al.) took over 2.5 s with over 15% errors on 30 px targets.  So the chooser must be the exception, one number or one blink, never a mode.  Zoom tools raise hit rates but slow selection (Skovsgaard et al., ETRA 2010, as summarised by Spakov et al.).

**Reuse.**  No library.  Actigaze is patented, so use numbers and voice rather than copying its stable colour-coded confirm buttons without advice.

### 11. Face-gesture clicks beside blinks and winks
**Impact High, effort Medium.  Touches vision, UI.**

**Change.**  Generalise `gesture_actions` into trigger-to-action for any of: long blink, winks, brow raise, smile, mouth open, cheek puff.  All of these are in the 52 blendshapes already computed.  Extend the "eyes" calibration into a short trainer that records neutral and each gesture, scores how well each separates (as `fit_eye_profile` does for winks), shows the false-trigger rate over 30 s of talking and reading, and offers the best two or three.  Fire on a rising edge after 120 to 200 ms with a refractory period, gate mouth gestures on "not speaking", and compensate brow raise for looking up.

**Why.**  A click made with the eyes open can fire roughly 200 ms sooner than a 350 ms closure plus re-open (my estimate), needs no lookback because the gaze never leaves the target, and works for people who cannot wink or who have ptosis, dry eyes or blink fatigue.  Project GameFace (Google, Apache-2.0, now archived) binds facial gestures to actions and was released for Android in 2024 (press reports: open the mouth to move, raise the eyebrows to click).  Grauman et al. (2003) built a blink and eyebrow-raise interface for people with severe disabilities (paper title and abstract).

**Reuse.**  MediaPipe blendshapes (Apache-2.0).  GameFace for binding ideas only, since it is archived.

### 12. A UIAccess build: signed, installed under Program Files
**Impact High, effort Medium.  Touches desktop, packaging.**

**Change.**  Replace "Run Sightline as administrator" with a UIAccess build, which lets a normal-user process drive elevated windows (Task Manager, installers, admin tools) without running the whole app as administrator.  It needs a manifest with `uiAccess="true"`, an Authenticode signature, and installation under Program Files, which means a frozen build and an installer instead of `pip install`.  Apply to the SignPath Foundation as soon as there is a first tagged release (it requires a released, actively maintained project), and start the packaging spike now.  State the secure-desktop limit honestly: no app can click the dimmed UAC prompt.  The options are the UAC level that does not dim the desktop (test on Windows 11 that a UIAccess build can click it) or Voice Access.

**Why.**  Microsoft's security guidance for assistive technology lists exactly these requirements and says UIAccess is for assistive technology only (vendor).  `elevation.py` already detects it.  SignPath gives free signing to open-source projects that meet its rules: an OSI licence, actively maintained, already released, an automated build, and a published signing policy (vendor).  A signed installer also removes the SmartScreen warning a helper would otherwise have to click through.  This is a calendar problem more than an engineering one.

**Reuse.**  SignPath Foundation (free for open source), PyInstaller or Nuitka for freezing, Inno Setup for the installer (check each licence for your build).

### 13. A practice room and a gentle ramp
**Impact High, effort Medium.  Touches UI.**

**Change.**  After calibration, a practice page with large targets that scores hits, misses and time and shows the ring and highlight clearly.  Unlock features in steps: pointer, click, five commands, dictation, numbers and grid, the rest.  A dry-run setting for the first sessions that shows the target and caption without clicking.  Once a day, one line when the user does something the slow way ("You can say scroll down").  Show five commands at a time rather than the whole sheet.

**Why.**  A deployment study of dwell-free typing with real AAC users (Tobii Dynavox Communicator, IUI 2023) found opacity ("it feels like guessing"), poor error correction and noisy-gaze accuracy to be adoption barriers, and lists "provide a tutorial and performance indicator" as a design implication (paper).  The Vocal Joystick authors note that speech interfaces are often abandoned after a short initial trial (paper, 2005).  For this group, learning is part of the product.

**Reuse.**  None.

### 14. Recovery and trust: undo, did-you-mean, graded confirmation
**Impact High, effort Medium.  Touches speech, desktop, UI.**

**Change.**  (a) "Undo that" for the last Sightline action where an undo exists (dictated phrase: remove it; closed tab or window: reopen; drag: Control+Z), and a caption that says plainly when none exists.  (b) After a not-understood utterance, show the top three candidates as numbered chips ("1 scroll down, 2 scroll up"), accepted by "one" or a blink.  (c) A 1.2 s cancel window for actions that are hard to reverse (close window, quit app, delete line or selection, send, empty the recycle bin), and never run those from a paraphrase match without it.  (d) Count, per hour, actions undone or cancelled within 3 s as a false-activation proxy, and show it in diagnostics.

**Why.**  Carrying uncertainty and mediating late is the recommended pattern for ambiguous input (Schwarz et al. 2010).  An error costs more for people who cannot fix it by hand: every mistake means the same slow route again.  The IUI 2023 study found poorly integrated error correction to be a barrier.

**Reuse.**  None.

### 15. Edit and correct text by content
**Impact High, effort Medium.  Touches speech, desktop, UI.**

**Change.**  Add "select <phrase>", "select from <phrase> to <phrase>", "move before/after <phrase>", "correct <word>" (numbered alternatives from the recogniser's n-best list or a spelling list), "spell that" and "add to vocabulary".  Implement with UI Automation TextPattern where the control supports it (Notepad, Word, many browser text fields).  Otherwise use Find then Escape, or a caret walk using the text-before-caret the desktop module already reads.  Keep a short dictation buffer for "correct".

**Why.**  Windows Voice Access ships these: "Correct that", "Correct <text>", "Spell that", "Select <text>", "Select from <text 1> to <text 2>", "Move before/after <text>", "Insert before/after <text>", "Add to vocabulary" (vendor command list).  Dragon's Select-and-Say and correction menu are the long-standing model (my knowledge).  Without hands-free correction, one misheard word costs a long detour through arrow keys and shift-select.

**Reuse.**  `uiautomation` (Apache-2.0) wraps TextPattern; check its find-text call before relying on it.

### 16. Atypical speech and noise: aliases from corrections, adaptive endpoints, a test set
**Impact High for affected users, effort Medium.  Touches speech.**

**Change.**  (a) Personal aliases: when an utterance is rejected and the user then does the intended thing within 8 s (by another route, or by picking a chip), offer to add what was heard as an alias, accepted by "yes" or a blink.  (b) Learn each user's pause distribution and set the command endpoint per person, since slow or breath-limited speakers pause inside a command.  The matcher's `PrefixInfo` already says when a command is incomplete, so lengthen the wait only then.  (c) A ten-word check at set-up that tests each installed recogniser on the user's own voice and picks the best for commands.  (d) Noise suppression ahead of the recogniser for fans and ventilators.  (e) A regression set built from the Speech Accessibility Project and similar corpora, under their data agreements.

**Why.**  80 to 95% of people with ALS eventually cannot meet daily communication needs with natural speech (Beukelman, Fager and Nordness 2011), so voice alone will degrade over a user's lifetime; keep the other channels strong.  Open recognisers are weak on atypical speech.  In the Speech Accessibility Project's 2025 challenge the winning team reached about 6% and 8% word error rate on two test sets, against about 15% and 18% for current open-source recognisers (project news).  Fine-tuning on a Parkinson's dysarthria set cut error from 36.3% to 23.7% in a 2024 JSLHR study (abstract).  Project Euphonia reported a 62% relative gain for ALS speech with personalisation, 71% of it from the first five minutes of data (press and blog summaries; I did not check the paper).  Fine-tuning alone leaves large error, which is why a small closed command set with personal aliases is the better lever.

**Reuse.**  sherpa-onnx (Apache-2.0).  RNNoise (BSD-3-Clause) or DeepFilterNet (MIT or Apache-2.0) for suppression; check both.  Speech Accessibility Project data is licensed to researchers and cannot be redistributed.

### 17. Measure with real people early
**Impact High, effort Medium.  Touches process, UI.**

**Change.**  Build into the practice room an ISO 9241-9 style tapping test (throughput, error rate), the false-activation counter from item 14d, and a one-page summary a therapist can read.  Add an "access method comparison" that lets a clinician try gaze, head, face gestures and voice for two minutes each and prints the numbers.  Recruit 5 to 10 pilot users through MND and ALS associations, spinal units, cerebral palsy organisations and groups such as SpecialEffect before polishing further.  Agree go and no-go numbers in advance, for example at least 95% of intended clicks land, fewer than one false activation an hour, and 80% of new users finishing the practice room in ten minutes.  Extend the existing `recorder.py` into an opt-in, local, consented recording flow so tuning data can come back.

**Why.**  Every threshold is a prior (R8).  Assistive technology is usually prescribed by clinicians who compare access methods with numbers, and a view in their language gets the product tried.

**Reuse.**  The University of Auckland's GazeBrowser experiment framework (github.com/aucklandhci/gazebrowser) is open; I did not check its licence.  The tapping test is a published method.

### 18. Posture profiles and a five-second drift fix
**Impact Medium to High, effort Medium.  Touches vision, UI.**

**Change.**  Keep up to three gaze models keyed by distance, head pitch and lighting (desk, wheelchair, bed), switching with a short toast.  Add "fix pointer": look at one dot for 1.5 s and apply the result as an offset to the current model (three dots optional).  Offer it by voice, from the dock, and automatically when click learning sees a growing error.

**Why.**  Drift is the main enemy of webcam gaze.  Spakov et al. list lighting, eye geometry and camera-to-eye position among its causes (paper), and Microsoft's Eye Control page warns that on laptops the eye position can easily change (vendor).  Apple's camera-based Eye Tracking (iOS 18, 2024) uses a short dot sequence (press reports say about 20 s to a minute) and pairs Dwell Control with "Snap to Item" and "Smoothing" settings.  A full calibration is expensive in effort for someone who tires, and lab trackers routinely run a one-point drift check between trials (my knowledge).

**Reuse.**  None.

### 19. A pointer-anchored action menu instead of dock round trips
**Impact Medium to High, effort Medium.  Touches UI.**

**Change.**  Open a six to eight wedge menu at the pointer from "menu", from the stage-2 blink hold, or from a dock button: scroll here, drag from here, double click, right click, zoom here, numbers here, keyboard.  Select by dwell on a wedge, by a blink while looking at it, or by number.  Make wedges large and visible, with no border-crossing gestures in empty space.  Keep the dock for global modes: pause, sleep, help, settings.

**Why.**  Target-then-action keeps the target and its context, and avoids the edge (R4).  Pie menus have a long history with gaze (pEYEs, Huckauf and Urbina, ETRA 2008).  Border-crossing in empty space is error prone: a CHI 2022 study of gaze marking menus found about 12% errors for BorderPie in its largest layout against about 1% for experts using Lattice Menu, which adds visual anchors (paper; VR with an accurate tracker, so treat the numbers as a direction).  Gaze gestures in general rely much less on positional accuracy and produced under half the errors of dwell in one 12-person game study (Hyrskykari, Istance and Vickers, ETRA 2012, abstract).

**Reuse.**  None; Qt painting as in `grid.py`.

### 20. App-aware command packs mined from the Talon community
**Impact Medium, effort Medium.  Touches speech, desktop.**

**Change.**  Add contexts keyed by process name and window title so commands exist only where they make sense, and generate packs for the dozen apps this audience uses (browsers, mail, Word, Excel, a PDF reader, Teams and Zoom, media players, File Explorer, Settings, VS Code).  Convert talonhub/community `.talon` files to hassil sentences with a script, review them, and ship.

**Why.**  Fewer words per action and fewer collisions with ordinary speech.  `vocab.py` already comes from that repository.

**Reuse.**  talonhub/community (MIT); hassil (Apache-2.0).

### 21. Fatigue-aware adaptation
**Impact Medium, effort Small to Medium.  Touches vision, UI.**

**Change.**  Track per-gesture success over a session: cancelled holds, retries within 3 s, clicks undone.  When it trends worse, show a rest suggestion, offer a lower-effort trigger (dwell, face gesture, voice), and lengthen hold thresholds temporarily.  Add a "rest eyes" command that pauses tracking for 20 s with a quiet chime.

**Why.**  The brief names fatigue, and I found no published threshold, so measure per user (my reasoning).  Natural blink duration is quoted as roughly 100 to 300 ms by sources that disagree on the details, and blinks are likely to be slower and less crisp when tired (my reasoning), so a fixed 350 ms rule will probably misfire more late in the day.  The eye profile already stores `natural_blink_ms`.

**Reuse.**  None.

### 22. Gaze-enhanced scrolling
**Impact Medium, effort Small to Medium.  Touches vision, desktop.**

**Change.**  A "reading scroll" mode: with the pointer in a document, looking in the bottom fifth scrolls at a rate that grows with depth, the top fifth scrolls back, the middle stops.  Use wide hysteresis because of webcam error.  Keep the dock buttons and "keep scrolling".  Later, a rate that follows reading rhythm.

**Why.**  Scrolling is tied to reading, and gaze-enhanced manual and automatic scrolling were an early target of gaze research (Kumar and Winograd, UIST 2007, abstract).  Today scrolling takes a dock click or two voice commands per step.

**Reuse.**  None.

### 23. Auto-tune for tremor and involuntary movement
**Impact Medium to High, effort Medium.  Touches vision.**

**Change.**  Measure instead of asking.  At head calibration, run a 10 s hold-still and a short sweep, estimate the tremor band (about 3 to 12 Hz) in the head and gaze signals, and set the deadband, smoothing and gain from it.  Re-check quietly and suggest a re-run if jitter grows.  Consider an Angle-Mouse-style gain that lowers speed when the path wanders and raises it when the path is straight.

**Why.**  Ability-based design (Wobbrock et al., TACCESS 2011) argues that the system should adapt to the user's abilities rather than the user to the system.  The Angle Mouse (Wobbrock et al., CHI 2009) raised throughput by about 10% for motion-impaired participants and not for typically developed ones (secondary report of a 16-person study).  The settings list has dozens of sliders, and people with tremor should not have to tune them.

**Reuse.**  None; the Angle Mouse is a published algorithm (software from the University of Washington; licence not checked).

### 24. Mouth-sound triggers
**Impact Medium to High, effort Medium.  Touches speech.**

**Change.**  A small detector on the microphone stream, independent of the recogniser, for a lip pop, a tongue click and a held "sss", calibrated from ten examples of each.  Map them to click, scroll start and stop, or mode toggles.  Gate it while speech is in progress.

**Why.**  Talon ships "pop" and "hiss" noise commands (vendor), and Hammerspoon has a low-latency lip-pop and "sss" recogniser (macOS, by Tristan Hume).  A pop can be detected within a few tens of milliseconds (my estimate), against 250 ms of endpoint plus recogniser time for the word "click", and needs no eye closure.  The Vocal Joystick (Bilmes et al., 2005) used discrete sounds such as [k] and [ch] for clicks and vowel quality for direction (paper).  Not everyone can make these sounds (for example with bulbar ALS), so it is an option, not a default.

**Reuse.**  I found nothing permissively licensed in Python.  Hammerspoon is MIT but its detector licence is not stated on its documentation page; read `detectors.c` for ideas rather than copying.  Write a spectral-flux onset detector with a small per-user classifier.

### 25. A fallback ladder ending in switch scanning
**Impact Medium to High, effort Medium to Large.  Touches vision, UI, desktop.**

**Change.**  A tracker-health meter that explains problems in plain words (glare, face too far, ptosis, camera lost) and steps down automatically: gaze, head, voice with numbers and grid, then single-switch scanning of the dock, numbers and keyboard.  The switch can be a blink, a face gesture, a pop, or any key or USB switch.  Scanning groups numbers by row and column.

**Why.**  Some users will never get a usable gaze estimate (nystagmus, strong glasses glare, ptosis, involuntary movement), and others will lose it late in the disease.  Tobii lists Switch-Based Mouse Emulation beside its gaze modes (vendor), and the Windows OSK has a scanning mode that `accessibility.py` already names.  It also turns a dead end into a graceful failure.

**Reuse.**  None; the OSK scanning mode is a stop-gap.

### 26. Own large-key keyboard with prediction (later)
**Impact Medium to High, effort Large.  Touches UI, desktop, speech.**

**Change.**  A Sightline keyboard with large keys, word prediction in a fixed place, cascading dwell (shorter on likely keys), optional voice or face-gesture confirm, a numbered-key mode and a password mode.  Offer dwell-free decoding only as an optional extra, with letter locking.

**Why.**  Cascading dwell reached 12.39 words a minute against 10.62 for fixed dwell with 17 non-disabled users, and 9.51 with 5 people with ALS (Mott et al., CHI 2017).  Dwell-free typing reached 46 words a minute in simulation (Kristensson and Vertanen, ETRA 2012) and 15.95 for real users after 100 minutes (Filteryedping, TACCESS 2015, abstract).  In a deployment with real AAC users the barriers were noisy gaze, error correction and a lack of transparency (IUI 2023), so with webcam error start with dwell and prediction.  Read Microsoft's dynamic-dwell patents before copying cascading dwell.

**Reuse.**  OptiKey (GPL-3.0) and Dasher (GPL) can only be run as separate programs.  An MIT web version of Dasher exists under the Ace Centre's dasher-project.  Neither can be copied in.

---

## 4. The three changes I would make first

1. **Item 1: aim voice commands at where the user looked when they began to speak.**  It is a few days of work, it fixes the most likely wrong-target error in the voice-plus-gaze path, it has measured evidence behind it, and it is cheapest to add now, before the speech service exists.  It also makes the deictic commands ("select this word") possible.
2. **Item 2: only act on speech that is for the computer.**  The Midas-touch problem for voice is an open microphone in a house with other people.  The head-direction gate costs almost nothing because the signal exists, the graded-trust idea avoids hard muting, and it protects the one thing that decides whether people keep using the product: trust.
3. **Item 10: ambiguity-aware targeting with a local number chooser.**  Webcam error of 60 to 230 px against 24 to 40 px controls is the central problem, and gaze-only users, who may have little or no head control, have no head fine-tune to fall back on.  It reuses the existing snapshot, labels and speech-number machinery and turns "nearest within 48 px" into something that knows when it is unsure.

Two things to start in parallel because they are mostly waiting, not building: the packaging spike and SignPath application (as soon as a first release exists) for item 12, and recruiting pilot users for item 17.

## 5. Smaller points

* **Dictation endpointing.**  Pipecat's Smart Turn v3 is an open end-of-turn model (BSD 2-Clause, about 8 million parameters, 12 ms on a modern CPU, vendor figures).  It could shorten the 650 ms dictation endpoint when a sentence is complete and lengthen it when it is not, which also helps slow speakers.  Impact Low to Medium, effort Small.
* **Browser and non-accessible apps.**  From Chrome 138 (2025) Chromium browsers use native UI Automation by default, and Microsoft says Voice Access now works across Chromium browsers (Chrome blog).  Retest labels and "click <name>" on Chrome 138 or later before building a browser helper (Rango is MIT, if you ever need one) or vision-based target proposals.  The `UiAutomationProviderEnabled` policy, supported through Chrome 146, lets you compare old and new behaviour.
* **Local alarm.**  An optional loud "help" sound and flashing screen, clearly described as not a medical alarm.
* **Localisation.**  The vocabulary and models are English; plan the alphabet and formatter words per language.
* **Voice Access as a companion.**  Do not try to replace Windows Voice Access where it already works (sign-in, set-up screens); document it as the fallback and test the two together.

## 6. Sources

* Kaur et al., "Where is 'it'? Event synchronization in gaze-speech input systems", ICMI 2003: https://digitalcommons.njit.edu/fac_pubs/14266
* Zhai, Morimoto, Ihde, MAGIC pointing (slides): https://www.cs.princeton.edu/courses/archive/fall08/cos436/Zhai/magic.pdf
* Spakov, Isokoski, Majaranta, "Look and Lean", ETRA 2014: https://homepages.tuni.fi/oleg.spakov/publications/Spakov_ETRA_14a.pdf
* Hou et al., "GazeSwitch", PACM HCI 2024: https://dro.deakin.edu.au/articles/journal_contribution/GazeSwitch_Automatic_Eye-Head_Mode_Switching_for_Optimised_Hands-Free_Pointing/26779108
* Lutteroth, Penkar, Weber, "Gaze vs. Mouse" (Actigaze), UIST 2015: https://www.cs.auckland.ac.nz/~lutteroth/publications/LutterothPenkarWeber-Actigaze2015.pdf
* Schwarz et al., "A framework for robust and flexible handling of inputs with uncertainty", UIST 2010: https://uist.acm.org/archive/html/keywords/kwhandling.html
* Bi and Zhai, "Bayesian Touch", UIST 2013: https://research.google/pubs/bayesian-touch-a-statistic-criterion-of-target-selection-with-finger-touch/
* Grossman and Balakrishnan, bubble cursor, CHI 2005: https://www.dgp.toronto.edu/papers/tgrossman_CHI2005.pdf
* Skovsgaard et al., "Small-Target Selection with Gaze Alone", ETRA 2010: https://research.wright.edu/en/publications/small-target-selection-with-gaze-alone/
* McGuffin and Balakrishnan, "Fitts' law and expanding targets", TOCHI 2005: https://www.dgp.toronto.edu/~ravin/papers/tochi2005_expandingtargets.pdf
* Miniotas et al. 2006 (spoken colour chooser) as reported in Spakov et al., "Look and Lean" (same link as above).
* Kumar, Paepcke, Winograd, EyePoint (CHI 2007) and gaze-enhanced scrolling (UIST 2007): https://hci.stanford.edu/publications/paper.php?id=76 and https://hci.stanford.edu/publications/paper.php?id=75
* Hyrskykari, Istance, Vickers, "Gaze gestures or dwell-based interaction?", ETRA 2012 (programme page; abstract only): https://gaze.ime.usp.br/etra2012/program.html
* Huckauf and Urbina, pEYEs, ETRA 2008: https://www.doi.org/10.1145/1743666.1743689
* Lattice Menu, CHI 2022 (arXiv 2025): https://arxiv.org/html/2511.21131
* Mott et al., cascading dwell, CHI 2017: https://faculty.washington.edu/wobbrock/pubs/chi-17.02.pdf
* Kristensson and Vertanen, dwell-free eye-typing, ETRA 2012: https://keithv.com/pub/dwellfree
* Kristensson, Mjelde, Vertanen, adoption barriers to dwell-free eye-typing, IUI 2023: https://www.pokristensson.com/pubs/KristenssonEtAlIUI2023.pdf
* Pedrosa et al., Filteryedping, TACCESS 2015: https://unpaywall.org/10.1145%2F2724728
* Wobbrock et al., Ability-Based Design, TACCESS 2011: https://www.eecs.harvard.edu/~kgajos/papers/2011/wobbrock11abd.shtml
* Angle Mouse project page: https://depts.washington.edu/acelab/proj/angle/index.html
* Bilmes et al., The Vocal Joystick, HLT/EMNLP 2005: https://www.microsoft.com/en-us/research/wp-content/uploads/2005/10/hlt2005.pdf
* Grauman et al., blink and eyebrow communication: https://www.cs.utexas.edu/~grauman/research/projects/blink/blinkdetection.html
* Beukelman, Fager, Nordness, ALS communication support, 2011: https://pmc.ncbi.nlm.nih.gov/articles/PMC3096454/
* Speech Accessibility Project results and 2025 challenge: https://speechaccessibilityproject.beckman.illinois.edu/article/2025/06/16/competitors-slash-word-error-rate-in-speech-accessibility-project-s-first-technical-competition
* Project Euphonia personalisation: https://research.google/blog/project-euphonias-personalized-speech-recognition-for-non-standard-speech/
* WebEyeTrack, August 2025: https://arxiv.org/abs/2508.19544
* Google Look and Talk: https://9to5google.com/2022/07/27/google-assistant-look-and-talk/
* Project GameFace repository (archived 5 September 2025): https://github.com/google/project-gameface
* Windows Voice Access command list: https://support.microsoft.com/en-US/Accessibility/windows/voice-access/voice-access-command-list
* Voice Access at set-up and sign-in: https://support.microsoft.com/en-US/accessibility/windows/voice-access/use-voice-access-to-set-up-and-log-in-to-your-pc
* Security considerations for assistive technologies (UIAccess): https://learn.microsoft.com/en-gb/windows/win32/winauto/uiauto-securityoverview
* Tobii Dynavox on UAC and gaze: https://www.tobiidynavox.com/blogs/support-articles/there-is-a-window-asking-me-if-i-want-to-allow-a-program-to-make-changes-to-my-computer
* Tobii Dynavox Gaze Selection versus Mouse Emulation: https://us.tobiidynavox.com/blogs/support-articles/what-are-some-of-the-main-differences-between-gaze-interaction-and-mouse-emulation
* Tobii Dynavox Windows Control modes (including switch-based): https://www.tobiidynavox.com/blogs/support-articles/what-is-tobii-dynavox-windows-control
* Windows Eye Control (supported hardware, laptop note): https://support.microsoft.com/windows/get-started-with-eye-control-in-windows-1a170a20-1083-2452-8f42-17a7d4fe89a9
* Multi-app camera setting in Windows 11: https://www.elevenforum.com/t/enable-or-disable-multiple-apps-to-use-camera-in-windows-11.31199/
* SignPath Foundation: https://signpath.org/terms
* Chrome native UI Automation: https://developer.chrome.com/blog/windows-uia-support-update
* Pipecat Smart Turn v3: https://www.daily.co/blog/announcing-smart-turn-v3-with-cpu-inference-in-just-12ms/
* Talon documentation (noise recognition): https://talonvoice.com/docs/
* Hammerspoon `hs.noises`: https://www.hammerspoon.org/docs/hs.noises.html
* OptiKey (GPL-3.0): https://github.com/OptiKey/OptiKey
* Dasher licensing and the dasher-project: https://github.com/dasher-project
* Rango (MIT): https://github.com/david-tejada/rango
* Webcam gaze accuracy by screen region (vendor blog): https://realeye.io/blog/post/optimizing-realeye-calibration-point-count-and-accuracy-on-computers
* Apple Eye Tracking (iOS 18) press coverage: https://appleinsider.com/articles/24/06/13/eye-tracking-lets-you-navigate-ios-18-without-touching-your-iphone
