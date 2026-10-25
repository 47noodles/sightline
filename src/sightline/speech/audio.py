"""Microphone capture with sounddevice (PortAudio), and a file source for tests.

Audio reaches the speech thread as 16 kHz mono float32 blocks of about
20 ms, each stamped with the ``time.perf_counter()`` at which its last
sample was captured.  Devices that cannot record at 16 kHz (WDM-KS devices
usually only offer their native rate) are opened at their own rate and
resampled here.

Device ids are "<host API>:<device name>", which survive reboots and
replugging, unlike PortAudio's indices.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
import wave
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from sightline.core.types import MicInfo

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCK_MS = 20

# Host APIs in order of preference for opening a device: shared-mode WASAPI and MME
# resample in Windows; WDM-KS gives the native format only.
_HOSTAPI_PREFERENCE = ("Windows WASAPI", "MME", "Windows DirectSound", "Windows WDM-KS")
_HOSTAPI_SHORT = {"Windows WASAPI": "WASAPI", "MME": "MME", "Windows DirectSound": "DirectSound", "Windows WDM-KS": "WDM-KS"}
_SKIP_NAMES = ("Microsoft Sound Mapper", "Primary Sound Capture Driver", "Primary Sound Driver")


@dataclass(frozen=True, slots=True)
class Block:
    samples: np.ndarray  # float32 mono at 16 kHz
    t_end: float  # perf_counter of the last sample


# --------------------------------------------------------------------------- resampling


class Resampler:
    """Streaming sample-rate conversion: windowed-sinc low-pass, then linear interpolation.

    Good enough for speech recognition (which only uses up to 8 kHz) at a
    fraction of the cost of a full polyphase filter.  Keeps state between
    blocks so there are no clicks at block edges.
    """

    def __init__(self, src_rate: float, dst_rate: float = SAMPLE_RATE, taps: int = 63) -> None:
        self.src = float(src_rate)
        self.dst = float(dst_rate)
        self.passthrough = abs(self.src - self.dst) < 1e-6
        cutoff = 0.95 * min(self.src, self.dst) / 2.0 / self.src  # normalised to the source rate
        n = np.arange(taps) - (taps - 1) / 2.0
        h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.blackman(taps)
        self._h = (h / h.sum()).astype(np.float32)
        self._hist = np.zeros(taps - 1, np.float32)
        self._step = self.src / self.dst
        self._pos = 0.0  # position of the next output sample, in source samples relative to the filtered buffer
        self._tail = np.zeros(0, np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.passthrough:
            return np.asarray(x, dtype=np.float32)
        x = np.asarray(x, dtype=np.float32)
        padded = np.concatenate([self._hist, x])
        filtered = np.convolve(padded, self._h, mode="valid").astype(np.float32)
        self._hist = padded[-(len(self._h) - 1) :]
        buf = np.concatenate([self._tail, filtered])
        if len(buf) < 2:
            self._tail = buf
            return np.zeros(0, np.float32)
        last = len(buf) - 1
        count = int(math.floor((last - self._pos) / self._step)) + 1 if self._pos <= last else 0
        if count <= 0:
            self._tail = buf
            return np.zeros(0, np.float32)
        positions = self._pos + self._step * np.arange(count)
        i0 = np.floor(positions).astype(np.int64)
        frac = (positions - i0).astype(np.float32)
        i1 = np.minimum(i0 + 1, last)
        out = buf[i0] * (1.0 - frac) + buf[i1] * frac
        next_pos = self._pos + self._step * count
        keep_from = int(math.floor(next_pos))
        keep_from = min(keep_from, last)
        self._tail = buf[keep_from:]
        self._pos = next_pos - keep_from
        return out.astype(np.float32)


# --------------------------------------------------------------------------- devices


@dataclass(frozen=True, slots=True)
class DeviceChoice:
    index: int
    id: str
    name: str
    hostapi: str
    samplerate: float
    channels: int


def _query() -> tuple[list[dict], list[dict]]:
    import sounddevice as sd

    return list(sd.query_devices()), list(sd.query_hostapis())


def refresh_portaudio() -> None:
    """Make PortAudio rescan devices (it caches the list at start).  Only call with no stream open."""
    import sounddevice as sd

    try:
        sd._terminate()
        sd._initialize()
    except Exception as e:  # pragma: no cover - depends on the audio stack
        log.debug("PortAudio rescan failed: %s", e)


def input_devices() -> list[DeviceChoice]:
    devices, hostapis = _query()
    out: list[DeviceChoice] = []
    for index, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) < 1:
            continue
        name = str(dev["name"]).strip()
        if any(name.startswith(skip) for skip in _SKIP_NAMES):
            continue
        api = hostapis[dev["hostapi"]]["name"]
        short = _HOSTAPI_SHORT.get(api, api)
        out.append(DeviceChoice(index, f"{short}:{name}", name, api, float(dev["default_samplerate"]), int(dev["max_input_channels"])))
    return out


def _default_input_index() -> int | None:
    import sounddevice as sd

    try:
        idx = sd.default.device[0]
    except Exception:
        return None
    return idx if isinstance(idx, int) and idx >= 0 else None


def _same_device(a: str, b: str) -> bool:
    # MME truncates names to 31 characters; compare on the shorter one.
    a, b = a.lower(), b.lower()
    n = min(len(a), len(b))
    return n >= 8 and a[:n] == b[:n]


def list_microphones() -> list[MicInfo]:
    """One entry per physical input, best host API first; the system default is marked."""
    choices = input_devices()
    default_idx = _default_input_index()
    default_name = next((c.name for c in choices if c.index == default_idx), None)
    seen: list[DeviceChoice] = []
    for api in _HOSTAPI_PREFERENCE:
        for c in choices:
            if c.hostapi != api:
                continue
            if any(_same_device(c.name, s.name) for s in seen):
                continue
            seen.append(c)
    for c in choices:  # anything on an unexpected host API
        if not any(_same_device(c.name, s.name) for s in seen):
            seen.append(c)
    mics = [MicInfo(c.id, c.name, default_name is not None and _same_device(c.name, default_name)) for c in seen]
    mics.sort(key=lambda m: not m.default)
    return mics


def resolve_device(mic_id: str) -> DeviceChoice | None:
    """The device to open for a saved id: exact match, the same name on another host API, the default, any input."""
    choices = input_devices()
    if not choices:
        return None
    if mic_id and mic_id != "default":
        for c in choices:
            if c.id == mic_id:
                return c
        name = mic_id.split(":", 1)[-1]
        for api in _HOSTAPI_PREFERENCE:
            for c in choices:
                if c.hostapi == api and _same_device(c.name, name):
                    return c
        log.warning("Microphone %r not found; using the default", mic_id)
    default_idx = _default_input_index()
    if default_idx is not None:
        default = next((c for c in choices if c.index == default_idx), None)
        if default is not None:
            # Prefer the same physical device on a better host API.
            for api in _HOSTAPI_PREFERENCE:
                for c in choices:
                    if c.hostapi == api and _same_device(c.name, default.name):
                        return c
            return default
    for api in _HOSTAPI_PREFERENCE:
        for c in choices:
            if c.hostapi == api:
                return c
    return choices[0]


# --------------------------------------------------------------------------- live capture

BlockSink = Callable[[Block], None]


class Microphone:
    """Captures from one device on PortAudio's callback thread and hands 16 kHz blocks to ``sink``.

    A watchdog reopens the device when callbacks stop (unplugged, driver
    reset) and falls back to the default microphone if the chosen one is gone.
    """

    def __init__(self, sink: BlockSink, on_status: Callable[[str, str | None], None] | None = None) -> None:
        self._sink = sink
        self._on_status = on_status or (lambda name, error: None)
        self._stream = None
        self._lock = threading.RLock()
        self._mic_id = ""
        self._device: DeviceChoice | None = None
        self._resampler: Resampler | None = None
        self._last_callback = 0.0
        self._running = False
        self._watchdog: threading.Thread | None = None
        self._stop = threading.Event()
        self._channels = 1

    @property
    def device_name(self) -> str:
        return self._device.name if self._device else ""

    def start(self, mic_id: str) -> None:
        with self._lock:
            self._mic_id = mic_id
            self._running = True
            self._stop.clear()
            self._open()
            if self._watchdog is None or not self._watchdog.is_alive():
                self._watchdog = threading.Thread(target=self._watch, name="sightline-mic-watchdog", daemon=True)
                self._watchdog.start()

    def switch(self, mic_id: str) -> None:
        with self._lock:
            self._mic_id = mic_id
            if self._running:
                self._close()
                self._open()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._stop.set()
            self._close()
        if self._watchdog is not None:
            self._watchdog.join(timeout=2.0)
            self._watchdog = None

    def _open(self) -> bool:
        import sounddevice as sd

        device = resolve_device(self._mic_id)
        if device is None:
            self._device = None
            self._on_status("", "No microphone found")
            return False
        # Ask for 16 kHz mono first (the shared-mode host APIs convert); fall back to the native format.
        attempts: list[tuple[float, int]] = []
        if device.hostapi != "Windows WDM-KS":
            attempts.append((SAMPLE_RATE, 1))
        attempts.append((device.samplerate, 1))
        attempts.append((device.samplerate, min(2, device.channels)))
        attempts.append((device.samplerate, device.channels))
        last_error: Exception | None = None
        for rate, channels in attempts:
            try:
                blocksize = max(1, int(rate * BLOCK_MS / 1000))
                stream = sd.InputStream(
                    device=device.index,
                    samplerate=rate,
                    channels=channels,
                    dtype="float32",
                    blocksize=blocksize,
                    latency="low",
                    callback=self._callback,
                )
                stream.start()
            except Exception as e:
                last_error = e
                continue
            self._stream = stream
            self._device = device
            self._channels = channels
            self._resampler = Resampler(rate, SAMPLE_RATE)
            self._last_callback = time.perf_counter()
            log.info("Microphone open: %s (%s, %g Hz, %d ch)", device.name, device.hostapi, rate, channels)
            self._on_status(device.name, None)
            return True
        log.warning("Could not open microphone %s: %s", device.name, last_error)
        self._device = None
        self._on_status(device.name, "The microphone could not be opened")
        return False

    def _close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as e:  # pragma: no cover - device already gone
                log.debug("Closing the microphone: %s", e)

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001 - PortAudio signature
        now = time.perf_counter()
        self._last_callback = now
        data = indata
        if data.ndim > 1:
            data = data[:, 0] if self._channels == 1 else data.mean(axis=1)
        resampler = self._resampler
        samples = resampler.process(data) if resampler is not None else np.asarray(data, np.float32)
        if len(samples):
            try:
                self._sink(Block(samples.copy(), now))
            except Exception:  # never let an exception escape into PortAudio
                log.exception("Audio sink failed")

    def _watch(self) -> None:
        missing_since: float | None = None
        while not self._stop.wait(1.0):
            with self._lock:
                if not self._running:
                    return
                stream = self._stream
                stale = stream is None or not stream.active or time.perf_counter() - self._last_callback > 2.0
                if not stale:
                    missing_since = None
                    continue
                if missing_since is None:
                    missing_since = time.perf_counter()
                    log.warning("Microphone stopped delivering audio; reopening")
                self._close()
                refresh_portaudio()
                if not self._open():
                    self._on_status("", "Microphone disconnected")


# --------------------------------------------------------------------------- file source (tests, benchmarks)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        audio = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"{path}: unsupported sample width {width}")
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, sr


class FileSource:
    """Feeds audio (arrays or WAV files) through the same path as the microphone.

    ``realtime`` paces blocks like a live microphone so latency figures are
    real; otherwise blocks are pushed as fast as the consumer takes them and
    timestamps advance with the audio.
    """

    def __init__(self, sink: BlockSink, *, realtime: bool = False) -> None:
        self._sink = sink
        self._realtime = realtime

    def play(self, audio: np.ndarray, rate: int = SAMPLE_RATE, *, silence_after_ms: int = 0) -> None:
        if rate != SAMPLE_RATE:
            audio = Resampler(rate, SAMPLE_RATE).process(audio)
        if silence_after_ms:
            audio = np.concatenate([audio, np.zeros(SAMPLE_RATE * silence_after_ms // 1000, np.float32)])
        block = SAMPLE_RATE * BLOCK_MS // 1000
        start = time.perf_counter()
        for i in range(0, len(audio), block):
            chunk = np.asarray(audio[i : i + block], dtype=np.float32)
            t_end = start + (i + len(chunk)) / SAMPLE_RATE
            if self._realtime:
                delay = t_end - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                t_end = time.perf_counter()
            self._sink(Block(chunk, t_end))

    def play_files(self, paths: Iterable[Path], *, gap_ms: int = 600) -> None:
        for path in paths:
            audio, sr = read_wav(Path(path))
            self.play(audio, sr, silence_after_ms=gap_ms)


class BlockQueue:
    """Bounded hand-off from the capture thread to the speech thread.  Drops the oldest audio if the consumer stalls."""

    def __init__(self, max_seconds: float = 10.0) -> None:
        self._q: queue.Queue[Block] = queue.Queue(maxsize=int(max_seconds * 1000 / BLOCK_MS))

    def put(self, block: Block) -> None:
        try:
            self._q.put_nowait(block)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            self._q.put_nowait(block)

    def get(self, timeout: float) -> Block | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def clear(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return
