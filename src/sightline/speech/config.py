"""Speech settings.  Owned by the speech subsystem; extend freely, keep labels plain English."""

from __future__ import annotations

from dataclasses import dataclass

from sightline.core.settings_base import setting

from .models import engine_choices


@dataclass
class SpeechConfig:
    microphone: str = setting("", label="Microphone", group="Microphone", hidden=True)
    engine: str = setting(
        "auto",
        label="Speech recogniser",
        help="Recommended picks the most accurate recogniser this computer runs quickly.",
        group="Recognition",
        advanced=True,
        choices=engine_choices(),
    )
    command_endpoint_ms: int = setting(
        250,
        label="Pause that ends a command",
        help="Shorter reacts faster; too short can cut you off mid-command.",
        group="Recognition",
        minimum=100,
        maximum=1000,
        unit="ms",
    )
    learned_command_pause: str = setting(
        "Not learned yet",
        label="Your pauses inside commands",
        help="Sightline learns how long you pause in the middle of a command, and waits that long for the rest of one.  It never waits less than the pause that ends a command.",
        group="Recognition",
        readonly=True,
    )
    learn_pauses: bool = setting(
        True,
        label="Learn how long you pause inside commands",
        help="Waits longer for the rest of a command only when what you have said so far is not a whole command.",
        group="Recognition",
        advanced=True,
    )
    learn_aliases: bool = setting(
        True,
        label="Offer to remember how you say commands",
        help="When Sightline mishears a command and you then give it another way, it offers to remember what it heard.  Say yes to keep it.",
        group="Recognition",
    )
    dictation_endpoint_ms: int = setting(
        650,
        label="Pause that ends dictation",
        group="Recognition",
        minimum=250,
        maximum=2000,
        unit="ms",
    )
    speech_threshold: float = setting(
        0.5,
        label="Voice detection sensitivity",
        help="Lower picks up quieter speech but also more background noise.",
        group="Microphone",
        minimum=0.2,
        maximum=0.9,
        step=0.05,
        advanced=True,
    )
    wake_phrase: str = setting("wake up", label="Wake phrase", group="Listening")
    sleep_phrase: str = setting("go to sleep", label="Sleep phrase", group="Listening")
    attention_gate: bool = setting(
        True,
        label="Ignore speech when you're not facing the screen",
        help="While you look away, nothing is typed and only stop, cancel, sleep, wake, pause or resume eyes, undo that and help work, so talking to someone else does not run things.  Without a camera there is no check.",
        group="Listening",
        protected=True,
    )
    mouth_gate: bool = setting(
        False,
        label="Also require your mouth to move",
        help="Ignores speech from a television or another person in the room.  Set-up can check whether this works for you.",
        group="Listening",
        protected=True,
    )
    semantic_matching: bool = setting(
        True,
        label="Understand commands said in other words",
        help="Uses a small language model to match phrasing like 'go down a bit' to 'scroll down'.",
        group="Recognition",
    )
    spoken_punctuation: bool = setting(True, label="Say punctuation by name", help="'comma', 'full stop', 'new line'.", group="Dictation")
    auto_capitalize: bool = setting(True, label="Capitalise sentences automatically", group="Dictation")
    smart_spacing: bool = setting(True, label="Add spaces between dictated phrases", group="Dictation")
    auto_punctuation: bool = setting(
        False,
        label="Let the recogniser add punctuation",
        help="Keeps the commas and full stops the recogniser guesses.  Off means you say punctuation yourself.",
        group="Dictation",
    )
