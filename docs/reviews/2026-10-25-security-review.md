# Sightline: security review

Reviewed on 25 October 2026 by an independent reviewer (Sonnet 5.5), read-only.  Nothing in the project was changed except this file.  I did not run the app or its live tests, and nothing was injected, opened, played, recorded or downloaded.  Where a finding says "ran", I imported the real modules in a throw-away script (grammar, interpreter, action registry, loaders, key parser, hassil) with no hardware and no windows; those scripts were not added to the repo.

Snapshot.  The tree as it stood at about 05:12 on 25 October.  Other work was still landing in `app.py`, `ui/overlays/*`, `vision/pipeline.py` and tests between 05:01 and 05:11, and `app.py` now has the watchdog (reviewed here).  Line numbers are from that tree.  Every finding also names the function, so a grep finds it if the lines move.

## 1. Summary

The mechanics are mostly careful.  There is no network code except hash-pinned model downloads, no pickle, `eval`, `exec` or `shell=True`, the integrity-level guard fails safe, settings and caches are written atomically, user files are capped in most places, and the practice page escapes what it prints.

What is weak is the boundary the product exists to create: the microphone.  Once a face is toward the screen, or there is no camera at all, nothing separates the owner's voice from any other sound the microphone hears, and the barriers behind that point are narrow (S-01, S-04).  The second theme is privacy: the log keeps what is said (S-02), and the password-field rule tests the kind of action, not what is shown (S-03).  The rest are small hardening items and robustness issues that matter more than usual here, because a person who can only use eyes and voice cannot repair a broken file or press a stuck key.

| Severity | Count | Ids |
|---|---|---|
| Critical | 0 | |
| High | 2 | S-01, S-02 |
| Medium | 2 | S-03, S-04 |
| Low | 12 | S-05 to S-16 |
| Info | 6 | I-01 to I-06 |
| Questions | 3 | Q-1 to Q-3 |

If you do three things: decide what sound alone may do (S-01 with S-04), stop logging words and fix the password rule (S-02 with S-03), and make a damaged user file unable to stop start-up (S-06 with S-07).

## 2. Threat model used

Actors, heaviest first.

1. **Sound that is not the owner.**  Speakers (media, calls, web pages with audio), other people in the room, anything the microphone hears.  This is the actor that matters most, because the app turns sound into keys, clicks and launches.
2. **Hostile content on screen.**  Window titles, element names and pages that Sightline reads through UI Automation or navigates to.
3. **Same-user code and helpers.**  Another process running as the user, or a carer editing `commands.toml`, `aliases.txt`, `vocabulary.txt`, `settings.json`.  The docs invite a helper to edit these.  I weight this low where the actor could do the same thing directly as the user, and higher where a file is parsed in a way that crashes or hangs the app.
4. **Accident.**  Misrecognition, mis-aim, a crash at a bad moment.
5. **Whoever later sees what Sightline keeps.**  The log, captions on a shared or remote screen, a roaming profile, a bug report.

Assets: the ability to act as the user (keys, clicks, launching), what the user says and types, the user's files and settings, and availability of the app for a person who has no other way to use the PC.

Out of scope: administrator or SYSTEM attackers, physical access to an unlocked machine, compromise of a hash-pinned upstream (noted only where the code could do better), kernel and driver bugs, and bugs inside onnxruntime, sherpa-onnx, MediaPipe or OpenCV except where Sightline hands them data an attacker can shape.

## 3. Findings

A **Status** paragraph under each finding records what was done about it later on 25 October 2026 (fixed, or not and why).  Line numbers in the findings are from the reviewed tree and have moved since.

### High

#### S-01 High: sound that is not the owner can run commands, and the barriers behind it are narrow

**Where.**  `speech/service.py:987-1007` (`_away`), `speech/interpret.py:193-204` (`decide`), `speech/config.py:84-95`, `speech/grammar.py:1131-1133` with `desktop/matching.py:56-61`, `desktop/input.py:326-360`, `speech/grammar.py:1511-1536` with `ui/settings_model.py:149-158`.

**What is wrong.**
1. The attention gate is the only thing that tells the owner from other sound, and it is soft.  `_away` returns "not away" when the gate is off (990), when there is no vision service or no `attention` (993-995), when the call raises (996-1000) and when there were no frames (1001-1002).  Voice-only use (`--no-vision`, a busy or lost camera) has no gate at all.
2. When the gate does say "away", exact commands still run.  `decide` returns any parse with confidence 0.99 or more as a command whatever `situation.away` says (193-199; the dictation-mode twin is at 171-172).  `away` only drops free dictation and inexact matches (202-204); the comment at 203 says this is intended ("only exact commands run").  I ran the real grammar and interpreter with `away` set: "open command prompt", "press enter", "close window", "paste", "go to evil dot com", "open settings" and "type calc dot exe" all came back as commands.  A free sentence in a text field came back as ignored.  The setting's help text says that looking away means "talking to someone else does not type or run things" (`speech/config.py:86`); that is true of free text only.
3. Facing the screen is not evidence of the owner's voice.  A video, a call or a page with audio plays through the speakers while the owner looks at the screen.  The mouth gate (checks that the owner's mouth moves) would help, but it is off by default (`speech/config.py:90-95`; the set-up wizard can switch it on, `ui/windows/wizards.py:1129`).
4. What exact commands can do is large.  Launch a shell by name: `(open|launch|start|run) {app_name}` with the aliases terminal, command prompt, cmd, powershell, task manager.  Type text and a real Enter (`type_text` turns `\n` into Enter, `desktop/input.py:344-350`).  Paste the clipboard.  Go to any web address.  Open the voice-operable settings window, where every visible setting, including `confirm_risky`, `attention_gate` and `password_manager_keys`, can be set by voice (`set_value` has no protected fields).

**How it could happen.**  Someone on a call, or a video the owner is watching, says "open command prompt".  Sightline opens it.  The next sentence is typed because the console is an editable focus, and "new line" becomes Enter.  Nothing was held for a countdown (S-04).  A shorter route to the same place: "open settings", step to "Count down before closing, deleting or sending", "set to off"; after that close-window and Delete run at once.  Influence can also persist: "add ... to vocabulary" and "yes" to an alias offer write `vocabulary.txt` and `aliases.txt` (`speech/service.py:1335-1373`); risky aliases still ask first.

**Why High and not Critical.**  The channel needs sound the microphone can hear while Sightline is awake.  UIPI keeps injected input out of elevated windows, and sleep mode ignores everything but the wake phrase.  The impact is still code execution as the user, from content that any web page can play.

**Fix.**
* Make "exact" mean something when `away`: allow only a short list (stop, cancel, undo, sleep, wake, release) and ignore the rest.
* Add an echo check.  Capture what the PC itself is playing (WASAPI loopback) and hold or drop an utterance that overlaps speech-like output.  It is the one signal that separates a loudspeaker from the owner.
* Without vision, say so once ("no camera: anyone the microphone hears can drive the computer") and put the items in S-04 behind the countdown.
* Keep `confirm_risky`, `attention_gate`, `mouth_gate`, `password_manager*` and `keep_awake` out of the voice-only settings path, or put a countdown or an eyes-only confirmation on changing them.
* Recommend the mouth gate once set-up shows it works, and correct the help text.

**Confidence.**  Routing, exemptions and reachable actions: confirmed by reading the code and by running the interpreter.  The acoustic path (speaker, microphone, recogniser) is plausible; I did not play audio.

**Status.**  Fixed, except the echo check.  (a) While the attention gate says away, only the safety commands run, and only when said exactly: sleep and wake, stop, cancel, pause and resume eye control, "undo that" and help (`speech/interpret.py` `SAFE_WHEN_AWAY`); everything else is ignored with the existing caption, and nothing is typed.  The gate still fails open without a camera, because voice-only users need it.  (b) `setting(protected=True)` marks `confirm_risky`, `attention_gate` and `mouth_gate`; the settings window refuses a voice-sourced change that turns one off or weakens it ("Look at Off and blink, or dwell on it"), and a preset from voice leaves them as they are; turning one on by voice works.  The setting's help text now says what looking away does.  (c) The echo check (WASAPI loopback) is left for later.  Not done: the mouth gate stays off by default; there is no one-time "no camera" warning; and a voice "click" through the numbers or grid on the Off button is a real click, which the window cannot tell from a dwell (the countdown and the safety-only rule limit what such speech can reach).

#### S-02 High: what is said is in the log, including speech meant for someone else

**Where.**  `speech/service.py:1089` (also 671, 1247, 1347, 1373), `core/types.py:163-170`, `core/actions.py:202,206,219,268`, `ui/overlays/confirm.py:445,531,542`, `ui/overlays/chooser.py:978`, `desktop/windows.py:203`, `core/log.py:15-30`.

**What is wrong.**  `_handle` writes `Speech: '<recognised text>' -> <kind>: <display>` at INFO for every utterance that is not pure noise (1049, 1089).  That covers dictation (the words appear twice, raw and in the "Typed: ..." display), commands, and speech the attention gate ignored ("Ignored: you were looking away" counts as a reason, so it is not "noise").  Lines 671, 1247, 1347 and 1373 log the heard words of merged commands, chosen suggestions, aliases and vocabulary words.  `Action.describe()` redacts only an argument literally named `text`, so the failure and countdown paths log `query`, `url` and `name` (a window title, an element name, an app) in full.  `ui/overlays/chooser.py:978` logs 40 characters of a clicked element's accessible name and `desktop/windows.py:203` a window title.  The file is `%LOCALAPPDATA%\Sightline\logs\sightline.log`, plain text, up to four files of 2 MB.

**How it could happen.**  The owner dictates a message that names a diagnosis, or a bystander's conversation is picked up while the owner looks away; both are in the log.  Later the owner attaches the log to a public bug report (the project is about to be published, and the log is the first thing a maintainer asks for), or a backup tool, malware or a helper reads it.  The project's work log says "typed text is no longer written to logs".  That is true of `Action.describe` and not of line 1089.

**Fix.**  Log kind, method, latency and length at INFO, not words.  Put verbatim text at DEBUG (off by default) or behind a diagnostic switch that turns itself off.  Replace the block-list in `describe()` with an allow-list (names, numbers, key combos; everything else as `<n chars>`).  Log window titles and element names at DEBUG only.  Add a test that speaks a sentinel phrase through `_handle` and fails if it appears in any INFO record.  Offer a "copy log for a bug report" action that strips what remains.

**Confidence.**  Confirmed by reading: line 1089 has no guard except the password branch.

**Status.**  Fixed.  Recognised speech is never logged at INFO; at DEBUG each utterance is its kind, method, length and latency, and the words are written only with the hidden developer setting `general.log_speech` (and never from a password field).  `Action.describe()` writes `text`, `query`, `url`, `phrase`, `start`, `end` and `name` as their length.  Every logger call in `src/` was checked: merged commands, chosen suggestions, aliases and vocabulary words are counts; the countdown logs the action and the kind of risk; the chooser, `windows.py` and `apps.py` no longer log names, titles or shell targets; a matcher builder failure logs the length; notices without a toast window log their length.  Tests speak a sentinel through `_handle` and check the log.  Not done: a "copy log for a bug report" action, and `describe()` keeps a list of private argument names rather than an allow-list.

### Medium

#### S-03 Medium: the password-field rule hides typed text, but not `press` sequences or snippet captions

**Where.**  `speech/service.py:1073-1089` (`private`, 1075), `speech/custom.py:167`, `speech/grammar.py:378-390` (`_key_outcome`).  Contradicts `docs/modules/speech.md:419` ("nothing heard is shown or logged").

**What is wrong.**  `private = situation.password and (typed or kind != "command")` tests the kind of action.  When it is true, only `heard` is blanked; the caption and the log still carry `display`.  A custom snippet's display is `"Typed: " + text[:40]`, so it is shown and logged.  A command that presses keys is `kind == "command"` with no `text.type`, so `private` is false and both the words and the display are shown and logged.

**How it could happen.**  In a password box the owner says "press h u n t e r".  The caption reads "Press H, U, N, T, E, R" and the log has `Speech: 'press h u n t e r' -> command: Press H, U, N, T, E, R`.  Or a `commands.toml` snippet called "my password" types the password and shows its first 40 characters in the caption and the log.  I ran both through the real interpreter and the predicate: the press sequence gives `private=False`; the snippet gives `private=True` with display `Typed: <the text>`.

**Fix.**  In a password field, replace `display` as well as `heard` whenever any action is `text.type` or a key press of a printable key ("Typed N characters", "Pressed N keys").  Build a snippet's display after the password check.  Add both cases to the tests beside the existing password tests.

**Confidence.**  Confirmed by reading and by running.

**Status.**  Fixed.  In a password field anything typed or pressed shows as bullets ("Typed ••••••••", "Pressed ••••••"): dictation, spelled `key.press` sequences and `commands.toml` snippets alike, built after the password check.  Those actions carry no utterance and a `private` marker, so the log shows `text.type(<private>)`.  Shortcuts that type nothing (Ctrl+A) keep their caption.  Both of the review's cases are tests in `tests/speech/test_service.py`.

#### S-04 Medium: the countdown guards ten things; the more dangerous ones are not among them, and one path avoids it

**Where.**  `ui/overlays/confirm.py:60-61,122-129,392`, `desktop/handlers.py:566-587` (`login_fill`, call at 583), `ui/tray.py:112-116`, `desktop/keys.py:447-485`, `ui/overlays/shared.py:37`.

**What is wrong.**
* The list is two actions (`window.close`, `app.quit`) and eight key combos.  I ran `risk_of` on others.  Not held: Enter, Ctrl+Q, Ctrl+Shift+Q, Ctrl+Shift+Delete (clear browsing data), Win+R, Win+X, Ctrl+Shift+Esc, Win+L, `app.open` of a shell, `text.type` with a newline into a console or Run box, `web.go`, and `element.click` on a button called Delete or Send.
* `login.fill` presses the chord in the setting `password_manager_keys` by calling `input.key_combo` itself (583).  The interceptor only sees the name `login.fill`, so a chord of Alt+F4 or Delete in that setting runs with no countdown.  The setting is free text, editable by voice (S-01) or by file.
* `parse_combo` presses every non-modifier key of a combo together, so `alt+f4+a` presses Alt+F4+A, and `risk_of` sees `alt+f4+a`, which is not on the list.  Speech cannot make such a combo (`parse_key_sequence` gives one key per chord), but a custom command's `keys` can.
* The tray's Quit dispatches `app.quit` with source `ui` (`ui/tray.py:113-116`), and the countdown applies to `eye`, `dwell` and `voice` only.  One accidental dwell click on Quit ends the app, and as a normal quit the watchdog does not bring it back.

**How it could happen.**  See S-01 (a spoken shell).  Or the owner dwell-clicks Quit by accident.

**Fix.**  Add shell launches (cmd, powershell, pwsh, wt, regedit, mmc, taskmgr), Win+R, Ctrl+Q, Ctrl+Shift+Q, Ctrl+Shift+Delete and a newline typed into a console or Run box; or turn the list into a policy that asks "can this be undone?".  Have `login_fill` dispatch `key.press` for its chord, or call `risk_of` on it.  Make the normaliser reject or split multi-key chords.  Give the tray's Quit the countdown.

**Confidence.**  Confirmed by reading and by running `risk_of` and `parse_combo`.

**Status.**  Fixed, except the tray's Quit.  `core/risk.py` is the one policy (see S-15).  It adds Ctrl+Q, Ctrl+Shift+Q, Ctrl+Shift+Delete, Windows+R, Ctrl+Shift+Esc and Windows+X; `app.open` of a command window, PowerShell, Terminal, the registry editor, Run, Task Manager and similar tools; Enter or a typed line into a command window; and `element.click`, or an eye, dwell or voice click, on a control named Delete, Remove, Discard, Send, Submit, Pay, Buy, Checkout or Empty Recycle Bin.  Chords are normalised and extra keys or modifiers still count, so `alt+f4+a` is Alt+F4.  `login.fill` refuses keys the policy counts as risky instead of pressing them.  Not done: the tray's Quit (source `ui`) still runs at once (it was not in the agreed list); `web.go` is not held (it opens only web pages now, S-09); Windows+L and Ctrl+Alt+Delete cannot be injected by any app.

### Low

#### S-05 Low: a guard that fails lets the action through

**Where.**  `core/actions.py:211-223` (`_intercept`, 216-219).

**What is wrong.**  If an interceptor raises, the exception is logged and the chain goes on with the action unchanged.  For the countdown, or any later guard, that is fail-open.  I ran a registry with a raising interceptor at priority 120: `window.close` ran.

**How it could happen.**  `ConfirmCountdown.intercept` calls into Qt through `bridge.post` and into the desktop; an exception from the bridge during shutdown, from `point_of` while armed, or from a future edit lets the close or delete through.  I found no trigger today.

**Fix.**  Let an interceptor declare itself critical (the countdown is).  On an exception, drop the action and tell the user.

**Confidence.**  Confirmed by reading and by running; no concrete trigger found.

**Status.**  Fixed.  `ActionRegistry.add_interceptor(..., fail_closed=True)`: when such an interceptor raises, the action is dropped and reported with "Sightline stopped that because a safety check failed. Please try again."  The countdown installs itself that way.

#### S-06 Low: some values in settings.json or pauses.json stop the app starting, and the watchdog gives up without saying so

**Where.**  `settings.py:49`, `core/settings_base.py:157,161`, `speech/pauses.py:98-116` (103), `core/log.py:17`, `app.py:101-102`, `speech/service.py:316,921`; the watchdog at `app.py:48-49,286-312`.

**What is wrong.**  The loaders catch `ValueError`, `OSError` and `TypeError` only.  I ran the real loaders: `Settings.load` raises `RecursionError` on a deeply nested file and `OverflowError` on `1e999` or `Infinity` in an integer or float setting; `PauseModel.load` raises both (`commands: 1e999`); `setup_logging("BASIC_FORMAT")` raises `ValueError` (the `log_level` setting is only checked to be text).  `Settings.load` runs first in `main()`, before logging exists, and `PauseModel.load` runs inside `SpeechService.__init__`; neither is in a `try`.  The child dies at start-up, the watchdog restarts it three times in ten minutes and then stops (304-309), writing only to `watchdog.log`.  There is no window or sound, so a person who uses only eyes and voice cannot tell why nothing happens or fix the file.

**How it could happen.**  A helper or a script writes `Infinity` into a number field, or a damaged sync leaves a file of brackets.  Rare, but the machine's only input route is gone.

**Fix.**  Catch `Exception` around each user-data load, move the bad file aside as `Settings.load` already does for `ValueError`, start from defaults and show a notice.  Check `log_level` against `logging.getLevelNamesMapping()`.  Reject non-finite numbers in `_coerce`.  When the watchdog gives up, have it show a message.

**Confidence.**  Confirmed by running the loaders.

**Status.**  Fixed.  `settings.json` and `pauses.json` are read with `settings_base.loads_strict` (no NaN, Infinity, 1e999 or nesting deeper than 32) under a size cap; any failure moves the file aside as `*.broken.json` and starts from defaults, and a damaged settings file is announced once the UI is up.  `_coerce` rejects non-finite numbers field by field.  `log_level` must be a real level name.  When the watchdog gives up, or cannot start the app, it shows a plain message box saying so and where the log is (`app.show_message` over `MessageBoxW`; every test replaces it).  The review's `check_corrupt.py` now reports OK for every case.

#### S-07 Low: commands.toml has no limits, a few stacked optionals hang matching, and a phrase can silently replace a safety word

**Where.**  `speech/custom.py:87-122` (`load`; only `parse_sentence` is tried, 117), `132-143` (`to_specs`, `priority=10`), `159-172`; `speech/service.py:837-848` (reload every 2 s) and the lock in `_handle` (1039-1047); `speech/matcher.py:153`.

**What is wrong.**
* A phrase of n stacked optionals, `[a] [a] ... [a]`, loads in a few milliseconds but costs about four times more to match for every two extra optionals.  I ran hassil on a non-matching sentence: 8 optionals 3 ms, 14 164 ms, 18 2.9 s.  By extrapolation, about 45 s at 22 and hours at 30.  Matching runs under the speech lock on every utterance, so speech stalls from the moment the file is saved (the reload is automatic).
* There are no caps on the number of commands, the length of `text`, or the file size.
* Custom commands take priority over built-ins and nothing checks the phrase.  I ran a file with "cancel" and "stop" mapped to Enter and "go to sleep" to typed text: all three replaced the built-ins and no problem was reported.  The countdown tells the user to "Say cancel ... to stop" (`ui/overlays/confirm.py:58`), so this removes the voice route out of it.

**How it could happen.**  A helper pastes a phrase with many optional words, or a shared "command pack" shadows "stop".

**Fix.**  Count expansions when loading and reject a phrase over a small bound (say 8 optionals or 500 sentences); cap commands (200), text (2,000 characters) and file size (256 KB); report problems through the existing notice.  Refuse or warn when a phrase equals a built-in safety phrase (`ui.cancel`, `key.release_all`, `action.undo`, sleep and wake).

**Confidence.**  Confirmed by running for the stall (measured to 18, extrapolated beyond) and the shadowing; the missing caps by reading.

**Status.**  Fixed.  The file is capped at 256 KB, 200 commands, 200 characters a phrase, 8 optional parts and 500 ways of saying it (counted from the parse tree, so an enormous phrase costs no more to measure), 2,000 characters of text and 200 of keys.  Eighteen stacked optionals are refused in a few milliseconds.  A command that could be heard as a safety command (stop, cancel, sleep and wake with the user's own phrases, undo that, pause or resume eyes, help) is left out and the notice says why; the first problem is shown again when speech starts.  The review's `check_hassil.py` and `check_shadow.py` confirm it.

#### S-08 Low: keys and buttons held when the app dies are not let go on the restart

**Where.**  `desktop/service.py:146-159` (`start`; `release_all` appears only in `stop`, 166), `app.py:184`, and the watchdog (`app.py:286-312`).

**What is wrong.**  Injected key and button state outlives the process.  If Sightline crashes while "hold shift", a drag or `mouse.down` is active, the key or button stays down in Windows.  A Python `finally` cannot run after a native fault in the camera, recogniser or UI Automation code, and those are the crashes a watchdog exists for.  The new process starts with an empty `_held_keys` and never calls `release_all()`, which would also release any modifier or button Windows reports stuck (`desktop/input.py:364-382`).

**How it could happen.**  A crash during a dictation with shift held; after the restart every click is a shift-click until someone finds a physical shift key.

**Fix.**  Call `input.release_all()` at the end of `DesktopService.start()`.

**Confidence.**  Plausible: I confirmed that nothing releases at start; the stuck state after a hard crash is Windows behaviour I did not provoke.

**Status.**  Fixed.  `WindowsDesktop.start()` calls `input.release_all()` before anything else (key-ups and button-ups only).  Not tried after a real crash: that needs the live desktop.

#### S-09 Low: any scheme can reach ShellExecute from a custom `url`; the spoken-URL rule accepts anything that starts with "http"

**Where.**  `desktop/handlers.py:834-844` (`web_go`, 839), `desktop/apps.py:250-276` (`shell_open`), `speech/grammar.py:559-584` (577), `speech/custom.py:164-172`.

**What is wrong.**  With no browser open, `web_go` hands `url` unchanged to `ShellExecuteW` whenever it contains `"://"`.  The shell then passes it to whatever handler is registered for that scheme (`file:`, `ms-settings:`, `search-ms:`, app-specific schemes).  Spoken URLs pass `spoken_url`, which accepts anything starting with "www." or "http" (577) and otherwise a host with a dot.  By reading that rule, speech cannot produce a `file:` scheme, so this is reachable mainly from a custom command's `url` or a file a helper wrote.  With a browser open the string is typed into the address bar, which is safer.

**Fix.**  Before `open_url`, parse the target and allow only `http` and `https`.  In `spoken_url`, require an explicit `http://` or `https://` or a dotted host.  Check the same in `custom.load` and report it.

**Confidence.**  Confirmed by reading; what the shell does with a given scheme is documented Windows behaviour, not exercised here.

**Status.**  Fixed.  `core/urls.py` allows http, https and mailto (no scheme means https; paths with a backslash are refused).  `web.go` refuses anything else before typing or calling the shell, a `commands.toml` `url` is checked when the file loads, and `spoken_url` needs "http colon slash slash" said in full and passes the same check.

#### S-10 Low: bare executable names and `python -m` take the working directory into account

**Where.**  `desktop/apps.py:89-111` (94, `powershell.exe`), `desktop/accessibility.py:119` (`osk.exe` through ShellExecute), `app.py:268` (`[sys.executable, "-m", "sightline"]`).

**What is wrong.**  `CreateProcess` with a bare name looks in the parent's current directory before System32, and ShellExecute of a bare name searches similarly.  `python -m sightline` puts the working directory first on `sys.path`.  I ran it with a decoy `sightline` package in the working directory: the decoy ran instead of the app, and `-P` prevented that.  The same path serves every import the child does.  (`desktop/uia.py:182` also loads the `UIAutomationCore.dll` type library by bare name through comtypes; I did not test whether the working directory can influence that.)

**How it could happen.**  The app is started from a shortcut whose "Start in" folder, or a terminal whose directory, is somewhere a downloaded archive or another process can write.  That needs same-user write access, so Low.

**Fix.**  Use absolute system paths for `powershell.exe` and `osk.exe`; start the child as `[sys.executable, "-P", "-m", "sightline"]` (needs 3.11 or newer; the project requires 3.12); change directory to the data folder at the start of `supervise` and `main`.

**Confidence.**  Confirmed by reading and by running the `-m` case.  The `CreateProcess` and ShellExecute search orders are documented Windows behaviour that I did not exercise.

**Status.**  Fixed.  PowerShell (`Get-StartApps`) and osk.exe are started by full path under the Windows folder (`GetSystemWindowsDirectoryW`, with `Sysnative` for a 32-bit Python).  The watchdog starts `python -P -m sightline` from the data folder.  Not done: `main()` itself does not change directory (a developer's direct `python -m sightline` keeps Python's own rules), and the `UIAutomationCore.dll` type library load was not examined.

#### S-11 Low: the model downloader has no size cap, follows any redirect, and trusts the folder afterwards

**Where.**  `core/models.py:192-242` (211, 229-231), `114-130`.

**What is wrong.**  Every file is https and pinned by SHA-256, so a wrong file cannot be installed.  But (a) the stream is not limited to the declared `size`, so a server can send gigabytes to `%LOCALAPPDATA%` before the hash fails, three times over; `urlopen` also follows a redirect from https to http or ftp (urllib's handler allows those targets; I checked its source); (b) `is_ready` accepts the `.ready` marker and the file's existence, not a hash or even the size, so a model replaced later on disk is loaded without detection by libraries that parse it natively.

**Fix.**  Abort when `received` passes `f.size`.  Install a redirect handler that refuses anything but https.  Compare sizes in `is_ready`, and re-hash the small files at start.

**Confidence.**  Confirmed by reading.

**Status.**  Fixed (a); (b) not done.  A download stops as soon as the server says, or sends, more than the file's size plus max(64 KB, 1%); the first URL must be https and redirects are followed only to https; the SHA-256 check is unchanged.  `is_ready` still trusts the `.ready` marker (no size check or re-hash at start); it was not in the agreed fixes.

#### S-12 Low: the single-instance mutex can be taken by any process in the session, and Sightline then exits without a word

**Where.**  `app.py:80-93` (88) and `app.py:106-108`.

**What is wrong.**  `Local\Sightline.SingleInstance` is a fixed name with a default ACL.  A process that creates it first makes the real app log "already running" at INFO and exit 0, and the watchdog treats exit 0 as a normal quit.  Nothing appears on screen.  If `CreateMutexW` fails with "access denied", `GetLastError() != 183` makes the app behave as the first instance.

**How it could happen.**  Malware, or any program that picked the same name.

**Fix.**  Check the handle.  On "already exists", signal the first instance (a named event or pipe) and, if it does not answer, show a visible notice instead of exiting.

**Confidence.**  Confirmed by reading.

**Status.**  Fixed in part.  A mutex that already exists, or that Windows refuses to open (a NULL handle), counts as taken, and the second copy shows a plain message ("Sightline is already running...") through the same message-box function instead of exiting silently.  Not done: the first instance is not signalled, and the fixed name and default ACL are unchanged, so a squatter can still stop Sightline starting, but no longer invisibly.

#### S-13 Low: the PC never locks while Sightline runs unless the user turns that off

**Where.**  `desktop/config.py:37-42` (default on), `desktop/winevents.py:59-68,133-136` (`ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED`), `desktop/service.py:154`.

**What is wrong.**  The reason is sound (a screen lock is a lock-out for someone who cannot type a password) and it is a visible setting.  Together with S-01 it means an unattended PC with the microphone open stays unlocked and receptive until Sightline is quit.

**Fix.**  Keep it, but release the request when vision has seen no face for some minutes, and say what it does in set-up.

**Confidence.**  Confirmed by reading.

**Status.**  Fixed.  The display is kept on only while a face has been seen in the last `desktop.keep_awake_minutes` (new setting, 5 by default, 1 to 60); a timer lets the request go when the face is too old, even if vision stops publishing, so an empty room locks as usual.  Before any face is seen (no camera, voice only) the setting alone decides, like the attention gate.  Not tried with a real camera and lock screen.

#### S-14 Low: nothing warns or refuses when Sightline itself runs elevated

**Where.**  `desktop/elevation.py:147` (`is_elevated_self`, no caller), `94-110` (`account_kind`), `desktop/checks.py:160-170`.

**What is wrong.**  The guard allows injection into anything at or below Sightline's own level.  If the app is started "as administrator", or the account never gets a UAC prompt (`checks.py` already detects the second case), S-01 becomes administrator-level.  The code knows (`is_elevated_self`) and does not use it.

**Fix.**  At start-up, if elevated, show a notice and recommend a standard launch; consider disabling the shell and registry-touching actions while elevated.

**Confidence.**  Confirmed by reading.

**Status.**  Fixed in part.  When Sightline runs elevated, a notice at start says it does not need to and recommends a normal start.  Shell and registry actions are not disabled while elevated.

#### S-15 Low: two lists of risky keys and two key-name normalisers

**Where.**  `speech/interpret.py:71-72,104-108,111-120`, `ui/overlays/confirm.py:60-61,92-119`.

**What is wrong.**  `interpret.is_risky` decides whether a paraphrase or an alias may run without "Say 1 to confirm".  It uses a copy of the list and a plainer normaliser.  I ran both: `Control+W`, `alt f4`, `Del` and `shift+del` are risky to the countdown's normaliser and not to the interpreter's.  The built-in grammar emits canonical names, so it works today; a custom command or alias target in another spelling is not seen as risky at the paraphrase stage (the countdown still catches it for eye, dwell and voice sources).

**Fix.**  One list and one normaliser in `core`, imported by both.

**Confidence.**  Confirmed by running.

**Status.**  Fixed.  One list and one normaliser in `core/risk.py` (`assess`, `risk_of`, `normalize_combo`); speech's paraphrase and alias gating and the countdown both use it, and `check_confirm.py` shows the two spellings now agree.

#### S-16 Low: vocabulary lines reach sherpa's hotword syntax with `:` and `@` intact

**Where.**  `speech/custom.py:195-205` (`vocabulary_entry`), `speech/asr.py:67-83,280`.

**What is wrong.**  The hotword string joins entries with `/` and sherpa reads `:score` and `@tag` after an entry.  The entry filter removes `/` and caps length and word count but leaves `:` and `@`, so a line like `foo :99` changes the boost.  `speech/asr.py:260-261` records that sherpa-onnx 2.0 has already taken the whole process down over hotwords once (a greedy-search recogniser given hotwords it can encode), so native hotword handling is not forgiving.  The file is hot-reloaded and voice can append to it (S-01).  I did not load a recogniser, so whether a particular line crashes is untested.

**Fix.**  Allow letters, digits, apostrophe, hyphen and space in entries.  Wrap `create_stream` so a bad string falls back to no hotwords.

**Confidence.**  Plausible: the filter gap is confirmed by reading; the effect is untested.

**Status.**  Fixed.  Vocabulary entries keep only letters, digits, apostrophes, hyphens and spaces, within the existing 60-character, six-word and 200-entry caps; `hotword_variants` filters again and caps length and count; a `create_stream` that raises falls back to no hotwords with a warning.  A native crash inside sherpa-onnx still cannot be caught.  The real-model biasing test still passes.

### Info

* **I-01.  OSK settings are written and never restored.**  `desktop/accessibility.py:95-98,115` writes `HKCU\Software\Microsoft\Osk` (Mode, HoverPeriod, UseDevice, Dock, window rectangle) before starting `osk.exe`.  The user's own keyboard settings are changed for good.  Not a security problem; save the old values and restore them on quit, or say so in set-up.

  **Status.**  Fixed.  Before its first change Sightline keeps the values it is about to replace and puts them back once the keyboard it opened has closed (on hide, or as Sightline quits, closing it first); values the user never had are removed.  A keyboard the user opened is not touched.
* **I-02.  The same-user boundary.**  A process running as the user can rewrite `commands.toml`, `aliases.txt`, `vocabulary.txt` (reloaded within 2 s, `speech/service.py:837-848`), `settings.json`, the app cache `desktop\apps.json` (its `target` goes to ShellExecute, `desktop/apps.py:207-219,238-247`) and the model files.  That is the normal boundary for a user-level app and the code does not pretend otherwise.  A short `SECURITY.md` saying so will save reports.

  **Status.**  The code is unchanged: not in the agreed fixes.  `SECURITY.md`, added when the project was published, now describes this same-user boundary and how to report problems privately.
* **I-03.  Machine-specific and personal data is in the roaming profile.**  `core/paths.py:28-33,48-50` puts settings, `commands.toml`, vocabulary (names of people), calibration and face profiles (`profiles/<name>/vision/*`) and practice summaries in `%APPDATA%`.  Calibrations belong to one camera and screen.  On a machine with roaming profiles or folder redirection this data leaves the PC.  Consider `%LOCALAPPDATA%` for the profile data and practice pages.

  **Status.**  Not changed: not in the agreed fixes.
* **I-04.  Python dependencies are not pinned.**  `pyproject.toml:22-40` has lower bounds only and there is no lock file or hash list, while onnxruntime, sherpa-onnx, MediaPipe, OpenCV and PySide6 parse data natively.  The models are pinned; the packages are not.  For the public release, publish a lock file with hashes and run `pip-audit` in CI.

  **Status.**  Not changed: not in the agreed fixes (and no packages could be installed or downloaded).
* **I-05.  A functional bug seen in passing.**  `speech/service.py:623` rebuilds the interpreter after the paraphrase model loads without `aliases=` (compare 457), so learned aliases stop working until the next grammar rebuild.  No safety effect, since risky aliases are confirmed anyway.

  **Status.**  Fixed.  The interpreter built when the paraphrase model loads keeps the learned aliases (`SpeechService._install_semantic`), with a test.
* **I-06.  Two small things.**  `AllowSetForegroundWindow(ASFW_ANY)` (`desktop/apps.py:243`) lets any process take the foreground until the next input; pass the launched process's id instead.  `--quit-file` (`app.py:68,212-219`) writes "quit" to whatever path the command line names; only someone who controls the command line can use it.

  **Status.**  Not changed: not in the agreed fixes.

## 4. Questions

* **Q-1.**  Is a UIAccess build still planned (concept review R3; the `elevation.py` header)?  UIAccess lifts the UIPI barrier that today keeps an audio-borne attacker (S-01) out of elevated windows.  S-01 and S-04 should be closed first.  UIAccess also needs a signed binary in a protected location, which is a question about installers and updates.
* **Q-2.**  What does the user see before the first model download?  `ui/shell.py:159-173` opens the models window and downloads.  Everything else the app says is "nothing is sent to a server", but the downloads contact five hosts (github.com, raw.githubusercontent.com, huggingface.co, storage.googleapis.com, download.moonshine.ai) and so reveal the address and the fact of installation.  Are the hosts shown, is consent explicit, and is there an offline route (`copy_into`) in the UI?
* **Q-3.**  Does the project mean users to share `sightline.log`?  If so, S-02 should be fixed before publishing, and a redacted support bundle added.

## 5. Checked and found sound

* **Network.**  `urllib` is used only in `core/models.py`; no sockets, no listening ports, no telemetry.  Clipboard is never read or written by Sightline (copy and paste are key presses).
* **Dangerous primitives.**  No `pickle`, `eval`, Python `exec` (Qt's event-loop `exec()` aside), `shell=True` or `os.system` in `src/`.  Two `subprocess` uses (PowerShell with a fixed argument list; the watchdog child), see S-10.
* **Model downloads.**  I checked every entry in `speech/models.py` and `vision/models.py` programmatically: 9 models, 24 files, all https, all with a 64-hex SHA-256 and a size.  The hash is computed while streaming and compared before `os.replace`; `.part` files are removed on failure; tar extraction uses `filter="data"`; zip members are checked against the destination; the sentence-embedding model is pinned to a commit; the `.ready` marker records the pinned hashes, so a changed spec forces a re-download.
* **Deserialisation.**  NumPy loads use `allow_pickle=False` (`vision/profile.py`, `vision/gaze_model.py:147`, `vision/recorder.py:156`); everything else is JSON or TOML.
* **UIPI and secure desktop.**  `elevation.input_blocked` counts an unknown target level as blocked; `win32.input_desktop_name` reports "Winlogon" when the input desktop cannot be opened (access denied), which `secure_desktop_active` treats as the secure desktop; typing and clicking check both first.  Windows enforces UIPI regardless.
* **Typed text cannot become shortcuts.**  `type_text` sends `KEYEVENTF_UNICODE` code units, so no typed character can act as Ctrl or Alt chords; only `\n` and `\t` are mapped to real keys, on purpose.
* **The countdown's own mechanics.**  The `confirmed` marker cannot be set from the speech grammar, custom commands or vision settings; chooser re-runs use source `ui` only after a deliberate pick; the interceptor runs first (priority 120); `_aim` pins the key or close to the window in front when the countdown starts, so the confirmed key goes to the same window; actions from the same utterance wait in order.
* **Files Sightline writes.**  `Settings.save`, the app cache and pauses are written to a temporary file and replaced; `profile_dir` strips a profile name to letters, digits, `-`, `_` and space (no traversal); aliases are capped at 500 and normalised; vocabulary at 200 entries of at most 60 characters and 6 words; the log is bounded (2 MB x 4, plus 200 KB x 2 for the watchdog); the reload of the user files logs counts and the file's own problem messages, not speech.
* **Windows integration.**  `win32.py` uses private `WinDLL` objects with bare system DLL names (I checked in the venv's `ctypes` source that a bare name is loaded with `_LOAD_LIBRARY_SEARCH_DEFAULT_DIRS`, which leaves out the working directory); registry reads in `desktop/checks.py` are read-only and the only write is the OSK key (I-01); EDID parsing is bounds-checked; keep-awake is tied to a thread that lives as long as the app and is released on exit.
* **Practice summary.**  `ui/measures.py` and `ui/windows/practice.py` pass every interpolated value through `html.escape` and put no window titles or element names in the page.
* **Camera and recordings.**  The app itself never records; `tools/vision_record.py` is opt-in, stores landmarks (video only with `--video`) under the local data folder.  Audio is held in a bounded queue and never written; an utterance is capped at 30 s.
* **Tests.**  Live-desktop tests are skipped unless asked for by marker or `SIGHTLINE_LIVE_DESKTOP=1` (`tests/desktop/conftest.py:20-30`).
* **Repository.**  A search of the tree for keys, tokens, e-mail addresses and user-profile paths found none; `out/` (benchmark audio) is ignored by `.gitignore`.
* **Hostile names.**  Window titles and element names feed a name table matched by fuzzy search; they are not compiled into grammar templates or regular expressions.

## 6. Limits of this review

No audio was played and no camera, microphone or input was used, so S-01's acoustic path, S-08's stuck-key behaviour and S-16's effect on sherpa are reasoned, not observed.  I did not audit the bundled native libraries.  The tree was changing during the review; anything edited after about 05:12 on 25 October is not covered.
