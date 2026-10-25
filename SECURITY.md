# Security policy

Sightline turns what a webcam sees and a microphone hears into real
clicks, key presses and app launches on a Windows PC.  A security problem
here can mean that someone else's voice, or a video playing nearby, acts
as the user.  Reports are very welcome.

## Reporting a problem

Please report security problems privately, through GitHub's private
vulnerability reporting:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.

Or go straight to
<https://github.com/47noodles/sightline/security/advisories/new>.

Please do not open a public issue, pull request or discussion for a
security problem until it has been fixed.

Helpful things to include: what you did, what happened, what you expected,
the Sightline commit you ran (`git rev-parse HEAD`), your Windows version,
and whether a camera and microphone were in use.  Sightline's log
(`%LOCALAPPDATA%\Sightline\logs\sightline.log`) does not record what was
said, typed, clicked or visited, but read it before you attach it anywhere.

Sightline is a small alpha project with no releases yet, so only the
latest commit on `main` is supported.

## What counts

Anything that lets something other than the user make Sightline act, or
that leaks what the user says, types or looks at.  For example:

* sound that is not the owner's voice running commands, typing, or
  getting past the countdown before hard-to-undo actions;
* getting around the safety settings that voice must not turn off
  (`ui.confirm_risky`, `speech.attention_gate`, `speech.mouth_gate`);
* window titles, element names or web pages that make Sightline do
  something the user did not ask for;
* user files (`commands.toml`, `aliases.txt`, `vocabulary.txt`,
  `settings.json`) that crash or hang Sightline, or stop it starting;
* model downloads that accept a file other than the pinned one;
* speech, typed text, window titles or addresses reaching the log, the
  screen in a password field, or anywhere off the computer.

## Known boundaries

* **The same Windows user.**  A program running as the same user can edit
  Sightline's settings and user files, its app cache and its model files,
  and can send input itself.  That is the normal boundary for an app that
  runs as the user, so it is not treated as a vulnerability.
* **Administrator windows and the secure desktop.**  Windows blocks input
  from ordinary apps into apps running as administrator, and nothing can
  drive the secure desktop (permission prompts, the lock screen).
  Sightline says so rather than working around it, and should not be run
  as administrator.
* **Out of scope:** attackers who are already administrator or SYSTEM,
  physical access to an unlocked PC, and bugs inside third-party libraries
  (ONNX Runtime, sherpa-onnx, MediaPipe, OpenCV, Qt) unless Sightline hands
  them data an attacker can shape.

## What has been reviewed

An independent security review of the code is published in
[`docs/reviews/2026-10-25-security-review.md`](docs/reviews/2026-10-25-security-review.md),
with a status note under every finding.  Items still open there include an
echo check that would tell the owner's voice from the PC's own speakers,
re-checking model files at start-up, and pinned dependency hashes.
