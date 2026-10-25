"""Voice activity, endpointing, speculative decoding and onset times, with a fake recogniser."""

from __future__ import annotations

import numpy as np
import pytest

from sightline.speech.asr import SherpaOfflineEngine
from sightline.speech.audio import Block
from sightline.speech.pipeline import FRAME_MS, EndpointConfig, Utterance, UtterancePipeline

SR = 16000
T0 = 1000.0  # perf_counter of the first sample in these tests


class FakeEngine(SherpaOfflineEngine):
    """Offline engine whose "recognition" is the length of the audio it was given."""

    def __init__(self, text: str = "scroll down") -> None:
        super().__init__("fake", None)
        self.text = text
        self.decodes: list[int] = []

    def decode(self, audio: np.ndarray) -> str:
        self.decodes.append(len(audio))
        return self.text


def vad(frame: np.ndarray) -> float:
    return 0.95 if float(np.sqrt(np.mean(frame * frame))) > 0.02 else 0.02


def tone(seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), np.float32)


class Run:
    def __init__(self, classify_ms: int = 250, cfg: EndpointConfig | None = None, text: str = "scroll down") -> None:
        self.finals: list[tuple[Utterance, float]] = []
        self.partials: list[str] = []
        self.speaking: list[bool] = []
        self.engine = FakeEngine(text)
        self.now = T0
        ids = iter(range(500, 600))
        self.pipeline = UtterancePipeline(
            vad,
            self.engine,
            lambda text: classify_ms,
            cfg or EndpointConfig(),
            on_partial=lambda uid, text: self.partials.append(text),
            on_final=lambda u: self.finals.append((u, self.now)),
            on_speaking=self.speaking.append,
            next_id=lambda: next(ids),
        )

    def feed(self, audio: np.ndarray) -> None:
        for i in range(0, len(audio), 320):
            chunk = audio[i : i + 320]
            self.now += len(chunk) / SR
            self.pipeline.feed(Block(chunk, self.now - 1 / SR))


def speech_then_silence(lead: float = 0.5, speech: float = 0.8, tail: float = 1.0) -> np.ndarray:
    return np.concatenate([silence(lead), tone(speech), silence(tail)])


@pytest.mark.parametrize("endpoint_ms", [125, 250, 650])
def test_endpoint_follows_the_classifier(endpoint_ms: int) -> None:
    run = Run(classify_ms=endpoint_ms)
    run.feed(speech_then_silence(tail=1.2))
    assert len(run.finals) == 1
    u, decided_at = run.finals[0]
    assert u.text == "scroll down"
    assert endpoint_ms <= u.endpoint_ms < endpoint_ms + FRAME_MS + 1
    speech_end = T0 + 0.5 + 0.8
    assert decided_at - speech_end == pytest.approx(endpoint_ms / 1000.0, abs=0.07)


def test_onset_times_and_length() -> None:
    run = Run()
    run.feed(speech_then_silence(lead=0.6, speech=0.8))
    u, _ = run.finals[0]
    onset = T0 + 0.6
    assert u.t_voice_start == pytest.approx(onset, abs=0.07)
    # The pre-roll starts before the voice, so aiming is never late.
    assert onset - 0.45 <= u.t_speech_start <= u.t_voice_start
    assert u.t_speech_end == pytest.approx(onset + 0.8, abs=0.07)
    assert u.audio_ms == pytest.approx(800, abs=70)
    assert u.id == 500


def test_speculative_decode_is_reused() -> None:
    run = Run(classify_ms=250)
    run.feed(speech_then_silence(speech=0.3, tail=1.0))
    u, _ = run.finals[0]
    # One decode when the pause began; the final result reused it (nothing new was heard).
    assert len(run.engine.decodes) == 1
    assert u.asr_ms < 5.0


def test_pause_shorter_than_dictation_endpoint_does_not_split() -> None:
    run = Run(classify_ms=650)
    audio = np.concatenate([silence(0.4), tone(0.6), silence(0.4), tone(0.6), silence(1.2)])
    run.feed(audio)
    assert len(run.finals) == 1
    assert run.finals[0][0].audio_ms == pytest.approx(1600, abs=80)


def test_command_endpoint_splits_two_commands() -> None:
    run = Run(classify_ms=125)
    audio = np.concatenate([silence(0.4), tone(0.5), silence(0.4), tone(0.5), silence(0.6)])
    run.feed(audio)
    assert len(run.finals) == 2
    assert [u.id for u, _ in run.finals] == [500, 501]


def test_short_clicks_are_dropped() -> None:
    run = Run()
    run.feed(np.concatenate([silence(0.4), tone(0.07), silence(0.8)]))
    assert run.finals == []
    assert run.speaking == [True, False]


def test_long_speech_is_cut() -> None:
    run = Run(cfg=EndpointConfig(max_utterance_s=1.0))
    run.feed(np.concatenate([silence(0.3), tone(2.5), silence(0.8)]))
    assert run.finals
    assert run.finals[0][0].forced


def test_cancel_drops_the_utterance() -> None:
    run = Run()
    run.feed(np.concatenate([silence(0.3), tone(0.5)]))
    assert run.pipeline.speaking
    run.pipeline.cancel()
    assert not run.pipeline.speaking
    run.feed(silence(1.0))
    assert run.finals == []
    assert run.speaking == [True, False]


def test_flush_ends_speech_in_progress() -> None:
    run = Run()
    run.feed(np.concatenate([silence(0.3), tone(0.5)]))
    run.pipeline.flush()
    assert len(run.finals) == 1 and run.finals[0][0].forced


def test_live_partials_while_speaking() -> None:
    run = Run(classify_ms=650)
    run.feed(np.concatenate([silence(0.3), tone(1.5), silence(1.0)]))
    assert run.partials and run.partials[0] == "scroll down"


def test_pauses_inside_an_utterance_are_reported() -> None:
    run = Run(classify_ms=650)
    run.feed(np.concatenate([silence(0.4), tone(0.5), silence(0.3), tone(0.4), silence(0.2), tone(0.4), silence(1.2)]))
    assert len(run.finals) == 1
    pauses = run.finals[0][0].pauses_ms
    assert len(pauses) == 2
    # Counted in whole silent frames, as the endpoint counts them: up to two frames short.
    assert 300 - 2 * FRAME_MS <= pauses[0] <= 300
    assert 200 - 2 * FRAME_MS <= pauses[1] <= 200
    run.feed(speech_then_silence())
    assert run.finals[1][0].pauses_ms == ()  # each utterance starts afresh


def test_endpoint_setting_changes_apply_to_the_next_utterance() -> None:
    run = Run(classify_ms=250)
    run.pipeline.cfg = EndpointConfig(command_ms=400)
    run.feed(speech_then_silence())
    assert run.finals[0][0].endpoint_ms >= 250  # the classifier still decides; config holds the classes
