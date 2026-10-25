"""Downloading the model files: a reusable progress view and a small window around it.

The download runs on a worker thread (``ModelManager.ensure`` blocks); the
view follows the ``ModelProgress`` events.  Network trouble is retried
automatically with growing pauses, and "Try again" is always there.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from sightline.core.events import ModelProgress
from sightline.core.models import ModelError, ModelSpec

from ..services import UiServices
from ..widgets import ProgressLine, Rule, SightlineWindow, text

log = logging.getLogger(__name__)

RETRY_DELAYS_S = (5, 15, 30, 60)

_STATUS_WORDS = {
    "queued": "Waiting",
    "downloading": "Downloading",
    "verifying": "Checking",
    "extracting": "Unpacking",
    "ready": "Ready",
    "error": "Problem",
}


def format_bytes(n: float) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    if n >= 1e7:
        return f"{n / 1e6:.0f} MB"
    if n >= 1e6:
        return f"{n / 1e6:.1f} MB"
    return f"{max(n, 0) / 1e3:.0f} KB"


class ModelDownloader(QObject):
    """Runs ``models.ensure()`` on a thread.  ``finished(ok, error)`` arrives on the GUI thread."""

    finished = Signal(bool, str)

    def __init__(self, ensure: Callable[..., None], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._ensure = ensure
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._cancel.clear()
        self._thread = threading.Thread(target=self._run, name="sightline-models", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self) -> None:
        try:
            self._ensure(cancel=self._cancel)
        except ModelError as e:
            self.finished.emit(False, str(e))
            return
        except Exception as e:  # anything unexpected still ends the attempt cleanly
            log.exception("Model download failed")
            self.finished.emit(False, f"{type(e).__name__}: {e}")
            return
        self.finished.emit(True, "")


class ModelDownloadView(QWidget):
    """The list of files with their state, one overall bar, and a retry path."""

    done = Signal()

    def __init__(self, services: UiServices, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.svc = services
        t = services.theme
        self.specs: list[ModelSpec] = list(services.ctx.models.missing())
        self._progress: dict[str, ModelProgress] = {}
        self._attempt = 0
        self._countdown = 0
        self.complete = not self.specs

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(t.px(14))
        total = sum(s.total_bytes for s in self.specs)
        if self.specs:
            intro = f"Sightline needs {len(self.specs)} file{'s' if len(self.specs) != 1 else ''} ({format_bytes(total)}) the first time. They stay on this computer, and everything runs offline after this."
        else:
            intro = "Everything Sightline needs is already on this computer."
        root.addWidget(text(intro, "body"))

        grid = QGridLayout()
        grid.setHorizontalSpacing(t.px(16))
        grid.setVerticalSpacing(t.px(8))
        self._status_labels: dict[str, object] = {}
        if self.specs:
            grid.addWidget(text("File", "label", wrap=False), 0, 0)
            grid.addWidget(text("Size", "label", wrap=False), 0, 1)
            grid.addWidget(text("Status", "label", wrap=False), 0, 2)
            grid.addWidget(Rule(t, strong=True), 1, 0, 1, 3)
            row = 2
            for spec in self.specs:
                grid.addWidget(text(spec.title, "body"), row, 0)
                grid.addWidget(text(format_bytes(spec.total_bytes), "body", wrap=False), row, 1)
                status = text("Waiting", "body", wrap=False)
                grid.addWidget(status, row, 2)
                self._status_labels[spec.id] = status
                grid.addWidget(Rule(t), row + 1, 0, 1, 3)
                row += 2
            grid.setColumnStretch(0, 1)
        root.addLayout(grid)

        self.bar = ProgressLine(t)
        root.addWidget(self.bar)
        self.summary = text("", "small", wrap=False)
        self.summary.setProperty("role", "secondary")
        root.addWidget(self.summary)
        self.error = text("", "body")
        self.error.hide()
        root.addWidget(self.error)
        buttons = QHBoxLayout()
        self.retry_button = QPushButton("Try again")
        self.retry_button.hide()
        self.retry_button.clicked.connect(self.start)
        buttons.addWidget(self.retry_button)
        buttons.addStretch(1)
        root.addLayout(buttons)

        self._downloader = ModelDownloader(services.ctx.models.ensure, self)
        self._downloader.finished.connect(self._on_finished)
        self._remove_handler = services.bridge.on(ModelProgress, self.on_progress)
        self._retry_timer = QTimer(self)
        self._retry_timer.setInterval(1000)
        self._retry_timer.timeout.connect(self._tick)
        self.destroyed.connect(lambda *_: self._remove_handler())
        if self.complete:
            self.bar.set_value(1.0)

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        if self.complete:
            QTimer.singleShot(0, self, self.done.emit)
            return
        self._retry_timer.stop()
        self.error.hide()
        self.retry_button.hide()
        self._attempt += 1
        self.summary.setText("Starting the download")
        self._downloader.start()

    def cancel(self) -> None:
        self._retry_timer.stop()
        self._downloader.cancel()

    def close_view(self) -> None:
        self.cancel()
        self._remove_handler()

    # ------------------------------------------------------------------ progress

    def on_progress(self, e: ModelProgress) -> None:
        if e.model_id not in self._status_labels:
            return
        self._progress[e.model_id] = e
        word = _STATUS_WORDS.get(e.status, e.status)
        if e.status == "downloading" and e.total_bytes:
            word = f"{100.0 * e.done_bytes / e.total_bytes:.0f}%"
        self._status_labels[e.model_id].setText(word)  # type: ignore[attr-defined]
        total = sum(s.total_bytes for s in self.specs) or 1
        done = 0
        for spec in self.specs:
            p = self._progress.get(spec.id)
            if p is None:
                continue
            done += spec.total_bytes if p.status == "ready" else min(p.done_bytes, spec.total_bytes)
        self.bar.set_value(done / total)
        if e.status != "error":
            self.summary.setText(f"{format_bytes(done)} of {format_bytes(total)}")

    def _on_finished(self, ok: bool, error: str) -> None:
        if ok:
            self.complete = True
            self.bar.set_value(1.0)
            self.summary.setText("All files are ready")
            for label in self._status_labels.values():
                label.setText("Ready")  # type: ignore[attr-defined]
            self.done.emit()
            return
        log.warning("Model download attempt %d failed: %s", self._attempt, error)
        self.error.setText(f"The download stopped: {error}")
        self.error.show()
        self.retry_button.show()
        if self._attempt <= len(RETRY_DELAYS_S):
            self._countdown = RETRY_DELAYS_S[self._attempt - 1]
            self._retry_timer.start()
            self._tick(first=True)
        else:
            self.summary.setText("Check the internet connection, then choose Try again.")

    def _tick(self, first: bool = False) -> None:
        if not first:
            self._countdown -= 1
        if self._countdown <= 0:
            self._retry_timer.stop()
            self.start()
            return
        self.summary.setText(f"Trying again in {self._countdown} s")


class ModelsWindow(SightlineWindow):
    """The small window ``ensure_models`` shows when files are missing at start-up."""

    def __init__(self, services: UiServices, on_ready: Callable[[], None]) -> None:
        super().__init__(services.theme, "Getting Sightline ready")
        self.svc = services
        self._on_ready = on_ready
        self._finished = False
        t = services.theme
        root = QVBoxLayout(self)
        m = t.px(32)
        root.setContentsMargins(m, m, m, m)
        root.setSpacing(t.px(18))
        root.addWidget(text("Getting Sightline ready", "title", wrap=False))
        root.addWidget(Rule(t, strong=True))
        self.view = ModelDownloadView(services, self)
        self.view.done.connect(self._done)
        root.addWidget(self.view)
        root.addStretch(1)
        self.resize(t.px(620), t.px(420))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

    def begin(self) -> None:
        self.show()
        self.view.start()

    def _done(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.view.close_view()
        QTimer.singleShot(600, self, self._close_and_continue)

    def _close_and_continue(self) -> None:
        self.hide()
        self._on_ready()

    def closeEvent(self, event) -> None:  # noqa: ANN001
        # Closing early leaves the download running in the background; the engines start when it is done.
        if not self._finished:
            event.ignore()
            self.hide()
            return
        super().closeEvent(event)
