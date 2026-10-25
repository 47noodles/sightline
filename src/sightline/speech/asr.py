"""Speech recognisers behind one small interface.

Two libraries are wrapped:

* moonshine-voice (MIT): Moonshine streaming models.  Audio is fed while the
  user speaks, so most of the work is done by the time they stop.
* sherpa-onnx (Apache-2.0): NVIDIA Parakeet TDT and Moonshine through
  offline recognisers (the whole utterance is decoded at the end), and
  streaming transducers (fed as audio arrives).

A ``Session`` covers one utterance: ``accept`` audio while speech goes on,
``partial`` for live captions and endpoint decisions, ``finish`` for the
final text.  Sessions are used from one thread (the speech thread).
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000

# Key-term biasing (the user's vocabulary).  sherpa-onnx adds this much to each token of a
# hotword during beam search.  With 15 unfamiliar names (docs/modules/speech.md), 0.5 took the
# 0.6B from 25 to 35 of 56 name sentences and left commands understood at 88.7% (88.6% with
# beam search alone); 2.0 cut commands understood to 82.1%.
HOTWORDS_SCORE = 0.5
MAX_HOTWORDS = 200
MAX_HOTWORD_CHARS = 60


class Session(Protocol):
    def accept(self, samples: np.ndarray) -> None:
        """Add 16 kHz mono float32 audio."""

    def partial(self) -> str:
        """Best text so far.  May run the model, so call it sparingly."""

    def finish(self) -> str:
        """Final text for the utterance.  The session is finished afterwards."""


class Engine(Protocol):
    id: str
    streaming: bool
    biasing: bool  # set_hotwords has an effect (the recogniser was built for key-term biasing)

    def session(self) -> Session: ...

    def warm_up(self) -> None: ...

    def set_hotwords(self, words: list[str]) -> None: ...

    def close(self) -> None: ...


def hotword_variants(words: Iterable[str]) -> list[str]:
    """The vocabulary as the recogniser should look for it: each entry as written, plus a capitalised form.

    Parakeet writes names with capitals, so "siobhan" said at the start of a name
    is emitted as "Siobhan"; biasing towards both costs nothing.  Only letters,
    digits, apostrophes, hyphens and spaces reach the recogniser: it reads "/",
    ":" and "@" as its own syntax (review S-16), and the vocabulary file is
    written by hand.
    """
    out: list[str] = []
    seen: set[str] = set()
    for word in words:
        text = " ".join("".join(c if c.isalnum() or c in "'’-" else " " for c in str(word)).split())
        if not text or len(text) > MAX_HOTWORD_CHARS:
            continue
        for form in (text, " ".join(w[:1].upper() + w[1:] for w in text.split())):
            if form not in seen:
                seen.add(form)
                out.append(form)
    return out[:MAX_HOTWORDS]


def bpe_vocab_for(tokens: Path, cache_dir: Path) -> Path:
    """A sentencepiece-style vocabulary (piece, tab, score) made from a model's tokens.txt.

    sherpa-onnx needs one to turn hotwords into the model's tokens; the Parakeet
    exports ship only tokens.txt.  Their pieces are in merge order with single
    characters last, so minus the id ranks the merges the way the tokenizer did.
    Written once per tokens file into ``cache_dir``.
    """
    data = tokens.read_bytes()
    path = cache_dir / f"bpe-{hashlib.sha256(data).hexdigest()[:16]}.vocab"
    if path.exists():
        return path
    lines = []
    for line in data.decode("utf-8").splitlines():
        piece, _, index = line.rpartition(" ")
        if piece and index.lstrip("-").isdigit():
            lines.append(f"{piece}\t{-int(index)}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", "utf-8")
    os.replace(tmp, path)
    return path


def _threads() -> int:
    # Leave cores for vision and the GUI; inference scales poorly past four threads for short audio.
    return max(1, min(4, (os.cpu_count() or 4) // 2))


# --------------------------------------------------------------------------- moonshine-voice


class MoonshineEngine:
    """Moonshine streaming models through moonshine-voice.

    The library's own voice activity detection runs inside each stream; we
    start a stream when our VAD hears speech and stop it at our endpoint, so
    its segmentation only matters inside one utterance.
    """

    streaming = True
    biasing = True  # moonshine-voice has its own key-term biasing

    def __init__(self, engine_id: str, model_dir: Path, arch_name: str, *, update_interval: float = 0.2) -> None:
        from moonshine_voice import ModelArch, Transcriber

        self.id = engine_id
        arch = getattr(ModelArch, arch_name)
        self._transcriber = Transcriber(model_path=str(model_dir), model_arch=arch, update_interval=update_interval)
        self._lock = threading.Lock()

    def session(self) -> MoonshineSession:
        return MoonshineSession(self._transcriber)

    def warm_up(self) -> None:
        s = self.session()
        s.accept(np.zeros(SAMPLE_RATE // 2, np.float32))
        s.finish()

    def set_hotwords(self, words: list[str]) -> None:
        terms = [w.replace(",", " ") for w in words if w.strip()]
        try:
            self._transcriber.set_keyterms(terms[:100] or None)
        except Exception as e:  # older models cannot bias; not worth failing over
            log.debug("Moonshine keyterms not set: %s", e)

    def close(self) -> None:
        self._transcriber.close()


class MoonshineSession:
    def __init__(self, transcriber) -> None:  # noqa: ANN001 - moonshine_voice.Transcriber
        self._stream = transcriber.create_stream()
        self._stream.start()
        self._text = ""
        self._done = False

    def accept(self, samples: np.ndarray) -> None:
        if not self._done and len(samples):
            self._stream.add_audio(samples.tolist(), SAMPLE_RATE)

    def _join(self, transcript) -> str:  # noqa: ANN001
        if transcript is None:
            return self._text
        parts = [line.text.strip() for line in transcript.lines if line.text and line.text.strip()]
        return " ".join(parts)

    def partial(self) -> str:
        if self._done:
            return self._text
        self._text = self._join(self._stream.update_transcription())
        return self._text

    def finish(self) -> str:
        if self._done:
            return self._text
        self._done = True
        try:
            self._text = self._join(self._stream.stop())
        finally:
            self._stream.close()
        return self._text


# --------------------------------------------------------------------------- sherpa-onnx offline


class SherpaOfflineEngine:
    """Whole-utterance recognisers (Parakeet TDT, Moonshine) through sherpa-onnx.

    Transducers built with ``biasing`` decode with beam search so that the
    user's vocabulary can be passed as hotwords with each utterance.  Without
    a vocabulary they use greedy search, the configuration the bake-off chose.
    """

    streaming = False

    def __init__(self, engine_id: str, recognizer, *, biasing: bool = False) -> None:  # noqa: ANN001 - sherpa_onnx.OfflineRecognizer
        self.id = engine_id
        self._recognizer = recognizer
        self.biasing = biasing
        self._hotwords = ""

    @classmethod
    def transducer(
        cls,
        engine_id: str,
        model_dir: Path,
        *,
        prefix: str = "",
        suffix: str = ".int8.onnx",
        biasing: bool = False,
        cache_dir: Path | None = None,
    ) -> SherpaOfflineEngine:
        import sherpa_onnx

        tokens = model_dir / "tokens.txt"
        extra: dict[str, object] = {"decoding_method": "greedy_search"}
        if biasing:
            if cache_dir is None:
                from sightline.core import paths

                cache_dir = paths.data_dir() / "speech"
            extra = {
                "decoding_method": "modified_beam_search",
                "modeling_unit": "bpe",
                "bpe_vocab": str(bpe_vocab_for(tokens, cache_dir)),
                "hotwords_score": HOTWORDS_SCORE,
            }
        rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(model_dir / f"{prefix}encoder{suffix}"),
            decoder=str(model_dir / f"{prefix}decoder{suffix}"),
            joiner=str(model_dir / f"{prefix}joiner{suffix}"),
            tokens=str(tokens),
            num_threads=_threads(),
            model_type="nemo_transducer",
            **extra,
        )
        return cls(engine_id, rec, biasing=biasing)

    @classmethod
    def moonshine(cls, engine_id: str, model_dir: Path) -> SherpaOfflineEngine:
        import sherpa_onnx

        rec = sherpa_onnx.OfflineRecognizer.from_moonshine_v2(
            encoder=str(model_dir / "encoder_model.ort"),
            decoder=str(model_dir / "decoder_model_merged.ort"),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=_threads(),
        )
        return cls(engine_id, rec)

    def decode(self, audio: np.ndarray) -> str:
        hotwords = self._hotwords
        # Hotwords only ever go to a beam-search recogniser: sherpa-onnx 2.0 crashes the
        # process when a greedy-search recogniser is given hotwords it can encode.
        stream = None
        if hotwords and self.biasing:
            try:
                stream = self._recognizer.create_stream(hotwords)
            except Exception as e:  # a vocabulary the recogniser cannot read must not stop recognition
                log.warning("The vocabulary could not be used (%s); recognising without it", type(e).__name__)
                if self._hotwords == hotwords:
                    self._hotwords = ""
        if stream is None:
            stream = self._recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio)
        self._recognizer.decode_stream(stream)
        return stream.result.text.strip()

    def session(self) -> OfflineSession:
        return OfflineSession(self)

    def warm_up(self) -> None:
        self.decode(np.zeros(SAMPLE_RATE, np.float32))

    def set_hotwords(self, words: list[str], *, score: float | None = None) -> None:
        """Bias the next utterances towards these words (needs ``biasing``; otherwise nothing happens).

        ``score`` replaces ``HOTWORDS_SCORE`` for these words (the bench compares boosts with it).
        """
        if self.biasing:
            suffix = f" :{score:g}" if score is not None else ""
            self._hotwords = "/".join(w + suffix for w in hotword_variants(words))  # read once per utterance, so no lock is needed

    def close(self) -> None:
        self._recognizer = None


class OfflineSession:
    """Buffers audio; ``partial`` and ``finish`` decode everything so far, reusing the last result if nothing changed."""

    def __init__(self, engine: SherpaOfflineEngine) -> None:
        self._engine = engine
        self._chunks: list[np.ndarray] = []
        self._samples = 0
        self._decoded_at = -1
        self._text = ""

    def accept(self, samples: np.ndarray) -> None:
        if len(samples):
            self._chunks.append(np.asarray(samples, dtype=np.float32))
            self._samples += len(samples)

    def _audio(self) -> np.ndarray:
        if len(self._chunks) > 1:
            self._chunks = [np.concatenate(self._chunks)]
        return self._chunks[0] if self._chunks else np.zeros(0, np.float32)

    def partial(self) -> str:
        if self._decoded_at != self._samples:
            self._text = self._engine.decode(self._audio()) if self._samples else ""
            self._decoded_at = self._samples
        return self._text

    def finish(self) -> str:
        return self.partial()


# --------------------------------------------------------------------------- sherpa-onnx streaming


class SherpaOnlineEngine:
    """Streaming transducers through sherpa-onnx (fed as audio arrives)."""

    streaming = True
    biasing = False

    def __init__(self, engine_id: str, model_dir: Path, *, suffix: str = ".int8.onnx", model_type: str = "") -> None:
        import sherpa_onnx

        self.id = engine_id
        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(model_dir / "tokens.txt"),
            encoder=str(model_dir / f"encoder{suffix}"),
            decoder=str(model_dir / f"decoder{suffix}"),
            joiner=str(model_dir / f"joiner{suffix}"),
            num_threads=_threads(),
            decoding_method="greedy_search",
            model_type=model_type,
        )

    def session(self) -> OnlineSession:
        return OnlineSession(self._recognizer)

    def warm_up(self) -> None:
        s = self.session()
        s.accept(np.zeros(SAMPLE_RATE, np.float32))
        s.finish()

    def set_hotwords(self, words: list[str]) -> None:
        pass

    def close(self) -> None:
        self._recognizer = None


class OnlineSession:
    def __init__(self, recognizer) -> None:  # noqa: ANN001
        self._rec = recognizer
        self._stream = recognizer.create_stream()
        self._done = False
        self._text = ""

    def _drain(self) -> None:
        while self._rec.is_ready(self._stream):
            self._rec.decode_stream(self._stream)

    def accept(self, samples: np.ndarray) -> None:
        if self._done or not len(samples):
            return
        self._stream.accept_waveform(SAMPLE_RATE, np.asarray(samples, dtype=np.float32))
        self._drain()

    def partial(self) -> str:
        if not self._done:
            self._text = self._rec.get_result(self._stream).strip()
        return self._text

    def finish(self) -> str:
        if self._done:
            return self._text
        self._done = True
        # Right context so the last word is emitted, then flush.
        self._stream.accept_waveform(SAMPLE_RATE, np.zeros(int(0.4 * SAMPLE_RATE), np.float32))
        self._stream.input_finished()
        self._drain()
        self._text = self._rec.get_result(self._stream).strip()
        return self._text


# --------------------------------------------------------------------------- timing helper


@dataclass(slots=True)
class Timed:
    text: str
    ms: float


def timed(fn, *args) -> Timed:  # noqa: ANN001
    started = time.perf_counter()
    text = fn(*args)
    return Timed(text, (time.perf_counter() - started) * 1000.0)
