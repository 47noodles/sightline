# Third-party notices

Sightline is licensed under the Apache License 2.0 (see `LICENSE`).  It
builds on the open-source work listed below.  Code adapted into this
repository keeps its original licence terms, reproduced or referenced here.

## Code adapted into this repository

| Project | Licence | Used in | What |
|---|---|---|---|
| Microsoft PowerToys (FancyZonesLib/WindowUtils.cpp `SwitchToWindow`) | MIT | `src/sightline/desktop/windows.py` | an empty SendInput mouse event before `SetForegroundWindow` |
| talonhub/community (`core/keys`) | MIT | `src/sightline/desktop/keys.py` | spoken key and symbol names |
| EyeTrax, https://github.com/ck-zhang/EyeTrax (`GazeEstimator.extract_features`, `models/ridge.py`) | MIT | `src/sightline/vision/features.py`, `src/sightline/vision/gaze_model.py` | head-normalised eye landmark features; the ridge gaze model recipe, rewritten in NumPy |
| Project GameFace, https://github.com/google/project-gameface (`src/accel_graph.py` SigmoidAccel) | Apache-2.0 | `src/sightline/vision/head.py` | sigmoid speed-dependent gain |
| OneEuroFilter, https://github.com/casiez/OneEuroFilter | BSD-3-Clause | `src/sightline/vision/filters.py` | One Euro filter |
| talonhub/community (commit de1c2a33) | MIT | `src/sightline/speech/vocab.py`, `src/sightline/speech/formatting.py` | alphabets, keys, symbols, formatters, spacing and capitalisation |
| snakers4/silero-vad (`OnnxWrapper`) | MIT | `src/sightline/speech/vad.py` | running the Silero VAD model |
| shaal/eye-tracker, https://github.com/shaal/eye-tracker | MIT | `src/sightline/vision/` (ideas only, no code) | eye-frame iris features, leave-one-target-out penalty, fixation clamp, telling winks from blinks, yielding to a real mouse |

## Libraries used at runtime (installed separately)

| Project | Licence |
|---|---|
| MediaPipe | Apache-2.0 |
| OpenCV (opencv-python) | Apache-2.0 / MIT |
| NumPy | BSD-3-Clause |
| PySide6 (Qt for Python) | LGPL-3.0 |
| ONNX Runtime | MIT |
| sounddevice | MIT |
| hassil | Apache-2.0 |
| text2num | MIT |
| RapidFuzz | MIT |
| comtypes | MIT |
| cv2-enumerate-cameras | MIT |
| sherpa-onnx | Apache-2.0 |
| tokenizers (Hugging Face) | Apache-2.0 |

## Models and assets downloaded or bundled

| Item | Licence | Source |
|---|---|---|
| Atkinson Hyperlegible Next (four TTFs, unmodified, in `src/sightline/assets/fonts`) | SIL OFL 1.1 | The Atkinson Hyperlegible Next Project Authors (Braille Institute), googlefonts/atkinson-hyperlegible-next commit 7925f50 |
| Lucide icons 1.61.0 (27 SVGs, unmodified, in `src/sightline/assets/icons/lucide`) | ISC; icons derived from Feather are MIT | lucide.dev |
| MediaPipe Face Landmarker `face_landmarker.task` (float16 v1, downloaded on first run) | Apache-2.0 | https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task |
| Six Wikimedia Commons photos (test data only, in `tests/vision/data`) | public domain and CC BY 2.0 | authors and links in `tests/vision/data/SOURCES.md` |
| Silero VAD v6.2.3 (downloaded on first run) | MIT | snakers4/silero-vad |
| NVIDIA Parakeet TDT 0.6B v2 and Parakeet TDT-CTC 110M, sherpa-onnx int8 exports (downloaded on first run) | CC BY 4.0 | NVIDIA NeMo; exports by k2-fsa/sherpa-onnx |
| Moonshine Base (optional download) | MIT | Useful Sensors |
| all-MiniLM-L6-v2 (downloaded on first run, for paraphrase matching) | Apache-2.0 | sentence-transformers |

Used for measurement only, not shipped: LibriSpeech test-clean (CC BY 4.0),
Speech Commands v0.02 (CC BY 4.0) and moonshine-voice (MIT).

The earcons in `src/sightline/assets/sounds` are synthesised by
`tools/ui_make_sounds.py` and are part of Sightline (Apache-2.0); they use no
third-party samples.
