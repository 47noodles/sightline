"""Speech benchmarks: recogniser bake-off, semantic fallback tuning, matcher and end-to-end latency.

    python tools/speech_bench.py                      # recogniser bake-off (all engines), prints tables
    python tools/speech_bench.py --engines parakeet-110m moonshine-base
    python tools/speech_bench.py --engines parakeet-0.6b --biasing --vocabulary words.txt
    python tools/speech_bench.py --names --vocabulary out/speech_names/vocabulary.txt --hotwords-score 2
    python tools/speech_bench.py --semantic           # precision and recall of the paraphrase matcher
    python tools/speech_bench.py --matcher            # time spent matching text
    python tools/speech_bench.py --pipeline           # end of speech to final text, real-time audio
    python tools/speech_bench.py --prepare-human LIBRISPEECH_DIR SPEECH_COMMANDS_DIR

Needs the test set (tools/speech_testset.py) and the models (installed by
setup, or ``ModelManager.ensure``).  Timing runs hold the shared ``cpu``
lock so agents benchmarking at the same time do not skew each other.
SAPI is only used to write WAV files; nothing is played aloud.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
import unicodedata
from contextlib import nullcontext
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from text_to_num import alpha2digit  # noqa: E402

from tools.speech_testset import DEFAULT_OUT, load_manifest, pad, read_wav, render_sapi, trim, write_wav  # noqa: E402

HUMAN_DIR = ROOT / "out" / "speech_human"
SR = 16000

# --------------------------------------------------------------------------- scoring

_SPELLING = {
    "minimise": "minimize",
    "maximise": "maximize",
    "recentre": "recenter",
    "centre": "center",
    "colour": "color",
    "favourite": "favorite",
    "licence": "license",
    "organise": "organize",
    "realise": "realize",
    "ok": "okay",
    "alright": "all right",
    "grey": "gray",
}


def wer_norm(text: str) -> list[str]:
    t = unicodedata.normalize("NFKC", text).lower().replace("’", "'")
    t = re.sub(r"(\d):(\d\d)", r"\1 \2", t)
    t = re.sub(r"(?<=[a-z])\.(?=[a-z])", " dot ", t)
    t = re.sub(r"[^\w\s']", " ", t)
    t = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if any(c.isalpha() for c in t):
        try:
            t = alpha2digit(t, "en", threshold=0)
        except Exception:
            pass
    words = []
    for w in t.split():
        w = w.strip("'")
        if not w:
            continue
        words.extend(_SPELLING.get(w, w).split())
    return words


def edit_distance(ref: list[str], hyp: list[str]) -> int:
    d = list(range(len(hyp) + 1))
    for i in range(1, len(ref) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(hyp) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (ref[i - 1] != hyp[j - 1]))
            prev = cur
    return d[len(hyp)]


def pct(a: float, b: float) -> str:
    return f"{100.0 * a / b:.1f}%" if b else "n/a"


def cpu_lock(enabled: bool = True):
    if not enabled:
        return nullcontext()
    from tools.desktop_lock import desktop_lock

    return desktop_lock("speech-bench", name="cpu")


# --------------------------------------------------------------------------- interpretation (intent accuracy)


def make_interpreter():
    from sightline.speech.grammar import BuildContext, build_commands, sleep_and_wake
    from sightline.speech.interpret import Interpreter
    from sightline.speech.matcher import Grammar
    from sightline.speech.names import DynamicNames

    grammar = Grammar(build_commands() + sleep_and_wake("wake up", "go to sleep"))
    names = DynamicNames()
    names.set_apps(["Google Chrome", "Notepad", "File Explorer", "Calculator", "Microsoft Word", "Visual Studio Code"])
    names.set_windows([("Untitled - Notepad", "notepad.exe", 1), ("New Tab - Google Chrome", "chrome.exe", 2)])
    names.set_elements(["Save", "File", "Edit", "View", "Documents", "Back", "Reload"])
    ctx = BuildContext()
    ctx.resolve_app = names.resolve_app
    ctx.resolve_window = names.resolve_window
    ctx.resolve_element = names.resolve_element
    return Interpreter(grammar), ctx


def intent_signature(interp, ctx, text: str, overlay: str | None) -> tuple:
    from sightline.speech.interpret import Situation

    decision = interp.decide(text, Situation(mode="command", overlay=overlay), ctx, semantic=False)
    if decision.kind != "command" or decision.parse is None:
        return (decision.kind,)
    sig = []
    for m in decision.parse.matches:
        if m.outcome.type_request is not None:
            sig.append(("type", m.outcome.type_request.text.lower().strip(" .,!?")))
        for a in m.outcome.actions:
            sig.append((a.name, tuple(sorted((k, str(v).lower()) for k, v in a.args.items()))))
    return tuple(sig)


# --------------------------------------------------------------------------- engines


def load_engine(engine_id: str, *, biasing: bool = False, vocabulary: list[str] | None = None, score: float | None = None):
    """``biasing`` builds the recogniser the way Sightline does once the user has a vocabulary (beam search).

    ``score`` boosts the vocabulary by that much instead of Sightline's ``HOTWORDS_SCORE``.
    """
    from sightline.core.models import ModelManager
    from sightline.speech import models as M

    definition = M.resolve_engine(engine_id)
    mm = ModelManager()
    mm.register(definition.model)
    if not mm.is_ready(definition.model.id):
        raise SystemExit(f"model for {engine_id} is not installed ({definition.model.id}); run setup or ModelManager.ensure")
    engine = definition.build(mm.dir(definition.model.id), biasing=biasing)
    if vocabulary:
        if score is None:
            engine.set_hotwords(vocabulary)
        else:
            engine.set_hotwords(vocabulary, score=score)
    return engine


# --------------------------------------------------------------------------- names (the user's vocabulary)

NAMES_DIR = ROOT / "out" / "speech_names"
NAME_VOICES = ("Microsoft David Desktop", "Microsoft Zira Desktop")
# Names these recognisers do not know, each in a sentence.  The second spelling is how SAPI says
# the real name ("Shivawn" for Siobhan), so that audio carries the pronunciation, not the spelling.
NAMES = [
    ("Siobhan", "Shivawn", "Please send the notes to {} before Friday"),
    ("Aoife", "Eefa", "{} will join the call at three"),
    ("Nguyen", "Nuwen", "I spoke to doctor {} about the results"),
    ("Saoirse", "Seersha", "{} is driving us to the airport"),
    ("Niamh", "Neeve", "Ask {} for a copy of the report"),
    ("Tadhg", "Taig", "{} left his keys on the kitchen table"),
    ("Dervla", "Dervla", "{} has the tickets for tonight"),
    ("Kubernetes", "Koobernetties", "We moved the service to {} last week"),
    ("Worcestershire", "Woostersher", "Add a splash of {} sauce"),
    ("Auckland", "Awkland", "Our flight lands in {} on Monday"),
    ("Ptolemy", "Tollemy", "{} wrote about the stars"),
    ("Sightline", "Sightline", "{} moves the pointer where you look"),
    ("PySide", "Pie side", "The window is built with {}"),
    ("Grafana", "Graffahna", "Open the {} dashboard for the servers"),
    ("Okonkwo", "Oh konkwo", "Mister {} will see you now"),
]


def name_clips() -> list[dict]:
    """The name sentences as WAV files, rendered with SAPI the first time (never played).

    Also writes ``vocabulary.txt`` with the names beside them, for ``--vocabulary``.
    """
    clips, jobs = [], []
    for name, sounded, sentence in NAMES:
        for form, said in (("spelled", name), ("sounded", sounded)):
            if form == "sounded" and said == name:
                continue
            for voice in NAME_VOICES:
                path = NAMES_DIR / f"{name}_{form}_{voice.split()[1].lower()}.wav"
                clips.append({"name": name, "form": form, "path": path})
                if not path.exists():
                    jobs.append((voice, 0, sentence.format(said), NAMES_DIR / "raw" / path.name))
    if jobs:
        render_sapi(jobs)
        for _, _, _, raw in jobs:
            write_wav(NAMES_DIR / raw.name, pad(read_wav(raw)))
    vocabulary = NAMES_DIR / "vocabulary.txt"
    if not vocabulary.exists():
        vocabulary.write_text("# The names said in out/speech_names, as a vocabulary.txt\n" + "\n".join(n for n, _, _ in NAMES) + "\n", "utf-8")
    return clips


def _squash(text: str) -> str:
    return re.sub(r"[\s-]", "", text.lower())


def names_found(engine, clips: list[dict]) -> dict[str, list[int]]:
    """How many name sentences come back with the name spelled right (spaces and capitals aside)."""
    found = {"spelled": [0, 0], "sounded": [0, 0]}
    for clip in clips:
        text, _, _ = decode_clip(engine, read_wav(clip["path"]))
        found[clip["form"]][0] += _squash(clip["name"]) in _squash(text)
        found[clip["form"]][1] += 1
    return found


def read_vocabulary(path: Path | None) -> list[str]:
    """One word or phrase per line, # for comments: the format of the user's vocabulary.txt."""
    if path is None:
        return []
    from sightline.speech.custom import parse_vocabulary

    return parse_vocabulary(path.read_text("utf-8"))


def decode_clip(engine, audio: np.ndarray, *, endpoint_ms: int = 250) -> tuple[str, float, float]:
    """Feed like the live pipeline does, up to the endpoint; return text, time after the endpoint, total compute."""
    speech = trim(audio)
    lead = int(0.3 * SR)
    end = min(len(audio), lead + len(speech) + int(endpoint_ms * SR / 1000))
    session = engine.session()
    t0 = time.perf_counter()
    for i in range(0, end, 320):
        session.accept(audio[i : min(i + 320, end)])
    t1 = time.perf_counter()
    text = session.finish()
    t2 = time.perf_counter()
    return text, (t2 - t1) * 1000.0, (t2 - t0) * 1000.0


def human_sets() -> tuple[list[dict], list[dict]]:
    libri, words = [], []
    manifest = HUMAN_DIR / "manifest.json"
    if manifest.exists():
        data = json.loads(manifest.read_text("utf-8"))
        for item in data:
            item["abspath"] = str(HUMAN_DIR / item["path"])
            (libri if item["kind"] == "librispeech" else words).append(item)
    return libri, words


def asr_bakeoff(
    engine_ids: list[str],
    *,
    limit: int | None,
    lock: bool,
    biasing: bool = False,
    vocabulary: list[str] | None = None,
    score: float | None = None,
    names: bool = False,
) -> str:
    interp, ictx = make_interpreter()
    clips = name_clips() if names else []
    manifest = load_manifest(DEFAULT_OUT)
    if limit:
        random.Random(1).shuffle(manifest)
        manifest = manifest[:limit]
    libri, words = human_sets()
    ref_sig_cache: dict[tuple[str, str | None], tuple] = {}
    rows = []
    with cpu_lock(lock):
        for engine_id in engine_ids:
            t_load = time.perf_counter()
            engine = load_engine(engine_id, biasing=biasing, vocabulary=vocabulary, score=score)
            load_s = time.perf_counter() - t_load
            engine.warm_up()
            stats: dict[str, list] = {k: [0, 0] for k in ("cmd_clean", "cmd_noisy", "dict_clean", "dict_noisy", "libri")}
            intents = {"ok": 0, "wrong": 0, "missed": 0}
            lat_cmd: list[float] = []
            rtf: list[float] = []
            for item in manifest:
                audio = read_wav(Path(item["abspath"]))
                text, after_ms, total_ms = decode_clip(engine, audio)
                ref, hyp = wer_norm(item["text"]), wer_norm(text)
                key = ("cmd" if item["kind"] == "command" else "dict") + ("_clean" if item["noise"] == "clean" else "_noisy")
                stats[key][0] += edit_distance(ref, hyp)
                stats[key][1] += len(ref)
                rtf.append(total_ms / 1000.0 / (len(trim(audio)) / SR + 0.55))
                if item["kind"] == "command":
                    lat_cmd.append(after_ms)
                    rk = (item["text"], item.get("overlay"))
                    if rk not in ref_sig_cache:
                        ref_sig_cache[rk] = intent_signature(interp, ictx, item["text"], item.get("overlay"))
                    got = intent_signature(interp, ictx, text, item.get("overlay"))
                    want = ref_sig_cache[rk]
                    if got == want:
                        intents["ok"] += 1
                    elif got[0] in ("rejected", "ignored", "dictation"):
                        intents["missed"] += 1
                    else:
                        intents["wrong"] += 1
            for item in libri:
                audio, _ = _read_any(Path(item["abspath"]))
                audio = np.concatenate([np.zeros(int(0.3 * SR), np.float32), audio, np.zeros(int(0.6 * SR), np.float32)])
                text, _, _ = decode_clip(engine, audio)
                stats["libri"][0] += edit_distance(wer_norm(item["text"]), wer_norm(text))
                stats["libri"][1] += len(wer_norm(item["text"]))
            word_ok = 0
            for item in words:
                audio, _ = _read_any(Path(item["abspath"]))
                audio = np.concatenate([np.zeros(int(0.3 * SR), np.float32), audio, np.zeros(int(0.6 * SR), np.float32)])
                text, _, _ = decode_clip(engine, audio)
                word_ok += wer_norm(text) == wer_norm(item["text"])
            found = names_found(engine, clips) if clips else None
            total_intents = sum(intents.values())
            label = engine_id + (" beam search" if biasing else "") + (f" + {len(vocabulary)} words" if vocabulary else "")
            if vocabulary and score is not None:
                label += f" at {score:g}"
            row = {
                "engine": label,
                "load_s": round(load_s, 1),
                "lat_med": statistics.median(lat_cmd) if lat_cmd else float("nan"),
                "lat_p90": sorted(lat_cmd)[int(0.9 * (len(lat_cmd) - 1))] if lat_cmd else float("nan"),
                "rtf": statistics.mean(rtf) if rtf else float("nan"),
                "intent_ok": pct(intents["ok"], total_intents),
                "intent_wrong": pct(intents["wrong"], total_intents),
                "intent_missed": pct(intents["missed"], total_intents),
                "words": pct(word_ok, len(words)) if words else "n/a",
            }
            if found is not None:
                row["names"] = " / ".join(f"{found[k][0]} of {found[k][1]}" for k in ("spelled", "sounded"))
            for key, (err, n) in stats.items():
                row[key] = pct(err, n)
            rows.append(row)
            print(json.dumps(row), flush=True)
            engine.close()
    header = "| Recogniser | Model time after pause, 1-2 s commands (median / p90) | Real-time factor | Command WER clean / noisy | Commands understood (wrong) | Dictation WER clean / noisy | LibriSpeech WER | Spoken words (Speech Commands) |"
    rule = "|---|---|---|---|---|---|---|---|"
    if names:
        header += " Names found, spelled / sounded |"
        rule += "---|"
    lines = [header, rule]
    for r in rows:
        lines.append(
            f"| {r['engine']} | {r['lat_med']:.0f} / {r['lat_p90']:.0f} ms | {r['rtf']:.3f} | {r['cmd_clean']} / {r['cmd_noisy']} | {r['intent_ok']} ({r['intent_wrong']}) | {r['dict_clean']} / {r['dict_noisy']} | {r['libri']} | {r['words']} |"
            + (f" {r['names']} |" if names else "")
        )
    table = "\n".join(lines)
    print()
    print(table)
    return table


def _read_any(path: Path) -> tuple[np.ndarray, int]:
    if path.suffix.lower() == ".wav":
        from sightline.speech.audio import read_wav as rw

        return rw(path)
    import soundfile as sf

    audio, sr = sf.read(str(path), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return audio, sr


# --------------------------------------------------------------------------- human speech


def prepare_human(libri_dir: Path, commands_dir: Path, *, n_libri: int = 80, per_word: int = 25) -> None:
    """Copy a small, fixed subset of LibriSpeech (CC-BY-4.0) and Speech Commands (CC-BY-4.0) into out/speech_human."""
    import soundfile as sf

    from tools.speech_testset import write_wav

    rng = random.Random(3)
    out = HUMAN_DIR
    items = []
    trans: list[tuple[Path, str]] = []
    for tf in sorted(libri_dir.rglob("*.trans.txt")):
        for line in tf.read_text("utf-8").splitlines():
            uid, _, text = line.partition(" ")
            trans.append((tf.parent / f"{uid}.flac", text))
    rng.shuffle(trans)
    for path, text in trans:
        if len([i for i in items if i["kind"] == "librispeech"]) >= n_libri:
            break
        audio, sr = sf.read(str(path), dtype="float32")
        if sr != SR or len(audio) > 9 * SR:
            continue
        dest = out / "libri" / f"{path.stem}.wav"
        write_wav(dest, audio)
        items.append({"kind": "librispeech", "text": text.lower(), "path": str(dest.relative_to(out))})
    for word_dir in sorted(p for p in commands_dir.iterdir() if p.is_dir() and not p.name.startswith("_")):
        files = sorted(word_dir.glob("*.wav"))
        rng.shuffle(files)
        for f in files[:per_word]:
            audio, sr = _read_any(f)
            dest = out / "words" / word_dir.name / f.name
            write_wav(dest, audio)
            items.append({"kind": "word", "text": word_dir.name, "path": str(dest.relative_to(out))})
    (out / "manifest.json").write_text(json.dumps(items, indent=1), "utf-8")
    print(f"wrote {len(items)} human clips to {out}")


# --------------------------------------------------------------------------- semantic tuning


def semantic_report(lock: bool) -> str:
    from sightline.core.models import ModelManager
    from sightline.speech import models as M
    from sightline.speech import semantic as sem
    from sightline.speech.grammar import BuildContext, build_commands, sleep_and_wake
    from sightline.speech.matcher import Grammar
    from sightline.speech.normalize import normalize_command
    from tools.speech_testset import DICTATION, NON_COMMANDS, PARAPHRASES

    specs = build_commands() + sleep_and_wake("wake up", "go to sleep")
    grammar = Grammar(specs)
    entries = sem.build_entries(specs, {s.id: grammar.canonical_phrases(s.id, 6) for s in specs if s.semantic})
    mm = ModelManager()
    mm.register(M.EMBEDDING)
    folder = mm.dir(M.EMBEDDING.id)
    embedder = sem.OnnxSentenceEmbedder(folder / "model_quint8_avx2.onnx", folder / "tokenizer.json", "all-MiniLM-L6-v2 int8")
    index = sem.SemanticIndex(embedder, entries, threshold=M.SEMANTIC_THRESHOLD, margin=M.SEMANTIC_MARGIN)
    ctx = BuildContext()
    positives = []
    for text, sid in PARAPHRASES:
        norm = normalize_command(text)
        p = grammar.parse(norm, ["global"], ctx)
        if p is not None and p.confidence >= 0.99:
            continue  # the grammar already understands it; the fallback never sees it
        positives.append((norm, sid))
    negatives = [normalize_command(t) for t in NON_COMMANDS + DICTATION]
    negatives = [t for t in negatives if not ((p := grammar.parse(t, ["global"], ctx)) and p.confidence >= 0.99)]
    lines = ["| Threshold | Margin | Precision | Recall | Wrong command | Non-commands accepted |", "|---|---|---|---|---|---|"]
    with cpu_lock(lock):
        t0 = time.perf_counter()
        for _ in range(100):
            index.match("get rid of this window")
        per_query = (time.perf_counter() - t0) / 100 * 1000
    for th in (0.50, 0.55, 0.60, 0.65, 0.70):
        for mg in (0.02, 0.04, 0.06):
            index.threshold, index.margin = th, mg
            tp = wrong = fp = 0
            for text, sid in positives:
                hit = index.match(text)
                if hit is None:
                    continue
                if hit.spec_id == sid:
                    tp += 1
                else:
                    wrong += 1
            fp = sum(1 for t in negatives if index.match(t) is not None)
            accepted = tp + wrong + fp
            mark = " (used)" if abs(th - M.SEMANTIC_THRESHOLD) < 1e-6 and abs(mg - M.SEMANTIC_MARGIN) < 1e-6 else ""
            lines.append(f"| {th:.2f}{mark} | {mg:.2f} | {pct(tp, accepted) if accepted else '100%'} | {pct(tp, len(positives))} | {wrong} | {fp} of {len(negatives)} |")
    table = "\n".join(lines)
    print(f"{len(entries)} index phrases, {len(positives)} held-out paraphrases, {len(negatives)} non-commands; {per_query:.2f} ms per query")
    print(table)
    return table


# --------------------------------------------------------------------------- matcher timing


def matcher_report(lock: bool) -> None:
    from sightline.speech.interpret import Situation
    from tools.speech_testset import COMMANDS, DICTATION, PARAPHRASES

    interp, ctx = make_interpreter()
    texts = COMMANDS + DICTATION + [p for p, _ in PARAPHRASES]
    times = []
    with cpu_lock(lock):
        for text in texts:
            t0 = time.perf_counter()
            interp.decide(text, Situation(mode="auto"), ctx, semantic=False)
            times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    print(f"matching (no semantic): median {statistics.median(times):.2f} ms, p90 {times[int(0.9 * len(times))]:.2f} ms, max {times[-1]:.2f} ms over {len(times)} utterances")


# --------------------------------------------------------------------------- pipeline latency


def pipeline_report(engine_id: str, *, n: int, lock: bool, biasing: bool = False, vocabulary: list[str] | None = None) -> None:
    """Real-time playback of command clips through VAD, endpointing and the recogniser."""
    from sightline.core.models import ModelManager
    from sightline.speech import models as M
    from sightline.speech.audio import FileSource
    from sightline.speech.interpret import Situation
    from sightline.speech.pipeline import EndpointConfig, UtterancePipeline
    from sightline.speech.service import endpoint_ms
    from sightline.speech.vad import SileroVad

    interp, ictx = make_interpreter()
    mm = ModelManager()
    mm.register(M.VAD)
    vad = SileroVad(mm.dir(M.VAD.id) / "silero_vad.onnx")
    engine = load_engine(engine_id, biasing=biasing, vocabulary=vocabulary)
    engine.warm_up()
    cfg = EndpointConfig()

    def classify(text: str) -> int:
        return endpoint_ms(text, interp, Situation(mode="auto"), cfg)

    results = []
    pipe = UtterancePipeline(vad, engine, classify, cfg, on_final=results.append)
    clips = [m for m in load_manifest(DEFAULT_OUT) if m["kind"] == "command" and m["noise"] == "clean" and not m.get("overlay")]
    random.Random(5).shuffle(clips)
    clips = clips[:n]
    rows = []
    with cpu_lock(lock):
        src = FileSource(pipe.feed, realtime=True)
        for item in clips:
            audio = read_wav(Path(item["abspath"]))
            before = len(results)
            src.play(audio, silence_after_ms=700)
            if len(results) == before:
                pipe.flush()
            if len(results) > before:
                u = results[-1]
                t0 = time.perf_counter()
                interp.decide(u.text, Situation(mode="auto"), ictx, semantic=False)
                match_ms = (time.perf_counter() - t0) * 1000
                total = (u.t_ready - u.t_speech_end) * 1000 + match_ms
                rows.append((item["text"], u.text, u.endpoint_ms, u.asr_ms, total))
    for text, heard, ep, asr_ms, total in rows:
        print(f"  {text!r:32} heard {heard!r:34} pause {ep:5.0f} ms  model {asr_ms:5.1f} ms  end of speech to decision {total:6.1f} ms")
    totals = sorted(r[4] for r in rows)
    if totals:
        print(f"end of speech to decision: median {statistics.median(totals):.0f} ms, p90 {totals[int(0.9 * (len(totals) - 1))]:.0f} ms over {len(totals)} commands")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engines", nargs="*", default=None)
    parser.add_argument("--limit", type=int, default=None, help="only this many test clips (random, fixed seed)")
    parser.add_argument("--semantic", action="store_true")
    parser.add_argument("--matcher", action="store_true")
    parser.add_argument("--pipeline", action="store_true")
    parser.add_argument("--pipeline-engine", default="parakeet-110m")
    parser.add_argument("--pipeline-clips", type=int, default=40)
    parser.add_argument("--prepare-human", nargs=2, type=Path, metavar=("LIBRISPEECH_DIR", "SPEECH_COMMANDS_DIR"))
    parser.add_argument("--biasing", action="store_true", help="build recognisers for key-term biasing (beam search), as with a user vocabulary")
    parser.add_argument("--vocabulary", type=Path, default=None, help="a vocabulary.txt to pass as hotwords (implies --biasing)")
    parser.add_argument("--hotwords-score", type=float, default=None, help="boost for the vocabulary instead of Sightline's HOTWORDS_SCORE")
    parser.add_argument("--names", action="store_true", help="also count names found in out/speech_names (rendered with SAPI the first time)")
    parser.add_argument("--no-lock", action="store_true", help="do not take the cpu lock")
    parser.add_argument("--out", type=Path, default=None, help="also write the table(s) to this file")
    args = parser.parse_args(argv)
    lock = not args.no_lock
    vocabulary = read_vocabulary(args.vocabulary)
    biasing = args.biasing or bool(vocabulary)
    tables = []
    if args.prepare_human:
        prepare_human(*args.prepare_human)
        return 0
    if args.semantic:
        tables.append(semantic_report(lock))
    if args.matcher:
        matcher_report(lock)
    if args.pipeline:
        pipeline_report(args.pipeline_engine, n=args.pipeline_clips, lock=lock, biasing=biasing, vocabulary=vocabulary)
    if not (args.semantic or args.matcher or args.pipeline):
        from sightline.speech import models as M

        engines = args.engines or (list(M.ENGINES) + list(M.BENCH_ONLY))
        tables.append(asr_bakeoff(engines, limit=args.limit, lock=lock, biasing=biasing, vocabulary=vocabulary, score=args.hotwords_score, names=args.names))
    if args.out and tables:
        args.out.write_text("\n\n".join(tables) + "\n", "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
