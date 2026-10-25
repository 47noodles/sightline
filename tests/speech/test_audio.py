"""Resampling, the file source and the capture queue.  No audio device is opened here."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from sightline.speech.audio import BLOCK_MS, Block, BlockQueue, FileSource, Resampler, read_wav

SR = 16000


def sine(rate: int, freq: float, seconds: float, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def dominant(x: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.argmax(spectrum) * rate / len(x))


@pytest.mark.parametrize("rate", [48000, 44100, 32000, 22050, 8000])
def test_resampler_keeps_pitch_and_level(rate: int) -> None:
    src = sine(rate, 1000.0 if rate > 8000 else 500.0, 1.0)
    out = Resampler(rate, SR).process(src)
    assert abs(len(out) - SR) <= 2
    mid = out[2000:-2000]
    assert dominant(mid, SR) == pytest.approx(1000.0 if rate > 8000 else 500.0, abs=10)
    assert float(np.sqrt(np.mean(mid**2))) == pytest.approx(0.5 / np.sqrt(2), rel=0.05)


def test_resampler_removes_what_16k_cannot_hold() -> None:
    out = Resampler(48000, SR).process(sine(48000, 12000.0, 0.5))
    assert float(np.sqrt(np.mean(out[1000:-1000] ** 2))) < 0.02


def test_resampler_streams_without_seams() -> None:
    src = sine(48000, 440.0, 1.0)
    whole = Resampler(48000, SR).process(src)
    r = Resampler(48000, SR)
    parts = np.concatenate([r.process(src[i : i + 480]) for i in range(0, len(src), 480)])
    n = min(len(whole), len(parts))
    assert np.max(np.abs(whole[:n] - parts[:n])) < 1e-3


def test_file_source_blocks(tmp_path: Path) -> None:
    blocks: list[Block] = []
    FileSource(blocks.append).play(np.ones(SR // 2, np.float32), silence_after_ms=100)
    assert sum(len(b.samples) for b in blocks) == SR // 2 + SR // 10
    assert all(len(b.samples) == SR * BLOCK_MS // 1000 for b in blocks)
    times = [b.t_end for b in blocks]
    assert times == sorted(times)
    assert times[1] - times[0] == pytest.approx(BLOCK_MS / 1000, abs=1e-6)


def test_file_source_resamples(tmp_path: Path) -> None:
    blocks: list[Block] = []
    FileSource(blocks.append).play(sine(48000, 440.0, 1.0), 48000)
    assert abs(sum(len(b.samples) for b in blocks) - SR) <= 2


def test_read_wav(tmp_path: Path) -> None:
    path = tmp_path / "x.wav"
    data = (sine(SR, 300.0, 0.25) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())
    audio, rate = read_wav(path)
    assert rate == SR
    assert len(audio) == len(data)
    assert float(np.max(np.abs(audio))) == pytest.approx(0.5, abs=0.01)


def test_block_queue_drops_oldest_when_full() -> None:
    q = BlockQueue(max_seconds=0.1)  # 5 blocks
    for i in range(8):
        q.put(Block(np.zeros(320, np.float32), float(i)))
    got = []
    while (b := q.get(timeout=0.01)) is not None:
        got.append(b.t_end)
    assert got == [3.0, 4.0, 5.0, 6.0, 7.0]
    q.put(Block(np.zeros(1, np.float32), 9.0))
    q.clear()
    assert q.get(timeout=0.01) is None


def test_microphone_list_is_stable() -> None:
    from sightline.speech.audio import list_microphones

    try:
        first = list_microphones()
    except Exception as e:  # no PortAudio on this machine
        pytest.skip(f"PortAudio unavailable: {e}")
    second = list_microphones()
    assert [m.id for m in first] == [m.id for m in second]
    assert len({m.id for m in first}) == len(first)
    assert sum(m.default for m in first) <= 1
    for m in first:
        assert ":" in m.id and m.name
        assert "Sound Mapper" not in m.name
