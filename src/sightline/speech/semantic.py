"""Semantic fallback: maps paraphrased commands ("get rid of this window") to intents with sentence embeddings.

The index holds, for each command that allows it, a few canonical phrases
from its templates plus hand-written seed paraphrases.  A query is accepted
only when its best cosine similarity clears a threshold and beats the best
different command by a margin; both were tuned on held-out paraphrases and
everyday sentences (``tools/speech_bench.py --semantic``) for precision first.

Two embedders are supported: model2vec static embeddings (MIT, about 0.1 ms
per query) and small ONNX sentence transformers (MiniLM, a few ms).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from .grammar import CommandSpec

log = logging.getLogger(__name__)


class Embedder(Protocol):
    name: str

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Unit-length rows, one per text."""


def _unit(x: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    return (x / np.maximum(norms, 1e-9)).astype(np.float32)


class Model2VecEmbedder:
    """model2vec StaticModel loaded from a local folder (config.json, model.safetensors, tokenizer.json)."""

    def __init__(self, folder: Path, name: str = "model2vec") -> None:
        from model2vec import StaticModel

        self.name = name
        self._model = StaticModel.from_pretrained(str(folder))

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        return _unit(np.asarray(self._model.encode(list(texts)), dtype=np.float32))


class OnnxSentenceEmbedder:
    """A sentence-transformers model exported to ONNX (MiniLM family), mean-pooled."""

    def __init__(self, model_path: Path, tokenizer_path: Path, name: str = "minilm", threads: int = 1) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.name = name
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(str(model_path), opts, providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._session.get_inputs()}
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._tokenizer.enable_truncation(64)
        self._tokenizer.enable_padding()

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        enc = self._tokenizer.encode_batch(list(texts))
        ids = np.asarray([e.ids for e in enc], dtype=np.int64)
        mask = np.asarray([e.attention_mask for e in enc], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feeds["token_type_ids"] = np.zeros_like(ids)
        hidden = self._session.run(None, feeds)[0]
        m = mask[..., None].astype(np.float32)
        pooled = (hidden * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1e-9)
        return _unit(pooled)


# Embeddings place opposites close together ("top" and "bottom", "next" and
# "previous").  A candidate whose phrase uses the opposite word from the
# query is skipped, which removes most wrong-command answers.
_OPPOSITES: tuple[tuple[frozenset[str], frozenset[str]], ...] = tuple(
    (frozenset(a.split()), frozenset(b.split()))
    for a, b in (
        ("up upwards above higher raise", "down downwards below lower"),
        ("top beginning start", "bottom end"),
        ("next following forward", "previous last back before"),
        ("louder increase more bigger enlarge maximise maximize", "quieter softer decrease less smaller shrink minimise minimize"),
        ("on enable resume show open start", "off disable pause hide close stop"),
        ("left", "right"),
        ("in", "out"),
        ("copy copied", "paste pasted"),
        ("undo", "redo"),
    )
)


def _conflicts(query_words: set[str], phrase: str) -> bool:
    words = set(phrase.split())
    for a, b in _OPPOSITES:
        if (query_words & a and words & b and not words & a) or (query_words & b and words & a and not words & b):
            return True
    return False


@dataclass(frozen=True, slots=True)
class SemanticHit:
    spec_id: str
    slots: dict[str, Any]
    score: float
    margin: float
    phrase: str  # the index phrase that matched best


@dataclass(frozen=True, slots=True)
class Entry:
    phrase: str
    spec_id: str
    slots: tuple[tuple[str, Any], ...]


class SemanticIndex:
    """Seed phrases per command, embedded once; ``match`` compares one query against all of them."""

    def __init__(self, embedder: Embedder, entries: list[Entry], *, threshold: float, margin: float) -> None:
        self.embedder = embedder
        self.entries = entries
        self.threshold = threshold
        self.margin = margin
        started = time.perf_counter()
        self._matrix = embedder.embed([e.phrase for e in entries]) if entries else np.zeros((0, 1), np.float32)
        self._spec_ids = np.asarray([e.spec_id for e in entries])
        log.info("Semantic index: %d phrases with %s in %.0f ms", len(entries), embedder.name, (time.perf_counter() - started) * 1000)

    def scores(self, query: str) -> list[tuple[float, Entry]]:
        if not self.entries:
            return []
        q = self.embedder.embed([query])[0]
        sims = self._matrix @ q
        order = np.argsort(-sims)
        return [(float(sims[i]), self.entries[i]) for i in order[:40]]

    def match(self, query: str) -> SemanticHit | None:
        words = set(query.split())
        ranked = [(s, e) for s, e in self.scores(query) if not _conflicts(words, e.phrase)]
        if not ranked:
            return None
        best_score, best = ranked[0]
        runner = next((s for s, e in ranked[1:] if e.spec_id != best.spec_id), 0.0)
        # The same command with different slot values (scroll up versus down) also needs a margin.
        same_other = next((s for s, e in ranked[1:] if e.spec_id == best.spec_id and e.slots != best.slots), 0.0)
        margin = best_score - max(runner, same_other)
        if best_score < self.threshold or margin < self.margin:
            return None
        return SemanticHit(best.spec_id, dict(best.slots), best_score, margin, best.phrase)

    def near_misses(self, query: str, *, min_score: float, limit: int = 3) -> list[SemanticHit]:
        """The closest commands, one per command and slot values, without the margin test: "did you mean" candidates."""
        words = set(query.split())
        out: list[SemanticHit] = []
        seen: set[tuple[str, tuple]] = set()
        for score, entry in self.scores(query):
            if score < min_score or len(out) >= limit:
                break
            key = (entry.spec_id, entry.slots)
            if key in seen or _conflicts(words, entry.phrase):
                continue
            seen.add(key)
            out.append(SemanticHit(entry.spec_id, dict(entry.slots), score, 0.0, entry.phrase))
        return out


def build_entries(specs: Sequence[CommandSpec], canonical: dict[str, list[tuple[str, dict[str, Any]]]]) -> list[Entry]:
    """Seeds from each spec plus its canonical template phrases.  Only specs with seeds take part."""
    entries: list[Entry] = []
    for spec in specs:
        if not spec.semantic or spec.exact_only:
            continue
        seen: set[tuple[str, tuple]] = set()
        items: list[tuple[str, dict[str, Any]]] = []
        for seed in spec.semantic:
            items.append(seed if isinstance(seed, tuple) else (seed, {}))
        items.extend(canonical.get(spec.id, []))
        for phrase, slots in items:
            key = (phrase, tuple(sorted(slots.items())))
            if key in seen:
                continue
            seen.add(key)
            entries.append(Entry(phrase, spec.id, tuple(sorted(slots.items()))))
    return entries


def save_entries(path: Path, entries: list[Entry]) -> None:
    path.write_text(json.dumps([[e.phrase, e.spec_id, list(e.slots)] for e in entries], indent=0), "utf-8")
