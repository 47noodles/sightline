"""Read-only checks of the Windows settings Sightline depends on, for the set-up check page.

Every check reads the registry and nothing else, so the page can run them at
any time; together they take a few milliseconds.  Each failing check says
what is wrong in plain words and links the Settings page a helper needs.

Where the values live:

* Camera and microphone privacy: the ``CapabilityAccessManager\\ConsentStore``
  keys behind Settings > Privacy & security.  The machine-wide switch is in
  HKLM, "Let apps access" and "Let desktop apps access" are in HKCU (the
  latter under ``NonPackaged``).  "Deny" means off; a missing value means
  Windows' default, which is on.
* Voice Access before sign-in: the ``Configuration`` list under
  ``HKLM\\...\\Windows NT\\CurrentVersion\\Accessibility``, where Windows keeps
  the assistive technologies that start on the sign-in screen (``voiceaccess``
  for Voice Access; community guides agree, Microsoft does not document it).
* User Account Control: ``EnableLUA``, ``ConsentPromptBehaviorAdmin``,
  ``ConsentPromptBehaviorUser`` and ``PromptOnSecureDesktop``.
* Sharing the camera between apps: Windows 11 24H2 added a switch per camera,
  but where it is stored is not documented, so that check reports ``ok=None``
  and tells the helper where to look.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal, Protocol

from sightline.core.types import SystemCheck

log = logging.getLogger(__name__)

HKLM = "HKLM"
HKCU = "HKCU"

CONSENT = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore"
CAMERA_POLICY = r"SOFTWARE\Policies\Microsoft\Camera"
POLICIES_SYSTEM = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
ACCESSIBILITY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Accessibility"
CURRENT_VERSION = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
DESKTOP = r"Control Panel\Desktop"
DESKTOP_POLICY = r"Software\Policies\Microsoft\Windows\Control Panel\Desktop"

SPEECH_SETTINGS = "ms-settings:easeofaccess-speechrecognition"  # Accessibility > Speech, where Voice Access lives

Account = Literal["admin", "standard", "unprompted"]


class RegistryUnreadable(Exception):
    """Windows refused to let Sightline read a value."""


class Registry(Protocol):
    def value(self, hive: str, path: str, name: str) -> Any:
        """The value, or None when the key or value does not exist.  Raises RegistryUnreadable."""


class WinRegistry:
    """The real registry, read only, always the 64-bit view."""

    def value(self, hive: str, path: str, name: str) -> Any:
        import winreg

        root = winreg.HKEY_LOCAL_MACHINE if hive == HKLM else winreg.HKEY_CURRENT_USER
        try:
            with winreg.OpenKey(root, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                return winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None
        except OSError as e:
            raise RegistryUnreadable(f"{hive}\\{path}\\{name}: {e}") from e


def _quoted(names: list[str]) -> str:
    quoted = [f"“{n}”" for n in names]
    return quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " and " + quoted[-1]


def _denied(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() == "deny"


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _on(value: Any) -> bool:
    return str(value).strip() == "1" if value is not None else False


# --------------------------------------------------------------------------- the checks


def privacy(reg: Registry, device: Literal["camera", "microphone"]) -> SystemCheck:
    store = "webcam" if device == "camera" else "microphone"
    noun = device
    check_id = f"{device}_privacy"
    label = f"Apps can use the {noun}"
    fix = "ms-settings:privacy-webcam" if device == "camera" else "ms-settings:privacy-microphone"
    try:
        if device == "camera" and _int(reg.value(HKLM, CAMERA_POLICY, "AllowCamera"), 1) == 0:
            return SystemCheck(
                check_id, label, False,
                "A policy set by whoever manages this computer turns the camera off.  Only they can turn it back on.",
                fix,
            )  # fmt: skip
        machine = _denied(reg.value(HKLM, rf"{CONSENT}\{store}", "Value"))
        apps = _denied(reg.value(HKCU, rf"{CONSENT}\{store}", "Value"))
        desktop = _denied(reg.value(HKCU, rf"{CONSENT}\{store}\NonPackaged", "Value"))
    except RegistryUnreadable as e:
        log.info("Could not read the %s privacy settings: %s", noun, e)
        return SystemCheck(check_id, label, None, f"Sightline could not read the {noun} privacy settings.", fix)
    off = [name for name, is_off in (
        (f"{noun.capitalize()} access", machine),
        (f"Let apps access your {noun}", apps),
        (f"Let desktop apps access your {noun}", desktop),
    ) if is_off]  # fmt: skip
    if not off:
        return SystemCheck(check_id, label, True, "", fix)
    effect = "see you" if device == "camera" else "hear you"
    them = "it" if len(off) == 1 else "them"
    detail = f"{_quoted(off)} {'is' if len(off) == 1 else 'are'} off in {noun} privacy settings, so Sightline cannot {effect}.  Turn {them} on."
    if machine:
        detail += f"  “{noun.capitalize()} access” needs an administrator."
    return SystemCheck(check_id, label, False, detail, fix)


def camera_sharing(reg: Registry) -> SystemCheck:
    label = "Other apps can share the camera"
    fix = "ms-settings:camera"
    try:
        build = _int(reg.value(HKLM, CURRENT_VERSION, "CurrentBuildNumber"), 0)
    except RegistryUnreadable:
        build = 0
    if 0 < build < 26100:
        return SystemCheck(
            "camera_sharing", label, False,
            "On this version of Windows only one app can use a camera at a time, so a video call can take it "
            "from Sightline.  A second camera for calls avoids that.",
            fix,
        )  # fmt: skip
    return SystemCheck(
        "camera_sharing", label, None,
        "Sightline cannot read this switch.  In Settings, open Bluetooth & devices, then Cameras, choose the camera, "
        "and under Advanced camera options turn on “Allow multiple apps to use camera at the same time”, if it is "
        "there.  Without it, a video call can take the camera from Sightline.",
        fix,
    )  # fmt: skip


def uac_prompts(reg: Registry, account: Account) -> SystemCheck:
    label = "Windows permission prompts"
    try:
        lua = _int(reg.value(HKLM, POLICIES_SYSTEM, "EnableLUA"), 1)
        admin_behaviour = _int(reg.value(HKLM, POLICIES_SYSTEM, "ConsentPromptBehaviorAdmin"), 5)
        user_behaviour = _int(reg.value(HKLM, POLICIES_SYSTEM, "ConsentPromptBehaviorUser"), 3)
        secure = _int(reg.value(HKLM, POLICIES_SYSTEM, "PromptOnSecureDesktop"), 1)
    except RegistryUnreadable:
        return SystemCheck("uac_prompts", label, None, "Sightline could not read the User Account Control settings.", SPEECH_SETTINGS)
    if lua == 0 or account == "unprompted":
        return SystemCheck("uac_prompts", label, True, "", SPEECH_SETTINGS)
    if account == "admin":
        # 0 elevates without asking; 1 and 2 always use the dimmed secure screen.
        behaviour, always_dimmed = admin_behaviour, admin_behaviour in (1, 2)
    else:
        # 0 refuses without asking; 1 always uses the dimmed secure screen.
        behaviour, always_dimmed = user_behaviour, user_behaviour == 1
    if behaviour == 0:
        return SystemCheck("uac_prompts", label, True, "", SPEECH_SETTINGS)
    if always_dimmed or secure != 0:
        detail = (
            "When an app asks for permission to make changes, Windows dims the screen and shows a prompt that "
            "Sightline cannot see or answer.  Someone else will need to answer it, or you can try Windows Voice Access."
        )
    else:
        detail = (
            "When an app asks for permission to make changes, Windows shows a prompt that runs as administrator, so "
            "Sightline cannot press its buttons.  Someone else will need to answer it, or you can try Windows Voice Access."
        )
    return SystemCheck("uac_prompts", label, False, detail, SPEECH_SETTINGS)


def voice_access_before_sign_in(reg: Registry) -> SystemCheck:
    label = "Voice Access starts before sign-in"
    try:
        installed = reg.value(HKLM, rf"{ACCESSIBILITY}\ATs\VoiceAccess", "ATExe")
        configured = reg.value(HKLM, ACCESSIBILITY, "Configuration")
    except RegistryUnreadable:
        return SystemCheck("voice_access_signin", label, None, "Sightline could not read the sign-in screen settings.", SPEECH_SETTINGS)
    if installed is None:
        return SystemCheck(
            "voice_access_signin", label, False,
            "This version of Windows does not have Voice Access, which lets you sign in by voice.  It comes with "
            "Windows 11 version 22H2 and later.",
            "ms-settings:windowsupdate",
        )  # fmt: skip
    names = {part.strip().lower() for part in str(configured or "").split(",")}
    if "voiceaccess" in names:
        return SystemCheck("voice_access_signin", label, True, "", SPEECH_SETTINGS)
    return SystemCheck(
        "voice_access_signin", label, False,
        "Sightline only starts after you sign in.  Windows Voice Access can let you sign in by voice: turn on "
        "“Start voice access before you sign in to your PC” in Speech settings.  It may need an administrator.",
        SPEECH_SETTINGS,
    )  # fmt: skip


def keep_awake(reg: Registry, enabled: bool) -> SystemCheck:
    label = "The screen stays on and unlocked while you read"
    problems: list[str] = []
    fix = "ms-settings:powersleep"
    if not enabled:
        problems.append(
            "Sightline's “Keep the screen on while Sightline is running” setting is off, so the screen can turn off "
            "and Windows can lock while you read or listen."
        )

    def screen_saver(name: str) -> Any:
        policy = reg.value(HKCU, DESKTOP_POLICY, name)  # a policy, when set, wins over the user's choice
        return policy if policy is not None else reg.value(HKCU, DESKTOP, name)

    try:
        saver_on = _on(screen_saver("ScreenSaveActive"))
        saver_locks = _on(screen_saver("ScreenSaverIsSecure"))
        saver = screen_saver("SCRNSAVE.EXE")
        inactivity = _int(reg.value(HKLM, POLICIES_SYSTEM, "InactivityTimeoutSecs"), 0)
    except RegistryUnreadable:
        saver_on = saver_locks = False
        saver, inactivity = None, 0
    if saver_on and saver_locks and saver:
        # SetThreadExecutionState keeps the display on but, as documented, does not stop a screen saver.
        problems.append(
            "A screen saver that asks for your password is on.  Windows starts it even while Sightline keeps the "
            "screen on, so it can lock the computer while you read.  A helper can turn it off from Lock screen settings."
        )
        fix = "ms-settings:lockscreen"
    if inactivity > 0:
        minutes = max(1, round(inactivity / 60))
        problems.append(
            f"A policy locks this computer after {minutes} minute{'s' if minutes != 1 else ''} without keyboard or mouse "
            "use.  Only whoever manages this computer can change it."
        )
    if not problems:
        return SystemCheck("keep_awake", label, True, "", fix)
    return SystemCheck("keep_awake", label, False, "  ".join(problems), fix)


def run_checks(reg: Registry, *, keep_awake_enabled: bool, account: Account | Callable[[], Account]) -> list[SystemCheck]:
    """Every check, in the order the set-up page shows them.  A check that fails to run says so."""
    try:
        kind: Account = account() if callable(account) else account
    except Exception:
        log.exception("Could not tell how Windows asks this user for permission")
        kind = "admin"  # the usual set-up: an administrator Windows asks for consent

    steps: list[tuple[str, str, Callable[[], SystemCheck]]] = [
        ("camera_privacy", "Apps can use the camera", lambda: privacy(reg, "camera")),
        ("microphone_privacy", "Apps can use the microphone", lambda: privacy(reg, "microphone")),
        ("camera_sharing", "Other apps can share the camera", lambda: camera_sharing(reg)),
        ("uac_prompts", "Windows permission prompts", lambda: uac_prompts(reg, kind)),
        ("voice_access_signin", "Voice Access starts before sign-in", lambda: voice_access_before_sign_in(reg)),
        ("keep_awake", "The screen stays on and unlocked while you read", lambda: keep_awake(reg, keep_awake_enabled)),
    ]
    out: list[SystemCheck] = []
    for check_id, label, step in steps:
        try:
            out.append(step())
        except Exception:
            log.exception("System check %s failed", check_id)
            out.append(SystemCheck(check_id, label, None, "Sightline could not check this."))
    return out
