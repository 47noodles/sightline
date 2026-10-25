"""From audio blocks to finished utterances: voice activity, pre-roll, adaptive endpointing.

Runs on the speech thread.  Each 32 ms frame gets a Silero speech
probability.  Speech starts after two speech frames (with ~400 ms of
pre-roll so the first word is not clipped) and ends after a pause whose
length depends on what has been said so far:

* a complete command that nothing longer could extend ("scroll down"):
  the short "early" pause;
* a command that a name could follow ("click", "open"): the command pause;
* dictation, or a command that takes free text ("type ..."): the dictation pause;
* the start of a command that is not finished yet ("scroll", "switch to"):
  at least the pause this person has been heard to make inside commands
  (``pauses.py``).

Pauses inside an utterance (silence, then speech again before the endpoint)
are reported with it, so the service can learn that last value.

When the pause starts, the recogniser decodes what it has (a speculative
decode).  That text picks the pause length, and if the user stays silent it
is the final transcript, so offline recognisers add no time after the pause.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .asr import Engine, Session
from .audio import SAMPLE_RATE, Block
from .vad import FRAME

log = logging.getLogger(__name__)

FRAME_MS = FRAME * 1000 / SAMPLE_RATE  # 32 ms


@dataclass(slots=True)
class EndpointConfig:
    command_ms: int = 250
    dictation_ms: int = 650
    early_ms: int = 125
    incomplete_ms: int = 0  # learned wait for an unfinished command (pauses.py); 0 until learned
    onset: float = 0.5  # speech probability that starts (or continues) speech
    offset: float = 0.35  # below this a frame counts as silence
    start_frames: int = 2
    preroll_ms: int = 400
    speculative_after_ms: int = 64  # silence before the speculative decode
    min_speech_ms: int = 120
    max_utterance_s: float = 30.0
    partial_every_ms: int = 400
    partial_budget_ms: float = 80.0  # skip live partials when a decode costs more than this


@dataclass(slots=True)
class Utterance:
    id: int
    text: str
    audio_ms: float  # from speech start to the end of the last speech frame
    endpoint_ms: float  # silence waited before deciding it had ended
    asr_ms: float  # recogniser time after the pause
    t_speech_end: float  # perf_counter at the end of the last speech frame
    t_ready: float  # perf_counter when the text was ready
    forced: bool = False  # ended by the length limit, not a pause
    timings: dict[str, float] = field(default_factory=dict)
    # perf_counter at the start of the audio kept for this utterance, pre-roll included.  People
    # look at what they mean before they speak, so spoken commands aim at this moment.
    t_speech_start: float = 0.0
    t_voice_start: float = 0.0  # perf_counter at the start of the first frame the VAD called speech
    pauses_ms: tuple[float, ...] = ()  # silences inside the utterance that speech resumed after


Classifier = Callable[[str], int]


class UtterancePipeline:
    def __init__(
        self,
        vad: Callable[[np.ndarray], float],
        engine: Engine,
        classify: Classifier,
        config: EndpointConfig | None = None,
        *,
        on_partial: Callable[[int, str], None] | None = None,
        on_final: Callable[[Utterance], None] | None = None,
        on_speaking: Callable[[bool], None] | None = None,
        next_id: Callable[[], int] | None = None,
    ) -> None:
        self.vad = vad
        self.engine = engine
        self.classify = classify
        self.cfg = config or EndpointConfig()
        self.on_partial = on_partial or (lambda uid, text: None)
        self.on_final = on_final or (lambda u: None)
        self.on_speaking = on_speaking or (lambda speaking: None)
        self._next_id = 1
        self._ids = next_id  # the service numbers typed and spoken utterances in one sequence
        self._buf = np.zeros(0, np.float32)
        self._buf_t0 = 0.0  # perf_counter of the first sample in _buf
        self._preroll: deque[np.ndarray] = deque(maxlen=max(1, int(self.cfg.preroll_ms / FRAME_MS)))
        self.reset()

    # ------------------------------------------------------------------ state

    def reset(self) -> None:
        self._state = "idle"
        self._session: Session | None = None
        self._run = 0
        self._uid = 0
        self._speech_frames = 0
        self._silence_frames = 0
        self._pending: list[np.ndarray] = []
        self._spec_text: str | None = None
        self._endpoint_ms = self.cfg.command_ms
        self._pauses: list[float] = []
        self._t_last_voiced = 0.0
        self._t_start = 0.0
        self._t_voice = 0.0
        self._since_partial_ms = 0.0
        self._partial_cost_ms = 0.0
        self._last_partial = ""
        self._preroll.clear()
        if hasattr(self, "vad") and hasattr(self.vad, "reset"):
            self.vad.reset()

    @property
    def speaking(self) -> bool:
        return self._state != "idle"

    def cancel(self) -> None:
        """Drop the utterance in progress (microphone muted, mode changed)."""
        if self._session is not None:
            try:
                self._session.finish()
            except Exception:
                pass
        was = self.speaking
        self.reset()
        if was:
            self.on_speaking(False)

    # ------------------------------------------------------------------ input

    def feed(self, block: Block) -> None:
        samples = block.samples
        if len(samples) == 0:
            return
        t_first = block.t_end - (len(samples) - 1) / SAMPLE_RATE
        if len(self._buf) == 0:
            self._buf_t0 = t_first
        self._buf = np.concatenate([self._buf, samples]) if len(self._buf) else samples.astype(np.float32, copy=True)
        while len(self._buf) >= FRAME:
            frame = self._buf[:FRAME]
            self._buf = self._buf[FRAME:]
            t_frame_end = self._buf_t0 + (FRAME - 1) / SAMPLE_RATE
            self._buf_t0 += FRAME / SAMPLE_RATE
            self._frame(frame, t_frame_end)

    def flush(self) -> None:
        """End any utterance in progress now (end of a file, microphone closed)."""
        if self._state != "idle":
            self._finalize(forced=True)

    # ------------------------------------------------------------------ frames

    def _frame(self, frame: np.ndarray, t_end: float) -> None:
        prob = self.vad(frame)
        if self._state == "idle":
            self._preroll.append(frame)
            self._speech_frames = self._speech_frames + 1 if prob >= self.cfg.onset else 0
            if self._speech_frames >= self.cfg.start_frames:
                self._begin(t_end)
            return

        voiced = prob >= self.cfg.offset if self._state == "speech" else prob >= self.cfg.onset
        if voiced:
            if self._state == "trailing":
                self._resume()
            self._feed_session([frame])
            self._run += 1
            self._t_last_voiced = t_end
            self._since_partial_ms += FRAME_MS
            if self._since_partial_ms >= self.cfg.partial_every_ms:
                self._since_partial_ms = 0.0
                self._live_partial()
            if (t_end - self._t_start) > self.cfg.max_utterance_s:
                self._finalize(forced=True)
            return

        # silence
        if self._state == "speech":
            self._state = "trailing"
            self._silence_frames = 0
            self._pending = []
            self._spec_text = None
        self._silence_frames += 1
        silence_ms = self._silence_frames * FRAME_MS
        if self._session is not None and getattr(self.engine, "streaming", False):
            self._feed_session([frame])
        else:
            self._pending.append(frame)
        if self._spec_text is None and silence_ms >= self.cfg.speculative_after_ms:
            self._speculate()
        if self._spec_text is not None and silence_ms >= self._endpoint_ms:
            self._finalize()

    def _begin(self, t_end: float) -> None:
        self._state = "speech"
        if self._ids is not None:
            self._uid = self._ids()
        else:
            self._uid = self._next_id
            self._next_id += 1
        self._run = self._speech_frames
        self._t_start = t_end - len(self._preroll) * FRAME_MS / 1000.0
        self._t_voice = t_end - self._speech_frames * FRAME_MS / 1000.0
        self._t_last_voiced = t_end
        self._since_partial_ms = 0.0
        self._last_partial = ""
        self._endpoint_ms = self.cfg.command_ms
        self._pauses = []
        try:
            self._session = self.engine.session()
        except Exception:
            log.exception("Could not start a recogniser session")
            self.reset()
            return
        self._feed_session(list(self._preroll))
        self._preroll.clear()
        self.on_speaking(True)

    def _resume(self) -> None:
        self._state = "speech"
        self._pauses.append(self._silence_frames * FRAME_MS)
        if self._pending:
            self._feed_session(self._pending)
        self._pending = []
        self._spec_text = None
        self._silence_frames = 0

    def _feed_session(self, frames: list[np.ndarray]) -> None:
        if self._session is None or not frames:
            return
        audio = frames[0] if len(frames) == 1 else np.concatenate(frames)
        try:
            self._session.accept(audio)
        except Exception:
            log.exception("Recogniser rejected audio")

    # ------------------------------------------------------------------ decoding

    def _decode_partial(self) -> tuple[str, float]:
        assert self._session is not None
        t0 = time.perf_counter()
        try:
            text = self._session.partial()
        except Exception:
            log.exception("Partial decode failed")
            text = ""
        return text, (time.perf_counter() - t0) * 1000.0

    def _live_partial(self) -> None:
        if self._session is None:
            return
        if not getattr(self.engine, "streaming", False) and self._partial_cost_ms > self.cfg.partial_budget_ms:
            return
        text, cost = self._decode_partial()
        self._partial_cost_ms = cost if self._partial_cost_ms == 0 else 0.7 * self._partial_cost_ms + 0.3 * cost
        if text and text != self._last_partial:
            self._last_partial = text
            self.on_partial(self._uid, text)

    def _speculate(self) -> None:
        if self._session is None:
            self._spec_text = ""
            return
        if self._pending and not getattr(self.engine, "streaming", False):
            self._feed_session(self._pending)
            self._pending = []
        text, cost = self._decode_partial()
        self._spec_text = text
        try:
            self._endpoint_ms = int(self.classify(text))
        except Exception:
            log.exception("Endpoint classifier failed")
            self._endpoint_ms = self.cfg.command_ms
        if text and text != self._last_partial:
            self._last_partial = text
            self.on_partial(self._uid, text)

    def _finalize(self, forced: bool = False) -> None:
        session, self._session = self._session, None
        uid = self._uid
        speech_frames = self._run
        silence_ms = self._silence_frames * FRAME_MS if self._state == "trailing" else 0.0
        t_last = self._t_last_voiced
        t_start, t_voice = self._t_start, self._t_voice
        pauses = tuple(self._pauses)
        self._pauses = []
        audio_ms = max(0.0, (t_last - t_voice) * 1000.0)
        t0 = time.perf_counter()
        text = ""
        if session is not None:
            try:
                text = session.finish()
            except Exception:
                log.exception("Final decode failed")
                text = self._spec_text or ""
        asr_ms = (time.perf_counter() - t0) * 1000.0
        self._state = "idle"
        self._preroll.clear()
        self._speech_frames = 0
        self._silence_frames = 0
        self._pending = []
        self._spec_text = None
        self.on_speaking(False)
        if speech_frames * FRAME_MS < self.cfg.min_speech_ms:
            log.debug("Ignored a %.0f ms sound", speech_frames * FRAME_MS)
            return
        self.on_final(Utterance(uid, text, audio_ms, silence_ms, asr_ms, t_last, time.perf_counter(), forced, t_speech_start=t_start, t_voice_start=t_voice, pauses_ms=pauses))
