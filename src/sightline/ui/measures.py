"""Numbers for the practice room and the end of calibration, without Qt.

* ``comfort_line``: the plain "comfort size" sentence: vision's own from the
  calibration report when it sent one, else worked out here from the error.
* ``tapping_order``, ``Trial`` and ``summarise_tapping``: an ISO 9241-9 style
  multidirectional tapping test (effective width from the spread of the
  selections, throughput in bits per second, error rate).
* ``FalseActivations``: actions undone with ``action.undo`` or cancelled
  within 3 s, per hour, from ``ActionExecuted``.
* ``summary_html``: the one-page summary a therapist can read.
"""

from __future__ import annotations

import html
import math
import statistics
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

# --------------------------------------------------------------------------- comfort size

WINDOWS_SCALES = (1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 3.0)
TYPICAL_CONTROL_PX = 32.0  # a button or list row at 100%, in pixels
ARM_LENGTH_MM = 600.0  # viewing distance assumed when the panel size is unknown


def error_mm(error_px: float | None, error_deg: float | None, mm_per_px: float | None) -> float | None:
    """The pointing error on the glass, from pixels and the panel size, else from degrees at arm's length."""
    if error_px is not None and mm_per_px:
        return error_px * mm_per_px
    if error_deg is not None:
        return ARM_LENGTH_MM * math.tan(math.radians(error_deg))
    return None


def comfort_line(
    error_px: float | None,
    error_deg: float | None,
    mm_per_px: float | None,
    windows_scale: float,
    details: Mapping[str, object] | None = None,
) -> str | None:
    """ "At your accuracy, controls smaller than about 2.5 cm are hard to hit by eyes alone. ..."

    ``details`` are a calibration report's: when vision put its own sentence there
    (``details["comfort"]``, worked out from its accuracy map and the monitor it
    measured), that is the one shown, so the wizard and vision never disagree.
    Otherwise the sentence is worked out here, and None when there is no error
    figure.  A control needs to be about twice the error across to be hit
    reliably; snapping, numbers and zoom cover the rest, so the scale advice aims
    for typical controls at 60% of that.
    """
    reported = (details or {}).get("comfort")
    if isinstance(reported, str) and reported.strip():
        return reported.strip()
    err = error_mm(error_px, error_deg, mm_per_px)
    if err is None or err <= 0:
        return None
    comfort_cm = max(0.5, round(2.0 * err / 10.0 * 2) / 2)  # nearest half centimetre
    size = f"{comfort_cm:g}"
    line = f"At your accuracy, controls smaller than about {size} cm are hard to hit by eyes alone."
    if mm_per_px:
        needed = 0.6 * 2.0 * err / (TYPICAL_CONTROL_PX * mm_per_px)
        target = next((s for s in WINDOWS_SCALES if s >= needed - 1e-6), WINDOWS_SCALES[-1])
        if target > windows_scale + 1e-6:
            line += f" Setting Windows text and app size to {round(target * 100)}% will help."
        else:
            line += " Numbers, the grid and zoom help with anything smaller."
    return line


# --------------------------------------------------------------------------- ISO 9241-9 tapping


def tapping_order(n: int) -> list[int]:
    """The classic multidirectional order: each selection is across the circle from the last."""
    step = (n + 1) // 2
    return [(i * step) % n for i in range(n)]


def circle_targets(cx: float, cy: float, amplitude: float, n: int) -> list[tuple[float, float]]:
    """``n`` target centres on a circle whose diameter is the movement amplitude, first one at the top."""
    r = amplitude / 2.0
    return [(cx + r * math.sin(2 * math.pi * i / n), cy - r * math.cos(2 * math.pi * i / n)) for i in range(n)]


@dataclass(frozen=True)
class Trial:
    start: tuple[float, float]  # centre of the previous target
    target: tuple[float, float]  # centre of this target
    hit_point: tuple[float, float]  # where the selection landed
    ms: float  # time since the previous selection
    hit: bool


@dataclass(frozen=True)
class TappingResult:
    width_px: float
    amplitude_px: float
    trials: int
    errors: int
    error_rate: float  # 0..1
    movement_ms: float  # mean
    effective_width_px: float
    effective_amplitude_px: float
    id_bits: float  # effective index of difficulty
    throughput_bps: float


def summarise_tapping(trials: Sequence[Trial], width_px: float, amplitude_px: float) -> TappingResult | None:
    """Effective width We = 4.133 x SD of the overshoot along the task axis; TP = IDe / MT (MacKenzie)."""
    if len(trials) < 2:
        return None
    dx: list[float] = []
    ae: list[float] = []
    for t in trials:
        ax, ay = t.target[0] - t.start[0], t.target[1] - t.start[1]
        a = math.hypot(ax, ay) or 1.0
        ux, uy = ax / a, ay / a
        d = (t.hit_point[0] - t.target[0]) * ux + (t.hit_point[1] - t.target[1]) * uy
        dx.append(d)
        ae.append(a + d)
    sd = statistics.stdev(dx) if len(dx) > 1 else 0.0
    we = 4.133 * sd if sd > 0 else width_px  # no spread at all: fall back to the nominal width
    ae_mean = statistics.fmean(ae)
    ide = math.log2(ae_mean / we + 1.0)
    mt = statistics.fmean(t.ms for t in trials)
    errors = sum(1 for t in trials if not t.hit)
    return TappingResult(
        width_px=width_px,
        amplitude_px=amplitude_px,
        trials=len(trials),
        errors=errors,
        error_rate=errors / len(trials),
        movement_ms=mt,
        effective_width_px=we,
        effective_amplitude_px=ae_mean,
        id_bits=ide,
        throughput_bps=ide / (mt / 1000.0) if mt > 0 else 0.0,
    )


# --------------------------------------------------------------------------- targets practice


@dataclass
class TargetScore:
    shown: int = 0
    hits: int = 0
    misses: int = 0  # clicks that landed outside the target
    skipped: int = 0  # targets that timed out
    times_ms: list[float] = field(default_factory=list)  # appearance to hit

    @property
    def mean_ms(self) -> float | None:
        return statistics.fmean(self.times_ms) if self.times_ms else None

    @property
    def accuracy(self) -> float | None:
        clicks = self.hits + self.misses
        return self.hits / clicks if clicks else None


# --------------------------------------------------------------------------- false activations

FALSE_WINDOW_S = 3.0
_NOT_USER_ACTIONS = ("action.undo", "ui.cancel")


class FalseActivations:
    """Counts actions the user undid (``action.undo``) or cancelled within 3 s, per hour of use.

    Fed from ``ActionExecuted`` on the GUI thread.  A cancellation is ``ui.cancel``
    soon after another action, or an action that reports it was cancelled.
    """

    def __init__(self, clock=time.monotonic) -> None:  # noqa: ANN001 - any () -> float
        self._clock = clock
        self.started = clock()
        self._recent: deque[tuple[float, str]] = deque(maxlen=50)
        self.undone = 0
        self.cancelled = 0
        self.events: list[tuple[float, str]] = []  # (time, what), for the summary

    @property
    def count(self) -> int:
        return self.undone + self.cancelled

    def hours(self) -> float:
        return max(0.0, self._clock() - self.started) / 3600.0

    def per_hour(self) -> float | None:
        """None until there is a quarter of an hour of use to divide by."""
        hours = self.hours()
        return self.count / hours if hours >= 0.25 else None

    def record(self, name: str, ok: bool, error: str | None, source: str) -> None:
        now = self._clock()
        if not ok and error and "cancel" in error.lower():
            self._count("cancelled", now, name)
            return
        if name in _NOT_USER_ACTIONS:
            if ok and self._recent and now - self._recent[-1][0] <= FALSE_WINDOW_S:
                _t, previous = self._recent.pop()
                self._count("undone" if name == "action.undo" else "cancelled", now, previous)
            return
        if ok and source not in ("system", "test"):
            self._recent.append((now, name))

    def _count(self, kind: str, now: float, name: str) -> None:
        if kind == "undone":
            self.undone += 1
        else:
            self.cancelled += 1
        self.events.append((now, f"{name} {kind}"))


# --------------------------------------------------------------------------- the summary page


def _fmt(v: float | None, places: int = 1, suffix: str = "") -> str:
    return "Not measured" if v is None else f"{v:.{places}f}{suffix}"


def summary_html(
    *,
    when: datetime,
    profile: str,
    targets: TargetScore | None,
    tapping: Sequence[TappingResult],
    false_count: int,
    false_hours: float,
    mm_per_px: float | None,
    calibration: str = "",
) -> str:
    """A self-contained page: plain words first, the method underneath, printable."""
    esc = html.escape
    rows: list[str] = []
    if targets is not None and targets.shown:
        acc = targets.accuracy
        rows.append(
            "<h2>Hitting targets</h2><table>"
            f"<tr><th>Targets shown</th><td>{targets.shown}</td></tr>"
            f"<tr><th>Hit</th><td>{targets.hits}</td></tr>"
            f"<tr><th>Clicks that missed</th><td>{targets.misses}</td></tr>"
            f"<tr><th>Not reached in time</th><td>{targets.skipped}</td></tr>"
            f"<tr><th>Clicks on target</th><td>{_fmt(None if acc is None else acc * 100, 0, '%')}</td></tr>"
            f"<tr><th>Average time to hit</th><td>{_fmt(None if targets.mean_ms is None else targets.mean_ms / 1000, 1, ' s')}</td></tr>"
            "</table>"
        )
    if tapping:
        body = []
        for r in tapping:
            size = f"{r.width_px:.0f} px"
            if mm_per_px:
                size += f" ({r.width_px * mm_per_px / 10:.1f} cm)"
            body.append(
                f"<tr><td>{esc(size)}</td><td>{r.trials}</td><td>{r.error_rate * 100:.0f}%</td>"
                f"<td>{r.movement_ms / 1000:.2f} s</td><td>{r.throughput_bps:.2f}</td></tr>"
            )
        mean_tp = statistics.fmean(r.throughput_bps for r in tapping)
        rows.append(
            "<h2>Tapping test</h2>"
            f"<p class='big'>Throughput {mean_tp:.2f} bits per second (mean over {len(tapping)} sizes).</p>"
            "<table><tr><th>Target size</th><th>Selections</th><th>Errors</th><th>Average time</th><th>Throughput (bits/s)</th></tr>"
            + "".join(body)
            + "</table>"
        )
    per_hour = false_count / false_hours if false_hours >= 0.25 else None
    rows.append(
        "<h2>Unwanted actions</h2>"
        f"<p>{false_count} action{'s' if false_count != 1 else ''} undone or cancelled within 3 seconds "
        f"over {false_hours:.1f} hours of use"
        + (f": {per_hour:.1f} per hour." if per_hour is not None else ". Too little use yet for a rate per hour.")
        + "</p>"
    )
    if calibration:
        rows.append(f"<h2>Calibration</h2><p>{esc(calibration)}</p>")
    method = (
        "<h2>How these were measured</h2><p>Hitting targets: large targets appear one at a time; a click inside counts "
        "as a hit and a click outside as a miss.  Tapping test: ISO 9241-9 style multidirectional selection, "
        "11 targets on a circle, selections alternating across it.  Effective width is 4.133 times the standard "
        "deviation of where selections landed along the direction of movement; throughput is the effective index "
        "of difficulty divided by the average movement time.  Errors are selections outside the target.  Clicks are "
        "not snapped to targets in the practice room.  Unwanted actions: actions undone with \"undo that\" or "
        "cancelled within 3 seconds of happening.</p>"
    )
    style = (
        "body{font-family:'Atkinson Hyperlegible Next','Atkinson Hyperlegible',Segoe UI,Arial,sans-serif;"
        "color:#111;background:#fff;max-width:46rem;margin:2rem auto;padding:0 1rem;line-height:1.45}"
        "h1{font-size:1.7rem;margin:0 0 .2rem}h2{font-size:1.15rem;margin:1.6rem 0 .4rem;border-bottom:1px solid #111}"
        "table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.35rem .5rem;border-bottom:1px solid #ccc}"
        "th{font-weight:600}.big{font-size:1.2rem;font-weight:600}.meta{color:#444}"
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>Sightline practice summary {when:%d %B %Y}</title><style>{style}</style></head><body>"
        f"<h1>Sightline practice summary</h1><p class='meta'>{when:%A %d %B %Y, %H:%M} &middot; profile {esc(profile)}</p>"
        + "".join(rows)
        + method
        + "</body></html>"
    )
