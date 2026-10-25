"""Model files for speech, pinned by URL, size and SHA-256, and the recognisers built from them.

Every file is fetched by ``core.models.ModelManager`` during setup; nothing
downloads while Sightline runs.  The recogniser offered by default and the
alternatives in Settings were chosen by the bake-off in
docs/modules/speech.md (``tools/speech_bench.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sightline.core.models import ModelFile, ModelSpec

_SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
_MOONSHINE = "https://download.moonshine.ai/model"

# --------------------------------------------------------------------------- voice activity

VAD = ModelSpec(
    id="silero_vad",
    title="Voice detection",
    files=(
        ModelFile(
            url="https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.3/src/silero_vad/data/silero_vad.onnx",
            path="silero_vad.onnx",
            sha256="1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3",
            size=2327524,
        ),
    ),
    license="MIT",
    homepage="https://github.com/snakers4/silero-vad",
)

# --------------------------------------------------------------------------- speech recognition


def _archive(model_id: str, title: str, name: str, sha256: str, size: int, license: str, homepage: str, expect: tuple[str, ...], *, required: bool) -> ModelSpec:
    return ModelSpec(
        id=model_id,
        title=title,
        files=(ModelFile(url=f"{_SHERPA}/{name}.tar.bz2", path=f"{name}.tar.bz2", sha256=sha256, size=size, extract=True),),
        license=license,
        homepage=homepage,
        required=required,
        expect=tuple(f"{name}/{e}" for e in expect),
    )


PARAKEET_110M_DIR = "sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8"
PARAKEET_06B_DIR = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"
MOONSHINE_BASE_DIR = "sherpa-onnx-moonshine-base-en-quantized-2026-02-27"
MOONSHINE_TINY_DIR = "sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27"
_TRANSDUCER_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
_MOONSHINE_FILES = ("encoder_model.ort", "decoder_model_merged.ort", "tokens.txt")

ASR_PARAKEET_110M = _archive(
    "asr_parakeet_110m",
    "Speech recognition (Parakeet 110M)",
    PARAKEET_110M_DIR,
    "f628312e9fdf8686374cb01a69425c41732529d540860311f16f37cbc32cfe9b",
    108035095,
    "CC-BY-4.0",
    "https://huggingface.co/nvidia/parakeet-tdt_ctc-110m",
    _TRANSDUCER_FILES,
    required=True,
)
ASR_PARAKEET_06B = _archive(
    "asr_parakeet_0_6b",
    "Speech recognition, most accurate (Parakeet 0.6B)",
    PARAKEET_06B_DIR,
    "157c157bc51155e03e37d2466522a3a737dd9c72bb25f36eb18912964161e1ad",
    482468385,
    "CC-BY-4.0",
    "https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2",
    _TRANSDUCER_FILES,
    required=True,
)
ASR_MOONSHINE_BASE = _archive(
    "asr_moonshine_base",
    "Speech recognition, fastest (Moonshine Base)",
    MOONSHINE_BASE_DIR,
    "43232c1d13013d37317163baec3135bd771a186a4356f28c889bab453bb0e891",
    111266225,
    "MIT",
    "https://github.com/moonshine-ai/moonshine",
    _MOONSHINE_FILES,
    required=False,
)
ASR_MOONSHINE_TINY = _archive(
    "asr_moonshine_tiny",
    "Speech recognition (Moonshine Tiny)",
    MOONSHINE_TINY_DIR,
    "9ec31b342d8fa3240c3b81b8f82e1cf7e3ac467c93ca5a999b741d5887164f8d",
    29858559,
    "MIT",
    "https://github.com/moonshine-ai/moonshine",
    _MOONSHINE_FILES,
    required=False,
)


def _moonshine_streaming(model_id: str, title: str, folder: str, files: dict[str, tuple[str, int]]) -> ModelSpec:
    return ModelSpec(
        id=model_id,
        title=title,
        files=tuple(ModelFile(url=f"{_MOONSHINE}/{folder}/quantized_26_08_21/{name}", path=name, sha256=sha, size=size) for name, (sha, size) in files.items()),
        license="MIT",
        homepage="https://github.com/moonshine-ai/moonshine",
        required=False,
    )


ASR_MOONSHINE_TINY_STREAMING = _moonshine_streaming(
    "asr_moonshine_tiny_streaming",
    "Speech recognition (Moonshine Tiny streaming)",
    "tiny-streaming-en",
    {
        "adapter.ort": ("22ecc949e146c49667fda28d102d4e30749a107dc88a396292aa8f277ef1347c", 1319664),
        "cross_kv.ort": ("143a36667b8d05fd9d04e8c337b7ee121f37ef299aea6b3d82bdb3d3401950b4", 1287544),
        "decoder_kv.ort": ("8852553f312adb6c9aa4d17418015049b30f412209ee569d336548c0044627de", 32583720),
        "encoder.ort": ("a8414e1a5dedf9f2093d7680601dd8a9b0433e7020260eafe0e370ead91134ca", 7675440),
        "frontend.model.ort": ("5121b561417b638afce0c6c31b760e37c93cf97f80d9b0031aad1fe7b6f25d61", 23344),
        "frontend.weights.ort": ("217da24ac6f522ebf02da8ef288e77d1ac68d50d4a6821433182e4fbf4204bbd", 2093464),
        "streaming_config.json": ("74fe5ddebd63b17caf59e8a3b18c17547ff7bce1642050edbb1c3962674f8950", 509),
        "tokenizer.bin": ("6884b35fd6377d4c4d32336a0bc152f36b64d1e45b6503683cdc238250a8472d", 249974),
    },
)
ASR_MOONSHINE_SMALL_STREAMING = _moonshine_streaming(
    "asr_moonshine_small_streaming",
    "Speech recognition (Moonshine Small streaming)",
    "small-streaming-en",
    {
        "adapter.ort": ("c665f742364febad597cc9ac1e0b341ffbee0e24a1466e2f3bde95e6e4771762", 2870368),
        "cross_kv.ort": ("e2d3417144e9514055ebfefe8dcc4c0a55a55adcb8530435844c75c53e352bf6", 5356536),
        "decoder_kv.ort": ("1a05465b1dd955858dfcbee039c0020fb5dd982b0f5094c34e61735d518d771b", 81878600),
        "encoder.ort": ("2d4d973e91e8aca08c51e7e7efa28a46ab265b63d809d5294d18b86bcd85b993", 44148576),
        "frontend.model.ort": ("09b1210ae30dc5f0f3e45f0ebab914c254741323114f53fbbe5ae62cca35058f", 26944),
        "frontend.weights.ort": ("7ef97521bd4bad3928f5bb6808586f4fcc6e92bd5990394112eed7d4052ec338", 7769464),
        "streaming_config.json": ("26f02b6afb22d60871a5efd85c3d38e569cc0ddb6c5eb6e93d3260152ae8a47a", 512),
        "tokenizer.bin": ("6884b35fd6377d4c4d32336a0bc152f36b64d1e45b6503683cdc238250a8472d", 249974),
    },
)

# --------------------------------------------------------------------------- sentence embeddings (semantic fallback)

_MINILM = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
EMBEDDING = ModelSpec(
    id="minilm_l6_int8",
    title="Command paraphrase model",
    files=(
        ModelFile(url=f"{_MINILM}/onnx/model_quint8_avx2.onnx", path="model_quint8_avx2.onnx", sha256="b941bf19f1f1283680f449fa6a7336bb5600bdcd5f84d10ddc5cd72218a0fd21", size=23046789),
        ModelFile(url=f"{_MINILM}/tokenizer.json", path="tokenizer.json", sha256="be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037", size=466247),
    ),
    license="Apache-2.0",
    homepage="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2",
)

# Tuned on held-out paraphrases and everyday sentences; see docs/modules/speech.md.
SEMANTIC_THRESHOLD = 0.60
SEMANTIC_MARGIN = 0.04

# --------------------------------------------------------------------------- recognisers offered in Settings


@dataclass(frozen=True, slots=True)
class EngineDef:
    id: str
    label: str
    model: ModelSpec
    # (model folder, biasing) -> asr.Engine.  ``biasing`` asks for a recogniser that takes the
    # user's vocabulary as hotwords; engines that cannot bias ignore it.
    build: Callable[..., object]
    can_bias: bool = False


def _parakeet(engine_id: str, sub: str) -> Callable[..., object]:
    def build(folder: Path, *, biasing: bool = False) -> object:
        from .asr import SherpaOfflineEngine

        return SherpaOfflineEngine.transducer(engine_id, folder / sub, biasing=biasing)

    return build


def _moonshine_offline(engine_id: str, sub: str) -> Callable[..., object]:
    def build(folder: Path, *, biasing: bool = False) -> object:
        from .asr import SherpaOfflineEngine

        return SherpaOfflineEngine.moonshine(engine_id, folder / sub)

    return build


def _moonshine_stream(engine_id: str, arch: str) -> Callable[..., object]:
    def build(folder: Path, *, biasing: bool = False) -> object:
        from .asr import MoonshineEngine

        return MoonshineEngine(engine_id, folder, arch)

    return build


ENGINES: dict[str, EngineDef] = {
    "parakeet-0.6b": EngineDef("parakeet-0.6b", "Most accurate (Parakeet 0.6B)", ASR_PARAKEET_06B, _parakeet("parakeet-0.6b", PARAKEET_06B_DIR), can_bias=True),
    "parakeet-110m": EngineDef("parakeet-110m", "Faster (Parakeet 110M)", ASR_PARAKEET_110M, _parakeet("parakeet-110m", PARAKEET_110M_DIR), can_bias=True),
    "moonshine-base": EngineDef("moonshine-base", "Fastest, less accurate (Moonshine Base)", ASR_MOONSHINE_BASE, _moonshine_offline("moonshine-base", MOONSHINE_BASE_DIR)),
}
# The most accurate recogniser whose model time for a short command stays under budget (bake-off:
# 156 ms median on a 6-core i5-12400).  "Recommended" times it at start-up and steps down the list
# on slower computers.
DEFAULT_ENGINE = "parakeet-0.6b"
AUTO_ORDER = ("parakeet-0.6b", "parakeet-110m")
AUTO_BUDGET_MS = 200.0

# Measured in the bake-off but not offered: slower than the latency budget on a 6-core desktop CPU,
# or less accurate than the engines above.
BENCH_ONLY: dict[str, EngineDef] = {
    "moonshine-tiny": EngineDef("moonshine-tiny", "Moonshine Tiny", ASR_MOONSHINE_TINY, _moonshine_offline("moonshine-tiny", MOONSHINE_TINY_DIR)),
    "moonshine-tiny-streaming": EngineDef("moonshine-tiny-streaming", "Moonshine Tiny streaming", ASR_MOONSHINE_TINY_STREAMING, _moonshine_stream("moonshine-tiny-streaming", "TINY_STREAMING")),
    "moonshine-small-streaming": EngineDef("moonshine-small-streaming", "Moonshine Small streaming", ASR_MOONSHINE_SMALL_STREAMING, _moonshine_stream("moonshine-small-streaming", "SMALL_STREAMING")),
}


def engine_choices() -> dict[str, str]:
    choices = {"auto": "Recommended"}
    choices.update({e.id: e.label for e in ENGINES.values()})
    return choices


def resolve_engine(setting: str) -> EngineDef:
    if setting in ENGINES:
        return ENGINES[setting]
    if setting in BENCH_ONLY:
        return BENCH_ONLY[setting]
    return ENGINES[DEFAULT_ENGINE]


def all_specs() -> list[ModelSpec]:
    specs = [VAD, EMBEDDING]
    specs.extend(e.model for e in ENGINES.values())
    return specs
