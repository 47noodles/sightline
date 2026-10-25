"""Model pins, and the real models when they are downloaded (marker ``models``)."""

from __future__ import annotations

import statistics
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from sightline.speech import models as M

ROOT = Path(__file__).resolve().parents[2]
TESTSET = ROOT / "out" / "speech_testset"


def test_every_model_is_pinned() -> None:
    specs = M.all_specs() + [e.model for e in M.BENCH_ONLY.values()]
    for spec in specs:
        assert spec.license and spec.homepage, spec.id
        for f in spec.files:
            assert len(f.sha256) == 64 and f.size > 0, (spec.id, f.path)
            assert f.url.startswith("https://"), f.url
            assert "/main/" not in f.url and "latest" not in f.url, f.url  # pinned to a release or commit


def test_engine_choices() -> None:
    choices = M.engine_choices()
    assert choices["auto"] == "Recommended"
    assert set(choices) == {"auto", *M.ENGINES}
    assert M.resolve_engine("auto").id == M.DEFAULT_ENGINE
    assert M.resolve_engine("nonsense").id == M.DEFAULT_ENGINE
    assert M.ENGINES[M.DEFAULT_ENGINE].model.required


def _models(real_models_dir: Path, *specs):
    from sightline.core.models import ModelManager

    mm = ModelManager(root=real_models_dir)
    mm.register_all(specs)
    missing = [s.id for s in specs if not mm.is_ready(s.id)]
    if missing:
        pytest.skip(f"models not downloaded: {', '.join(missing)}")
    return mm


@pytest.mark.models
def test_silero_vad(real_models_dir: Path) -> None:
    from sightline.speech.vad import FRAME, SileroVad

    mm = _models(real_models_dir, M.VAD)
    vad = SileroVad(mm.dir(M.VAD.id) / "silero_vad.onnx")
    quiet = [vad(np.zeros(FRAME, np.float32)) for _ in range(10)]
    assert max(quiet) < 0.2
    started = time.perf_counter()
    for _ in range(100):
        vad(np.zeros(FRAME, np.float32))
    assert (time.perf_counter() - started) / 100 < 0.005  # well under a millisecond on a desktop CPU


@pytest.mark.models
def test_semantic_precision(real_models_dir: Path) -> None:
    """Paraphrases held out from the seed phrases, and everyday sentences that must not run anything."""
    from tools.speech_testset import NON_COMMANDS, PARAPHRASES

    from sightline.speech.grammar import BuildContext, build_commands, sleep_and_wake
    from sightline.speech.interpret import Interpreter, Situation
    from sightline.speech.matcher import Grammar
    from sightline.speech.semantic import OnnxSentenceEmbedder, SemanticIndex, build_entries

    mm = _models(real_models_dir, M.EMBEDDING)
    folder = mm.dir(M.EMBEDDING.id)
    grammar = Grammar(build_commands() + sleep_and_wake("wake up", "go to sleep"))
    specs = [s for s in grammar.specs.values() if s.semantic]
    index = SemanticIndex(
        OnnxSentenceEmbedder(folder / "model_quint8_avx2.onnx", folder / "tokenizer.json", "minilm"),
        build_entries(specs, {s.id: grammar.canonical_phrases(s.id, 6) for s in specs}),
        threshold=M.SEMANTIC_THRESHOLD,
        margin=M.SEMANTIC_MARGIN,
    )
    interp = Interpreter(grammar, index)
    ctx = BuildContext()
    ran_right = ran_wrong = confirmed = 0
    costs = []
    for text, spec_id in PARAPHRASES:
        started = time.perf_counter()
        d = interp.decide(text, Situation(mode="command"), ctx)
        costs.append((time.perf_counter() - started) * 1000)
        if d.kind == "command" and d.parse:
            if d.parse.matches[0].spec.id == spec_id:
                ran_right += 1
            else:
                ran_wrong += 1
        elif d.reason.startswith("Say 1 to confirm") and d.alternatives[0].spec.id == spec_id:
            confirmed += 1  # a risky command offered for confirmation instead of run
    accepted = [t for t in NON_COMMANDS if interp.decide(t, Situation(mode="command"), ctx).kind == "command"]
    precision = ran_right / max(1, ran_right + ran_wrong)  # of what actually ran
    recall = (ran_right + confirmed) / len(PARAPHRASES)
    assert precision >= 0.9, (precision, recall)
    assert recall >= 0.7, (precision, recall)
    assert len(accepted) <= 1, accepted
    assert statistics.median(costs) < 15.0


# ---------------------------------------------------------------------- end to end: audio in, actions out

E2E = [
    ("click", None, "mouse.click"),
    ("scroll down three", None, "mouse.scroll"),
    ("press control shift t", None, "key.press"),
    ("go back", None, "key.press"),
    ("open notepad", None, "app.open"),
    ("select all", None, "key.press"),
    ("next tab", None, "key.press"),
    ("volume up", None, "system.volume"),
    ("show numbers", None, "ui.labels.show"),
    ("twelve", "labels", "ui.labels.select"),
    ("go to sleep", None, "speech.mode"),
]


def _clips(tmp_path: Path) -> list[tuple[Path, str, str | None, str]]:
    """Clean SAPI clips of the E2E commands: from the generated test set, or rendered to WAV files now."""
    from tools.speech_testset import load_manifest, render_sapi

    wanted = {(t, o): spec for t, o, spec in E2E}
    if (TESTSET / "manifest.json").exists():
        found: dict[tuple[str, str | None], Path] = {}
        for item in load_manifest(TESTSET):
            key = (item["text"], item.get("overlay"))
            if key in wanted and item["noise"] == "clean" and item["rate"] == 0 and key not in found:
                found[key] = Path(item["abspath"])
        if len(found) == len(wanted):
            return [(found[k], k[0], k[1], spec) for k, spec in wanted.items()]
    jobs = [("Microsoft Zira Desktop", 0, text, tmp_path / f"{i}.wav") for i, (text, _, _) in enumerate(E2E)]
    try:
        render_sapi(jobs)  # writes WAV files only; never plays anything
    except Exception as e:
        pytest.skip(f"SAPI voices unavailable: {e}")
    return [(path, text, overlay, spec) for (_, _, text, path), (_, overlay, spec) in zip(jobs, E2E)]


@pytest.mark.models
@pytest.mark.slow
def test_spoken_commands_end_to_end(real_models_dir: Path, tmp_path: Path, monkeypatch) -> None:
    """WAV files through the real VAD, endpointing, recogniser and grammar, paced like a microphone."""
    import sightline.speech.service as service_module
    from sightline.core.context import AppContext
    from sightline.core.events import IntentResult
    from sightline.core.models import ModelManager
    from sightline.core.types import SpeechMode
    from sightline.speech import create
    from sightline.speech.audio import FileSource
    from sightline.speech.audio import read_wav as read_audio

    from speech_helpers import FakeMicrophone

    mm = ModelManager(root=real_models_dir)
    mm.register_all(M.all_specs())
    if not (mm.is_ready(M.VAD.id) and mm.is_ready(M.ENGINES[M.DEFAULT_ENGINE].model.id)):
        pytest.skip("speech models not downloaded")
    clips = _clips(tmp_path)

    monkeypatch.setattr(service_module, "Microphone", FakeMicrophone)
    ctx = AppContext.for_tests()
    ctx.models = mm
    results: list[IntentResult] = []
    got = threading.Event()
    ctx.bus.subscribe(IntentResult, lambda r: (results.append(r), got.set()))
    svc = create(ctx)
    svc.start()
    try:
        source = FileSource(svc._queue.put, realtime=True)
        latencies = []
        for path, text, overlay, spec in clips:
            ctx.state.update(active_overlay=overlay, speech_mode=SpeechMode.AUTO)
            svc._build_ctx.current_mode = "auto"
            got.clear()
            before = len(results)
            audio, rate = read_audio(path)
            source.play(audio, rate, silence_after_ms=900)
            got.wait(5.0)
            mine = results[before:]
            assert mine, f"nothing heard for {text!r}"
            r = mine[-1]
            assert r.kind == "command", (text, r)
            assert r.actions[0].name == spec, (text, r)
            assert r.actions[0].aim_t is not None
            latencies.append(r.latency_ms)
    finally:
        svc.stop()
    # End of speech to dispatch.  The bar in ARCHITECTURE.md is about 300 ms for short commands.
    assert statistics.median(latencies) < 400, latencies


@pytest.mark.models
@pytest.mark.slow
def test_vocabulary_brings_a_name_through(real_models_dir: Path, tmp_path: Path) -> None:
    """Key-term biasing with the real recogniser: beam search, the bpe vocabulary made from tokens.txt, hotwords."""
    from tools.speech_bench import decode_clip
    from tools.speech_testset import pad, read_wav, render_sapi

    definition = M.ENGINES["parakeet-110m"]
    mm = _models(real_models_dir, definition.model)
    raw = tmp_path / "raw.wav"
    try:
        render_sapi([("Microsoft Zira Desktop", 0, "Dervla has the tickets for tonight", raw)])  # a WAV file; nothing is played
    except Exception as e:
        pytest.skip(f"SAPI voices unavailable: {e}")
    audio = pad(read_wav(raw))
    engine = definition.build(mm.dir(definition.model.id), biasing=True)
    try:
        assert "dervla" not in decode_clip(engine, audio)[0].lower()  # "Durvla"
        engine.set_hotwords(["Dervla"])
        assert "Dervla" in decode_clip(engine, audio)[0]
    finally:
        engine.close()
