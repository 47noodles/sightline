"""Synthesise Sightline's earcons into ``src/sightline/assets/sounds/*.wav``.

Run with the project interpreter::

    .venv\\Scripts\\python.exe tools\\ui_make_sounds.py

Why synthesised rather than downloaded: the sounds must be soft enough to
hear hundreds of times a day, short enough not to delay anything, and
clearly distinct from each other while the user's eyes are closed.  Building
them from a few bar and bell tones gives that control and leaves no licence
question (the output is released with Sightline under Apache-2.0).

Two families keep them learnable:

* wood taps (marimba-like) for pointer things: armed ticks, clicks, pausing
  and resuming the eyes;
* soft bells for voice things: recognised, not understood, sleep, wake,
  success.

Rising patterns mean on, awake or done; falling patterns mean off, asleep
or not understood.
"""

from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np

SR = 44100
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "sightline" / "assets" / "sounds"


def _t(duration: float) -> np.ndarray:
    return np.arange(int(round(duration * SR))) / SR


def _env(t: np.ndarray, attack: float, decay: float) -> np.ndarray:
    """Linear attack (no click) into an exponential decay."""
    a = np.clip(t / max(attack, 1e-4), 0.0, 1.0)
    return a * np.exp(-np.maximum(t - attack, 0.0) / decay)


def tap(freq: float, duration: float = 0.09, decay: float = 0.022, brightness: float = 1.0) -> np.ndarray:
    """A wooden bar: strong fundamental plus the bar's inharmonic fourth partial, which dies quickly."""
    t = _t(duration)
    out = np.sin(2 * np.pi * freq * t) * _env(t, 0.0015, decay)
    out += 0.16 * brightness * np.sin(2 * np.pi * freq * 3.93 * t) * _env(t, 0.001, decay / 3.5)
    out += 0.07 * brightness * np.sin(2 * np.pi * freq * 2.0 * t) * _env(t, 0.001, decay / 2.0)
    return out


def bell(freq: float, duration: float = 0.45, decay: float = 0.14) -> np.ndarray:
    """A soft electric-piano bell: mostly fundamental, a quiet octave and a faint inharmonic shimmer."""
    t = _t(duration)
    out = np.sin(2 * np.pi * freq * t) * _env(t, 0.004, decay)
    out += 0.22 * np.sin(2 * np.pi * freq * 2.0 * t) * _env(t, 0.003, decay / 1.8)
    out += 0.05 * np.sin(2 * np.pi * freq * 3.01 * t) * _env(t, 0.002, decay / 3.0)
    return out


def hum(freq: float, duration: float = 0.16, decay: float = 0.07) -> np.ndarray:
    """Two slightly detuned low sines: a soft, beating "uh-uh" for errors, never a buzzer."""
    t = _t(duration)
    out = np.sin(2 * np.pi * freq * t) + 0.8 * np.sin(2 * np.pi * freq * 1.059 * t)
    out += 0.15 * np.sin(2 * np.pi * freq * 2.0 * t)
    return out * _env(t, 0.012, decay)


def sequence(*notes: tuple[float, np.ndarray], tail: float = 0.05) -> np.ndarray:
    """Mix notes that start at the given offsets (seconds)."""
    length = max(int(round(start * SR)) + len(sig) for start, sig in notes) + int(tail * SR)
    out = np.zeros(length)
    for start, sig in notes:
        i = int(round(start * SR))
        out[i : i + len(sig)] += sig
    return out


def finish(sig: np.ndarray, peak_db: float) -> np.ndarray:
    sig = sig - np.mean(sig)
    fade = min(len(sig), int(0.012 * SR))
    sig[-fade:] *= np.linspace(1.0, 0.0, fade)
    peak = np.max(np.abs(sig)) or 1.0
    return sig / peak * (10 ** (peak_db / 20.0))


# Note frequencies (equal temperament).
G4, A4, B4, C5, D5, E5, G5, A5, B5, C6, E6 = 392.0, 440.0, 493.9, 523.3, 587.3, 659.3, 784.0, 880.0, 987.8, 1046.5, 1318.5


def build() -> dict[str, np.ndarray]:
    s: dict[str, np.ndarray] = {}
    # Eyes held shut.  Stage 1 (click armed): one clear tap.  Stage 2 (double click armed): a higher pair.
    s["hold_1"] = finish(tap(1046.5, 0.10, 0.030), -11)
    s["hold_2"] = finish(sequence((0.0, tap(1318.5, 0.08, 0.022)), (0.075, tap(1318.5, 0.09, 0.026))), -11)
    # Clicks performed: lower and drier than the armed ticks so "armed" and "done" never blur.
    s["click"] = finish(tap(660.0, 0.08, 0.018, brightness=0.8), -13)
    s["double_click"] = finish(sequence((0.0, tap(660.0, 0.07, 0.016)), (0.07, tap(660.0, 0.08, 0.018))), -13)
    s["right_click"] = finish(sequence((0.0, tap(660.0, 0.07, 0.016)), (0.075, tap(494.0, 0.09, 0.02))), -13)
    s["middle_click"] = finish(sequence((0.0, tap(660.0, 0.07, 0.016)), (0.075, tap(880.0, 0.09, 0.02))), -13)
    # Voice.
    s["recognised"] = finish(sequence((0.0, bell(E5, 0.18, 0.06)), (0.07, bell(B5, 0.32, 0.10))), -17)
    s["not_understood"] = finish(sequence((0.0, bell(B4, 0.2, 0.07)), (0.11, bell(G4 * 1.0595, 0.32, 0.10))), -17)
    s["sleep"] = finish(sequence((0.0, bell(G5, 0.3, 0.09)), (0.13, bell(D5, 0.3, 0.09)), (0.26, bell(G4, 0.5, 0.16))), -17)
    s["wake"] = finish(sequence((0.0, bell(G4, 0.3, 0.09)), (0.12, bell(D5, 0.3, 0.09)), (0.24, bell(G5, 0.5, 0.16))), -17)
    # Eye control paused and resumed: the wood family, falling and rising.
    s["eyes_paused"] = finish(sequence((0.0, tap(784.0, 0.1, 0.03)), (0.11, tap(523.3, 0.14, 0.045))), -13)
    s["eyes_resumed"] = finish(sequence((0.0, tap(523.3, 0.1, 0.03)), (0.11, tap(784.0, 0.14, 0.045))), -13)
    # Something failed.
    s["error"] = finish(sequence((0.0, hum(233.1, 0.13, 0.05)), (0.15, hum(220.0, 0.2, 0.07))), -12)
    # Calibration point collected, and the end of a longer task.
    s["calibration_point"] = finish(bell(E6, 0.16, 0.05), -19)
    s["success"] = finish(
        sequence((0.0, bell(C5, 0.4, 0.12)), (0.09, bell(E5, 0.4, 0.12)), (0.18, bell(G5, 0.4, 0.12)), (0.27, bell(C6, 0.7, 0.22))),
        -17,
    )
    return s


def write_wav(path: Path, sig: np.ndarray) -> None:
    pcm = np.clip(sig * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, sig in build().items():
        write_wav(args.out / f"{name}.wav", sig)
        print(f"{name:18s} {len(sig) / SR * 1000:5.0f} ms  peak {20 * np.log10(np.max(np.abs(sig))):6.1f} dBFS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
