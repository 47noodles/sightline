"""Face gestures made with the eyes open: raised eyebrows, a smile, an open mouth and puffed cheeks.

They come from MediaPipe's blendshapes, which the face model already
computes every frame.  A gesture counts on a rising edge: the signal must
come up from below its "off" level, pass its "on" level and stay above "off"
for ``hold_s`` (about 150 ms, so a passing expression does not count).
After it is released there is a refractory period before it can count
again.  The click goes where the pointer was when the signal began to rise.

Thresholds come from a short trainer (calibration kind "gestures") that
records the resting face and each gesture, plus about half a minute each of
talking and reading, and counts how often each trigger would have gone off
by mistake (``fit_face_gestures``).  An untrained gesture never fires, and
one that misfired or could not be told apart from the resting face is not
offered.

Two corrections:

* looking up raises the eyebrows a little on its own, so the brow signal
  has the eyes' upward look subtracted, by a factor fitted from the resting
  and reading frames;
* the mouth gestures (smile, open mouth, puffed cheeks) do not count while
  the user is talking, judged from the mouth movement in the second before
  the gesture began (``AttentionTracker``), so no audio is needed.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import numpy as np

from .attention import AttentionTracker
from .eyes import EyeGestureDetector, Gesture, GestureConfig
from .landmarks import BS

log = logging.getLogger(__name__)

FACE_KINDS: tuple[str, ...] = ("brow_raise", "smile", "mouth_open", "cheek_puff")
MOUTH_KINDS = frozenset({"smile", "mouth_open", "cheek_puff"})
EYE_KINDS: tuple[str, ...] = ("long_blink", "hold_blink", "wink_left", "wink_right")
NAMES = {
    "brow_raise": "raised eyebrows",
    "smile": "a smile",
    "mouth_open": "an open mouth",
    "cheek_puff": "puffed cheeks",
    "long_blink": "a long blink",
    "hold_blink": "a held blink",
    "wink_left": "a left wink",
    "wink_right": "a right wink",
}
DEFAULT_LOOK_K = 0.5
PROFILE_VERSION = 1

# What the trainer asks of a usable trigger.
MIN_GAP = 0.06  # the gesture's typical level must clear the resting face's 95th percentile by this much
MIN_SEPARATION = 0.9  # chance that a gesture frame reads higher than a resting frame
MIN_HELD = 0.6  # share of the gesture's frames above the "on" level
MAX_FALSE_PER_MIN = 1.0  # mistaken triggers a minute while talking or reading

_BROW = [BS["browInnerUp"], BS["browOuterUpLeft"], BS["browOuterUpRight"]]
_LOOK_UP = [BS["eyeLookUpLeft"], BS["eyeLookUpRight"]]
_SMILE = [BS["mouthSmileLeft"], BS["mouthSmileRight"]]
_JAW = BS["jawOpen"]
_CHEEK = BS["cheekPuff"]
_NEEDED = max(_BROW + _LOOK_UP + _SMILE + [_JAW, _CHEEK])


def has_blendshapes(bs: np.ndarray | None) -> bool:
    return bs is not None and len(bs) > _NEEDED and bool(np.any(np.asarray(bs) > 0))


def raw_signals(bs: np.ndarray) -> tuple[float, float, float, float, float]:
    """(brow, look_up, smile, jaw, cheek) from the 52 blendshapes."""
    # Plain indexing: this runs every frame, and NumPy's fancy indexing costs more than the arithmetic.
    b = bs
    brow = (float(b[_BROW[0]]) + float(b[_BROW[1]]) + float(b[_BROW[2]])) / 3.0
    look = (float(b[_LOOK_UP[0]]) + float(b[_LOOK_UP[1]])) / 2.0
    smile = (float(b[_SMILE[0]]) + float(b[_SMILE[1]])) / 2.0
    return brow, look, smile, float(b[_JAW]), float(b[_CHEEK])


def signals(bs: np.ndarray, look_k: float = DEFAULT_LOOK_K) -> dict[str, float]:
    brow, look, smile, jaw, cheek = raw_signals(bs)
    return {"brow_raise": brow - look_k * look, "smile": smile, "mouth_open": jaw, "cheek_puff": cheek}


# --------------------------------------------------------------------------- profile


@dataclass
class Trigger:
    """One trained face gesture."""

    on: float  # the signal must pass this ...
    off: float  # ... and fall below this before it can count again
    resting: float  # typical level of the resting face
    separation: float  # 0.5 no better than chance .. 1 always apart from the resting face
    held: float  # share of the gesture's frames above "on"
    false_per_min: float | None  # mistaken triggers a minute while talking or reading; None: not measured
    ok: bool  # good enough to offer
    reason: str = ""  # why not, in a few words


@dataclass
class FaceGestureProfile:
    triggers: dict[str, Trigger] = field(default_factory=dict)
    look_k: float = DEFAULT_LOOK_K
    created: float = field(default_factory=time.time)

    def usable(self, kind: str) -> bool:
        trig = self.triggers.get(kind)
        return trig is not None and trig.ok

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": PROFILE_VERSION, "look_k": self.look_k, "created": self.created,
                "triggers": {k: asdict(v) for k, v in self.triggers.items()}}  # fmt: skip
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), "utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> FaceGestureProfile | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
            if int(data.get("version", 0)) != PROFILE_VERSION:
                return None
            triggers = {k: Trigger(**v) for k, v in data.get("triggers", {}).items() if k in FACE_KINDS}
            return cls(triggers, float(data.get("look_k", DEFAULT_LOOK_K)), float(data.get("created", 0.0)))
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("Could not load the face gesture profile %s: %s", path, e)
            return None


# --------------------------------------------------------------------------- detector


@dataclass
class FaceGestureConfig:
    hold_s: float = 0.15  # the gesture must be held this long (the concept review suggests 120 to 200 ms)
    refractory_s: float = 0.8  # after release, wait this long before the same gesture can count again
    talk_window_s: float = 1.0  # mouth movement in this window before a mouth gesture means talking
    talk_share: float = 0.25


@dataclass(frozen=True, slots=True)
class FaceHit:
    kind: str
    onset: float  # when the signal began to rise: the pointer position from then is used
    t: float


@dataclass(slots=True)
class _State:
    phase: str = "idle"  # idle, rising, held, refractory
    armed: bool = False  # has been below "off" since the last trigger: only a rising edge counts
    low_t: float = 0.0  # last time the signal was below "off"
    start: float = 0.0  # when it passed "on"
    until: float = 0.0  # end of the refractory period


class FaceGestureDetector:
    """Feed it the blendshapes every frame; it returns the gestures that fired."""

    def __init__(self, profile: FaceGestureProfile | None = None, cfg: FaceGestureConfig | None = None) -> None:
        self.cfg = cfg or FaceGestureConfig()
        self.profile = profile
        self._states: dict[str, _State] = {}

    def set_profile(self, profile: FaceGestureProfile | None) -> None:
        self.profile = profile
        self.reset()

    def reset(self) -> None:
        self._states.clear()

    def update(
        self,
        t: float,
        bs: np.ndarray | None,
        kinds: Iterable[str],
        *,
        blocked: bool = False,
        talking: Callable[[float], bool] | None = None,
    ) -> list[FaceHit]:
        """``kinds``: the gestures to watch (trained and mapped to an action).  ``blocked``: watch but never fire
        (paused, calibrating, eyes closing).  ``talking(t)``: was the user talking just before ``t``?"""
        prof = self.profile
        if prof is None or not has_blendshapes(bs):
            self._states.clear()
            return []
        assert bs is not None
        watched = [k for k in kinds if k in prof.triggers]
        for k in list(self._states):
            if k not in watched:
                del self._states[k]  # a gesture switched off starts clean when it comes back
        if not watched:
            return []
        values = signals(bs, prof.look_k)
        cfg = self.cfg
        hits: list[FaceHit] = []
        for kind in watched:
            trig = prof.triggers[kind]
            st = self._states.setdefault(kind, _State())
            s = values[kind]
            if st.phase == "refractory":
                if s < trig.off:
                    st.low_t = t
                if t < st.until:
                    continue
                st.phase = "idle"
            if st.phase == "idle":
                if s < trig.off:
                    st.armed = True
                    st.low_t = t
                elif st.armed and s >= trig.on:
                    st.phase = "rising"
                    st.start = t
                continue
            if st.phase == "rising":
                if s < trig.off:
                    st.phase = "idle"  # too short: a passing expression
                    st.low_t = t
                    continue
                if t - st.start >= cfg.hold_s:
                    st.phase = "held"
                    st.armed = False
                    if blocked:
                        continue
                    if kind in MOUTH_KINDS and talking is not None and talking(st.low_t):
                        log.debug("Face gesture %s ignored: talking", kind)
                        continue
                    hits.append(FaceHit(kind, st.low_t, t))
                continue
            if st.phase == "held" and s < trig.off:
                st.phase = "refractory"
                st.until = t + cfg.refractory_s
                # Not armed until the signal is seen low after the refractory period: a gesture begun during
                # it never counts, rather than counting late at a moment the user did not choose.
                st.armed = False
                st.low_t = t
        return hits


# --------------------------------------------------------------------------- trainer


@dataclass(slots=True)
class TrainingFrame:
    t: float
    label: str  # neutral, brow_raise, smile, mouth_open, cheek_puff, talking or reading
    segment: int  # consecutive frames of one request share a number
    bs: np.ndarray
    mouth: tuple[float, float, float] | None  # attention mouth signals
    closure: tuple[float, float]
    pose: tuple[float, float]  # yaw, pitch relative to neutral


@dataclass
class FaceFitResult:
    profile: FaceGestureProfile | None
    ok: bool
    message: str
    details: dict


def separation(rest: np.ndarray, gesture: np.ndarray) -> float:
    """Chance that a gesture frame reads higher than a resting frame (the area under the ROC curve)."""
    if len(rest) == 0 or len(gesture) == 0:
        return 0.5
    a = np.sort(rest)
    below = np.searchsorted(a, gesture, side="left")
    equal = np.searchsorted(a, gesture, side="right") - below
    return float(np.mean((below + 0.5 * equal) / len(a)))


def _look_k(frames: Sequence[TrainingFrame]) -> float:
    """How much looking up raises the brow signal, from frames where nobody raised their eyebrows on purpose."""
    if len(frames) < 30:
        return DEFAULT_LOOK_K
    raw = np.array([raw_signals(f.bs) for f in frames])
    brow, look = raw[:, 0], raw[:, 1]
    if float(np.var(look)) < 1e-4:
        return DEFAULT_LOOK_K  # the eyes hardly looked up: nothing to fit
    k = float(np.cov(brow, look)[0, 1] / np.var(look, ddof=1))
    return min(max(k, 0.0), 1.5)


def _simulate(
    segments: Sequence[Sequence[TrainingFrame]], profile: FaceGestureProfile, cfg: FaceGestureConfig, eye_cfg: GestureConfig
) -> tuple[dict[str, int], float]:
    """Run the detectors over talking and reading: how many times each trigger went off, and over how many minutes."""
    counts = {k: 0 for k in FACE_KINDS + EYE_KINDS}
    minutes = 0.0
    kinds = list(profile.triggers)
    for seg in segments:
        if len(seg) < 2:
            continue
        det = FaceGestureDetector(profile, cfg)
        eyes = EyeGestureDetector(replace(eye_cfg))
        att = AttentionTracker()
        for f in seg:
            att.update_signals(f.t, f.pose[0], f.pose[1], f.mouth)
            for hit in det.update(f.t, f.bs, kinds,
                                  talking=lambda t0: att.talking(t0 - cfg.talk_window_s, t0 - 0.05, cfg.talk_share)):  # fmt: skip
                counts[hit.kind] += 1
            for ev in eyes.update(f.t, f.closure[0], f.closure[1]):
                if isinstance(ev, Gesture):
                    counts[ev.kind] += 1
        minutes += (seg[-1].t - seg[0].t + 1.0 / 30.0) / 60.0
    return counts, minutes


def _join(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def fit_face_gestures(
    frames: Sequence[TrainingFrame], cfg: FaceGestureConfig | None = None, eye_cfg: GestureConfig | None = None
) -> FaceFitResult:
    """Thresholds for each recorded gesture, how well each separates, and how often each misfires."""
    cfg = cfg or FaceGestureConfig()
    eye_cfg = eye_cfg or GestureConfig()
    frames = [f for f in frames if has_blendshapes(f.bs)]
    by: dict[str, list[TrainingFrame]] = {}
    for f in frames:
        by.setdefault(f.label, []).append(f)
    details: dict = {"frames": {k: len(v) for k, v in by.items()}, "scores": {}, "offer": [], "suggested_actions": {}}
    neutral = by.get("neutral", [])
    if len(neutral) < 20:
        return FaceFitResult(None, False, "Not enough data. Keep your face in view of the camera and try again.", details)

    look_k = _look_k(neutral + by.get("reading", []))
    details["look_k"] = round(look_k, 3)

    def values(items: Sequence[TrainingFrame], kind: str) -> np.ndarray:
        return np.array([signals(f.bs, look_k)[kind] for f in items])

    triggers: dict[str, Trigger] = {}
    for kind in FACE_KINDS:
        shown = by.get(kind, [])
        if len(shown) < 10:
            continue
        rest = values(neutral, kind)
        gest = values(shown, kind)
        rest_hi = float(np.percentile(rest, 95))
        peak = float(np.percentile(gest, 50))
        gap = peak - rest_hi
        sep = separation(rest, gest)
        if gap < MIN_GAP:
            triggers[kind] = Trigger(math.inf, math.inf, float(np.median(rest)), sep, 0.0, None, False, "not apart")
            continue
        on = rest_hi + 0.5 * gap
        off = rest_hi + 0.2 * gap
        held = float(np.mean(gest >= on))
        triggers[kind] = Trigger(on, off, float(np.median(rest)), sep, held, None, False)

    profile = FaceGestureProfile({k: v for k, v in triggers.items() if math.isfinite(v.on)}, look_k)

    # Mistaken triggers while talking and reading, each request replayed on its own.
    segs: dict[tuple[str, int], list[TrainingFrame]] = {}
    for f in by.get("talking", []) + by.get("reading", []):
        segs.setdefault((f.label, f.segment), []).append(f)
    counts, minutes = _simulate(list(segs.values()), profile, cfg, eye_cfg)
    details["false_minutes"] = round(minutes, 2)
    rates = {k: (counts[k] / minutes if minutes > 0.1 else None) for k in counts}
    details["eye_false_per_min"] = {k: None if rates[k] is None else round(rates[k], 2) for k in EYE_KINDS}

    # Does the mouth move enough while talking for the speech gate to work for this user?
    talk = by.get("talking", [])
    if len(talk) >= 30:
        att = AttentionTracker()
        for f in talk:
            att.update_signals(f.t, f.pose[0], f.pose[1], f.mouth)
        share = att.query(talk[0].t, talk[-1].t).mouth
        details["talking_mouth_moving"] = None if share is None else round(share, 2)
        details["mouth_gate_ok"] = share is not None and share >= 0.3

    for kind, trig in triggers.items():
        if not math.isfinite(trig.on):
            continue
        trig.false_per_min = None if rates[kind] is None else round(float(rates[kind]), 2)
        if trig.separation < MIN_SEPARATION or trig.held < MIN_HELD:
            trig.reason = "not apart" if trig.separation < MIN_SEPARATION else "not held"
        elif trig.false_per_min is not None and trig.false_per_min > MAX_FALSE_PER_MIN:
            trig.reason = "misfires"
        else:
            trig.ok = True

    def score(k: str) -> float:
        t = triggers[k]
        return t.separation * t.held - 0.05 * (t.false_per_min or 0.0)

    usable = sorted((k for k in triggers if triggers[k].ok), key=score, reverse=True)
    offer = usable[:3]
    details["triggers"] = {
        k: {"separation": round(t.separation, 3), "held": round(t.held, 2), "false_per_min": t.false_per_min,
            "on": None if not math.isfinite(t.on) else round(t.on, 3), "resting": round(t.resting, 3),
            "ok": t.ok, "reason": t.reason}  # fmt: skip
        for k, t in triggers.items()
    }
    # Only gestures good enough to offer get a score, so a wizard that takes the best two or three can
    # never offer one that failed.
    details["scores"] = {k: round(min(max(score(k), 0.0), 1.0), 3) for k in usable}
    details["offer"] = offer
    details["suggested_actions"] = dict(zip(offer, ("click", "right_click", "double_click")))

    sentences = []
    if offer:
        names = [NAMES[k] for k in offer]
        sentences.append(f"{'These work' if len(names) > 1 else 'This works'} well for you: {_join(names)}.")
    else:
        sentences.append("None of the face gestures could be told apart reliably, so they stay off. Blinks and winks still work.")
    misfiring = [k for k, t in triggers.items() if t.reason == "misfires"]
    if misfiring:
        worst = max(misfiring, key=lambda k: triggers[k].false_per_min or 0.0)
        rate = triggers[worst].false_per_min or 0.0
        often = "about once a minute" if rate < 1.5 else f"about {rate:.0f} times a minute"
        sentences.append(f"{NAMES[worst].capitalize()} went off {often} while you talked or read, so it is not offered.")
    apart = [NAMES[k] for k, t in triggers.items() if t.reason in ("not apart", "not held")]
    if apart and offer:
        sentences.append(f"{_join(apart).capitalize()} could not be told apart from your resting face.")
    if details.get("mouth_gate_ok") is False:
        sentences.append("Your mouth moved too little while you talked for Sightline to notice speech.")
    return FaceFitResult(profile, True, " ".join(sentences), details)
