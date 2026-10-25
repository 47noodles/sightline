"""Helpers shared by the speech tests.

Kept out of ``conftest.py`` on purpose: several test folders have a
``conftest.py``, and ``from conftest import ...`` picks up whichever one
pytest imported last, so it breaks when the whole suite runs.  This module
name is unique, so test files can import it directly.
"""

from __future__ import annotations

import zlib

import numpy as np


class BagOfWords:
    """A stand-in embedder: shared words make vectors close.  Enough to test decisions without the model."""

    name = "bag-of-words"

    def embed(self, texts):
        out = np.zeros((len(texts), 512), np.float32)
        for row, text in enumerate(texts):
            for word in text.split():
                out[row, zlib.crc32(word.encode()) % 512] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.maximum(norms, 1e-9)


def bag_of_words_index(grammar):
    from sightline.speech.semantic import SemanticIndex, build_entries

    specs = [s for s in grammar.specs.values() if s.semantic]
    entries = build_entries(specs, {s.id: grammar.canonical_phrases(s.id, 6) for s in specs})
    return SemanticIndex(BagOfWords(), entries, threshold=0.6, margin=0.04)


class FakeMicrophone:
    """Stands in for audio.Microphone so no test opens a real device."""

    opened: list[str] = []

    def __init__(self, sink, on_status=None) -> None:
        self.sink = sink

    def start(self, mic_id: str) -> None:
        FakeMicrophone.opened.append(mic_id)

    def switch(self, mic_id: str) -> None:
        FakeMicrophone.opened.append(mic_id)

    def stop(self) -> None:
        pass
