"""Live capture from a real input device.  Opt-in: set SIGHTLINE_LIVE_AUDIO=1.

Only records; nothing here plays sound.  It reads the capture side of the
VB-Audio Point virtual cable ("CABLE Output (VB-Audio Point)"), as on the
development machine, so no physical microphone is needed, and it skips when
that cable is not installed.  Playing speech into the cable and checking what
is recognised is a manual check (docs/TESTING.md).
"""

from __future__ import annotations

import os
import threading
import time

import numpy as np
import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(os.environ.get("SIGHTLINE_LIVE_AUDIO") != "1", reason="set SIGHTLINE_LIVE_AUDIO=1 to open a real input device"),
]

CABLE = "CABLE Output (VB-Audio Point)"


def test_capture_from_the_virtual_cable() -> None:
    from sightline.speech.audio import SAMPLE_RATE, Block, Microphone, list_microphones

    mics = [m for m in list_microphones() if m.name.startswith(CABLE)]
    if not mics:
        pytest.skip(f"{CABLE} is not installed")
    blocks: list[Block] = []
    statuses: list[tuple[str, str | None]] = []
    got = threading.Event()

    def sink(block: Block) -> None:
        blocks.append(block)
        if sum(len(b.samples) for b in blocks) >= SAMPLE_RATE:
            got.set()

    mic = Microphone(sink, lambda name, error: statuses.append((name, error)))
    mic.start(mics[0].id)
    try:
        assert got.wait(5.0), f"no audio arrived; status {statuses}"
    finally:
        mic.stop()
    assert mic.device_name.startswith(CABLE)
    samples = np.concatenate([b.samples for b in blocks])
    assert samples.dtype == np.float32
    assert np.all(np.abs(samples) <= 1.0)
    times = [b.t_end for b in blocks]
    assert times == sorted(times)
    # Blocks arrive at the real rate: one second of audio in roughly one second.
    span = times[-1] - times[0]
    assert 0.5 < span * SAMPLE_RATE / max(1, len(samples) - len(blocks[0].samples)) < 2.0
    assert time.perf_counter() - times[-1] < 2.0
