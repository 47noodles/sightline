"""Speech test set: command and dictation utterances, rendered to WAV with the Windows SAPI voices.

SAPI only ever writes WAV files here (SetOutputToWaveFile); nothing is played
through a sound device.

    python tools/speech_testset.py [--out out/speech_testset] [--quick]

Writes ``manifest.json`` and 16 kHz mono WAVs: each utterance once per voice
(Microsoft David and Zira) at a varied speaking rate, plus a noisy copy of
each (pink noise or background babble at 12-18 dB SNR).

The utterance lists are also imported by the tests and the benchmark.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "out" / "speech_testset"
SAMPLE_RATE = 16000
VOICES = ("Microsoft David Desktop", "Microsoft Zira Desktop")

# --------------------------------------------------------------------------- utterances

# What the user says.  For WER the reference is this text; for the end-to-end
# tests the expected Actions are whatever ``feed_text`` produces for it.
COMMANDS: list[str] = [
    # mouse
    "click",
    "double click",
    "right click",
    "middle click",
    "triple click",
    "drag",
    "drop",
    "scroll down",
    "scroll up",
    "scroll down three",
    "scroll up five",
    "scroll left",
    "scroll right two",
    "scroll down a bit",
    "keep scrolling down",
    "stop scrolling",
    "scroll faster",
    "move mouse to the center",
    "nudge left",
    "nudge up three",
    "click save",
    "click file",
    "right click edit",
    "double click documents",
    # keys
    "press enter",
    "enter",
    "tab",
    "tab three times",
    "press control shift t",
    "press escape",
    "escape",
    "backspace",
    "backspace four",
    "delete",
    "down five",
    "up",
    "left three",
    "right",
    "page down",
    "page up",
    "home",
    "end",
    "press alt f four",
    "control c",
    "control v",
    "control z",
    "press windows d",
    "press f five",
    "press alpha",
    "press air bat cap",
    "press shift tab",
    "press space",
    "press comma",
    "press question mark",
    "hold shift",
    "release shift",
    "release all",
    "alt tab",
    "shift tab twice",
    # text editing
    "select all",
    "select word",
    "select line",
    "delete word",
    "delete line",
    "go to end of line",
    "go to start of line",
    "undo",
    "undo three times",
    "redo",
    "copy",
    "cut",
    "paste",
    "save",
    "find",
    "copy that",
    "paste that",
    "select word left",
    "go to top",
    "go to bottom",
    "word left",
    "word right three",
    "bold",
    "find invoice",
    "save as",
    # dictation control
    "type hello world",
    "spell air bat cap",
    "spell alpha bravo charlie",
    "scratch that",
    "new line",
    "new paragraph",
    "cap london",
    "all caps hello",
    "snake user name",
    "camel get value",
    "kebab main menu",
    "type see you tomorrow",
    "say good morning",
    "title the great gatsby",
    "constant max size",
    # windows and apps
    "open notepad",
    "open google chrome",
    "open file explorer",
    "switch to chrome",
    "switch to notepad",
    "close window",
    "minimise window",
    "maximise window",
    "snap left",
    "snap right",
    "show desktop",
    "next window",
    "previous window",
    "restore window",
    "launch chrome",
    "start notepad",
    "task view",
    "start menu",
    # browser
    "go back",
    "go forward",
    "new tab",
    "close tab",
    "next tab",
    "previous tab",
    "reopen tab",
    "refresh",
    "go to google dot com",
    "go to wikipedia dot org",
    "search for weather in london",
    "search for cheap flights to paris",
    "address bar",
    "reload the page",
    "bookmark this",
    "zoom in",
    "go to tab two",
    # system
    "volume up",
    "volume down three",
    "mute",
    "play",
    "pause",
    "next track",
    "previous track",
    "volume up five",
    # Sightline
    "pause eyes",
    "resume eyes",
    "eyes only",
    "head only",
    "recenter",
    "dwell on",
    "dwell off",
    "dwell right click",
    "show numbers",
    "hide numbers",
    "show grid",
    "hide grid",
    "zoom",
    "show keyboard",
    "hide keyboard",
    "magnifier",
    "what can I say",
    "settings",
    "calibrate",
    "dictation mode",
    "command mode",
    "go to sleep",
    "stop",
    "cancel",
    "quit sightline",
]

# Said while an overlay is open: (utterance, active_overlay).
OVERLAY_COMMANDS: list[tuple[str, str]] = [
    ("twelve", "labels"),
    ("three", "labels"),
    ("click seven", "labels"),
    ("right click four", "labels"),
    ("five", "grid"),
    ("click", "grid"),
    ("back", "grid"),
    ("next", "setup"),
    ("skip", "calibration"),
    ("continue", "setup"),
]

DICTATION: list[str] = [
    "I will be there in about ten minutes",
    "Thanks for your help with the report yesterday",
    "Can we move the meeting to Thursday afternoon",
    "The quick brown fox jumps over the lazy dog",
    "Please remember to buy milk eggs and bread",
    "My appointment is at three thirty on Friday",
    "I think the new design looks much better than the old one",
    "Let me know if you have any questions",
    "The weather has been lovely all week",
    "We need to finish the project before the end of the month",
    "Happy birthday I hope you have a wonderful day",
    "The train was delayed by twenty minutes this morning",
    "Could you send me the latest version of the document",
    "I am looking forward to the holidays",
    "She said the results would be ready by Monday",
    "Our team won the match on Saturday",
    "The doctor told me to rest for a few days",
    "Dinner will be ready at seven o'clock",
    "I could not find my keys anywhere",
    "This is a test of the dictation system",
    "Hello comma how are you question mark",
    "See you soon full stop",
    "The price went up by fifteen percent last year",
    "Please call me back when you get this message",
    "I have uploaded the photos to the shared folder",
    "Remind me to water the plants tomorrow",
    "The library opens at nine in the morning",
    "We should plan a trip to the mountains this summer",
    "My favourite book is about a boy who learns magic",
    "It was great to catch up with you last week",
    "The meeting has been moved to the large conference room",
    "I would like to order two coffees and a muffin",
    "There are four hundred people on the waiting list",
    "Can you check whether the printer is working",
    "Our flight leaves early on Sunday morning",
    "I really enjoyed the concert last night",
    "The kids are going to their grandparents for the weekend",
    "Do not forget to bring your passport",
    "The new software update fixed most of the problems",
    "I am sorry I missed your call earlier",
    "We have a lot of work to do before the deadline",
    "The garden looks beautiful in the spring",
    "Please let the team know that I will be late",
    "He has been learning to play the piano for two years",
    "Thank you for the lovely flowers",
    "I need to renew my driving licence next month",
    "The museum is closed on public holidays",
    "Let us meet outside the station at noon",
    "My computer has been running slowly lately",
    "Good morning everyone and welcome to the meeting",
]

# Paraphrases that are not in the grammar, labelled with the command they mean.
# Used to tune and test the semantic fallback (held out from its index).
PARAPHRASES: list[tuple[str, str]] = [
    ("go down a bit", "mouse.scroll"),
    ("move down the page", "mouse.scroll"),
    ("show me more of the page below", "mouse.scroll"),
    ("go back up the page", "mouse.scroll"),
    ("get rid of this window", "window.close"),
    ("shut this window", "window.close"),
    ("close this program", "window.close"),
    ("make this window bigger", "window.maximize"),
    ("make the window fill the whole screen", "window.maximize"),
    ("hide this window", "window.minimize"),
    ("send this window to the taskbar", "window.minimize"),
    ("make it louder", "system.louder"),
    ("turn up the sound", "system.louder"),
    ("i can't hear it turn it up", "system.louder"),
    ("make it quieter please", "system.quieter"),
    ("turn the sound down a little", "system.quieter"),
    ("silence the sound", "system.mute"),
    ("open a fresh tab", "browser.new_tab"),
    ("get rid of this tab", "browser.close_tab"),
    ("go to the previous page", "browser.back"),
    ("take me back a page", "browser.back"),
    ("reload this page", "browser.refresh"),
    ("load the page again", "browser.refresh"),
    ("highlight everything", "edit.select_all"),
    ("select the whole thing", "edit.select_all"),
    ("take that back", "edit.undo"),
    ("undo the last change", "edit.undo"),
    ("put the copied text here", "edit.paste"),
    ("copy this to the clipboard", "edit.copy"),
    ("save my work", "edit.save"),
    ("save the file now", "edit.save"),
    ("jump to the top of the page", "edit.doc_start"),
    ("go all the way to the bottom", "edit.doc_end"),
    ("remove the last word", "edit.delete_word"),
    ("erase the previous word", "edit.delete_word"),
    ("what commands can i use", "ui.help.show"),
    ("how do i use this", "ui.help.show"),
    ("put numbers on everything", "ui.labels.show"),
    ("label the buttons", "ui.labels.show"),
    ("bring up the keyboard", "keyboard.show"),
    ("i need the on screen keyboard", "keyboard.show"),
    ("stop following my eyes", "pointer.pause"),
    ("turn off eye tracking", "pointer.pause"),
    ("start following my eyes again", "pointer.resume"),
    ("play the next song", "system.next_track"),
    ("skip this song", "system.next_track"),
    ("pause the music", "system.play_pause"),
    ("open the settings", "ui.settings.show"),
    ("remove what i just said", "speech.scratch"),
    ("delete the last thing i dictated", "speech.scratch"),
    ("magnify the screen", "magnifier.toggle"),
    ("go to the end of this line", "edit.line_end"),
    ("move to the start of the line", "edit.line_start"),
    ("switch to the other window", "window.next"),
    ("minimise everything", "desktop.show"),
    ("the pointer is in the wrong place", "ui.calibrate"),
]

# Things people say that are not commands.  The semantic fallback must reject them.
NON_COMMANDS: list[str] = [
    "i went down to the shops yesterday",
    "the window in the kitchen is broken",
    "can you believe how loud that was",
    "my brother lives in the next town",
    "she closed the door behind her",
    "we watched a film about space",
    "the music at the party was great",
    "i love the colour of that car",
    "what time does the bus leave",
    "please pass the salt",
    "the cat is sleeping on the sofa",
    "he scored three goals in the second half",
    "it is raining again",
    "let's have pasta for dinner",
    "my phone battery is nearly dead",
    "the meeting went really well",
    "i need a new pair of shoes",
    "they are moving house next month",
    "the book was better than the film",
    "do you want a cup of tea",
    "the river flooded after the storm",
    "my favourite colour is blue",
    "we should go for a walk later",
    "the children are playing outside",
    "how was your weekend",
    "the shop closes at five",
    "i forgot my umbrella at work",
    "the new restaurant is very popular",
    "turn left at the traffic lights",
    "the page was torn out of the book",
    "he saved money for a new bike",
    "she copied her friend's homework",
    "the volume of the tank is ten litres",
    "we scrolled through old photos",
    "open the door for the dog",
    "the tab at the restaurant was expensive",
    "close to the station there is a park",
    "keep going straight on",
    "the eyes of the portrait follow you",
    "settings on the oven are confusing",
    "i'll be back in a minute",
    "go and ask your mother",
    "this is the best day ever",
    "wow",
    "hmm let me think",
    "sorry what was that",
    "no not that one",
    "yes please",
    "maybe tomorrow",
    "i don't know",
]

# --------------------------------------------------------------------------- generation

_PS_SCRIPT = r"""
param([string]$Jobs)
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
foreach ($line in [System.IO.File]::ReadAllLines($Jobs)) {
  if (-not $line) { continue }
  $parts = $line -split "`t"
  $synth.SelectVoice($parts[0])
  $synth.Rate = [int]$parts[1]
  $synth.SetOutputToWaveFile($parts[3], $fmt)
  $synth.Speak($parts[2])
  $synth.SetOutputToNull()
}
$synth.Dispose()
"""


def render_sapi(jobs: list[tuple[str, int, str, Path]]) -> None:
    """Render (voice, rate, text, path) jobs to WAV files with SAPI.  Never touches an audio device."""
    if not jobs:
        return
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "render.ps1"
        script.write_text(_PS_SCRIPT, "utf-8")
        job_file = Path(tmp) / "jobs.tsv"
        lines = []
        for voice, rate, text, path in jobs:
            path.parent.mkdir(parents=True, exist_ok=True)
            safe = text.replace("\t", " ").replace("\n", " ")
            lines.append(f"{voice}\t{rate}\t{safe}\t{path}")
        job_file.write_text("\n".join(lines), "utf-8")
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Jobs", str(job_file)],
            check=True,
            capture_output=True,
        )


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        frames = w.readframes(w.getnframes())
        sr = w.getframerate()
        channels = w.getnchannels()
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if sr != SAMPLE_RATE:
        raise ValueError(f"{path}: expected {SAMPLE_RATE} Hz, got {sr}")
    return audio


def write_wav(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(audio, -1.0, 1.0)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((pcm * 32767.0).astype(np.int16).tobytes())


def pad(audio: np.ndarray, lead_ms: int = 300, tail_ms: int = 900) -> np.ndarray:
    """Silence around the speech, as a microphone would capture it."""
    trimmed = trim(audio)
    return np.concatenate([np.zeros(SAMPLE_RATE * lead_ms // 1000, np.float32), trimmed, np.zeros(SAMPLE_RATE * tail_ms // 1000, np.float32)])


def trim(audio: np.ndarray, threshold: float = 0.003) -> np.ndarray:
    idx = np.nonzero(np.abs(audio) > threshold)[0]
    if len(idx) == 0:
        return audio
    return audio[max(0, idx[0] - 160) : idx[-1] + 160]


def pink_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    white = rng.standard_normal(n + 1).astype(np.float32)
    spectrum = np.fft.rfft(white)
    freqs = np.arange(len(spectrum), dtype=np.float32)
    freqs[0] = 1.0
    spectrum /= np.sqrt(freqs)
    pink = np.fft.irfft(spectrum, n + 1)[:n].astype(np.float32)
    return pink / (np.std(pink) + 1e-9)


def add_noise(speech: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    voiced = speech[np.abs(speech) > 0.003]
    p_speech = float(np.mean(voiced**2)) if len(voiced) else 1e-6
    p_noise = float(np.mean(noise**2)) + 1e-12
    scale = np.sqrt(p_speech / (p_noise * 10 ** (snr_db / 10)))
    return (speech + noise[: len(speech)] * scale).astype(np.float32)


def generate(out: Path, *, quick: bool = False, seed: int = 7) -> Path:
    rng = random.Random(seed)
    nrng = np.random.default_rng(seed)
    raw_dir = out / "raw"
    items: list[dict] = []
    jobs: list[tuple[str, int, str, Path]] = []

    def plan(kind: str, text: str, overlay: str | None = None) -> None:
        for voice in VOICES:
            rate = rng.choice((-2, 0, 0, 2, 3)) if kind != "dictation" else rng.choice((-1, 0, 1, 2))
            vid = "david" if "David" in voice else "zira"
            idx = len(items)
            raw = raw_dir / f"{kind}_{idx:04d}_{vid}.wav"
            jobs.append((voice, rate, text, raw))
            items.append({"kind": kind, "text": text, "overlay": overlay, "voice": vid, "rate": rate, "raw": str(raw)})

    commands = COMMANDS[::4] if quick else COMMANDS
    dictation = DICTATION[::5] if quick else DICTATION
    for text in commands:
        plan("command", text)
    for text, overlay in OVERLAY_COMMANDS:
        plan("command", text, overlay)
    for text in dictation:
        plan("dictation", text)
    render_sapi(jobs)

    # Babble: a few dictation sentences mixed together, long enough for any clip.
    babble_parts = [read_wav(Path(it["raw"])) for it in items if it["kind"] == "dictation"][:6]
    babble = np.zeros(SAMPLE_RATE * 12, np.float32)
    for part in babble_parts:
        offset = int(nrng.integers(0, SAMPLE_RATE * 2))
        seg = np.tile(part, int(np.ceil(len(babble) / max(1, len(part)))) + 1)[: len(babble) - offset]
        babble[offset:] += seg
    babble /= np.std(babble) + 1e-9

    manifest: list[dict] = []
    for i, it in enumerate(items):
        clean = pad(read_wav(Path(it["raw"])))
        base = dict(it)
        base.pop("raw")
        clean_path = out / "clean" / f"{i:04d}.wav"
        write_wav(clean_path, clean)
        manifest.append({**base, "id": f"{i:04d}", "noise": "clean", "snr_db": None, "path": str(clean_path.relative_to(out)), "seconds": len(clean) / SAMPLE_RATE})
        if i % 2 == 0:
            noise = pink_noise(len(clean), nrng)
            label, snr = "pink", float(rng.choice((12, 15, 18)))
        else:
            start = int(nrng.integers(0, len(babble) - len(clean))) if len(babble) > len(clean) else 0
            noise = np.resize(babble[start:], len(clean))
            label, snr = "babble", float(rng.choice((12, 15, 18)))
        noisy = add_noise(clean, noise, snr)
        noisy_path = out / "noisy" / f"{i:04d}.wav"
        write_wav(noisy_path, noisy)
        manifest.append({**base, "id": f"{i:04d}n", "noise": label, "snr_db": snr, "path": str(noisy_path.relative_to(out)), "seconds": len(noisy) / SAMPLE_RATE})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), "utf-8")
    return out / "manifest.json"


def load_manifest(out: Path = DEFAULT_OUT) -> list[dict]:
    path = out / "manifest.json"
    data = json.loads(path.read_text("utf-8"))
    for item in data:
        item["abspath"] = str(out / item["path"])
    return data


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--quick", action="store_true", help="a quarter of the commands and a fifth of the dictation")
    args = parser.parse_args(argv)
    path = generate(args.out, quick=args.quick)
    data = json.loads(path.read_text("utf-8"))
    print(f"wrote {len(data)} clips to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
