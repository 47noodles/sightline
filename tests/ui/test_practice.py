"""Practice room numbers (pure) and the practice window."""

from __future__ import annotations

import math
from datetime import datetime

import pytest

from sightline.core.types import Action
from sightline.ui import measures
from sightline.ui.bridge import process_events_until

# =========================================================================== numbers


def test_tapping_order_crosses_the_circle_and_targets_sit_on_it():
    assert measures.tapping_order(11) == [0, 6, 1, 7, 2, 8, 3, 9, 4, 10, 5]
    pts = measures.circle_targets(500, 400, 600, 11)
    assert len(pts) == 11 and pts[0] == pytest.approx((500, 100))
    assert all(math.hypot(x - 500, y - 400) == pytest.approx(300) for x, y in pts)


def test_throughput_follows_the_effective_width_and_time():
    trials = []
    for i in range(10):
        start, target = (0.0, 0.0), (400.0, 0.0)
        overshoot = 5.0 if i % 2 else -5.0
        trials.append(measures.Trial(start, target, (400.0 + overshoot, 3.0), 1000.0, hit=i != 3))
    r = measures.summarise_tapping(trials, 40, 400)
    sd = math.sqrt(sum(5.0**2 for _ in range(10)) / 9)
    assert r.effective_width_px == pytest.approx(4.133 * sd)
    assert r.effective_amplitude_px == pytest.approx(400)
    assert r.id_bits == pytest.approx(math.log2(400 / (4.133 * sd) + 1))
    assert r.throughput_bps == pytest.approx(r.id_bits / 1.0)
    assert r.errors == 1 and r.error_rate == pytest.approx(0.1)
    assert measures.summarise_tapping(trials[:1], 40, 400) is None


def test_comfort_line_uses_the_panel_size_or_degrees():
    mm_per_px = 527 / 1920
    line = measures.comfort_line(40, 1.2, mm_per_px, 1.0)
    assert line.startswith("At your accuracy, controls smaller than about 2 cm are hard to hit by eyes alone.")
    assert "150%" in line
    assert "150%" not in measures.comfort_line(40, 1.2, mm_per_px, 1.5)
    degrees_only = measures.comfort_line(None, 1.2, None, 1.0)
    assert "2.5 cm" in degrees_only and "%" not in degrees_only
    assert measures.comfort_line(None, None, mm_per_px, 1.0) is None


def test_comfort_line_prefers_the_sentence_vision_measured():
    mm_per_px = 527 / 1920
    vision_says = "At your accuracy, controls smaller than about 2 cm are hard to hit with your eyes alone."
    assert measures.comfort_line(40, 1.2, mm_per_px, 1.0, {"comfort": vision_says, "comfort_px": 80}) == vision_says
    assert measures.comfort_line(None, None, None, 1.0, {"comfort": vision_says}) == vision_says
    for unusable in ({}, {"comfort": "  "}, {"comfort": 3}, None):
        line = measures.comfort_line(40, 1.2, mm_per_px, 1.0, unusable)
        assert line is not None and "by eyes alone" in line, unusable
    assert measures.comfort_line(None, None, None, 1.0, {}) is None


def test_false_activations_count_undo_and_cancel_within_three_seconds():
    now = [0.0]
    fa = measures.FalseActivations(clock=lambda: now[0])
    fa.record("mouse.click", True, None, "eye")
    now[0] = 1.0
    fa.record("action.undo", True, None, "voice")  # undone 1 s later: counts
    now[0] = 5.0
    fa.record("window.close", True, None, "voice")
    now[0] = 9.0
    fa.record("action.undo", True, None, "voice")  # 4 s later: a change of mind, not a false activation
    now[0] = 10.0
    fa.record("ui.labels.show", True, None, "voice")
    now[0] = 11.0
    fa.record("ui.cancel", True, None, "voice")
    fa.record("window.close", False, "Cancelled by the user", "voice")
    assert (fa.undone, fa.cancelled, fa.count) == (1, 2, 3)
    assert fa.per_hour() is None, "too little use to give a rate"
    now[0] = 3600.0
    assert fa.per_hour() == pytest.approx(3.0)


def test_summary_page_reads_plainly():
    score = measures.TargetScore(shown=12, hits=12, misses=2, times_ms=[1200.0] * 12)
    trial = measures.Trial((0, 0), (300, 0), (302, 1), 900.0, True)
    tap = measures.summarise_tapping([trial, measures.Trial((300, 0), (0, 0), (-3, 0), 1100.0, True)], 60, 300)
    page = measures.summary_html(
        when=datetime(2026, 10, 25, 9, 30), profile="default", targets=score, tapping=[tap], false_count=1, false_hours=2.0, mm_per_px=0.27
    )
    assert "Sunday 25 October 2026" in page and "Throughput" in page and "0.5 per hour" in page
    assert "ISO 9241-9" in page and "<script" not in page


# =========================================================================== window


@pytest.mark.gui
def test_targets_score_hits_and_misses(shell, ui_ctx):
    ui_ctx.actions.execute_now(Action("ui.practice.show"))
    window = shell._practice_window  # noqa: SLF001
    assert window.isVisible()
    window.start_targets()
    assert process_events_until(lambda: window.canvas.targets, timeout=2.0)
    from PySide6.QtCore import QPointF

    c, r = window.canvas.targets[0]
    window.select_at(QPointF(c.x() + r + 30, c.y()))
    assert window.score.misses == 1
    for _ in range(12):
        c, _r = window.canvas.targets[0]
        window.select_at(c)
    assert window.score.hits == 12 and window.mode == "menu"
    assert window.stack.currentWidget() is window.summary_page
    assert any(row.cells[0] == "Hits" and row.cells[1].startswith("12 of 12") for row in window.table.rows)


@pytest.mark.gui
def test_tapping_test_measures_three_sizes_and_saves_a_summary(shell, ui_ctx):
    from PySide6.QtCore import QPointF

    from sightline.core import paths

    ui_ctx.actions.execute_now(Action("ui.practice.show"))
    window = shell._practice_window  # noqa: SLF001
    window.start_tapping()
    for size in range(3):
        assert process_events_until(lambda s=size: window._condition == s and window.canvas.targets, timeout=2.0)  # noqa: SLF001
        assert len(window.canvas.targets) == 11
        for i in range(12):
            c, r = window.canvas.targets[window.canvas.current]
            # Land a little off centre, alternating, so there is a spread to measure.
            window.select_at(QPointF(c.x() + (0.3 * r if i % 2 else -0.3 * r), c.y()))
    assert process_events_until(lambda: window.mode == "menu", timeout=2.0)
    assert len(window.tapping) == 3
    assert all(r.trials == 11 and r.errors == 0 and r.throughput_bps > 0 for r in window.tapping)
    widths = [r.width_px for r in window.tapping]
    assert widths == sorted(widths, reverse=True)
    path = window.save_summary()
    assert path is not None and path.parent == paths.profile_dir("default")
    assert "Tapping test" in path.read_text(encoding="utf-8")
