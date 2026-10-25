"""Run a recording through the vision pipeline and report what it would have done.

    python tools/vision_replay.py                          # the newest recording
    python tools/vision_replay.py PATH                     # a recording folder
    python tools/vision_replay.py --video clip.mp4         # a video file, through the real face model
    python tools/vision_replay.py PATH --timeline          # also list every gesture and click
    python tools/vision_replay.py PATH --set blink_click_ms=500 --set winks=false
    python tools/vision_replay.py PATH --fresh             # ignore the saved calibration

Nothing on screen moves: the pipeline runs against a fake desktop, and the
clicks it would have made are listed instead of performed.  The saved
calibration of the current profile is read but never changed.  The report
shows, for each step of a guided recording, how often the face was found,
how closed each eye read, and which gestures and clicks happened; then
natural blink lengths, an eye calibration fitted from the labelled steps,
and suggestions for the settings.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import sys
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# What the guided steps of tools/vision_record.py ask for.  A gesture in any other step is a misfire.
EXPECTED: dict[str, tuple[str, int]] = {
    "long_blinks": ("long_blink", 4),
    "hold_blinks": ("hold_blink", 3),
    "winks_left": ("wink_left", 3),
    "winks_right": ("wink_right", 3),
}
NAMES = {"long_blink": "long blinks", "hold_blink": "hold blinks", "wink_left": "left winks", "wink_right": "right winks"}
EYE_LABELS = ("open", "closed", "wink_left", "wink_right")
SETTLE_S = 0.8  # the start of a step, while the user reacts to the prompt, is left out of the eye fit
GAP_S = 0.5


@dataclass
class Segment:
    label: str
    t0: float
    t1: float
    frames: int = 0
    faces: int = 0
    closure_l: list[float] = field(default_factory=list)
    closure_r: list[float] = field(default_factory=list)
    gestures: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    pointer: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class ReplayResult:
    t0: float
    frames: int
    duration: float
    fps: float
    face_share: float
    segments: list[Segment]
    gaps: list[tuple[float, float, str]]  # start, length, step
    timeline: list[tuple[float, str, str]]  # time, step, what happened
    actions: list[Any]
    blink_ms: list[float]
    eye_fit: Any  # EyeFitResult or None
    pipeline_ms: float
    hints: Counter
    distance_cm: float | None


class ReplayDisplay:
    """The monitors the saved calibration was made on, so the saved gaze model still applies."""

    def __init__(self, rects: Sequence[Sequence[float]]) -> None:
        from sightline.core.types import Monitor, Rect

        self._mons = [
            Monitor(f"replay{i}", f"Screen {i + 1}", Rect(*r), Rect(*r), 1.0, i == 0, None) for i, r in enumerate(rects)
        ]

    def monitors(self):
        return list(self._mons)

    def primary(self):
        return self._mons[0]

    def virtual_rect(self):
        from sightline.core.types import Rect

        return Rect.from_ltrb(
            min(m.rect.left for m in self._mons),
            min(m.rect.top for m in self._mons),
            max(m.rect.right for m in self._mons),
            max(m.rect.bottom for m in self._mons),
        )

    def monitor_at(self, x: float, y: float):
        def dist(m) -> float:
            r = m.rect
            dx = max(r.left - x, 0.0, x - r.right)
            dy = max(r.top - y, 0.0, y - r.bottom)
            return math.hypot(dx, dy)

        return min(self._mons, key=dist)


# --------------------------------------------------------------------------- inputs


def newest_recording() -> Path | None:
    from sightline.core import paths

    found = sorted(p.parent for p in paths.recordings_dir().glob("*/meta.json"))
    return found[-1] if found else None


def frames_from_video(path: Path, download: bool) -> tuple[list[tuple[float, Any]], dict, tuple[int, int]]:
    from sightline.vision.camera import VideoFileSource, silence_opencv_logs
    from sightline.vision.landmarks import FaceLandmarker
    from tools.vision_record import model_path

    silence_opencv_logs()
    landmarker = FaceLandmarker(model_path(download))
    src = VideoFileSource(path)
    src.open()
    frames: list[tuple[float, Any]] = []
    size = (0, 0)
    try:
        while (got := src.read()) is not None:
            image, t = got
            size = (image.shape[1], image.shape[0])
            frames.append((t, landmarker.detect(image, t)))
    finally:
        src.close()
        landmarker.close()
    return frames, {"camera": src.description, "file": str(path)}, size


def apply_overrides(cfg: Any, pairs: Iterable[str]) -> list[str]:
    """``name=value`` pairs onto the vision settings, converted to each setting's type."""
    names = {f.name for f in dataclasses.fields(cfg)}
    done = []
    for pair in pairs:
        key, sep, value = pair.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in names:
            raise SystemExit(f"No vision setting called {key!r}. Settings: {', '.join(sorted(names))}")
        current = getattr(cfg, key)
        new: Any
        if isinstance(current, bool):
            new = value.lower() in ("1", "true", "yes", "on")
        elif isinstance(current, Enum):
            new = type(current)(value)
        elif isinstance(current, int):
            new = int(value)
        elif isinstance(current, float):
            new = float(value)
        elif isinstance(current, (dict, list)):
            new = json.loads(value)
        else:
            new = value
        setattr(cfg, key, new)
        done.append(f"{key}={new}")
    return done


# --------------------------------------------------------------------------- replay


def make_segments(labels: Sequence[tuple[float, str]], t_first: float, t_last: float) -> list[Segment]:
    marks = sorted((float(t), str(text)) for t, text in labels)
    if not marks or marks[0][0] > t_first:
        marks.insert(0, (t_first, "all" if not marks else "start"))
    segs = []
    for i, (t, text) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else t_last
        segs.append(Segment(text, t, end))
    return segs


def describe_action(a: Any) -> str:
    if a.name == "mouse.click":
        n = int(a.args.get("count", 1))
        kind = {1: "click", 2: "double click"}.get(n, f"{n}x click")
        return f"{a.args.get('button', 'left')} {kind}"
    return a.name


def replay(
    frames: Sequence[tuple[float, Any]],
    labels: Sequence[tuple[float, str]],
    settings: Any,
    *,
    display: Any = None,
    load_profile: bool = True,
) -> ReplayResult:
    from sightline.core.context import AppContext
    from sightline.core.events import EyeGesture, EyeHold, EyeState, FaceGesture, FaceStatus, PointerSample
    from sightline.vision import features as feat
    from sightline.vision.eyes import EyeSample, fit_eye_profile
    from sightline.vision.pipeline import VisionPipeline

    if not frames:
        raise SystemExit("The recording has no frames.")
    ctx = AppContext.for_tests(settings)  # real bus and state, fake desktop: nothing on screen moves
    if display is not None:
        ctx.desktop.display = display  # type: ignore[union-attr]
    actions: list[Any] = []
    ctx.actions.dispatch = actions.append  # type: ignore[method-assign]
    events: list[Any] = []
    for cls in (EyeGesture, EyeHold, EyeState, FaceGesture, FaceStatus, PointerSample):
        ctx.bus.subscribe(cls, events.append)
    pipe = VisionPipeline(ctx, load_profile=load_profile)
    cfg = settings.vision
    mirrored, fov = bool(cfg.camera_mirrored), float(cfg.camera_fov_deg)

    t_first, t_last = float(frames[0][0]), float(frames[-1][0])
    segs = make_segments(labels, t_first, t_last)
    si = 0
    timeline: list[tuple[float, str, str]] = []
    samples: list[Any] = []
    gaps: list[tuple[float, float, str]] = []
    hints: Counter = Counter()
    distances: list[float] = []
    lost_since: float | None = None
    lost_label = ""
    spent = 0.0
    eye_state = "unknown"
    found = 0
    for t, face in frames:
        while si + 1 < len(segs) and t >= segs[si + 1].t0:
            si += 1
        seg = segs[si]
        ne, na = len(events), len(actions)
        started = time.perf_counter()
        pipe.process(face, t)
        spent += time.perf_counter() - started
        seg.frames += 1
        if face is not None:
            seg.faces += 1
            found += 1
        for ev in events[ne:]:
            if isinstance(ev, EyeState):
                eye_state = ev.state
                if ev.state != "unknown":
                    seg.closure_l.append(ev.left_closed)
                    seg.closure_r.append(ev.right_closed)
            elif isinstance(ev, PointerSample):
                if ev.phase not in ("lost", "paused"):
                    seg.pointer.append((ev.x, ev.y))
            elif isinstance(ev, EyeGesture):
                seg.gestures.append(ev.kind)
                timeline.append((t, seg.label, f"{ev.kind.replace('_', ' ')}, {ev.duration * 1000:.0f} ms"))
            elif isinstance(ev, FaceGesture):
                seg.gestures.append(ev.kind)
                timeline.append((t, seg.label, ev.kind.replace("_", " ")))
            elif isinstance(ev, EyeHold):
                what = "eyes opened" if ev.stage == 0 else f"hold stage {ev.stage}"
                timeline.append((t, seg.label, what + (" (cancelled)" if ev.cancelled else "")))
            elif isinstance(ev, FaceStatus):
                if ev.hint:
                    hints[ev.hint] += 1
                if ev.present and ev.distance_cm:
                    distances.append(ev.distance_cm)
        for a in actions[na:]:
            text = describe_action(a)
            seg.actions.append(text)
            where = f" at {a.args['x']:.0f}, {a.args['y']:.0f}" if "x" in a.args else ""
            timeline.append((t, seg.label, f"would {text}{where}"))
        if face is None:
            if lost_since is None:
                lost_since, lost_label = t, seg.label
        else:
            if lost_since is not None and t - lost_since >= GAP_S:
                gaps.append((lost_since, t - lost_since, lost_label))
            lost_since = None
            if seg.label in EYE_LABELS and seg.t0 + SETTLE_S <= t <= seg.t1 - 0.2:
                if seg.label == "open" and eye_state != "open":
                    continue  # a natural blink during an eyes-open step
                left, right = feat.user_eyes(feat.measure(face, fov), mirrored)
                samples.append(EyeSample(seg.label, left.blink, right.blink, left.ear, right.ear))
    if lost_since is not None and t_last - lost_since >= GAP_S:
        gaps.append((lost_since, t_last - lost_since, lost_label))

    labels_seen = Counter(s.label for s in samples)
    eye_fit = fit_eye_profile(samples) if labels_seen["open"] >= 10 and labels_seen["closed"] >= 10 else None
    duration = t_last - t_first
    return ReplayResult(
        t0=t_first,
        frames=len(frames),
        duration=duration,
        fps=(len(frames) - 1) / duration if duration > 0 else 0.0,
        face_share=found / len(frames),
        segments=segs,
        gaps=gaps,
        timeline=timeline,
        actions=actions,
        blink_ms=[d * 1000.0 for d in pipe.gestures.blink_durations],
        eye_fit=eye_fit,
        pipeline_ms=1000.0 * spent / len(frames),
        hints=hints,
        distance_cm=float(np.median(distances)) if distances else None,
    )


# --------------------------------------------------------------------------- report


def clock(s: float) -> str:
    s = max(s, 0.0)
    return f"{int(s // 60)}:{s % 60:04.1f}"


def peak(values: Sequence[float]) -> float:
    return float(np.percentile(values, 98)) if values else 0.0


def suggestions(res: ReplayResult, cfg: Any) -> list[str]:
    from sightline.vision.eyes import GestureConfig

    g = GestureConfig()
    out: list[str] = []
    click_ms = float(cfg.blink_click_ms)
    if res.blink_ms:
        longest = max(res.blink_ms)
        if longest > 0.85 * click_ms:
            new = int(math.ceil(longest * 1.25 / 50.0) * 50)
            out.append(
                f"Your longest natural blink lasted {longest:.0f} ms, close to the {click_ms:.0f} ms a long blink needs. "
                f"Setting blink_click_ms to about {new} keeps natural blinks from clicking."
            )
        elif longest < 0.5 * click_ms and click_ms > 350 and len(res.blink_ms) >= 10:
            new = max(300, int(math.ceil(longest * 2.0 / 50.0) * 50))
            out.append(f"Your natural blinks are short (longest {longest:.0f} ms): blink_click_ms could come down to {new}.")

    by_label: dict[str, list[Segment]] = {}
    for s in res.segments:
        by_label.setdefault(s.label, []).append(s)
    for label, (kind, wanted) in EXPECTED.items():
        segs = by_label.get(label)
        if not segs:
            continue
        got = sum(s.gestures.count(kind) for s in segs)
        if got >= wanted:
            continue
        cl = [v for s in segs for v in s.closure_l]
        cr = [v for s in segs for v in s.closure_r]
        what = NAMES[kind]
        if kind.startswith("wink") and not cfg.winks:
            out.append(f"Winks are turned off in the settings, so the {wanted} {what} did nothing.")
        elif not kind.startswith("wink") and not cfg.blink_clicks:
            out.append(f"Blink clicks are turned off in the settings, so the {wanted} {what} did nothing.")
        elif kind.startswith("wink"):
            eye, other = ("left", "right") if kind == "wink_left" else ("right", "left")
            shut, open_ = (cl, cr) if eye == "left" else (cr, cl)
            closed_frames = [o for s_, o in zip(shut, open_) if s_ > g.close]
            if peak(shut) < g.close:
                why = f"your {eye} eye never read as closed (it reached {peak(shut):.2f}; closed is over {g.close})"
            elif closed_frames and float(np.median(closed_frames)) > g.clear_left:
                why = f"your {other} eye narrowed too ({float(np.median(closed_frames)):.2f} while winking), so it did not look like a wink"
            else:
                why = "the winks were too short or too long"
            out.append(f"Only {got} of {wanted} {what} were seen: {why}. Eye calibration may fix this; otherwise turn winks off.")
        else:
            out.append(
                f"Only {got} of {wanted} {what} were seen (eyes reached {peak(cl):.2f} and {peak(cr):.2f} closed). "
                f"Try closing your eyes a little longer, or lower blink_click_ms."
            )

    misfires = [f"{k.replace('_', ' ')} in '{s.label}'" for s in res.segments if s.label not in EXPECTED for k in s.gestures]
    if misfires and any(s.label in EXPECTED for s in res.segments):
        shown = ", ".join(misfires[:5]) + (" ..." if len(misfires) > 5 else "")
        out.append(
            f"{len(misfires)} gestures happened in steps that asked for none ({shown}). "
            "If they were not deliberate, raise blink_click_ms, run eye calibration, or turn winks off."
        )

    present = [s for s in res.segments if s.label not in ("away",)]
    frames = sum(s.frames for s in present)
    if frames:
        share = sum(s.faces for s in present) / frames
        if share < 0.9:
            out.append(
                f"The face was found in only {share:.0%} of frames. More even light on the face and a camera at eye level help."
            )
    if 0 < res.fps < 20:
        out.append(f"Only {res.fps:.0f} frames a second. Close other apps that use the camera, or add light (cameras slow down in the dark).")
    if res.distance_cm is not None and res.distance_cm > 85:
        out.append(f"You sat about {res.distance_cm:.0f} cm from the camera. Closer (50 to 70 cm) gives steadier tracking.")
    return out


def print_report(res: ReplayResult, meta: dict, cfg: Any, source: str, show_timeline: bool, overrides: list[str]) -> None:
    print(f"Recording: {source}")
    camera = meta.get("camera") or "unknown camera"
    print(f"  {camera}, {clock(res.duration)} long, {res.frames} frames, {res.fps:.1f} frames a second")
    if overrides:
        print(f"  Settings changed for this replay: {', '.join(overrides)}")
    print(f"  Face found in {res.face_share:.0%} of frames; pipeline {res.pipeline_ms:.2f} ms a frame")
    if res.distance_cm is not None:
        print(f"  Distance from the camera: about {res.distance_cm:.0f} cm")
    if res.gaps:
        longest = max(res.gaps, key=lambda g: g[1])
        print(
            f"  {len(res.gaps)} gap{'' if len(res.gaps) == 1 else 's'} without a face over {GAP_S} s; longest {longest[1]:.1f} s "
            f"at {clock(longest[0] - res.t0)} in '{longest[2]}'"
        )
    if res.hints:
        common = ", ".join(f"{h} ({n})" for h, n in res.hints.most_common(3))
        print(f"  Advice shown most: {common}")

    print()
    print(f"  {'Step':<13}{'At':>7} {'Face':>5}  {'Closed L/R':>10}  {'Spread':>6}  Gestures and clicks")
    for s in res.segments:
        face = f"{s.faces / s.frames:.0%}" if s.frames else "-"
        closed = f"{peak(s.closure_l):.2f}/{peak(s.closure_r):.2f}" if s.closure_l else "-"
        spread = "-"
        if len(s.pointer) >= 10:
            p = np.array(s.pointer)
            q = np.percentile(p, [10, 90], axis=0)
            spread = f"{float(np.hypot(*(q[1] - q[0]))):.0f}"
        happened = Counter(s.gestures)
        clicks = Counter(s.actions)
        parts = [f"{k.replace('_', ' ')} x{n}" for k, n in happened.items()]
        parts += [f"-> {k} x{n}" for k, n in clicks.items()]
        want = EXPECTED.get(s.label)
        if want:
            parts.append(f"(asked for {want[1]} {NAMES[want[0]]})")
        print(f"  {s.label:<13}{clock(s.t0 - res.t0):>7} {face:>5}  {closed:>10}  {spread:>6}  {', '.join(parts) or '-'}")
    print("  (Closed: 98th percentile of how closed each eye read, 0 open to 1 shut. Spread: pointer travel in px.)")

    print()
    if res.blink_ms:
        b = np.array(res.blink_ms)
        print(
            f"Natural blinks: {len(b)}, median {np.median(b):.0f} ms, 90% under {np.percentile(b, 90):.0f} ms, "
            f"longest {b.max():.0f} ms (a long blink needs {float(cfg.blink_click_ms):.0f} ms)"
        )
    else:
        print("Natural blinks: none seen")
    if res.eye_fit is not None:
        print(f"Eye calibration from this recording: {res.eye_fit.message}")
        details = {k: v for k, v in res.eye_fit.details.items() if isinstance(v, (int, float, str, bool))}
        if details:
            print("  " + ", ".join(f"{k} {v:.2f}" if isinstance(v, float) else f"{k} {v}" for k, v in details.items()))

    tips = suggestions(res, cfg)
    print()
    print("Suggestions:" if tips else "Suggestions: none, the settings look right for this recording.")
    for tip in tips:
        print(f"  - {tip}")

    if show_timeline:
        print()
        print("Timeline:")
        for t, label, what in res.timeline:
            print(f"  {clock(t - res.t0):>7}  {label:<13} {what}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording", nargs="?", type=Path, help="recording folder (default: the newest)")
    ap.add_argument("--video", type=Path, help="replay a video file through the real face model")
    ap.add_argument("--save", type=Path, help="with --video: also save the face data as a recording here")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="change a vision setting")
    ap.add_argument("--fresh", action="store_true", help="ignore the saved calibration")
    ap.add_argument("--timeline", action="store_true", help="list every gesture and click")
    ap.add_argument("--download", action="store_true", help="download the face model if it is missing")
    args = ap.parse_args(argv)

    import logging

    from sightline.core.fakes import FakeDisplay
    from sightline.settings import Settings
    from sightline.vision.profile import VisionProfile
    from sightline.vision.recorder import Recorder, Recording

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings.load()
    labels: list[tuple[float, str]] = []
    if args.video:
        frames, meta, size = frames_from_video(args.video, args.download)
        source = str(args.video)
        if args.save:
            rec = Recorder(args.save, meta)
            for t, face in frames:
                rec.add(t, face, size=size)
            rec.close()
            print(f"Saved the face data to {args.save}")
    else:
        folder = args.recording or newest_recording()
        if folder is None:
            raise SystemExit("No recordings yet. Make one with tools/vision_record.py.")
        recording = Recording.load(folder)
        frames = list(recording.frames())
        meta, labels, source = recording.meta, recording.labels, str(folder)

    recorded = meta.get("vision") or {}
    for key in ("camera_mirrored", "camera_fov_deg"):
        if key in recorded:
            setattr(settings.vision, key, type(getattr(settings.vision, key))(recorded[key]))
    overrides = apply_overrides(settings.vision, args.set)

    display = None
    if not args.fresh:
        try:
            model, _, _ = VisionProfile(settings.general.profile).load_gaze()
        except Exception:
            model = None
        rects = model.meta.get("monitors") if model is not None else None
        if rects:
            display = ReplayDisplay(rects)
    if display is None and meta.get("screen"):
        w, h = meta["screen"]
        display = FakeDisplay(int(w), int(h))

    res = replay(frames, labels, settings, display=display, load_profile=not args.fresh)
    print_report(res, meta, settings.vision, source, args.timeline, overrides)
    return 0


if __name__ == "__main__":
    sys.exit(main())
