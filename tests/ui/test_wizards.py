"""The set-up and calibration wizards, end to end on the fake engines, with every wait shrunk."""

from __future__ import annotations

import pytest

from sightline.core.events import CalibrationActive, MicLevel
from sightline.core.types import Action, Attention
from sightline.ui.bridge import process_events_until

pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    from sightline.ui.windows import targets

    monkeypatch.setattr(targets, "TIME_SCALE", 0.004)


@pytest.fixture
def running(ui_ctx):
    ui_ctx.actions.start()
    yield ui_ctx
    ui_ctx.actions.stop()


def _wait(predicate, timeout: float = 5.0) -> None:
    assert process_events_until(predicate, timeout=timeout)


def _next(ctx) -> None:
    ctx.actions.execute_now(Action("ui.wizard.next"))


def _page(shell):
    return shell._wizards[-1].page  # noqa: SLF001


def test_setup_runs_with_a_helper_then_hands_over(recorder, running, shell):
    from sightline.ui.windows import wizards

    ctx = running
    engines: list[int] = []
    shell.run_setup(lambda: engines.append(1))
    wizard = shell._wizards[-1]  # noqa: SLF001
    opened: list[str] = []
    wizard.open_uri = opened.append
    assert ctx.state.active_overlay == "setup"
    assert isinstance(wizard.page, wizards.WelcomePage) and wizard.helper_mode

    _next(ctx)  # Windows settings, read off the GUI thread
    checks = wizard.page
    assert isinstance(checks, wizards.ChecksPage)
    _wait(lambda: len(checks.checks) == 2)
    assert set(checks.fix_buttons) == {"camera_sharing"}, "only what needs attention gets a button"
    checks.fix_buttons["camera_sharing"].click()
    assert opened == ["ms-settings:camera"]

    _next(ctx)  # downloads: nothing missing, so the engines start straight away
    assert isinstance(wizard.page, wizards.ModelsPage)
    _wait(lambda: engines == [1])
    _wait(lambda: any(a.name == "pointer.pause" for a in recorder))
    assert wizard.hand_over_button.isVisible()

    _next(ctx)  # camera
    assert isinstance(wizard.page, wizards.CameraPage)
    assert ctx.vision.preview
    _wait(lambda: wizard.page.choices is not None)
    _next(ctx)  # microphone
    mic = wizard.page
    assert isinstance(mic, wizards.MicrophonePage) and not ctx.vision.preview
    for _ in range(8):
        ctx.bus.publish(MicLevel(0.3, True))
        process_events_until(lambda: False, timeout=0.1)
    assert mic.passed and "hears you" in mic.result.text()

    _next(ctx)  # hand over
    assert isinstance(wizard.page, wizards.HandOverPage)
    assert wizard.next_button.text() == "Hand over"
    _next(ctx)
    assert not wizard.helper_mode and isinstance(wizard.page, wizards.EyesPage)
    _wait(lambda: any(a.name == "pointer.resume" for a in recorder))

    eyes = wizard.page
    assert wizard.next_button.text() == "Start"
    _next(ctx)  # Next doubles as Start
    _wait(lambda: eyes.report is not None)
    assert ctx.vision.calibration.labels == ["open", None, "closed", None, "wink_left", None, "wink_right", None]

    _next(ctx)  # gaze: dots including corners and edges, then a validation run
    gaze = wizard.page
    assert isinstance(gaze, wizards.GazePage)
    _next(ctx)
    _wait(lambda: gaze.report is not None and gaze.validation is not None, timeout=10.0)
    collected = [(x, y) for x, y, on in ctx.vision.calibration.targets if on]
    assert len(collected) == 5, "the validation dots"
    assert "cm are hard to hit" in gaze.comfort.text() and "150%" in gaze.comfort.text()

    _next(ctx)
    assert isinstance(wizard.page, wizards.HeadPage)
    ctx.actions.execute_now(Action("ui.wizard.skip"))  # head pointer is optional
    assert isinstance(wizard.page, wizards.FinishPage), "no mouth check without attention"
    assert wizard.next_button.text() == "Start practice"
    _next(ctx)
    assert ctx.settings.general.first_run_complete is True
    _wait(lambda: shell._practice_window is not None and shell._practice_window.isVisible())  # noqa: SLF001
    _wait(lambda: ctx.state.active_overlay is None)
    assert engines == [1]


def test_the_comfort_line_is_the_one_vision_measured(recorder, running, shell):
    from sightline.core.interfaces import CalibrationReport

    cal = running.vision.calibration
    measured = "At your accuracy, controls smaller than about 3 cm are hard to hit with your eyes alone."

    def finish() -> CalibrationReport:
        # The check on fresh dots comes last, and its sentence is the one to show.
        details = {"comfort": measured if cal.kind == "validate" else "From the calibration dots."}
        return CalibrationReport(True, cal.kind or "gaze", "Average error 50 px (about 1.4 degrees).", 50.0, 1.4, (), 270, details)

    cal.finish = finish
    running.actions.execute_now(Action("ui.calibrate", {"kind": "gaze"}))
    page = shell._calibration_window.page  # noqa: SLF001
    _next(running)
    _wait(lambda: page.report is not None and page.validation is not None, timeout=10.0)
    assert page.comfort.text() == measured


def test_gaze_dots_reach_the_corners_and_edges(recorder, running, shell):
    cal = running.vision.calibration
    seen: list[tuple[str, float, float]] = []
    original = cal.set_target

    def spy(x: float, y: float, collecting: bool) -> None:
        if collecting:
            seen.append((cal.kind, x, y))
        original(x, y, collecting)

    cal.set_target = spy
    running.actions.execute_now(Action("ui.calibrate", {"kind": "gaze"}))
    page = shell._calibration_window.page  # noqa: SLF001
    _next(running)
    _wait(lambda: page.report is not None, timeout=10.0)
    gaze = [(x, y) for kind, x, y in seen if kind == "gaze"]
    assert len(gaze) == 13
    r = page.run.screen.native
    for fx, fy in ((0.04, 0.05), (0.96, 0.05), (0.96, 0.95), (0.04, 0.95), (0.5, 0.95), (0.04, 0.5)):
        assert any(abs(x - (r.x + fx * r.w)) < 1 and abs(y - (r.y + fy * r.h)) < 1 for x, y in gaze), (fx, fy)
    assert len([1 for kind, _x, _y in seen if kind == "validate"]) == 5


def test_calibration_stops_when_tracking_restarts_underneath(recorder, running, shell):
    running.actions.execute_now(Action("ui.calibrate", {"kind": "eyes"}))
    wizard = shell._calibration_window  # noqa: SLF001
    page = wizard.page
    _next(running)
    assert page.busy()
    running.bus.publish(CalibrationActive(False, "eyes"))
    _wait(lambda: not page.busy())
    assert page.report is None and "restarted" in page.instruction.text()
    assert wizard.next_button.text() == "Try again"


def test_cancel_stops_a_dot_run_and_drift_fix_uses_one_centre_dot(recorder, running, shell):
    from sightline.ui.windows import targets

    running.actions.execute_now(Action("ui.calibrate", {"kind": "drift"}))
    run = shell._drift_run  # noqa: SLF001
    assert run is not None and run.kind == "drift"
    _wait(lambda: not run.running, timeout=5.0)
    collected = [(x, y) for x, y, on in running.vision.calibration.targets if on]
    centre = run.screen.native.center
    assert collected == [centre]
    assert any("Average error" in t.entry.text for t in shell.toasts.toasts)

    # A long gaze run stopped by "cancel".
    targets.TIME_SCALE = 1.0
    running.actions.execute_now(Action("ui.calibrate", {"kind": "gaze"}))
    page = shell._calibration_window.page  # noqa: SLF001
    _next(running)
    assert page.busy() and page.run.window.isVisible()
    running.actions.execute_now(Action("ui.cancel"))
    assert not page.busy() and not page.run.window.isVisible()
    assert running.vision.calibration.kind is None, "vision was told to cancel"


def test_mouth_check_offers_the_gate_only_when_it_works(recorder, running, shell):
    from sightline.ui.windows import wizards

    page_ref: dict = {}

    def attention(t0, t1):  # noqa: ANN001
        page = page_ref.get("page")
        quiet = page is not None and "quiet" in page._t and t0 >= page._t["quiet"]  # noqa: SLF001
        return Attention(1.0, 1.0, 0.05 if quiet else 0.8, 30)

    running.vision.attention = attention
    running.actions.execute_now(Action("ui.calibrate", {"kind": "full"}))
    wizard = shell._calibration_window  # noqa: SLF001
    mouth = next(p for p in wizard.pages if isinstance(p, wizards.MouthPage))
    assert mouth.available()
    page_ref["page"] = mouth
    wizard.go_to(mouth)
    _next(running)
    _wait(lambda: mouth.ready())
    assert mouth.works and mouth.gate.isVisible()
    mouth.gate.buttons[0][1].click()
    assert running.settings.speech.mouth_gate is True

    running.vision.attention = lambda t0, t1: None
    assert not mouth.available(), "hidden when vision cannot say"


def test_closing_setup_early_still_starts_the_engines_once(recorder, running, shell):
    engines: list[int] = []
    shell.run_setup(lambda: engines.append(1))
    shell._wizards[-1].close()  # noqa: SLF001
    assert engines == [1]
    assert running.settings.general.first_run_complete is False
