# Vision subsystem

Webcam eye and head tracking: the pointer, blink, wink and face gesture
clicks, dwell, the resume zone, the reading hold and reading scroll,
calibration, "fix pointer", and the facts speech asks about (where the user
was aiming, whether they were facing the screen).  Entry point:
`sightline.vision.create(ctx)` returns a `VisionService`, which satisfies
`VisionAPI` and, through `.calibration`, `CalibrationAPI`.  Construction
registers the face model and the `pointer.*` and `dwell.*` actions and
opens nothing; `start()` opens the camera.

## Files

| File | What it does |
|---|---|
| `service.py` | `VisionService`: threads, camera choice, actions, `aim_point`, `attention`, failure handling and `pointer.restart` |
| `pipeline.py` | `VisionPipeline`: everything done per camera frame, from a `FaceFrame` to events and actions |
| `camera.py` | Camera list (Media Foundation and DirectShow merged, virtual cameras labelled), `OpenCVCamera`, `VideoFileSource`, `ArraySource`, `FrameHealth`, `CameraSupervisor` |
| `landmarks.py` | MediaPipe Face Landmarker wrappers (`FaceLandmarker` in VIDEO mode, `ImageLandmarker` for photos), landmark indices, `FaceFrame` |
| `features.py` | Per-frame measurements: eye-frame iris position, lids, eye aspect ratio, head pose, the EyeTrax landmark block, distance |
| `eyes.py` | Eye closure (blendshape and eye aspect ratio fusion), the gesture state machine, eye calibration fit |
| `filters.py` | One Euro filter, gaze smoothing, fixation detection, noise estimate |
| `gaze_model.py` | Ridge regression gaze model, cross-validation, outlier rejection, drift correction, `.npz` storage |
| `head.py` | Head pointer: nose bridge motion, deadband, speed-dependent gain, head calibration fit |
| `pointer.py` | `PointerEngine` (where the pointer should be, its history, `rest_point`, `aim_point`, `take_intent`) and `CursorPump` (moves the cursor at 120 Hz) |
| `accuracy.py` | `AccuracyMap` (expected gaze error across each screen), the default calibration dots, the comfortable control size |
| `face_gestures.py` | Face gestures from blendshapes: detector, profile and the trainer's fit |
| `reading.py` | `ReadingDetector` (hold the pointer while reading) and `ReadingScroll` |
| `dwell.py` | Dwell clicking and the resume zone |
| `calibration.py` | `CalibrationController`, the `CalibrationAPI` implementation |
| `learning.py` | `ClickLearner`: learning from clicks with guards |
| `attention.py` | `AttentionTracker`: face, facing and mouth movement per frame for `attention(t0, t1)` |
| `recorder.py` | Recording and loading face data (`.npz` chunks plus `meta.json`) |
| `profile.py` | Where a profile's gaze model, eye, head and face gesture profiles live |
| `models.py` | The face model's `ModelSpec` |
| `events.py` | `VisionDiagnostics`; re-exports `GazeModelChanged` and `CalibrationActive`, which now live in `core/events.py` |
| `config.py` | `VisionConfig` |

Tools: `tools/vision_record.py` and `tools/vision_replay.py` (see "Record
and replay").

## Threads

| Thread | Work |
|---|---|
| `sightline-camera` | Inside `OpenCVCamera`: grabs frames and keeps only the newest, so processing never falls behind |
| `sightline-vision` | Reads the newest frame, runs MediaPipe (about 11 ms at 720p) and the pipeline (under 1 ms), publishes events |
| `sightline-cursor` | `CursorPump`: eases the cursor towards the engine's target 120 times a second |
| `sightline-gaze-refit` | Short-lived: refits the gaze model after clicks, off the vision thread |

`aim_point`, `attention`, the calibration API and the actions are safe from
any thread and never wait on the vision thread for more than a few
milliseconds.

## Per frame

`VisionPipeline.process(face, t)` runs once per camera frame, with `face`
None when no face was found.  In order: measure the face; estimate each
eye's closure; run the gesture state machine; track the head; record
attention; predict and smooth the gaze; judge whether the user is
reading; move the pointer target; run eye gestures, face gestures, dwell,
the resume zone and the reading scroll; hand the frame to an active
calibration; remember it for click learning; publish `PointerSample`
(with the accuracy map's `error_px` and the engine's `steered`) and `EyeState` every frame, `FaceStatus` about three times a second, `CameraFrame` about
15 times a second while the preview is on (also without a face, since the
setup wizard needs the picture most then), and `VisionDiagnostics` twice a
second while anyone listens.

## Camera

* Cameras are listed with `cv2-enumerate-cameras` for both Media
  Foundation and DirectShow and merged by device path (the part before the
  interface GUID, which differs between the two), then by name when a name
  is unique.  Physical cameras come first; software cameras (names such as
  OBS, Quest, Snap Camera, ManyCam or XSplit, or DirectShow-only devices
  without a hardware path) are labelled "(virtual camera)".  The saved camera is
  matched by name because indices change when devices come and go.
* Opening tries Media Foundation first, then DirectShow, asks for MJPG at
  the configured size and rate, and waits up to 3 s for a first picture.
* `CameraSupervisor` reopens a failed camera with backoff (1, 2, 4, 8, 10 s)
  and sets the hints "Camera not found" or "Another app is using the
  camera".  `FrameHealth` turns a black picture (1.5 s) or a picture that
  never changes (3 s, an idle virtual camera) into a hint.
* Distance comes from the iris: its diameter is about 11.7 mm, and the
  camera's horizontal field of view (setting, 62 degrees by default) gives
  the focal length.  `FaceStatus` hints: "Move closer" over 90 cm, "Move
  back a little" under 35 cm, "Face the screen" beyond 25 degrees from
  neutral, "More light would help" when the face is darker than 60 of 255.
* `VideoFileSource` and `ArraySource` sit behind the same interface for
  tests, demos and replays.

## Face model and left and right

MediaPipe Face Landmarker (float16, version 1) in VIDEO mode, one face,
blendshapes and the transformation matrix on.  The model file is read into
memory first because MediaPipe cannot open paths with non-ASCII characters
on Windows.  Photos in `tests/vision/data` settle the conventions:
`eyeBlinkLeft` and landmarks 263/362 are the subject's own left eye, which
is on the image's right in an unmirrored camera; flipping the image swaps
them.  The "My camera shows a mirror image" setting swaps them back, and
eye and head calibration detect a mirrored camera and set it.

## Gaze model

Features (`features.py`): iris position in each eye's own frame (along the
corner line, in eye widths, so roll and distance cancel), lid positions,
eye aspect ratio, the `eyeLook*` blendshapes, head yaw, pitch and roll, and
face position and scale; plus EyeTrax's head-normalised landmark block
(84 values).  Four feature sets of growing size compete: `basic`, `head`,
`per_eye` and `landmarks`, each only when there are enough targets for it.

Fit (`gaze_model.py`): standardise, then weighted ridge regression solved
in closed form through one eigendecomposition per feature set.  The
penalty for each screen axis is chosen by leave-one-target-out
cross-validation over 33 values, because the frames of one fixation are
near copies and only held-out targets measure generalisation.  The
simplest feature set within 3% of the best held-out error wins.  Samples
far from their target's consensus are dropped before the final fit.
Accuracy is the held-out error, reported in pixels and in degrees using
the monitor's physical size (`Monitor.size_mm`) and the measured distance.
A result worse than 6 degrees, or a quarter of the screen width, is
refused.  Models are saved per profile as `gaze_model.npz` plus
`gaze_model.json`, never pickle, and are not used if the monitors they were
calibrated on change.

## Accuracy map

The gaze fit holds each calibration dot out in turn, so it knows how far
off the model is at each dot (`GazeModel.per_target`, with the dots'
positions in `GazeModel.targets`).  `AccuracyMap` (`accuracy.py`)
interpolates those errors across each calibrated monitor by inverse
distance weighting, softened by a tenth of the monitor's diagonal so it
varies smoothly and never goes beyond the measured values, with a floor of
10 px.  A monitor without calibration dots has no map.  The pipeline
rebuilds the map whenever the model changes and publishes the error at the
pointer as `PointerSample.error_px`: None before calibration, in head mode
and off the calibrated monitors.  Snapping, the number chooser, target
sizes and the reading scroll's hysteresis use it instead of fixed numbers.

`default_gaze_targets(rect)` is the dot layout for one monitor (17 dots):
the middle, a ring a quarter of the way in, then the corners and edge
midpoints a little in from the border, because the Start button, the tray
and close buttons live there and webcam gaze is least accurate there.

Gaze and validate reports carry `details["target_points"]` (each dot's
position, in the order of `per_point_error_px`) and a comfortable control
size: `details["comfort_px"]`, `details["comfort_mm"]` and
`details["comfort"]`, a sentence for the wizard such as "At your accuracy,
controls smaller than about 2 cm are hard to hit with your eyes alone.
Setting Windows text and app size to 150 percent will help."  The size is
twice the mean error, shown in half centimetres (whole ones above 5 cm),
and the advice follows the monitor's current scale.

## Learning from clicks

Every click that lands near where the gaze was resting becomes a training
sample (`learning.py`).  The fixation used is the one before the eyes
closed (eye clicks), the dwell's (dwell clicks), or the one at the speech
onset, `ClickPerformed.aim_t`, for spoken clicks.  A click counts only if
the fixation was within 150 to 260 px of it (2.5 times the model's error,
bounded).  Click samples weigh 8 calibration frames, decay by 0.985 per
new click and are capped at 300.  After every 4 accepted clicks (at most
every 15 s) a background refit runs and is kept only if its held-out
calibration error is no worse than before (5% tolerance) and a model
trained without the newest clicks predicts them better than the current
one.  A posture change contradicts the calibration, so the full refit is
usually refused then; a separate affine drift correction on top of the
model follows the user instead, kept only if it beats no correction by 10%
in leave-one-click-out tests and stays within 300 px of shift and 40% of
scale.  On synthetic drift of 70 px this brings the error back to about
13 px, and misleading clicks are rejected.

Every click's distance from the fixation is also remembered, accepted or
not.  When the median of the last five (within ten minutes, each capped at
400 px) is more than 1.6 times the model's error and more than 50 px above
it, the Notice 'The pointer seems to have drifted. Say "fix pointer" to
correct it.' appears (key `vision-drift`), at most every 15 minutes.  A
"fix pointer" clears the record.  When a refit has too few clicks for a
correction of its own, the correction in use (a "fix pointer" shift, say)
is kept; a refit result that started before a calibration or a "fix
pointer" is dropped.

## Smoothing and fixations

A median of three removes single-frame spikes; a One Euro filter (minimum
cutoff 0.8 Hz, beta 0.006) smooths the rest.  The noise level is estimated
continuously from lag-3 differences (inter-quartile range), so it does not
depend on detecting fixations first.  Inside a fixation the estimate is
held within an adaptive radius (28 to 90 px, 2.5 times the noise) and
moves only by a small fraction per frame; a jump of more than 140 px ends
the fixation at once (saccade).  Smoothing follows the "Pointer smoothing"
setting.

## Pointer

Modes (`pointer.mode`): hybrid (default), head and gaze.  Before gaze
calibration the pointer is head-only whatever the mode.

* **Hybrid.**  When the user fixates further than the jump distance
  (180 px) from the pointer for 130 ms, the pointer jumps there with an
  80 ms eased animation.  The fixation becomes an anchor: while the eyes
  stay on that spot it will not jump back, so the head can steer the
  pointer away from an estimate that is a little off.  For 200 ms after a
  jump (`PointerConfig.head_quiet_s`) head movement is ignored, because
  people turn the head part of the way during a large eye movement and that
  turn would push the pointer past its target.  Inside the jump distance,
  head movement fine-tunes at 0.35 of head mode's gain.
* **Head.**  Relative, like a head mouse.  The signal is the nose bridge's
  image position divided by the outer eye corner distance.  A deadband
  (the "Ignore small head movements" setting) stops tremor completely, and
  a sigmoid gain curve (after GameFace) makes slow movements precise and
  fast ones cover the screen.
* **Gaze.**  The pointer follows the smoothed gaze.
* **Reading hold** (on by default, "Hold the pointer still while I read").
  `ReadingDetector` follows the smoothed gaze: resampled every 0.1 s over
  the last 1.2 s, a run of 110 to 1100 px along a line (70% of steps the
  same way, at most 45 px from a straight line, not mostly vertical), with
  the head still (under 0.06 face units a second), for 0.8 s, means
  reading; a jump back of 220 px or more to the next line confirms it.
  While reading the pointer holds still in hybrid and gaze modes, so
  tooltips, hover menus and video controls are not set off.  A deliberate
  head movement (over 0.12) releases it at once, as does a fixation of
  0.5 s (the user is looking at something to click) or 2 s without
  line-like movement.
* **Jump only when I move my head, click or speak** (`warp_on_intent`,
  off by default; MAGIC's conservative variant).  A far fixation only
  becomes a candidate; the pointer jumps there on the first head movement
  past the deadband (`intent_head_px`, 0.5 px in a frame), or when a click
  gesture or a spoken command is aimed at a moment the user was looking
  there (`take_intent`, `aim_point`).
* **Steered** (`PointerSample.steered`).  Once the head has moved the
  pointer 2 px (`PointerConfig.steer_px`) since gaze last placed it, the
  pointer is where the user put it rather than where gaze guessed, and the
  UI's number chooser lets clicks there through.  A nudge counts, and so
  does the cursor being moved by a person or another Sightline action
  (`adopt`, over a pixel, so the cursor's own rounding after a pause does
  not).  The next jump, or following the gaze in gaze mode, clears it;
  freezing keeps it.  Noise inside the deadband, the quiet time after a jump
  and a head pushing against a screen edge move nothing, so they do not set
  it.
* The pointer freezes while the face is lost, while the lids move, and for
  0.3 s after the face returns.  It stays on the desktop.
* `CursorPump` moves the cursor with a critically damped spring (no
  overshoot): fast for jumps, gentler otherwise.  If the cursor moves in a
  way Sightline did not cause (`InputInjector.last_injected()` tells them
  apart), a person is using the mouse: eye control waits until 1.5 s after
  the last movement, then continues from where the mouse left the cursor.
  Gestures and dwell are ignored meanwhile.  When another Sightline action
  moves the cursor (a spoken click at its aim point, a click that snapped,
  the grid), its press follows 12 to 40 ms later, so the pump keeps still
  for 150 ms (`hold_s`) and then carries on from where that action left the
  cursor instead of easing back.  Input that does not move the cursor
  (wheel turns of a running scroll, button presses) does not hold it, or
  the pointer would freeze for as long as a scroll runs.
* `pointer.recenter` makes the current head pose neutral; in head mode it
  also brings the pointer to the middle of its screen.  `pointer.nudge`
  moves a few pixels.  Pausing (`state.pointer_paused`) leaves the cursor
  alone.

## Aim point for spoken commands

People look at a target before they speak, and by the time "click" is
recognised the pointer may have moved on.  `aim_point(t)` answers with the
point where the pointer target came to rest at or just after `t` (speech
sets `Action.aim_t` to the speech onset).  "At rest" means it moved less
than 12 px over the previous 100 ms (`rest_radius_px`, `rest_s`).  If the
pointer was moving at `t` (the head steering), the answer is the first
resting point after it; if it has not rested since, the current target.
The engine keeps 3 s of targets.  None when paused, yielding to a real
mouse, not running, inactive (face lost, calibrating), or when `t` is
older than the history, so callers fall back to the cursor.

With "Jump only when I move my head, click or speak" on, a spoken command
is also the sign of intent: if at `t` the user was looking somewhere the
pointer would have jumped to, it jumps there now and that is the answer.
If it already went there since `t` (a head movement, or an earlier command
with the same aim), the answer is where it came to rest after that jump.

## Eye gestures

Closure per eye (0 open to 1 shut) fuses the `eyeBlink` blendshape with
the eye aspect ratio relative to that eye's own open level (the 80th
percentile of recent open frames), because a smiling wink scored only 0.22
to 0.55 on its blendshape in the test photos.  Eye calibration replaces the
fixed fusion with a per-eye logistic model.

| Gesture | Rule | Default action |
|---|---|---|
| Long blink | Both eyes closed 350 ms or more, then opened | Click (follows the click type) |
| Hold blink | Both closed 1.0 s or more | Double click |
| Right wink | Right eye closed 180 ms to 2.5 s while the left stays clearly open | Right click |
| Left wink | Left eye closed while the right stays clearly open | Middle click |
| Cancel | Both closed over 2.5 s, or one eye still shut 0.5 s after a two-eye hold | Nothing |

Clicks happen when the eyes open, at the pointer position from 120 ms
before the lids started to move (the pointer freezes as soon as they
move).  Both eyes must have been open for 150 ms before a gesture starts.
Natural blinks (typically 100 to 300 ms) never click, and their lengths
are kept for diagnostics and the replay tool.  `EyeHold` stage 1 and 2
events let the UI tick while the eyes are shut.  Gestures are ignored while
paused (except an action bound to `pointer.resume` or `pointer.toggle`),
calibrating, or yielding.  `gesture_actions` maps each gesture to `click`,
`double_click`, `right_click`, `middle_click`, `drag`, `none` or any action
name.  Eye calibration turns off winks for an eye whose winks cannot be
told apart from blinks, and says so.

## Face gestures

Raised eyebrows, a smile, an open mouth and puffed cheeks, made with the
eyes open, from the blendshapes the face model already computes
(`face_gestures.py`).  Signals: brows are the mean of `browInnerUp` and
`browOuterUp*` less the upward look (`eyeLookUp*`) times a factor fitted
per user, because looking up raises the brows a little on its own; smile
is the mean of `mouthSmile*`; open mouth is `jawOpen`; puffed cheeks is
`cheekPuff`.

A gesture counts on a rising edge: the signal comes up from below its
"off" level, passes "on" and stays above "off" for 150 ms (`face_hold_ms`,
80 to 600).  It counts once however long it is held.  After release there
is a 0.8 s refractory period, and a gesture begun inside it never counts
(rather than counting late at a moment the user did not choose).  The
mouth gestures do not count while the user is talking: mouth movement in
at least a quarter of the frames of the second before the gesture began
(`AttentionTracker`, no audio).  The click goes to the pointer position
from the moment the signal began to rise, and is dispatched with source
"eye" after a `FaceGesture(t, kind, x, y)` event.  Face gestures are
ignored while calibrating, yielding or while the eyes are closing, and
while paused unless bound to `pointer.resume` or `pointer.toggle`.

Every face gesture is off until the trainer has run and the user has given
it an action: the default `gesture_actions` map them to `none`, and a
gesture the trainer did not find reliable never fires whatever its action.

**Trainer** (calibration kind `gestures`).  The wizard labels periods with
`set_eye_label`: `neutral`, then each of `brow_raise`, `smile`,
`mouth_open` and `cheek_puff`, then about 30 s each of `talking` and
`reading`.  Frames count from 0.6 s after each label.  For each gesture:
"on" is the resting face's 95th percentile plus half the gap to the
gesture's median, "off" the same plus a fifth (the gap must be at least
0.06); separation is the chance a gesture frame reads above a resting
frame; "held" is the share of gesture frames above "on".  The detectors
are then replayed over the talking and reading periods to count mistaken
triggers a minute (blinks and winks too).  A gesture is offered when
separation is at least 0.9, held at least 0.6 and mistaken triggers at
most one a minute.  The report's details hold `scores` (only the gestures
good enough to offer, name to score from 0 to 1, higher is better),
`offer` (the best three), `suggested_actions` (click, right click, double
click), `triggers` (all numbers per gesture), `eye_false_per_min`,
`look_k` and `mouth_gate_ok` (whether the mouth moved enough while talking
for the speech gate to work).  The message is plain, for example "These
work well for you: a smile and raised eyebrows." or "Raised eyebrows went
off about 11 times a minute while you talked or read, so it is not
offered."  The profile is saved as `face_gestures.json`.

## Dwell and the resume zone

Dwell: holding the pointer within 40 px for 0.9 s clicks with the current
click type (`dwell.click_type`; drag presses then releases through
`mouse.drag_toggle`).  The pointer must leave the radius before the next
dwell can start.  `DwellProgress` drives the ring.

Resume zone: while paused, looking at the top middle of the primary screen
(or a top corner, or off) for 1.5 s dispatches `pointer.resume`, with
`GazeZoneProgress` for the indicator.  Before gaze calibration the head
direction stands in for the gaze.

## Attention

`attention(t0, t1)` gives the fractions of frames in the window with a
face, with the head within 25 degrees of the user's neutral pose, and with
the mouth moving, plus the frame count.  Mouth moving means `jawOpen`,
`mouthClose` or the inner lip gap (each a median of three frames) changed
by more than 0.06, 0.06 or 0.03 eye-corner distances compared with about
100 ms earlier.  On synthetic data a still face with the face model's
usual jitter reads under 10% moving and talking at four syllables a second
over 50%.  Mouth is None when the face model gives no blendshapes.  The
ring keeps 15 s and survives a pipeline rebuild.  None when vision is not
running.

## Calibration

`CalibrationAPI` kinds: `gaze` (still or moving dots), `validate` (measure
only), `eyes` (labels open, closed, wink_left, wink_right), `head` (turn
towards dots), `gestures` (the face gesture trainer, see "Face gestures")
and `drift` ("fix pointer", below).  The wizard only says what is on screen; frames are
assigned to targets by capture time at `finish()`, after removing 40 ms of
camera delay, so frames in flight when the dot moved are not mislabelled.
Still dots drop the first 100 ms of collection; moving dots drop the first
350 ms, and the eye's lag behind the dot (0 to 200 ms) is the one whose
quick fit is closest.
Frames with either eye closing are dropped.  `finish()` fits, saves to the
profile, activates and updates `gaze_calibrated` or `eyes_calibrated`.
Reports are short plain English, for example "Average error 41 px (about
1.1 degrees)."; eye calibration says when winks could not be told apart
from blinks and were turned off.  The profile is loaded at start.

**Fix pointer** (kind `drift`).  The wizard shows one dot (or a few) for
about 1.5 s.  For each dot, the median gaze estimate from 0.3 s after it
appeared (at least 8 frames with the eyes open) gives an offset; their
mean shifts the current model's output (`gaze_model.add_offset`, the
correction's constant column), with no refit, so it takes a few
milliseconds.  The model is saved and `GazeModelChanged(source="drift")`
is published.  Messages: "Pointer corrected by 65 px.", "The pointer was
already on target." (under 8 px), "Not enough data. Look at the dot until
it disappears, then try again.", "Calibrate eye pointing first.", and,
for an offset over 400 px or a quarter of the screen width, which is not
drift but a model that no longer fits, "The pointer is too far off to fix
this way. Please calibrate again."  The report's `mean_error_px` is the
size of the shift and `details["offset"]` its direction.

## Failures

A failure inside the pipeline costs one frame: it is logged (at most every
10 s), gestures, filters, dwell and the resume zone are reset, and the next
frame carries on.  After 20 failures in 10 s the pipeline is rebuilt from
scratch (profile reloaded) with the Notice "Eye tracking hit a problem and
restarted.", at most once every 30 s.  The face model is restarted after
20 failures of its own.  `pointer.restart` ("restart tracking") rebuilds
the pipeline and the vision thread on request.  Each run of the vision
thread has its own stop flag, so a thread that was slow to stop can never
resume alongside its replacement.

## Actions

| Action | Arguments | Undo |
|---|---|---|
| `pointer.pause` / `.resume` / `.toggle` | | not recorded |
| `pointer.mode` | `mode` in hybrid/head/gaze (also saved in settings) | previous mode |
| `pointer.recenter` | | not recorded |
| `pointer.nudge` | `direction` (up, down, left, right, up_left, ...), `amount=10` | nudge back |
| `pointer.restart` | | not recorded |
| `pointer.reading_scroll` | `on` (true/false or "on"/"off"; toggles when left out) | not recorded |
| `dwell.on` / `.off` / `.toggle` | | previous state |
| `dwell.click_type` | `type` in left/right/middle/double/drag | previous type |

Eye and face gestures and dwell dispatch `mouse.click` (with `button`,
`count`, `x`, `y`, source "eye" or "dwell") and `mouse.drag_toggle`.  The
reading scroll dispatches `mouse.scroll_start` (`direction`, `speed`) and
`mouse.scroll_stop`, source "eye".

## Reading scroll

`pointer.reading_scroll` turns the mode on or off (Notice "Reading scroll
on." or "Reading scroll off.", key `vision-reading-scroll`); it needs a
gaze calibration ("Reading scroll needs eye pointing to be calibrated
first").  While on, looking in the bottom fifth of the window under the
pointer scrolls down, faster the deeper the gaze (speed 0.25 at the zone
edge to 1.2 at the window edge, sent again when it changes by 0.15, at
most every 0.3 s); the top fifth scrolls up; the middle stops.  The zone
edges have hysteresis sized from `error_px` (clamped to 30 px up to half a
band): scrolling starts a quarter of that inside the zone after 0.35 s
there, so a glance does nothing, and stops three quarters of it outside.
Blinks are bridged; 0.4 s without a gaze estimate stops it, as do looking
outside the window, pausing, calibrating and a real mouse.  The window
comes from `desktop.windows.window_at()` at the pointer (one lookup, not a
list of every window: the app window under it, the owning window for a
dialog, Sightline's own windows looked through), asked at most every
0.25 s and about once a second while the pointer stays inside it, with the
monitor's work area when there is none or the lookup fails.  The mode
survives a pipeline rebuild.

## Defaults and why

| Setting | Default | Why |
|---|---|---|
| `blink_click_ms` | 350 | Natural blinks last about 100 to 300 ms |
| `blink_double_ms` | 1000 | Far enough from a click that a slow click is not a double |
| `blink_cancel_ms` | 2500 | Resting the eyes never clicks |
| `wink_min_ms` | 180 | Shorter one-eyed closures are mostly asymmetric blinks |
| `warp_distance_px` | 180 | About 4 to 5 degrees at 60 cm: beyond typical webcam gaze error near the centre, so a jump means a new target |
| `warp_dwell_ms` | 130 | Fixations shorter than this are mostly in-flight; longer feels slow |
| `head_dead_zone` | 0.3 | Stops tremor-scale movement; lower for very small, steady movements |
| `smoothing` | 0.5 | Middle of the One Euro range; higher for jittery cameras |
| `yield_ms` | 1500 | Long enough for a helper to pause, short enough not to feel stuck |
| `dwell_ms`, `dwell_radius_px` | 900, 40 | Common dwell defaults; the radius is smaller than webcam error, so dwell suits head pointing best |
| `resume_ms` | 1500 | Long enough that a glance never resumes |
| `camera_fov_deg` | 62 | Typical laptop and USB webcams are 55 to 80 degrees |
| `reading_hold` | on | Following the eyes along lines of text sets off hover effects and is no use to anyone |
| `warp_on_intent` | off | Jumping at once is what most people expect; the conservative jump suits users bothered by the pointer moving |
| `face_hold_ms` | 150 | Within the 120 to 200 ms that separates a gesture from a passing expression |
| Face gestures | off | They misfire for some faces, so only the trainer can turn one on |

All of these are priors from the literature and from synthetic tests, not
measurements on real users (see "Known limits").

## Tuning on a real webcam

1. Record a guided session: `python tools/vision_record.py` (about 2.5
   minutes; add `--video` to keep the picture for debugging).  It prints a
   step at a time and plays the system beep at each step so the
   closed-eye steps can be followed.
2. Replay it: `python tools/vision_replay.py`.  The report shows, per step,
   how often the face was found, how closed each eye read, the pointer's
   spread, and which gestures and clicks happened against what was asked;
   then natural blink lengths, an eye calibration fitted from the labelled
   steps, and suggestions.
3. Try settings without touching the saved ones:
   `python tools/vision_replay.py --set blink_click_ms=450 --set head_dead_zone=0.4`.
   `--fresh` ignores the saved calibration; `--timeline` lists every event.
4. What to look for: natural blinks longer than about 85% of
   `blink_click_ms` (raise it); winks where the other eye also narrows
   (run eye calibration, or turn winks off); a face found in under 90% of
   frames (light, camera angle); fewer than 20 frames a second (another app
   holds the camera, or the room is dark); pointer spread while looking at
   the middle of more than the dwell radius (raise `smoothing` or
   `head_dead_zone`).

## Record and replay

`tools/vision_record.py` writes a folder in the recordings folder:
`meta.json` (camera, screen size, mirroring, steps and their start times)
and `frames-NNN.npz` (one per minute: times, landmarks, blendshapes,
matrix, picture size), plus `video.avi` with `--video`.  Only face numbers
are saved unless video is asked for, and nothing leaves the computer.
`--source FILE` records from a video file instead of a camera,
`--free SECONDS` records without steps, and `--list-cameras` lists cameras.

`tools/vision_replay.py` runs a recording (or `--video FILE` through the
real face model, optionally saved with `--save DIR`) through the same
pipeline the app uses, against a fake desktop: clicks are listed, never
performed.  It reads the current profile's calibration but never changes
it, and uses the monitors the calibration was made on when they are known.

## Tests

`.venv\Scripts\python.exe -m pytest tests/vision -q`.  Everything after
landmark extraction runs on a synthetic face (`tests/vision/vision_synth.py`)
built from the real landmarks of a photo and moved like a person would move
it (eye rotation from screen geometry, head pose about a neck pivot, lid
closure, noise, a mirrored camera).  Service tests run the real vision
thread with a camera made of test images and a fake face model
(`vision_fakes.py`).  The cursor pump and pointer only ever drive
`FakeInput`; no test moves the real cursor, opens a window or plays sound.

`@pytest.mark.models` tests use the downloaded face model on the photos in
`tests/vision/data` (sources and licences in `SOURCES.md`): output shapes,
which eye is which (also flipped and with the mirror setting), head turn
direction, the capture path from a video file to a blink click, and speed.
Measured under the shared `cpu` lock: Face Landmarker at 1280x720 in VIDEO
mode 10.8 ms a frame (p95 12.7 ms); calibration fit for 13 dots about
105 ms.  With the wave 2 features (accuracy map, reading hold, four face
gestures watched, reading scroll on with a dozen windows): pipeline
0.43 ms a frame head-only, 0.44 ms with a gaze model and 0.45 ms with
everything on (p95 0.65 to 0.68 ms); the face gesture trainer's fit over
70 s of frames 28 ms; "fix pointer" under 10 ms.

Wave 2 tests: `test_vision_accuracy.py` (map, dots, comfort sentence,
report details, `error_px`), `test_vision_face_gestures.py` (rising
edge, hold, refractory, talking gate, brow and upward look, the trainer
through the calibration API with synthetic expressions, the pipeline),
`test_vision_drift.py` ("fix pointer", stale refits, the drift Notice)
and `test_vision_reading.py` (reading detector, reading hold,
conservative jumping, the reading scroll and its one-window lookup, the
action).  `test_vision_pointer.py` also covers `steered`: set by the head
after a jump, a nudge or the mouse; not by noise, a screen edge or coming
back from a pause; cleared by the next jump.  `vision_sim.py`
drives a pipeline with the synthetic user, expressions and a talking
mouth on one clock.

## Reused work

| Source | Licence | Used for |
|---|---|---|
| [EyeTrax](https://github.com/ck-zhang/EyeTrax) | MIT | Feature recipe (head-normalised landmark block) and the ridge model recipe, adapted to closed form (notices in `features.py`, `gaze_model.py`) |
| [MediaPipe Face Landmarker](https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker) | Apache-2.0 | Landmarks, blendshapes, head pose (model downloaded at setup) |
| [Project GameFace](https://github.com/google/project-gameface) | Apache-2.0 | Sigmoid speed-dependent gain (notice in `head.py`) |
| [shaal/eye-tracker](https://github.com/shaal/eye-tracker) | MIT | Ideas only: eye-frame features, leave-one-target-out penalty choice, fixation clamp and noise estimate, wink and blink rejection, yielding to a real mouse |
| [OneEuroFilter](https://github.com/casiez/OneEuroFilter) | BSD-3-Clause | One Euro filter (notice in `filters.py`) |
| [cv2-enumerate-cameras](https://github.com/lukehugh/cv2_enumerate_cameras) | MIT | Camera names and device paths |

## Known limits

* Nothing has been tuned on a real face: this machine has no webcam.  The
  closure levels, blink and wink timings, warp distance and dwell, the rest
  threshold for `aim_point` and the mouth thresholds for `attention` are
  priors checked on photos and synthetic data.  Record and replay with a
  real webcam before trusting them.
* Blink detection was checked on six photos, not on video of real blinks.
  Looking down narrows the eyes and raises closure; the 150 ms open-before
  rule and the 350 ms click time guard against it, but a user who reads
  the keyboard a lot may need eye calibration.
* Opening a real camera through Media Foundation is untested here (only
  virtual DirectShow cameras exist on this machine).
* Gaze accuracy on synthetic data (16 to 45 px) says nothing about real
  webcams, where 100 to 200 px is typical; snap and dwell sizes elsewhere
  should come from the calibration's own error map.
* The head tracker's neutral pose is the median of the first half-second of
  tracking; a user who starts turned away needs "recenter".
* Face gestures, the reading detector and the reading scroll were checked
  only on synthetic faces and synthetic reading.  The template photo
  smiles, so the synthetic resting face sets its expression explicitly;
  real blendshape levels, real talking and real reading at webcam accuracy
  may need other thresholds.  The reading detector needs a gaze estimate
  steady enough to show lines; with 100 to 200 px of error it may seldom
  engage, which only means the pointer is not held.
* The cursor pump checks for other Sightline input just before each move,
  but an action that sends its move within the same microseconds can still
  be overwritten; the press itself is safe because the injector holds its
  lock from the move to the release.
