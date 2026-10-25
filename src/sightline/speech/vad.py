"""Voice activity detection with Silero VAD.

The ONNX calling convention (512-sample windows at 16 kHz with the previous
64 samples prepended, and a recurrent state carried between calls) follows
``OnnxWrapper`` in snakers4/silero-vad (src/silero_vad/utils_vad.py, MIT
licence, Copyright (c) 2020-present Silero Team).
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME = 512  # 32 ms at 16 kHz, the window Silero expects
CONTEXT = 64


class SileroVad:
    """Speech probability for each 32 ms frame.  Not thread-safe: one instance per audio stream."""

    def __init__(self, model_path: Path) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        # A single thread is fastest for this tiny model and keeps cores free for the recogniser.
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(str(model_path), opts, providers=["CPUExecutionProvider"])
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT), dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        """``frame``: exactly 512 float32 samples."""
        x = np.asarray(frame, dtype=np.float32).reshape(1, FRAME)
        inp = np.concatenate([self._context, x], axis=1)
        out, self._state = self._session.run(None, {"input": inp, "state": self._state, "sr": self._sr})
        self._context = x[:, -CONTEXT:]
        return float(out[0, 0])


class EnergyVad:
    """Fallback when the Silero model is missing: a level gate with a slowly adapting noise floor.

    Much less selective than Silero (any loud sound counts), so it is only
    used to keep speech working until the model is downloaded.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._floor = 0.003

    def __call__(self, frame: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(np.square(frame)))) + 1e-9
        if rms < self._floor * 2.0:
            self._floor = 0.95 * self._floor + 0.05 * rms
        ratio = rms / max(self._floor, 1e-5)
        return float(np.clip((ratio - 2.0) / 4.0, 0.0, 1.0))
