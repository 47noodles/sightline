# Desktop subsystem

Everything that touches Windows itself: synthetic input, monitors, UI
Automation, windows, apps, the On-Screen Keyboard and Magnifier, and the
desktop actions in ARCHITECTURE.md section 7.  Entry point:
`sightline.desktop.create(ctx)` returns a `WindowsDesktop`.

## Files

| File | What it does |
|---|---|
| `service.py` | `WindowsDesktop`: builds the parts, registers actions, starts and stops threads, publishes `FocusChanged` and `HoverTarget` |
| `handlers.py` | `DesktopActions`: every desktop action, its argument checks and plain-English errors, `ClickPerformed` |
| `input.py` | `SendInputInjector`: exact cursor moves, clicks, wheel, key chords, Unicode typing, held-key tracking, `release_all` |
| `keys.py` | Key names and the combo parser; binds keys to the active keyboard layout |
| `display.py` | `WindowsDisplay`: monitors, work areas, scale, panel size from the EDID |
| `uia.py` | `UIAProvider`: the two UI Automation threads, focus, clickable scans, hover, element queries, invoke, selecting text |
| `spatial.py` | The clickable snapshot, its grid index and the snapping rule |
| `textedit.py` | Finding a spoken phrase near the caret, and the arrow-key walks that reach it |
| `checks.py` | `system_checks()`: read-only checks of the Windows settings Sightline depends on |
| `windows.py` | `Win32WindowManager`: Alt+Tab-style window list, activation, caption commands |
| `apps.py` | `StartMenuApps`: Start menu and AppsFolder scan, cache, shell launch |
| `matching.py` | Fuzzy matching (rapidfuzz) with app and process aliases |
| `elevation.py` | Integrity-level checks for administrator windows and the secure desktop |
| `accessibility.py` | The Windows On-Screen Keyboard (hover mode, docked) and Magnifier |
| `scrolling.py` | Continuous scrolling with a speed ramp |
| `winevents.py` | WinEvent hooks and a hidden window for display changes |
| `win32.py` | ctypes declarations on private `WinDLL` instances |
| `config.py` | Desktop settings |

## Threads

Construction starts nothing and costs about 35-70 ms, so vision and speech
can use `ctx.desktop` (display, input, the focus cache) from their
constructors.  `start()` takes 35-65 ms; `stop()` releases every key and
button, then joins the threads (20-60 ms).

| Thread | Work |
|---|---|
| `sightline-winevents` | Out-of-context WinEvent hooks (foreground, focus, menu popups, restore), a hidden top-level window for `WM_DISPLAYCHANGE` and work-area changes, and the keep-awake power request.  Callbacks only signal the workers. |
| `sightline-uia` | Interactive UI Automation: focus poll (5 Hz plus focus events), hover target (10 Hz while the pointer moves), `element_at`, `text_before_caret`, `invoke` |
| `sightline-uia-scan` | Clickable scans of the foreground window, its popups, the taskbar and the window under the pointer |
| `sightline-scroll` | Continuous scrolling, only while it runs |
| `sightline-apps` | Start menu scan, at most hourly |

There are two UI Automation threads because a clickable
scan of a large page takes 100-300 ms, and on a single thread every scan
would freeze the hover highlight and make speech's `text_before_caret`
(50 ms timeout) fail.  Both threads are in the multithreaded apartment with
their own `IUIAutomation`; COM objects never leave the thread that made them
(`ElementInfo.handle` holds an `ElementRef` ID).  Callers wait on a request
queue with a timeout and get the documented default when it expires.
`IUIAutomation2` connection and transaction timeouts (1.5 s, 3 s) stop a hung
app from holding a thread for the default 20 s, and hung windows
(`IsHungAppWindow`) are skipped.  Sightline's own windows are never queried
through UI Automation: Qt answers on the GUI thread, which could deadlock.

## Input

**Cursor moves.**  Measured at 125 % scaling with the process per-monitor
DPI aware v2:

| Method | Exact | Cost |
|---|---|---|
| `SetCursorPos` | 160 of 160 points | 8 us |
| `SendInput` absolute, `n = x * 65536 // width` | 53 of 2239 (the rest one pixel short) | 0.23 ms |
| `SendInput` absolute, `n = ceil(x * 65536 / width)` | 2239 of 2239 | 0.32 ms |
| `SendInput` absolute, pixel centre `n = (x * 65536 + 32768) // width` | 2239 of 2239 | 0.22 ms |

Moves use `SendInput` with `MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK`
and the pixel-centre formula, because a real mouse event wakes hover
effects, raw-input apps, the idle timer and a hidden cursor.  Each move is
checked with `GetCursorPos` (the new position is visible within 6 us,
43 us worst); when it did not land (Windows drops injected input over an
administrator window, or a clip rectangle) `SetCursorPos`, which is never
blocked, finishes it.  The remote desktop client occasionally moves the
cursor on its own (about once a second here), which the live test allows
for.

**Clicks.**  Move, 12 ms settle (so the target sees the hover), press,
30 ms hold, release.  A double click puts the second click 60 ms later,
well inside the double-click time (500 ms here), at the same spot.  Swapped
mouse buttons are compensated, so "left" is always the primary button.

**Keys.**  `keys.py` parses combos such as `ctrl+shift+t`, `win d`,
`alt+f4`, `page down`, `ctrl++`, `volume up`, `numpad 7`, `left bracket`,
`print screen`.  Keys are sent as virtual key plus scan code plus the
extended flag, without `KEYEVENTF_SCANCODE`.  A low-level hook on this
machine showed:

* virtual key with scan code: hooks (so remote desktop clients and games)
  see the scan code and the extended flag;
* virtual key alone: scan code 0, which remote desktop clients drop;
* scan code alone (`KEYEVENTF_SCANCODE`): scan code 0x47 arrived as Home,
  not numpad 7, because NumLock was off.

Letters, digits and symbols take their scan codes from the keyboard layout
of the foreground window, and symbols that need Shift or AltGr on that
layout get it ("ctrl+?" is Ctrl+Shift+/ on a US keyboard).  Modifiers are
pressed first and released in reverse, in a `finally`, and a modifier the
user is holding (`key.hold shift`) is left held.  Sleep and power keys are
deliberately missing from the table.

**Typing** uses `KEYEVENTF_UNICODE`: each UTF-16 unit is a down/up pair, so
emoji are two pairs in order; `\n` is a real Enter key and `\t` a real Tab.
Text goes out in chunks of 48 units with a 4 ms pause, so slow apps keep up.

**Wheel.**  `scroll(dy, dx)` takes notches with fractions; remainders
accumulate.  One-shot scrolls send one `WHEEL_DELTA` event per notch, 8 ms
apart, like a real wheel.  Continuous scrolling sends thirds of a notch
(`smooth_scrolling`, on by default) at 40 Hz, starting at 2 notches a second
and easing up to 8 over 2.5 s.

**Nothing stays stuck.**  Every key and button Sightline holds is tracked.
`release_all` releases them, plus any modifier or mouse button Windows
reports down, and resets the wheel remainder.  Every injected event carries
`dwExtraInfo = 0x5349474C` ("SIGL") so hooks can tell it apart.
`WindowsDesktop.start()` calls `release_all` before anything else (review
S-08): a crash during "hold shift" or a drag leaves the key or button down
in Windows, and the watchdog's restart would otherwise click with Shift
held.  It sends key-ups and button-ups only.

## Monitors

`EnumDisplayMonitors`, `GetMonitorInfo` and `GetDpiForMonitor` (effective
DPI), read lazily on first use (0.4 ms) and again on `WM_DISPLAYCHANGE`,
`WM_DPICHANGED` and work-area changes.  Panel size comes from the EDID in
`HKLM\SYSTEM\CurrentControlSet\Enum\DISPLAY\<model>\<instance>\Device Parameters`:
the detailed timing descriptor gives millimetres, bytes 21-22 whole
centimetres or an aspect ratio.  Remote and virtual displays have no EDID,
so `size_mm` is None there (as on this machine's RDP display).

## UI Automation

**Clickables.**  One `FindAllBuildCache` per window with the condition
"on screen and enabled, and either a clickable control type (Button,
Hyperlink, MenuItem, ListItem, TabItem, TreeItem, CheckBox, RadioButton,
ComboBox, SplitButton, Edit, DataItem, HeaderItem, Document, Slider) or an
Invoke, Toggle, SelectionItem or ExpandCollapse pattern" fetches every
candidate with its name, rectangle, type, automation ID and process in one
round trip.  Python then drops:

* scrollbar parts and containers (Thumb, ScrollBar, List, Tree, Tab and the like);
* documents that are not editable (a web page itself is not a target);
* invokable panes larger than half the window, which would swallow every click;
* elements whose centre is covered by another window;
* an invokable Text, Image or Group inside a button or link (the same target twice).

Scanned roots: the foreground window, visible windows of the same process
stacked above it (menus, drop-downs, Chromium popups are separate windows),
the window under the pointer, and the taskbars (refreshed every 5 s).
Sightline's own windows never count as covering an element (its overlays sit
over everything, and the numbered labels need what is underneath), and a
click-through Sightline overlay is looked through when deciding which window
a click would reach.
Scans run on foreground change (after 150 ms), when a menu opens, 0.35-0.5 s
after clicks, scrolls, key presses and typing, and every 1.5 s while the
pointer moves (less often on slow windows: at least 6 times the last scan
time).  Results are capped at 2500 elements (nearest the pointer kept) and a
1.5 s budget across windows.

**Chromium.**  Chrome, Edge and Electron apps build their web-content tree
only after a UI Automation client asks a `Chrome_RenderWidgetHostHWND` for
its root.  The scanner calls `ElementFromHandle` on each visible render
widget, then rescans after 0.4 s and 1 s while the tree fills in, and asks
again every minute (Chrome switches accessibility off when no client is
around).  On this machine that took the Claude desktop app from 3 to 183
clickables.  A browser window that is fully covered parks its render widgets
off screen and exposes no page content until it is shown.

**Snapshot and snapping.**  The scan result is an immutable
`ClickableSnapshot` with a 64 px grid index, swapped in whole, so
`nearest_clickable` and `visible_names` read it without locks.  The rule
(shared by the hover highlight and `mouse.click`, so what is outlined is
what gets clicked):

1. A point inside a clickable of the window under it is clicked exactly
   there; the smallest such element is the one named.
2. If the snapshot misses (new content), `ElementFromPoint` plus up to four
   ancestors finds the clickable under the point.
3. Otherwise the nearest clickable of that same window within
   `snap_radius_px` is clicked: at its centre when that is near the pointer,
   else just inside its edge nearest the pointer (a wide list row's centre
   can be 300 px from where the user looked).
4. Otherwise the point itself.

**Hover target.**  The interactive thread checks the cursor every 100 ms;
when it moved or the snapshot changed it applies the rule and publishes
`HoverTarget` only when the target changes (`rect` None clears it).  Off
when `highlight_target` is off.

**Focus.**  `GetFocusedElementBuildCache` with one cache request, polled at
5 Hz and immediately (30 ms) after a focus or foreground WinEvent.
`editable`: an enabled Edit, Document or ComboBox with a writable value
pattern; or, with no value pattern at all, a text pattern plus a live caret
(the Win32 caret from `GetGUIThreadInfo`, or an active `TextPattern2`
caret), which covers RichEdit, Word and terminals.  `password` is
`IsPassword`.  `FocusChanged` is published when anything but the time
changes.

**Text before the caret.**  `TextPattern2.GetCaretRange`, else the first
selection range of `TextPattern`; collapse to the caret, move the start back
`max_chars` characters, read.  None for password boxes, Sightline's own
windows and fields without a text pattern.

**Invoke.**  Invoke, Toggle, SelectionItem, ExpandCollapse, then
LegacyIAccessible's default action.  If the element's COM object has been
dropped, it is found again from its centre by name and type.

**Clickables near a point** (`clickables_near`, for the UI's number
chooser).  A filter on the last snapshot through the grid index: every
clickable whose rectangle comes within the radius, nearest first, equal
distances ordered smaller element first, then document order.  No UI
Automation call is made.  Between a change of foreground window and the
rescan it triggers (about 0.2 s), the snapshot still lists the old window,
which may now be hidden, so only the taskbar and the new window's elements
are offered until then.

**Editing text by content** (`text.select`, `text.select_range`,
`text.caret`).  On the interactive thread: the start of the selection (or
the caret) anchors an empty text range; 3000 characters each side of it are
read in one call; `textedit.py` finds the phrase there.  Matching ignores
case, accepts any short run of spaces, punctuation or line breaks between
the words ("quick brown" finds "quick, brown"), treats straight and curly
apostrophes alike and matches whole words only ("cat" does not find
"concatenate").  The occurrence nearest the caret wins; one before the
caret wins a tie, since people mostly correct what they just said.  "Select
from A to B" takes A nearest the caret and the first B after it (or the
nearest B before it, said the other way round).  The found offsets become
character moves on a clone of the range, whose text is read back and
compared before `Select()`, because providers disagree on whether CR LF is
one character or two.  When the read-back disagrees, `Select()` fails, or
the field has no TextPattern but `text_before_caret` still works, the action
thread walks there with the arrow keys instead (Shift held to select; one
Left first collapses an existing selection to its start; CR LF counts as one
press; at most 250 presses, about six seconds).  Password boxes are never
read, and a field with no readable text gets a plain error.

## Windows and apps

`list_windows` follows the Alt+Tab rules (visible, not cloaked, not a tool
or no-activate window unless it is an app window, last active popup of its
root owner), skips the shell and Sightline, and reports the real process
behind UWP frame windows.  `activate` restores a minimised window
asynchronously, then: a zero-length `SendInput` mouse event (PowerToys'
FancyZones trick, which makes this process the last to receive input) and
`SetForegroundWindow`; `AttachThreadInput` to the foreground thread; finally
`SwitchToThisWindow`.  It reports success only when the window is really in
front.  Close, minimise, maximise and restore post `WM_SYSCOMMAND`, like the
caption buttons; a refused post to an administrator window becomes the
elevated-window message.

`window_at(x, y)` names the app window under one point without the list:
`WindowFromPhysicalPoint`, its root, then its root owner, so a dialog or
popup an app owns stands for the app's window (unless that window is hidden
or minimised).  Sightline's own windows are looked through: the hit test
already passes click-through overlays, and over the dock or a chip it walks
the z-order beneath them.  The desktop and the taskbar give None.  It sends
other apps no messages and costs about 15 us, against 0.3-1 ms for
`list_windows`, so vision's reading scroll calls it on the vision thread.

Apps come from Start menu shortcuts (26 ms for 200 here) and the shell's
AppsFolder through `Shell.Application` (940 ms for 292 apps here; PowerShell
`Get-StartApps` takes 1.4 s and is the fallback).  PowerShell and osk.exe
are started by full path under the Windows folder (`apps.system_exe`, with
`Sysnative` for a 32-bit Python on 64-bit Windows), never by bare name,
which Windows would look for in the working folder first (review S-10).  Uninstallers, readmes and
help links are dropped.  The merged list is cached in
`%LOCALAPPDATA%\Sightline\desktop\apps.json` and rescanned in the background
at most hourly (checked on foreground changes).  Launching nudges input
first and calls `AllowSetForegroundWindow(ASFW_ANY)`, so the new window
opens in front instead of flashing in the taskbar, then `ShellExecute` on a
short-lived STA thread.

Matching (`matching.py`): exact name, aliases ("files" is File Explorer,
"terminal" is Terminal then PowerShell then Command Prompt, "outlook" picks
the new Outlook before the classic one), word-prefix and token matches,
then rapidfuzz `WRatio` for anything over three letters.  Window matching
also knows process names ("word" is `WINWORD.EXE`) and title substrings
("inbox").

## On-Screen Keyboard and Magnifier

`keyboard.show` writes `HKCU\Software\Microsoft\Osk` and starts `osk.exe`
through the shell (it has UIAccess, so `CreateProcess` would fail):

| Value | Written | Meaning |
|---|---|---|
| `Mode` | 1 | Text Services Framework `TYPEMODE`: 0 click, 1 hover, 2 scan |
| `HoverPeriod` | `keyboard_hover_ms` | hover time in ms |
| `UseDevice` | 0 | no joystick scanning |
| `Dock` | 1 | docked |
| `WindowLeft`, `WindowTop`, `WindowWidth`, `WindowHeight` | full width, bottom 36 % of the primary work area | position if docking is ignored |

The value names were read from `osk.exe` itself.  The planned empirical
check (change the option in OSK's Options dialog and diff the registry) has
not been run yet, because no windows could be opened on the development
machine at the time (`docs/TESTING.md` has it).
`tools/desktop_osk_probe.py` does it automatically through UI Automation (it
opens the Options dialog and reports which typing mode and hover time OSK
shows), or with `--manual` diffs the registry while a person changes an
option.  The live test checks the docked position.

Before its first change Sightline keeps the values it is about to replace
(review I-01).  osk.exe writes its settings back as it closes, so they are
put back once the keyboard Sightline opened has closed: on `keyboard.hide`,
or as Sightline quits (`DesktopService.stop` closes the keyboard first).  A
value the user never had is removed again.  A keyboard the user opened
themselves is never touched.

`keyboard.hide` posts `SC_CLOSE`.  UIAccess processes run above normal
integrity for User Interface Privilege Isolation (TabTip.exe and ctfmon.exe
are at High here), so the close may be refused; the window is then hidden
instead, and the settings are put back when it does close.  For the same reason clicks on OSK keys may be dropped; hover mode
needs only pointer movement, which `SetCursorPos` always delivers, and a
blink on the keyboard gets "The on-screen keyboard types when you rest the
pointer on a key" instead of the administrator message.

Magnifier is driven by its shortcuts: Windows+Plus starts it or zooms in,
Windows+Minus zooms out, Windows+Esc turns it off (`MagUIClass` tells
whether it runs).

## Actions

All of ARCHITECTURE.md section 7.  Details beyond the table there:

* **Aim.**  `mouse.click`, `mouse.down`, `mouse.up`, `mouse.drag_toggle`,
  `mouse.scroll` and `mouse.scroll_start` without `x` and `y` but with an
  `aim_t` use `ctx.vision.aim_point(aim_t)`, looked up when the action runs
  (vision is created after the desktop).  The pointer moves there first, so
  the highlight shows what is about to be hit, then the action runs as if
  the point had been given.  No vision, no `aim_point`, None, an exception
  or a non-finite point all mean the cursor.  `ClickPerformed.aim_t` is the
  action's `aim_t`.  `element.click` ranks elements whose names normalise
  to the best match's name ("Save" twice) by distance to the aim point,
  else to the cursor.
* `mouse.click` stops continuous scrolling first.  While a drag is held, a
  single left click drops it instead (so "blink to pick up, blink to drop"
  works however vision maps the gesture).  Returns where it clicked and
  whether it snapped.
* `mouse.scroll` moves the pointer to `x, y` first when given (the wheel
  scrolls what is under the pointer).
* `mouse.move` `to` accepts center, top, bottom, left, right and pairs such
  as "top left" (10 % in from the edges of the current monitor).
* `key.press` also accepts `key` for `combo`; `repeat` holds modifiers once.
* `element.click` clicks the centre; when another window covers it, a left
  single click falls back to the UI Automation pattern.
* `window.switch` to the app already in front moves to its next window.
* `window.next` / `window.previous` walk a snapshot of the window order for
  4 s, so repeating the command cycles through every window.
* `system.volume` `amount` is in percent; each volume key press is 2 points,
  so the default 2 is one press.  Keys (not an audio API) so Windows shows
  its volume flyout.
* `web.go` / `web.search` use the foreground browser, else the most recent
  browser window, with Ctrl+L, typing and Enter.  A search that looks like
  an address ("python.org") is typed as "? python.org", which forces a
  search in Chrome, Edge and Firefox.  With no browser open, the default
  browser opens the address or a Google search.  `web.go` opens only web
  pages and e-mail links (`core.urls.web_address`: http, https, mailto, or
  no scheme for https); `file:`, `ms-settings:`, `search-ms:`, paths and the
  like are refused with "Sightline only opens web pages and e-mail links."
  before anything is typed or handed to the shell (review S-09).
* `key.press` takes an optional `hwnd`: that window is brought to the front
  first, or the action fails with "That window is no longer open".  Undo
  uses it so a reopened tab cannot land in another app.
* `login.fill` presses the fill shortcut of the password manager chosen in
  settings (Sign-ins), or the keys entered there, which win.  It needs a
  text field with focus.  Keys the shared risk policy counts as hard to undo
  (Alt+F4, Delete, Ctrl+W and the rest) are refused rather than pressed:
  they are pressed here, not through `key.press`, so the countdown would
  never see them (review S-04).  Defaults, from the vendors' pages (October 2026):

  | Manager | Keys | What they do |
  |---|---|---|
  | Bitwarden | Ctrl+Shift+L | fills the last-used login (bitwarden.com/help/keyboard-shortcuts) |
  | 1Password | Ctrl+\ | "Fill in Browser"; needs the 1Password app running (support.1password.com/keyboard-shortcuts) |
  | LastPass | Alt+Page Up | "fill in next login"; third-party lists only, no LastPass page found |
  | Keeper | Alt+K | opens KeeperFill; Shift+Enter there fills the chosen login (docs.keeper.io) |
  | Dashlane | Ctrl+Shift+L | opens the Dashlane pop-up (support.dashlane.com) |
  | Proton Pass | none | Proton documents no shortcut; the user assigns one in the browser's extension shortcuts and enters it in Sightline |

* `window.place` (internal, `history=False`) puts a window back to a saved
  `GetWindowPlacement` result plus its exact window rectangle, asynchronously
  (`WPF_ASYNCWINDOWPLACEMENT`, `SWP_ASYNCWINDOWPOS`) so a hung window cannot
  hang the action thread.

**Undo.**  Core remembers each action the user caused and calls its `undo`
with the handler's result.  Reversals registered here:

| Action | Undone by |
|---|---|
| `window.minimize` | bringing the window back to the front, which restores it as it was (maximised or not) |
| `window.maximize`, `window.restore` | the opposite caption command, from the state captured before |
| `window.snap` | `window.place` with the placement captured before (or `window.maximize` if it was maximised) |
| `mouse.scroll` | the same number of notches the other way, at the same point |
| `key.press` Ctrl+W or Ctrl+F4 in a browser | Ctrl+Shift+T in that browser window |
| `key.hold` | `key.release` |
| `text.type` from speech | `speech.scratch` (speech keeps the dictation record and checks the caret has not moved) |
| `window.switch`, `window.next`, `window.previous` | switching back to the window that was in front |
| `desktop.show`, `magnifier.toggle` | the same action again |
| `system.volume`, `magnifier.zoom` | the opposite direction by the same amount; mute again for mute |
| `system.media` play or pause | play or pause again |

`history=False` (never "the last thing done"): `mouse.move`,
`mouse.scroll_stop`, `key.release_all`, `keyboard.show`, `.hide`,
`.toggle` and `window.place`.  Everything else, clicks, `window.close`,
`app.open`, `web.*`, `login.fill` and the text selection actions
included, has no reversal, and core says so.  The selection actions stay in
history on purpose: if "undo that" skipped them, it would scratch the
dictation before them with backspaces, and the first backspace would delete
the selected words.  A drop at the end of a drag is not undone with Ctrl+Z
(as the review suggested) because the commonest drag is a text selection,
where Ctrl+Z would undo an earlier edit instead.

Errors are `ActionError` with plain sentences: "I could not find an app
called Spotify", "I could not find a window called Spotify", "I could not
find "Save" on the screen", "I could not find "zebra" near "quick"",
"Windows blocks Sightline from controlling apps running as administrator.
Windows Voice Access or a helper can handle this window.", and so on.

## System checks

`Desktop.system_checks()` reads the registry only (0.3-0.9 ms here) and
returns, in order:

| id | Reads | Fix link |
|---|---|---|
| `camera_privacy` | `ConsentStore\webcam` `Value` in HKLM ("Camera access"), HKCU ("Let apps access your camera") and HKCU `NonPackaged` ("Let desktop apps access your camera"); the `AllowCamera` policy | `ms-settings:privacy-webcam` |
| `microphone_privacy` | the same three switches under `ConsentStore\microphone` | `ms-settings:privacy-microphone` |
| `camera_sharing` | nothing it can read: where Windows 11 stores the per-camera "Allow multiple apps to use camera at the same time" switch is undocumented, so `ok=None` with directions; before build 26100 `ok=False` (one app per camera) | `ms-settings:camera` |
| `uac_prompts` | `EnableLUA`, `ConsentPromptBehaviorAdmin` or `ConsentPromptBehaviorUser` (by account type, from the token's elevation type), `PromptOnSecureDesktop` | `ms-settings:easeofaccess-speechrecognition` (Windows has no Settings page for User Account Control; the remedy offered is Voice Access) |
| `voice_access_signin` | `voiceaccess` in `HKLM\...\Accessibility` `Configuration`, the list of assistive technologies started on the sign-in screen (community guides agree; Microsoft does not document it); `ATs\VoiceAccess` for whether Windows has it | `ms-settings:easeofaccess-speechrecognition` |
| `keep_awake` | Sightline's own setting; a password-protected screen saver (the policy key wins over the user's); the `InactivityTimeoutSecs` lock policy | `ms-settings:powersleep`, or `ms-settings:lockscreen` for the screen saver |

"Deny" turns a privacy switch off; a missing value is Windows' default, on.
A dimmed (secure desktop) permission prompt cannot be seen or answered by
Sightline; an undimmed one runs as administrator, so Sightline cannot press
its buttons either.  `SetThreadExecutionState` keeps the display on but, as
documented, does not stop a screen saver, which is why a locking screen
saver fails `keep_awake`.  A check that cannot read its values returns
`ok=None` with a sentence saying so.

## Timings

Measured on the development machine (i5-12400, Remote Desktop session,
1920x1080 at 125 %) with `tools/desktop_bench.py` and the probes above.
Only read-only measurements were possible there (no windows could be
opened), so Notepad, Chrome with page content and Edge timings come from
the live tests, which write them to `out/desktop-live-timings.json`
(`docs/TESTING.md`).

| What | Time |
|---|---|
| Clickables, taskbar (20 elements) | 14-16 ms |
| Clickables, File Explorer folder window (125 candidates) | 115-125 ms (255 ms first call) |
| Clickables, Claude desktop app, Electron (175-183 candidates) | 46-130 ms |
| Clickables, Chrome browser UI only (31) | 17-20 ms |
| Converting results to `ElementInfo` | about 10 us per element |
| `element_at` (ElementFromPoint with cache) | 1.3-2 ms (12 ms cold) |
| `GetFocusedElementBuildCache` (one focus poll) | 0.7-2 ms (10 ms cold); 5 Hz is under 1 % of a core |
| `GetGUIThreadInfo` | 1.2 us |
| `nearest_clickable`, 157 elements / 3000 elements | 7 us / 38 us |
| `clickables_near`, 157 elements, radius 60 / 150 / 230 px | 4 / 12 / 21 us (median) |
| `clickables_near`, 3000 elements, radius 60 / 150 / 230 px | 47 / 160 / 289 us (median), 0.7 ms worst |
| Finding a phrase in 6000 characters (select, range, not found) | 0.1 / 0.25 / 0.1 ms |
| `system_checks()` | 0.3-0.9 ms |
| Selecting text in Notepad and browsers | from the live run |
| Building the grid index, 3000 elements | 3 ms |
| `list_windows` | 1.0 ms cold, 0.4 ms warm |
| `window_at` (one point, root owner, `WindowInfo`) | 22 us first call, 15 us warm |
| Monitors | 0.4 ms first, 0.2 ms refresh |
| App match over 264 apps | 3 ms |
| `text_before_caret`, Notepad and browsers | from the live run |

## Windows limits and what users can do

* **Administrator windows.**  UIPI drops input from a normal process into an
  elevated one, silently.  Sightline checks the integrity level of the
  window under the click (mouse) or in front (keys) and says "Windows blocks
  Sightline from controlling apps running as administrator.  Windows Voice
  Access or a helper can handle this window."  Running Sightline itself as
  administrator is deliberately not suggested (review risk R3): the whole
  app, camera and microphone included, would run with full rights.  The
  On-Screen Keyboard, which has UIAccess, can still type into such a window.
  The planned fix is a signed UIAccess build installed in Program Files
  (needs a code-signing certificate and a native launcher, since
  python.exe's manifest cannot be changed).
* **UAC prompts, Ctrl+Alt+Delete and the lock screen** run on the secure
  desktop, which no ordinary process can see or drive.  Sightline detects it
  and says "Windows is showing a secure screen, such as a permission
  prompt.  Sightline cannot control it."  An undimmed prompt runs as
  administrator, so Sightline cannot press its buttons either.  The
  `uac_prompts` system check explains this on the set-up page.  Ctrl+Alt+
  Delete and Windows+L cannot be injected by any app.
* **Sign-in screen.**  Sightline is not running there.  Windows Voice Access
  can start before sign-in (the `voice_access_signin` check); other options
  are Windows Hello face sign-in (needs an infrared camera), automatic
  sign-in for a single-user home PC, or the On-Screen Keyboard at sign-in.
* **Apps that ignore part-notch wheel events.**  Turn off "Smooth
  continuous scrolling"; one-shot scrolls always send whole notches.
* **Games and remote desktop clients** read scan codes and raw input, which
  Sightline sends; full-screen games may still clip or recentre the cursor.
* **Hung apps** are skipped for UI Automation, and every query has a
  timeout, so a frozen app cannot freeze Sightline.
* **Screen off, sleep and the lock screen.**  With "Keep the screen on while
  you are at the computer" (`keep_awake`, on by default) the event thread
  holds `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED |
  ES_DISPLAY_REQUIRED)`, so a quiet spell of reading or listening does not
  end at a sign-in screen the user cannot pass.  Since the security review
  (S-13) it holds it only while someone is there (`AwakePolicy` in
  `service.py`): once the camera has seen a face, the request is let go when
  it has seen none for `keep_awake_minutes` (5 by default, 1 to 60), so an
  empty room locks as usual, and taken again when a face comes back.
  Before any face is seen (no camera, voice only) the setting alone
  decides, as the attention gate lets everything through without a camera.
  A machine-inactivity lock set by group policy, or a screen saver that
  asks for a password, still applies; the `keep_awake` system check reports
  both.
* **Running as administrator.**  If Sightline itself runs elevated, a
  notice at start says it does not need to (review S-14): what the
  microphone hears could otherwise reach administrator windows.

## Testing

* `.venv\Scripts\python.exe -m pytest tests/desktop` runs 272 unit tests
  (parser, event building with a recording SendInput, snapping and the
  spatial index, matching, handlers against the core fakes including aim,
  undo through the real action queue, text editing and sign-ins, phrase
  search and arrow-key walks, the system checks against a fake registry,
  scrolling, EDID, elevation logic, app cache, `window_at` against stubbed
  Win32 calls).  Nothing touches the
  desktop; the live tests are skipped unless asked for.
* `.venv\Scripts\python.exe -m pytest tests/desktop -m desktop -s` runs the
  live tests under the desktop lock: exact cursor moves on every monitor;
  scan codes and extended flags seen by a low-level hook that swallows the
  keys; Notepad on a temporary file (focus and editable detection, Unicode
  and emoji typing read back through UI Automation, select all and copy to
  the clipboard, `text.select`, `text.select_range` and `text.caret` checked
  through the clipboard and the text before the caret, the hover highlight
  snapping to the File menu, `clickables_near`, opening the Edit menu by
  name, snapping the window left and undoing it with `window.place`,
  `window_at` naming the window at its centre, clickables, `element_at`,
  `window_at` and `text_before_caret` timings); Explorer and
  taskbar clickables; Chrome and Edge with a
  throwaway profile and a generated page (clickables, click by name, typing
  into a field, switching windows by name); the On-Screen Keyboard in hover
  mode, docked and closed.  They skip rather than touch a Notepad or
  keyboard the user already has open, and close what they open.
* `tools\desktop_bench.py` (read-only timings) and
  `tools\desktop_osk_probe.py` (On-Screen Keyboard settings check).

## Reused work

| Source | Licence | Used for |
|---|---|---|
| Microsoft PowerToys, `FancyZonesLib/WindowUtils.cpp` `SwitchToWindow` | MIT | restore, empty `SendInput` mouse event, then `SetForegroundWindow` (`windows.py`) |
| hunt-and-peck (zsims) | none published, so the idea only, no code | enabled and on-screen elements with Invoke, Toggle, SelectionItem or ExpandCollapse, invoked in that order (`uia.py`) |
| talonhub/community `core/keys` | MIT | spoken key and symbol names (`keys.py`, vocabulary only) |
| Raymond Chen, "Which windows appear in the Alt+Tab list?" | blog post (algorithm) | window filter (`windows.py`) |
| Microsoft TSF `TYPEMODE` (Softkbdc.h) | documentation | On-Screen Keyboard `Mode` values |
| comtypes, rapidfuzz | MIT | used directly |
| uiautomation, pywinctl, pynput | Apache-2.0, BSD-3, LGPL-3.0 | evaluated, not imported (below) |

Why not those three directly: uiautomation calls
`SetProcessDpiAwareness(PerMonitorDpiAware)` (v1) when it is imported, which
would win over the app's per-monitor v2 if anything imported it first, and
its element wrappers fetch each property with its own cross-process call
instead of one cache request.  pywinctl's `getAllWindows` returned 50
windows here (tool windows and cloaked ones included) where the Alt+Tab
rules give 6, and its `activate` is a bare `SetForegroundWindow`, which the
foreground lock refuses for a background process.  pynput's keyboard
controller raises for characters outside the Basic Multilingual Plane (no
emoji), maps keys through the calling thread's layout rather than the
foreground window's, and keeps no record of held keys for a panic release;
being LGPL-3.0, its code cannot be copied in to fix that.  So the module
uses the comtypes-generated `UIAutomationClient` interfaces and ctypes
directly.
