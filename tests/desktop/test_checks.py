"""The set-up page's system checks, against a fake registry."""

from __future__ import annotations

import time

import pytest

from sightline.core.types import SystemCheck
from sightline.desktop import checks
from sightline.desktop.checks import (
    ACCESSIBILITY,
    CAMERA_POLICY,
    CONSENT,
    CURRENT_VERSION,
    DESKTOP,
    DESKTOP_POLICY,
    HKCU,
    HKLM,
    POLICIES_SYSTEM,
    RegistryUnreadable,
    run_checks,
)

VOICE_ACCESS = (HKLM, rf"{ACCESSIBILITY}\ATs\VoiceAccess", "ATExe")


class FakeRegistry:
    def __init__(self, values: dict[tuple[str, str, str], object] | None = None, refuse: set[str] | None = None) -> None:
        self.values = {(h, p.lower(), n.lower()): v for (h, p, n), v in (values or {}).items()}
        self.refuse = refuse or set()
        self.reads: list[tuple[str, str, str]] = []

    def value(self, hive, path, name):
        self.reads.append((hive, path, name))
        if any(part.lower() in path.lower() for part in self.refuse):
            raise RegistryUnreadable(path)
        return self.values.get((hive, path.lower(), name.lower()))


def healthy(changes: dict | None = None) -> FakeRegistry:
    values = {
        (HKLM, rf"{CONSENT}\webcam", "Value"): "Allow",
        (HKCU, rf"{CONSENT}\webcam", "Value"): "Allow",
        (HKCU, rf"{CONSENT}\webcam\NonPackaged", "Value"): "Allow",
        (HKLM, rf"{CONSENT}\microphone", "Value"): "Allow",
        (HKCU, rf"{CONSENT}\microphone", "Value"): "Allow",
        (HKCU, rf"{CONSENT}\microphone\NonPackaged", "Value"): "Allow",
        (HKLM, CURRENT_VERSION, "CurrentBuildNumber"): "26200",
        (HKLM, POLICIES_SYSTEM, "EnableLUA"): 1,
        (HKLM, POLICIES_SYSTEM, "ConsentPromptBehaviorAdmin"): 5,
        (HKLM, POLICIES_SYSTEM, "ConsentPromptBehaviorUser"): 3,
        (HKLM, POLICIES_SYSTEM, "PromptOnSecureDesktop"): 1,
        VOICE_ACCESS: "VoiceAccess.exe",
        (HKLM, ACCESSIBILITY, "Configuration"): "voiceaccess",
    }
    values.update(changes or {})
    return FakeRegistry(values)


def by_id(results: list[SystemCheck]) -> dict[str, SystemCheck]:
    return {c.id: c for c in results}


def test_every_check_is_present_with_plain_labels_and_settings_links():
    results = run_checks(healthy(), keep_awake_enabled=True, account="admin")
    assert [c.id for c in results] == [
        "camera_privacy", "microphone_privacy", "camera_sharing", "uac_prompts", "voice_access_signin", "keep_awake",
    ]  # fmt: skip
    for c in results:
        assert c.label and c.fix_uri.startswith("ms-settings:")
        assert "—" not in c.label + c.detail and "–" not in c.label + c.detail
        if c.ok is not True:
            assert c.detail.endswith(".")


def test_privacy_switches_are_named_when_off():
    reg = healthy({(HKCU, rf"{CONSENT}\webcam\NonPackaged", "Value"): "Deny"})
    camera = by_id(run_checks(reg, keep_awake_enabled=True, account="admin"))["camera_privacy"]
    assert camera.ok is False
    assert "“Let desktop apps access your camera” is off" in camera.detail
    assert camera.fix_uri == "ms-settings:privacy-webcam"
    reg = healthy({(HKLM, rf"{CONSENT}\microphone", "Value"): "Deny", (HKCU, rf"{CONSENT}\microphone", "Value"): "Deny"})
    mic = by_id(run_checks(reg, keep_awake_enabled=True, account="admin"))["microphone_privacy"]
    assert mic.ok is False
    assert "“Microphone access” and “Let apps access your microphone” are off" in mic.detail
    assert "needs an administrator" in mic.detail


def test_missing_privacy_values_mean_windows_default_on():
    reg = FakeRegistry({VOICE_ACCESS: "VoiceAccess.exe"})
    results = by_id(run_checks(reg, keep_awake_enabled=True, account="admin"))
    assert results["camera_privacy"].ok is True and results["microphone_privacy"].ok is True


def test_camera_policy_and_unreadable_values():
    reg = healthy({(HKLM, CAMERA_POLICY, "AllowCamera"): 0})
    assert by_id(run_checks(reg, keep_awake_enabled=True, account="admin"))["camera_privacy"].ok is False
    refused = healthy()
    refused.refuse = {"ConsentStore"}
    results = by_id(run_checks(refused, keep_awake_enabled=True, account="admin"))
    assert results["camera_privacy"].ok is None and "could not read" in results["camera_privacy"].detail


def test_camera_sharing_cannot_be_read_on_new_windows_and_is_absent_on_old():
    assert by_id(run_checks(healthy(), keep_awake_enabled=True, account="admin"))["camera_sharing"].ok is None
    old = healthy({(HKLM, CURRENT_VERSION, "CurrentBuildNumber"): "22631"})
    sharing = by_id(run_checks(old, keep_awake_enabled=True, account="admin"))["camera_sharing"]
    assert sharing.ok is False and "only one app" in sharing.detail


@pytest.mark.parametrize(
    ("changes", "account", "ok", "words"),
    [
        ({}, "admin", False, "dims the screen"),
        ({(HKLM, POLICIES_SYSTEM, "PromptOnSecureDesktop"): 0}, "admin", False, "cannot press its buttons"),
        ({(HKLM, POLICIES_SYSTEM, "PromptOnSecureDesktop"): 0, (HKLM, POLICIES_SYSTEM, "ConsentPromptBehaviorAdmin"): 2}, "admin", False, "dims the screen"),
        ({(HKLM, POLICIES_SYSTEM, "ConsentPromptBehaviorAdmin"): 0}, "admin", True, ""),
        ({(HKLM, POLICIES_SYSTEM, "EnableLUA"): 0}, "admin", True, ""),
        ({}, "standard", False, "dims the screen"),
        ({(HKLM, POLICIES_SYSTEM, "ConsentPromptBehaviorUser"): 0}, "standard", True, ""),
        ({}, "unprompted", True, ""),
    ],
)
def test_uac_prompt_level(changes, account, ok, words):
    uac = by_id(run_checks(healthy(changes), keep_awake_enabled=True, account=account))["uac_prompts"]
    assert uac.ok is ok
    assert words in uac.detail


def test_voice_access_before_sign_in():
    assert by_id(run_checks(healthy(), keep_awake_enabled=True, account="admin"))["voice_access_signin"].ok is True
    listed = healthy({(HKLM, ACCESSIBILITY, "Configuration"): "osk, VoiceAccess"})
    assert by_id(run_checks(listed, keep_awake_enabled=True, account="admin"))["voice_access_signin"].ok is True
    off = healthy({(HKLM, ACCESSIBILITY, "Configuration"): ""})
    check = by_id(run_checks(off, keep_awake_enabled=True, account="admin"))["voice_access_signin"]
    assert check.ok is False and "Start voice access before you sign in" in check.detail
    missing = healthy({VOICE_ACCESS: None})
    check = by_id(run_checks(missing, keep_awake_enabled=True, account="admin"))["voice_access_signin"]
    assert check.ok is False and "does not have Voice Access" in check.detail


def test_keep_awake_setting_screen_saver_lock_and_inactivity_policy():
    assert by_id(run_checks(healthy(), keep_awake_enabled=True, account="admin"))["keep_awake"].ok is True
    off = by_id(run_checks(healthy(), keep_awake_enabled=False, account="admin"))["keep_awake"]
    assert off.ok is False and "setting is off" in off.detail
    saver = healthy({(HKCU, DESKTOP, "ScreenSaveActive"): "1", (HKCU, DESKTOP, "ScreenSaverIsSecure"): "1",
                       (HKCU, DESKTOP, "SCRNSAVE.EXE"): r"C:\Windows\system32\Bubbles.scr"})  # fmt: skip
    check = by_id(run_checks(saver, keep_awake_enabled=True, account="admin"))["keep_awake"]
    assert check.ok is False and "screen saver" in check.detail and check.fix_uri == "ms-settings:lockscreen"
    # A policy that switches the screen saver off wins over the user's own setting.
    saver.values[(HKCU, DESKTOP_POLICY.lower(), "screensaveactive")] = "0"
    assert by_id(run_checks(saver, keep_awake_enabled=True, account="admin"))["keep_awake"].ok is True
    locked = healthy({(HKLM, POLICIES_SYSTEM, "InactivityTimeoutSecs"): 900})
    check = by_id(run_checks(locked, keep_awake_enabled=True, account="admin"))["keep_awake"]
    assert check.ok is False and "15 minutes" in check.detail


def test_a_failing_check_does_not_hide_the_others(monkeypatch):
    def boom(reg):
        raise RuntimeError("bug")

    monkeypatch.setattr(checks, "camera_sharing", boom)
    results = by_id(run_checks(healthy(), keep_awake_enabled=True, account=lambda: "admin"))
    assert results["camera_sharing"].ok is None
    assert results["camera_privacy"].ok is True


def test_the_real_registry_is_quick_and_read_only():
    started = time.perf_counter()
    results = run_checks(checks.WinRegistry(), keep_awake_enabled=True, account="admin")
    assert (time.perf_counter() - started) < 1.0
    assert len(results) == 6 and all(isinstance(c, SystemCheck) for c in results)
