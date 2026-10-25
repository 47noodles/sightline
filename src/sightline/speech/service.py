"""The speech service: microphone to recogniser to commands or typing.

Threads:

* PortAudio's callback thread hands 16 kHz blocks to a queue and never blocks;
* ``sightline-speech`` runs voice activity, endpointing, the recogniser and
  the interpreter, then dispatches Actions;
* ``sightline-speech-bg`` loads the paraphrase model, refreshes app, window
  and button names, watches the user's files (``commands.toml``,
  ``aliases.txt``, ``vocabulary.txt``), saves the learned pauses and swaps
  recognisers when the setting or the need for biasing changes.

``feed_text`` runs the same interpretation and dispatch synchronously in the
caller's thread (tests, the typed command box).

Learning from the user (concept review item 16): a miss followed within
8 s by the command given another way becomes an offer to remember an alias;
pauses inside commands set how long to wait for the rest of an unfinished
one.

Privacy (review S-02, S-03): the log never keeps what was said.  At DEBUG it
notes each utterance's kind and length; the words only when a developer turns
on ``general.log_speech``.  In a password field anything typed or pressed
shows as bullets in the caption, carries no utterance, and is marked
``PRIVATE`` so ``Action.describe`` logs none of it.
"""

from __future__ import annotations

import itertools
import logging
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import numpy as np

from sightline.core import paths
from sightline.core.actions import ActionError
from sightline.core.risk import split_combo
from sightline.core.events import (
    ActionExecuted,
    ClickPerformed,
    FocusChanged,
    IntentResult,
    MicLevel,
    Notice,
    SettingsChanged,
    SpeechStatus,
    StateChanged,
    TranscriptFinal,
    TranscriptPartial,
)
from sightline.core.types import PRIVATE, Action, CommandHelp, FocusInfo, MicInfo, SpeechMode

from . import aliases as alias_file
from . import custom
from . import models as speech_models
from . import pauses as pause_model
from .audio import BlockQueue, Microphone, list_microphones
from .formatting import apply_formatters, dictation_text, place_text
from .grammar import BuildContext, CommandSpec, TypeRequest, build_commands, help_rows, sleep_and_wake
from .interpret import Decision, Interpreter, Situation, is_risky
from .matcher import CONNECTORS, Grammar, Match
from .names import DynamicNames
from .normalize import clean_transcript, is_probably_noise, normalize_command
from .pipeline import EndpointConfig, Utterance, UtterancePipeline

if TYPE_CHECKING:
    from pathlib import Path

    from sightline.core.context import AppContext

log = logging.getLogger(__name__)

_LEVEL_PERIOD = 1.0 / 15.0  # MicLevel at about 15 Hz
_ELEMENTS_PERIOD = 1.5  # seconds between refreshes of on-screen names
_WINDOWS_PERIOD = 4.0
_COMMANDS_FILE_PERIOD = 2.0
_PENDING_TYPING_S = 1.5  # our own typing may not have reached the field yet for this long
_MAX_DISPLAY = 60
CHOICE_HOLD_S = 8.0  # how long "did you mean" suggestions wait for "one", "two" or "three"
# Personal aliases (concept review item 16a): a correction counts when it comes this soon after the miss.
ALIAS_WINDOW_S = 8.0
ALIAS_MAX_WORDS = 5  # misheard commands are short; longer misses are sentences, not commands
# What people say in reaction to a miss rather than to repeat it, so never offered as an alias target.
_NOT_ALIAS_TARGETS = frozenset(
    {
        "ui.help.show",
        "ui.help.hide",
        "ui.cancel",
        "key.stop",
        "action.undo",
        "speech.repeat",
        "speech.choose",
        "speech.remember",
        "speech.vocabulary",
        "speech.sleep",
        "speech.wake",
        "speech.mode_dictation",
        "speech.mode_command",
        "speech.mode_auto",
        "speech.listen_off",
    }
)
_PAUSES_SAVE_S = 10.0  # the learned pauses are written at most this often
_CHOOSING = frozenset({"speech.choose", "speech.remember"})  # take a numbered choice; "yes" takes an offer to remember
VOCABULARY_FILE = "vocabulary.txt"  # the user's words for the recogniser, beside commands.toml and aliases.txt
# Attention gate (ARCHITECTURE.md section 8): fractions of camera frames during the utterance.
MIN_FACE = 0.5
MIN_FACING = 0.5
MIN_MOUTH = 0.2
# Actions after which the caret may be somewhere else, so "scratch that" must not backspace.
_CARET_ACTIONS = frozenset(
    {"key.press", "mouse.click", "element.click", "ui.labels.select", "ui.grid.click", "text.select", "text.select_range", "text.caret", "login.fill"}
)
# Keys that type nothing, so pressing them in a password field reveals nothing ("press enter" may show).
_NON_PRINTING_KEYS = frozenset(
    {"enter", "tab", "escape", "backspace", "delete", "insert", "home", "end", "pageup", "pagedown", "up", "down", "left", "right", "capslock", "printscreen", "apps", "numlock", "scrolllock", "pause"}
)
_NON_PRINTING_PREFIXES = ("volume", "media", "browser", "launch", "play", "next", "previous")
_MAX_BULLETS = 32


def endpoint_ms(text: str, interp: Interpreter, situation: Situation, cfg: EndpointConfig) -> int:
    """The pause that ends an utterance whose words so far are ``text`` (adaptive endpointing).

    A complete command that nothing can extend ends after ``early_ms``; one a
    name or more words may follow waits ``command_ms``; dictation and free
    text wait ``dictation_ms`` so a breath does not split a sentence.  The
    start of a command that is not finished yet waits at least
    ``incomplete_ms``, the pause this person makes inside commands
    (``pauses.py``); complete commands never wait longer for it.
    """
    if situation.mode == "sleep":
        return cfg.command_ms
    norm = normalize_command(text)
    typing_likely = situation.mode == "dictation" or (situation.mode == "auto" and situation.editable)
    if not norm:
        return cfg.dictation_ms if typing_likely else cfg.command_ms
    if norm.rsplit(" ", 1)[-1] in CONNECTORS:
        return max(cfg.dictation_ms, cfg.incomplete_ms)  # "select all and ...": another command follows
    info = interp.grammar.prefix(norm, interp.contexts(situation))
    if info.complete:
        if info.free_text and not info.complete_fixed:
            return cfg.dictation_ms  # "type hello", "search for cats": more words may come
        if info.name_follows or info.free_text:
            return cfg.command_ms  # "click" may become "click save"; "find" may get words
        return cfg.early_ms  # nothing can follow: "tap", "copy", "scroll down"
    if info.free_text:
        return max(cfg.dictation_ms, cfg.incomplete_ms)
    if info.possible:
        # Not a whole command yet ("scroll", "switch to"): the rest is still coming.
        return max(cfg.dictation_ms if info.name_follows else cfg.command_ms, cfg.incomplete_ms)
    return cfg.dictation_ms if typing_likely else cfg.command_ms


def _decode_cost_ms(engine: object) -> float:
    """Time to recognise a 1.5 s command on this computer.

    The best of two runs after warm-up, so start-up work elsewhere (vision loading its models)
    does not push "Recommended" onto a less accurate recogniser.
    """
    audio = np.random.default_rng(0).normal(0, 0.01, 24000).astype(np.float32)
    costs = []
    for _ in range(2):
        session = engine.session()  # type: ignore[attr-defined]
        session.accept(audio)
        started = time.perf_counter()
        session.finish()
        costs.append((time.perf_counter() - started) * 1000.0)
    return min(costs)


def _focus_key(focus: FocusInfo) -> tuple:
    return (focus.hwnd, focus.control_type, focus.name)


def _mode_name(value: object) -> str:
    return value.value if isinstance(value, SpeechMode) else str(value)


@dataclass(slots=True)
class _Choices:
    """"Did you mean" suggestions from the last rejected utterance, or an offer to remember an alias."""

    items: list[tuple[str, Match]]  # (label shown, what it runs), best first
    heard: str
    aim_t: float | None
    t: float  # time.monotonic() when offered
    heard_norm: str = ""  # the utterance the choices are for, normalised
    spoken: bool = False  # it was heard, not typed: only misheard speech teaches aliases
    offer: tuple[str, str] | None = None  # (heard, command): "yes" remembers this alias instead of running anything


@dataclass(slots=True)
class _Miss:
    """A spoken utterance that was not understood, waiting to see whether the user gets the command another way."""

    heard: str  # normalised
    t: float  # time.monotonic()


@dataclass(slots=True)
class _Unfinished:
    """A rejected utterance that was the start of a command ("scroll"); the rest may follow."""

    heard: str  # normalised
    t_end: float  # perf_counter at the end of its speech
    overlay: str | None


@dataclass(slots=True)
class _Typed:
    text: str  # empty for password fields
    length: int
    focus_key: tuple
    t: float
    moved: bool = False  # a click or key press happened after this was typed


class DictationLog:
    """What speech typed recently, per field.

    Gives spacing and capitalisation context when UI Automation cannot read
    the field (or has not caught up with our own typing yet), and tells
    "scratch that" what to remove.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._history: deque[_Typed] = deque(maxlen=20)
        self._context_key: tuple | None = None
        self._context = ""

    def record(self, key: tuple, text: str, password: bool) -> None:
        with self._lock:
            self._history.append(_Typed("" if password else text, len(text), key, time.monotonic()))
            if key != self._context_key:
                self._context_key, self._context = key, ""
            if not password:
                self._context = (self._context + text)[-300:]

    def before(self, key: tuple) -> str | None:
        with self._lock:
            if key == self._context_key and self._context:
                return self._context
            return None

    def last(self, key: tuple) -> _Typed | None:
        with self._lock:
            if self._history and self._history[-1].focus_key == key and not self._history[-1].moved:
                return self._history[-1]
            return None

    def focus_changed(self, key: tuple) -> None:
        with self._lock:
            if key != self._context_key:
                self._context_key, self._context = key, ""

    def caret_moved(self) -> None:
        with self._lock:
            for item in self._history:
                item.moved = True
            self._context = ""

    def latest(self) -> _Typed | None:
        """The last phrase typed by voice, wherever it went and whatever happened since."""
        with self._lock:
            return self._history[-1] if self._history else None

    def pop_last(self) -> _Typed | None:
        with self._lock:
            if not self._history:
                return None
            item = self._history.pop()
            if self._context and item.text and self._context.endswith(item.text):
                self._context = self._context[: -len(item.text)]
            return item


class SpeechService:
    """Implements ``SpeechAPI`` (core/interfaces.py)."""

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._bus = ctx.bus
        ctx.models.register_all(speech_models.all_specs())
        self._names = DynamicNames()
        self._build_ctx = BuildContext()
        self._build_ctx.resolve_app = self._names.resolve_app
        self._build_ctx.resolve_window = self._names.resolve_window
        self._build_ctx.resolve_element = self._names.resolve_element
        self._build_ctx.current_mode = _mode_name(ctx.state.get("speech_mode"))
        self._sync_scroll_step()
        self._lock = threading.RLock()  # one utterance at a time; guards grammar, aliases, pauses and engine swaps
        self._dictation = DictationLog()
        # The user's own files sit together in the settings folder, where a helper finds them.
        folder = paths.config_dir()
        self._commands_path = folder / "commands.toml"
        self._aliases_path = folder / "aliases.txt"
        self._vocabulary_path = folder / VOCABULARY_FILE
        custom.write_example(self._commands_path)
        alias_file.write_example(self._aliases_path)
        custom.write_vocabulary_example(self._vocabulary_path)
        self._commands_mtime = 0.0
        self._aliases_mtime = 0.0
        self._vocabulary_mtime = 0.0
        self._custom_specs: list[CommandSpec] = []
        self._custom_problems: list[str] = []
        self._aliases: dict[str, str] = {}
        self._alias_problems: list[str] = []
        self._vocabulary: list[str] = []
        self._vocabulary_problems: list[str] = []
        self._load_custom()
        self._load_aliases()
        self._load_vocabulary()
        self._grammar = self._build_grammar()
        self._interpreter = Interpreter(self._grammar, aliases=self._aliases)
        self._semantic_failed = False
        self._seq = itertools.count(1)
        self._miss: _Miss | None = None
        self._unfinished: _Unfinished | None = None
        self._last_choice: tuple[_Choices, Match] | None = None  # the choice the current utterance ran
        self._profile = str(getattr(getattr(ctx.settings, "general", None), "profile", "default") or "default")
        self._pauses = pause_model.PauseModel.load(self._pauses_path())
        self._pauses_saved = time.monotonic()
        self._learned_ms: int | None = None
        self._update_learned_pause()

        self._queue = BlockQueue()
        self._mic: Microphone | None = None
        self._mic_lock = threading.Lock()
        self._mic_open = False
        self._mic_id = ctx.settings.speech.microphone
        self._mic_name = ""
        self._mic_error: str | None = None
        self._pipeline: UtterancePipeline | None = None
        self._engine = None
        self._engine_id = ""
        self._engine_setting: str | None = None  # the setting value the loaded recogniser was chosen for
        self._engine_label = ""
        self._vad = None
        self._running = False
        self._stop = threading.Event()
        self._speech_thread: threading.Thread | None = None
        self._bg_thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._refresh_all = threading.Event()
        self._refresh_elements = threading.Event()
        self._pending_engine: str | None = None
        self._rebuild_engine = False  # the pending load must rebuild the current recogniser (biasing changed)
        self._level_acc = 0.0
        self._level_n = 0
        self._level_t = 0.0
        self._last_status: SpeechStatus | None = None
        self._focus = FocusInfo()
        self._choices: _Choices | None = None
        self._last_element_names: list[str] | None = None
        self._last_windows: list[tuple[int, str]] | None = None

        self._register_actions()
        self._subs = [
            self._bus.subscribe(FocusChanged, self._on_focus),
            self._bus.subscribe(SettingsChanged, self._on_settings),
            self._bus.subscribe(StateChanged, self._on_state),
            self._bus.subscribe(ClickPerformed, lambda e: self._dictation.caret_moved()),
            self._bus.subscribe(ActionExecuted, self._on_action_executed),
        ]
        self._refresh_names(apps=True)

    # ================================================================== SpeechAPI

    def start(self) -> None:
        """Load models, open the microphone and start listening.  Called on a background thread by app.py."""
        if self._running:
            return
        self._running = True
        self._stop.clear()
        self._announce_problems()
        self._load_models()
        self._speech_thread = threading.Thread(target=self._speech_loop, name="sightline-speech", daemon=True)
        self._speech_thread.start()
        self._bg_thread = threading.Thread(target=self._background_loop, name="sightline-speech-bg", daemon=True)
        self._bg_thread.start()
        self._reconcile_microphone()
        self._publish_status()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop.set()
        self._wake.set()
        self._reconcile_microphone()
        for thread in (self._speech_thread, self._bg_thread):
            if thread is not None:
                thread.join(timeout=3.0)
        self._speech_thread = self._bg_thread = None
        with self._lock:
            if self._pipeline is not None:
                self._pipeline.cancel()
            self._close_engine(self._engine)
            self._engine = None
            self._engine_id = ""
            self._engine_setting = None
            self._pipeline = None
        self._save_pauses(force=True)
        self._publish_status()

    def microphones(self) -> list[MicInfo]:
        try:
            return list_microphones()
        except Exception as e:
            log.warning("Could not list microphones: %s", e)
            return []

    def set_microphone(self, mic_id: str) -> None:
        self._ctx.settings.speech.microphone = mic_id
        try:
            self._ctx.settings_changed("speech")  # _on_settings switches the device
        except OSError:
            log.exception("Could not save the microphone choice")
            self._apply_microphone_setting()

    def commands_help(self) -> list[CommandHelp]:
        with self._lock:
            specs = list(self._grammar.specs.values())
        return help_rows(specs)

    def feed_text(self, text: str) -> None:
        self._handle(text, next(self._seq), t_speech_end=None, audio_ms=None)

    def lists_changed(self) -> None:
        self._refresh_all.set()
        self._wake.set()

    # ================================================================== grammar

    def _build_grammar(self) -> Grammar:
        cfg = self._ctx.settings.speech
        self._build_ctx.wake_phrase = cfg.wake_phrase
        self._build_ctx.sleep_phrase = cfg.sleep_phrase
        base = build_commands() + sleep_and_wake(cfg.wake_phrase, cfg.sleep_phrase)
        try:
            return Grammar(base + self._custom_specs)
        except Exception:
            log.exception("Custom commands broke the grammar; using the built-in commands only")
            self._bus.publish(Notice("Some of your own commands could not be used", "warn", key="speech-commands"))
            return Grammar(base)

    def _load_custom(self) -> None:
        try:
            self._commands_mtime = self._commands_path.stat().st_mtime
        except OSError:
            self._commands_mtime = 0.0
        # A command file must never take the words that stop, cancel or undo (a shared "command pack"
        # mapping "stop" to Enter would remove the way out of everything else): load() leaves those out.
        commands, problems = custom.load(self._commands_path, protected=self._safety_phrases())
        self._custom_specs = custom.to_specs(commands)
        if problems and problems != self._custom_problems:
            log.warning("commands.toml: %s", "; ".join(problems))
            self._bus.publish(Notice(problems[0], "warn", key="speech-commands"))
        self._custom_problems = problems

    def _safety_phrases(self) -> frozenset[str]:
        """Every way of saying the safety commands (stop, cancel, sleep, wake, undo that, pause, help)."""
        cfg = self._ctx.settings.speech
        return custom.safety_phrases(cfg.wake_phrase, cfg.sleep_phrase)

    def _announce_problems(self) -> None:
        """Say again what is wrong with the user's files: when they were first read, nothing was showing toasts yet."""
        for problems, key in (
            (self._custom_problems, "speech-commands"),
            (self._alias_problems, "speech-aliases"),
            (self._vocabulary_problems, "speech-vocabulary"),
        ):
            if problems:
                self._bus.publish(Notice(problems[0], "warn", key=key))

    def _rebuild(self) -> None:
        grammar = self._build_grammar()
        with self._lock:
            self._grammar = grammar
            self._interpreter = Interpreter(grammar, self._interpreter.semantic, aliases=self._aliases)

    def _reload_commands(self) -> None:
        self._load_custom()
        self._rebuild()

    def _load_aliases(self) -> None:
        try:
            self._aliases_mtime = self._aliases_path.stat().st_mtime
        except OSError:
            self._aliases_mtime = 0.0
        found, problems = alias_file.load(self._aliases_path)
        with self._lock:
            self._aliases = found
            interpreter = getattr(self, "_interpreter", None)
            if interpreter is not None:
                interpreter.aliases = dict(found)
        if problems and problems != self._alias_problems:
            log.warning("aliases.txt: %s", "; ".join(problems))
            self._bus.publish(Notice(problems[0], "warn", key="speech-aliases"))
        self._alias_problems = problems

    def _load_vocabulary(self) -> None:
        try:
            self._vocabulary_mtime = self._vocabulary_path.stat().st_mtime
        except OSError:
            self._vocabulary_mtime = 0.0
        words, problems = custom.load_vocabulary(self._vocabulary_path)
        if problems and problems != self._vocabulary_problems:
            log.warning("%s: %s", VOCABULARY_FILE, "; ".join(problems))
            self._bus.publish(Notice(problems[0], "warn", key="speech-vocabulary"))
        self._vocabulary_problems = problems
        if words != self._vocabulary:
            self._vocabulary = words
            self._apply_vocabulary()

    def _apply_vocabulary(self) -> None:
        """Give the recogniser the vocabulary, or have it rebuilt for biasing when it was built without."""
        engine = getattr(self, "_engine", None)
        if engine is None:
            return  # _load_engine applies it
        if getattr(engine, "biasing", False):
            try:
                engine.set_hotwords(list(self._vocabulary))
            except Exception:
                log.exception("Could not give the recogniser the vocabulary")
        if self._wants_biasing(self._engine_id) != bool(getattr(engine, "biasing", False)) and self._running:
            # Biasing needs beam search, chosen when the recogniser is built: rebuild it in the background.
            self._pending_engine = self._ctx.settings.speech.engine
            self._rebuild_engine = True
            self._wake.set()

    def _wants_biasing(self, engine_id: str) -> bool:
        definition = speech_models.ENGINES.get(engine_id) or speech_models.BENCH_ONLY.get(engine_id)
        return bool(self._vocabulary) and definition is not None and definition.can_bias

    # ================================================================== models

    def _load_models(self) -> None:
        from .vad import EnergyVad, SileroVad

        models = self._ctx.models
        if self._vad is None and models.is_ready(speech_models.VAD.id):
            try:
                self._vad = SileroVad(models.dir(speech_models.VAD.id) / "silero_vad.onnx")
            except Exception:
                log.exception("The voice detection model failed to load")
        if self._vad is None:
            self._vad = EnergyVad()
            log.warning("Voice detection model missing; using a simple level detector")
        self._load_engine(self._ctx.settings.speech.engine)

    def _candidates(self, setting: str) -> list[speech_models.EngineDef]:
        """Installed recognisers to try, in order: the chosen one first, then the fallbacks."""
        models = self._ctx.models
        auto = setting in ("auto", "")
        wanted = speech_models.resolve_engine(setting)
        models.register(wanted.model)  # a recogniser only measured in the bake-off may be set by hand
        order = [speech_models.ENGINES[e] for e in speech_models.AUTO_ORDER] if auto else [wanted]
        order += [e for e in speech_models.ENGINES.values() if e not in order]
        ready = [e for e in order if models.is_ready(e.model.id)]
        if not auto and ready and ready[0] is not wanted:
            self._bus.publish(Notice(f"{wanted.label} is not downloaded yet, so Sightline is using {ready[0].label}", "warn", key="speech-engine"))
        return ready

    def _load_engine(self, setting: str, *, force: bool = False) -> None:
        auto = setting in ("auto", "")
        candidates = self._candidates(setting)
        if not candidates:
            log.warning("No speech recognition model is installed")
            return
        engine = definition = None
        for i, candidate in enumerate(candidates):
            if candidate.id == self._engine_id and self._engine is not None and not force:
                self._engine_setting = setting
                return
            started = time.perf_counter()
            try:
                built = candidate.build(self._ctx.models.dir(candidate.model.id), biasing=self._wants_biasing(candidate.id))
                built.warm_up()
                if getattr(built, "biasing", False) and self._vocabulary:
                    built.set_hotwords(list(self._vocabulary))  # before timing it, so "Recommended" sees the real cost
            except Exception:
                log.exception("Could not load the %s recogniser", candidate.id)
                continue
            if auto and i < len(candidates) - 1:
                # "Recommended" means the most accurate recogniser this computer runs quickly enough.
                cost = _decode_cost_ms(built)
                if cost > speech_models.AUTO_BUDGET_MS:
                    log.info("%s takes %.0f ms for a short command here; trying a faster recogniser", candidate.id, cost)
                    self._close_engine(built)
                    continue
            engine, definition = built, candidate
            log.info("Speech recogniser %s ready in %.1f s", candidate.id, time.perf_counter() - started)
            break
        if engine is None or definition is None:
            self._bus.publish(Notice("The speech recogniser could not start", "error", key="speech-engine"))
            return
        with self._lock:
            old = self._engine
            if self._pipeline is not None:
                self._pipeline.cancel()
            self._engine = engine
            self._engine_id = definition.id
            self._engine_setting = setting
            self._engine_label = definition.label
            self._pipeline = UtterancePipeline(
                self._vad,
                engine,
                self._classify,
                self._endpoint_config(),
                on_partial=self._on_partial,
                on_final=self._on_final,
                on_speaking=self._on_speaking,
                next_id=lambda: next(self._seq),
            )
        self._close_engine(old)

    @staticmethod
    def _close_engine(engine: object | None) -> None:
        if engine is None:
            return
        try:
            engine.close()  # type: ignore[attr-defined]
        except Exception:
            log.debug("Closing a recogniser failed", exc_info=True)

    def _load_semantic(self) -> None:
        if self._interpreter.semantic is not None or self._semantic_failed:
            return
        models = self._ctx.models
        spec = speech_models.EMBEDDING
        if not models.is_ready(spec.id):
            return
        from .semantic import OnnxSentenceEmbedder, SemanticIndex, build_entries

        started = time.perf_counter()
        try:
            folder = models.dir(spec.id)
            embedder = OnnxSentenceEmbedder(folder / "model_quint8_avx2.onnx", folder / "tokenizer.json", "all-MiniLM-L6-v2")
            with self._lock:
                grammar = self._grammar
            specs = [s for s in grammar.specs.values() if s.semantic]
            entries = build_entries(specs, {s.id: grammar.canonical_phrases(s.id, 6) for s in specs})
            index = SemanticIndex(embedder, entries, threshold=speech_models.SEMANTIC_THRESHOLD, margin=speech_models.SEMANTIC_MARGIN)
        except Exception:
            log.exception("The command paraphrase model failed to load")
            self._semantic_failed = True
            return
        self._install_semantic(index)
        log.info("Paraphrase matching ready (%d phrases, %.1f s)", len(entries), time.perf_counter() - started)

    def _install_semantic(self, index: object) -> None:
        """Use the paraphrase index from now on, keeping the learned aliases (review I-05)."""
        with self._lock:
            self._interpreter = Interpreter(self._grammar, index, aliases=self._aliases)  # type: ignore[arg-type]

    def _endpoint_config(self) -> EndpointConfig:
        cfg = self._ctx.settings.speech
        onset = min(0.9, max(0.2, float(cfg.speech_threshold)))
        command_ms = int(cfg.command_endpoint_ms)
        return EndpointConfig(
            command_ms=command_ms,
            dictation_ms=max(command_ms, int(cfg.dictation_endpoint_ms)),
            early_ms=max(100, command_ms // 2),
            incomplete_ms=self._learned_ms or 0,
            onset=onset,
            offset=max(0.1, onset - 0.15),
        )

    # ================================================================== pauses inside commands

    def _pauses_path(self) -> Path:
        return paths.profile_dir(self._profile) / "speech" / "pauses.json"

    def _update_learned_pause(self) -> bool:
        """Recompute the learned wait for unfinished commands.  True when it changed."""
        cfg = self._ctx.settings.speech
        learned = self._pauses.learned_ms(int(cfg.command_endpoint_ms)) if cfg.learn_pauses else None
        changed = learned != self._learned_ms
        self._learned_ms = learned
        if changed:
            log.info("Speech: waiting %s for the rest of an unfinished command", f"{learned} ms" if learned else "the usual pause")
            pipeline = getattr(self, "_pipeline", None)
            if pipeline is not None:
                pipeline.cfg = self._endpoint_config()
        return changed

    def _learn_pauses(self, utt: Utterance, decision: Decision, kind: str, situation: Situation) -> None:
        """Pauses inside a spoken command, and a pause that cut one in two.  Called with the lock held."""
        if not self._ctx.settings.speech.learn_pauses or situation.mode == "sleep":
            self._unfinished = None
            return
        unfinished, self._unfinished = self._unfinished, None
        if kind == "command":
            self._pauses.add_command(utt.pauses_ms)
        if unfinished is not None and kind != "ignored" and unfinished.overlay == situation.overlay and decision.normalized:
            gap_ms = (utt.t_voice_start - unfinished.t_end) * 1000.0
            whole = f"{unfinished.heard} {decision.normalized}"
            parse = self._grammar.parse(whole, self._interpreter.contexts(situation), self._build_ctx)
            if 0 < gap_ms <= pause_model.SPLIT_MAX_MS and parse is not None and parse.confidence >= 0.99:
                self._pauses.add_split(gap_ms)  # "scroll" ... "down" was one command, cut by the endpoint
                log.debug("Speech: a pause of %.0f ms cut a command in two", gap_ms)
        if kind == "rejected" and decision.normalized:
            info = self._grammar.prefix(decision.normalized, self._interpreter.contexts(situation))
            if info.possible and not info.complete:
                self._unfinished = _Unfinished(decision.normalized, utt.t_speech_end, situation.overlay)
        self._update_learned_pause()

    def _sync_learned_setting(self) -> None:
        """Show the learned wait in the settings (read-only).  On the background thread: it saves the settings file."""
        cfg = self._ctx.settings.speech
        words = pause_model.describe(self._learned_ms, int(cfg.command_endpoint_ms), enabled=bool(cfg.learn_pauses))
        if getattr(cfg, "learned_command_pause", words) == words:
            return
        cfg.learned_command_pause = words
        try:
            self._ctx.settings_changed("speech")
        except OSError:
            log.exception("Could not save the learned pause")

    def _save_pauses(self, *, force: bool = False) -> None:
        with self._lock:
            if not self._pauses.dirty or (not force and time.monotonic() - self._pauses_saved < _PAUSES_SAVE_S):
                return
            data = pause_model.PauseModel.from_json(self._pauses.to_json())
            self._pauses.dirty = False
            self._pauses_saved = time.monotonic()
            path = self._pauses_path()
        try:
            data.save(path)
        except OSError:
            log.exception("Could not save the learned pauses")
            with self._lock:
                self._pauses.dirty = True

    # ================================================================== microphone

    def _reconcile_microphone(self) -> None:
        """Open or close the microphone to match ``state.listening``.  Safe to call from any thread, in any order."""
        with self._mic_lock:
            want = self._running and bool(self._ctx.state.get("listening"))
            if want and not self._mic_open:
                if self._mic is None:
                    self._mic = Microphone(self._queue.put, self._on_mic_status)
                try:
                    self._mic.start(self._mic_id)
                    self._mic_open = True
                except Exception as e:
                    log.exception("The microphone failed to start")
                    self._on_mic_status("", f"The microphone could not be opened ({e})")
            elif not want and self._mic_open:
                if self._mic is not None:
                    self._mic.stop()
                self._mic_open = False
                self._queue.clear()
                with self._lock:
                    if self._pipeline is not None:
                        self._pipeline.cancel()
                self._bus.publish(MicLevel(0.0, False))
        self._publish_status()

    def _apply_microphone_setting(self) -> None:
        wanted = self._ctx.settings.speech.microphone
        with self._mic_lock:
            if wanted == self._mic_id:
                return
            self._mic_id = wanted
            if self._mic is not None and self._mic_open:
                self._mic.switch(wanted)

    def _on_mic_status(self, name: str, error: str | None) -> None:
        changed = error != self._mic_error or bool(name and name != self._mic_name)
        if name:
            self._mic_name = name
        self._mic_error = error
        if changed:
            if error:
                self._bus.publish(Notice(error, "warn", key="speech-mic"))
            self._publish_status()

    def _publish_status(self) -> None:
        error = None
        if self._running and self._engine is None:
            error = "Speech models are not downloaded yet"
        elif self._running and self._mic_open and self._mic_error:
            error = self._mic_error
        ready = self._running and self._engine is not None and error is None
        status = SpeechStatus(ready=ready, engine=self._engine_label, microphone=self._mic_name if self._mic_open else "", error=error)
        if status != self._last_status:
            self._last_status = status
            self._bus.publish(status)

    # ================================================================== threads

    def _speech_loop(self) -> None:
        while not self._stop.is_set():
            block = self._queue.get(timeout=0.05)
            if block is None or not self._mic_open:
                continue
            self._level(block.samples)
            with self._lock:
                pipeline = self._pipeline
            if pipeline is None:
                continue
            try:
                pipeline.feed(block)
            except Exception:
                log.exception("The speech pipeline failed on a block of audio")
                pipeline.cancel()

    def _level(self, samples: np.ndarray) -> None:
        self._level_acc += float(np.dot(samples, samples))
        self._level_n += len(samples)
        now = time.perf_counter()
        if now - self._level_t < _LEVEL_PERIOD or self._level_n == 0:
            return
        rms = math.sqrt(self._level_acc / self._level_n)
        self._level_acc, self._level_n, self._level_t = 0.0, 0, now
        # A meter that looks right to people: -60 dBFS is empty, 0 dBFS is full.
        level = 0.0 if rms <= 1e-6 else min(1.0, max(0.0, (20.0 * math.log10(rms) + 60.0) / 60.0))
        pipeline = self._pipeline
        self._bus.publish(MicLevel(round(level, 3), bool(pipeline is not None and pipeline.speaking)))

    def _background_loop(self) -> None:
        if self._ctx.settings.speech.semantic_matching:
            self._load_semantic()
        last_elements = last_windows = last_file = 0.0
        while not self._stop.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                if self._pending_engine is not None:
                    setting, self._pending_engine = self._pending_engine, None
                    force, self._rebuild_engine = self._rebuild_engine, False
                    self._load_engine(setting, force=force)
                    self._publish_status()
                if self._ctx.settings.speech.semantic_matching and self._interpreter.semantic is None:
                    self._load_semantic()
                now = time.monotonic()
                if self._refresh_all.is_set():
                    self._refresh_all.clear()
                    self._refresh_names(apps=True)
                    last_elements = last_windows = now
                elif self._refresh_elements.is_set() or now - last_elements >= _ELEMENTS_PERIOD:
                    self._refresh_elements.clear()
                    windows = now - last_windows >= _WINDOWS_PERIOD
                    self._refresh_names(windows=windows)
                    last_elements = now
                    if windows:
                        last_windows = now
                if now - last_file >= _COMMANDS_FILE_PERIOD:
                    last_file = now
                    self._check_user_files()
                self._save_pauses()
                self._sync_learned_setting()
            except Exception:
                log.exception("Speech background refresh failed")

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _check_user_files(self) -> None:
        """Re-read the user's commands, aliases and vocabulary when a helper (or Sightline) changed them."""
        if self._mtime(self._commands_path) != self._commands_mtime:
            self._load_custom()
            self._rebuild()
            log.info("Reloaded %s (%d commands)", self._commands_path.name, len(self._custom_specs))
        if self._mtime(self._aliases_path) != self._aliases_mtime:
            self._load_aliases()
            log.info("Reloaded %s (%d aliases)", self._aliases_path.name, len(self._aliases))
        if self._mtime(self._vocabulary_path) != self._vocabulary_mtime:
            self._load_vocabulary()
            log.info("Reloaded %s (%d words)", self._vocabulary_path.name, len(self._vocabulary))

    _check_commands_file = _check_user_files  # the name the tests and older callers know

    def _refresh_names(self, *, apps: bool = False, windows: bool = True) -> None:
        desktop = self._ctx.desktop
        if desktop is None:
            return
        try:
            names = list(desktop.uia.visible_names())
            if names != self._last_element_names:
                self._last_element_names = names
                self._names.set_elements(names)
        except Exception:
            log.debug("Could not read on-screen names", exc_info=True)
        if windows:
            try:
                wins = desktop.windows.list_windows()
                key = [(w.hwnd, w.title) for w in wins]
                if key != self._last_windows:
                    self._last_windows = key
                    self._names.set_windows((w.title, w.process, w.hwnd) for w in wins)
            except Exception:
                log.debug("Could not list windows", exc_info=True)
        if apps:
            try:
                self._names.set_apps(a.name for a in desktop.apps.apps())
            except Exception:
                log.debug("Could not list apps", exc_info=True)

    # ================================================================== events

    def _on_focus(self, event: FocusChanged) -> None:
        self._focus = event.focus
        self._dictation.focus_changed(_focus_key(event.focus))
        self._refresh_elements.set()
        self._wake.set()

    def _sync_scroll_step(self) -> None:
        # "scroll down 3" means three of the desktop's scroll steps, whatever the user set that to.
        desktop_cfg = getattr(self._ctx.settings, "desktop", None)
        try:
            self._build_ctx.scroll_step = max(1, int(getattr(desktop_cfg, "scroll_step", 3)))
        except (TypeError, ValueError):
            self._build_ctx.scroll_step = 3

    def _on_settings(self, event: SettingsChanged) -> None:
        if event.section == "desktop":
            self._sync_scroll_step()
        if event.section == "general":
            self._switch_profile()
        if event.section != "speech":
            return
        cfg = self._ctx.settings.speech
        if cfg.wake_phrase != self._build_ctx.wake_phrase or cfg.sleep_phrase != self._build_ctx.sleep_phrase:
            # New safety words: the user's commands are checked against them again.
            threading.Thread(target=self._reload_commands, name="sightline-speech-grammar", daemon=True).start()
        with self._lock:
            self._update_learned_pause()  # the floor follows "pause that ends a command"; learning may be off
            if self._pipeline is not None:
                self._pipeline.cfg = self._endpoint_config()
        if self._running and cfg.engine != self._engine_setting:
            self._pending_engine = cfg.engine
        self._apply_microphone_setting()
        self._wake.set()

    def _switch_profile(self) -> None:
        """Pauses are per person: another profile brings its own."""
        profile = str(getattr(self._ctx.settings.general, "profile", "default") or "default")
        if profile == self._profile:
            return
        self._save_pauses(force=True)
        with self._lock:
            self._profile = profile
            self._pauses = pause_model.PauseModel.load(self._pauses_path())
            self._update_learned_pause()
        self._wake.set()

    def _on_state(self, event: StateChanged) -> None:
        if event.field == "speech_mode":
            new, old = _mode_name(event.new), _mode_name(event.old)
            with self._lock:
                if new == "sleep" and old != "sleep":
                    self._build_ctx.previous_mode = old
                self._build_ctx.current_mode = new
                if self._pipeline is not None and (new == "sleep" or old == "sleep"):
                    self._pipeline.cancel()
        elif event.field == "listening" and self._running:
            # The tray or the dock may change this on the GUI thread; opening a device can take a while.
            threading.Thread(target=self._reconcile_microphone, name="sightline-speech-mic", daemon=True).start()

    def _on_action_executed(self, event: ActionExecuted) -> None:
        # Keys and clicks move the caret, and so does typing that did not come from dictation.
        name = event.action.name
        if event.ok and (name in _CARET_ACTIONS or (name == "text.type" and event.action.source != "voice")):
            self._dictation.caret_moved()

    def _on_speaking(self, speaking: bool) -> None:
        try:
            self._ctx.state.update(speaking=speaking)
        except Exception:
            log.debug("Could not update the speaking state", exc_info=True)

    def _on_partial(self, uid: int, text: str) -> None:
        if self._ctx.state.get("speech_mode") == SpeechMode.SLEEP or self._current_focus().password:
            return  # never show speech meant for someone else, or said into a password box
        self._bus.publish(TranscriptPartial(uid, text))

    def _on_final(self, utt: Utterance) -> None:
        self._handle(utt.text, utt.id, t_speech_end=utt.t_speech_end, audio_ms=utt.audio_ms, utterance=utt)

    def _classify(self, text: str) -> int:
        """How long a pause ends what has been heard so far (see pipeline.py)."""
        pipeline = self._pipeline
        cfg = pipeline.cfg if pipeline is not None else self._endpoint_config()
        return endpoint_ms(text, self._interpreter, self._situation(self._current_focus()), cfg)

    # ================================================================== one utterance

    def _current_focus(self) -> FocusInfo:
        desktop = self._ctx.desktop
        if desktop is None:
            return self._focus
        try:
            return desktop.uia.focus_info()
        except Exception:
            return self._focus

    def _situation(self, focus: FocusInfo, away: str = "") -> Situation:
        state = self._ctx.state
        return Situation(
            mode=_mode_name(state.get("speech_mode")),
            overlay=state.get("active_overlay"),
            editable=bool(focus.editable),
            password=bool(focus.password),
            away=away,
            choices=self._pending_choices(),
            offer=self._pending_offer(),
            process=str(focus.process or ""),
        )

    def _away(self, utt: Utterance) -> str:
        """Why this utterance seems not meant for the computer (the caption), or "" when it does."""
        cfg = self._ctx.settings.speech
        if not cfg.attention_gate:
            return ""
        vision = self._ctx.vision
        attention = getattr(vision, "attention", None) if vision is not None else None
        if attention is None:
            return ""
        try:
            seen = attention(utt.t_voice_start or utt.t_speech_start, utt.t_speech_end)
        except Exception:
            log.debug("Attention check failed", exc_info=True)
            return ""
        if seen is None or getattr(seen, "frames", 0) <= 0:
            return ""  # vision had nothing to go on: no gate
        if seen.face < MIN_FACE or seen.facing < MIN_FACING:
            return "Ignored: you were looking away"
        if cfg.mouth_gate and seen.mouth is not None and seen.mouth < MIN_MOUTH:
            return "Ignored: your mouth was not moving"
        return ""

    # ------------------------------------------------------------------ "did you mean"

    def _pending_choices(self) -> int:
        choices = self._choices
        if choices is None:
            return 0
        if time.monotonic() - choices.t > CHOICE_HOLD_S:
            self._choices = None
            return 0
        return len(choices.items)

    def _pending_offer(self) -> bool:
        return bool(self._pending_choices()) and self._choices is not None and self._choices.offer is not None

    def _take_choice(self, index: int) -> tuple[str, Match, _Choices] | None:
        if not self._pending_choices():
            return None
        choices = self._choices
        assert choices is not None
        if not 1 <= index <= len(choices.items):
            return None
        self._choices = None
        label, match = choices.items[index - 1]
        return label, match, choices

    def _handle(self, text: str, uid: int, *, t_speech_end: float | None, audio_ms: float | None, utterance: Utterance | None = None) -> None:
        started = time.perf_counter()
        aim_t = utterance.t_speech_start if utterance is not None and utterance.t_speech_start else None
        away = self._away(utterance) if utterance is not None else ""
        alternatives: tuple[str, ...] = ()
        with self._lock:
            focus = self._current_focus()
            situation = self._situation(focus, away)
            self._build_ctx.current_mode = situation.mode
            semantic = bool(self._ctx.settings.speech.semantic_matching)
            try:
                decision = self._interpreter.decide(text, situation, self._build_ctx, audio_ms=audio_ms, semantic=semantic)
            except Exception:
                log.exception("Could not interpret an utterance")
                decision = Decision("rejected", text, reason="Something went wrong understanding that")
            noise = decision.kind == "ignored" and not decision.reason
            chose = decision.parse is not None and any(m.spec.id in _CHOOSING for m in decision.parse.matches)
            self._last_choice = None
            try:
                actions, display, kind = self._act(decision, focus, aim_t)
            except Exception:
                log.exception("Could not act on an utterance")
                actions, display, kind = [], "Something went wrong with that command", "rejected"
            if kind == "rejected" and decision.alternatives:
                items = [(m.outcome.display, m) for m in decision.alternatives]
                self._choices = _Choices(items, decision.text, aim_t, time.monotonic(), heard_norm=decision.normalized, spoken=utterance is not None)
                alternatives = tuple(label for label, _ in items)
            elif kind in ("command", "dictation") and not chose:
                # A real command or new dictation withdraws the suggestions; talk that was not for
                # the computer, or another miss without suggestions, leaves them up.
                self._choices = None
            if utterance is not None and not noise:
                self._learn_pauses(utterance, decision, kind, situation)
            offer = self._offer_alias(kind, situation, decision.parse.matches if decision.parse is not None else None, decision.text)
            if offer is not None:
                self._choices = offer
                alternatives = (offer.items[0][0],)
            self._note_miss(decision, kind, situation, spoken=utterance is not None)

        typed = any(a.name == "text.type" for a in actions)
        secret = _secret_display(actions) if situation.password else None
        # Words said into a password box are never shown or logged, whatever they turned into: what was
        # typed or pressed there shows as bullets, whether it was dictation, spelled keys or a snippet.
        private = situation.password and (typed or secret is not None or kind != "command")
        heard = "" if private else decision.text
        if private and kind == "rejected":
            display = "Not understood"
        elif secret is not None:
            display = secret
        if utterance is not None and not noise and situation.mode != "sleep":
            self._bus.publish(TranscriptFinal(uid, heard, utterance.audio_ms, utterance.endpoint_ms, utterance.asr_ms))
        for action in actions:
            self._ctx.actions.dispatch(action)
        now = time.perf_counter()
        latency = (now - (t_speech_end if t_speech_end is not None else started)) * 1000.0
        if not noise:
            self._log_utterance(decision, kind, display, latency, private)
        self._bus.publish(IntentResult(uid, heard, kind, display, tuple(actions), round(decision.confidence, 3), round(latency, 1), alternatives))

    def _log_utterance(self, decision: Decision, kind: str, display: str, latency: float, private: bool) -> None:
        """What the log keeps of an utterance: its kind and length at DEBUG, never the words at INFO.

        The words and what they became are written only for a developer who turned on
        ``general.log_speech`` (and DEBUG logging), and never from a password field.
        """
        if not log.isEnabledFor(logging.DEBUG):
            return
        if private:
            log.debug("Speech: %s in a password field (%.0f ms)", kind, latency)
            return
        log.debug("Speech: %s (%s, %d chars, %.0f ms)", kind, decision.method or "-", len(decision.text), latency)
        if bool(getattr(getattr(self._ctx.settings, "general", None), "log_speech", False)):
            log.debug("Speech heard %r -> %s", decision.text, display)

    def _act(self, decision: Decision, focus: FocusInfo, aim_t: float | None) -> tuple[list[Action], str, str]:
        if decision.kind == "ignored":
            return [], decision.reason, "ignored"
        if decision.kind == "rejected":
            if decision.reason:
                return [], decision.reason, "rejected"
            heard = decision.text.strip().rstrip(".!?")
            return [], f"Not understood: '{_clip(heard, 40)}'", "rejected"
        if decision.kind == "dictation":
            text = self._format_dictation(decision.text, focus)
            if not text:
                return [], "", "ignored"
            return [self._type(text, focus, decision.text, aim_t)], self._typed_display(text, focus), "dictation"
        assert decision.parse is not None
        actions, shown = self._run_matches(decision.parse.matches, focus, decision.text, aim_t)
        if not actions and not shown:
            return [], "Nothing to choose", "rejected"
        display = ", ".join(shown) if shown else decision.parse.matches[0].spec.id
        return actions, display, "command"

    def _run_matches(self, matches: tuple[Match, ...], focus: FocusInfo, heard: str, aim_t: float | None) -> tuple[list[Action], list[str]]:
        """The actions and captions for matched commands; applies their effect on the build context."""
        actions: list[Action] = []
        shown: list[str] = []
        for match in matches:
            if match.spec.id in _CHOOSING:
                index = 1 if match.spec.id == "speech.remember" else int(match.slots.get("index", 0))  # "yes" takes the offer
                taken = self._take_choice(index)
                if taken is None:
                    continue
                more, captions = self._run_choice(taken, focus)
                actions.extend(more)
                shown.extend(captions)
                continue
            if match.spec.id == "speech.vocabulary":
                shown.append(self._add_vocabulary(match, heard))
                continue
            outcome = match.outcome
            if outcome.type_request is not None:
                text = self._format_request(outcome.type_request, focus)
                if text:
                    actions.append(self._type(text, focus, heard, aim_t))
                    shown.append(self._typed_display(text, focus))
            else:
                for a in outcome.actions:
                    if a.name == "text.type" and a.arg("text"):
                        # Snippets and typed symbols count as dictation, so "scratch that" removes them.
                        self._dictation.record(_focus_key(focus), str(a.arg("text")), focus.password)
                    aimed = a.aim_t if a.aim_t is not None else aim_t
                    if focus.password and a.name in ("text.type", "key.press"):
                        # Typed or pressed into a password box: no words travel with it, and logs show nothing.
                        actions.append(replace(a, args={**a.args, PRIVATE: True}, utterance=None, aim_t=aimed))
                    else:
                        actions.append(replace(a, utterance=a.utterance or heard, aim_t=aimed))
                if outcome.display:
                    shown.append(outcome.display)
            self._build_ctx.apply(outcome)
        if actions and not any(m.spec.id == "speech.repeat" or m.spec.id in _CHOOSING for m in matches):
            self._build_ctx.last_command = tuple(a for a in actions if a.name != "text.type")
        return actions, shown

    def _type(self, text: str, focus: FocusInfo, heard: str, aim_t: float | None = None) -> Action:
        self._dictation.record(_focus_key(focus), text, focus.password)
        if focus.password:
            return Action("text.type", {"text": text, PRIVATE: True}, source="voice", utterance=None, aim_t=aim_t)
        return Action("text.type", {"text": text}, source="voice", utterance=heard, aim_t=aim_t)

    @staticmethod
    def _typed_display(text: str, focus: FocusInfo) -> str:
        if focus.password:
            return "Typed " + _bullets(len(text))
        shown = " ".join(text.split())
        return f"Typed: {_clip(shown)}" if shown else "Typed a new line"

    # ================================================================== dictation text

    def _text_before(self, focus: FocusInfo) -> str | None:
        """Text left of the caret, for spacing and capitals.  None when unknown."""
        key = _focus_key(focus)
        own = self._dictation.before(key)
        desktop = self._ctx.desktop
        before: str | None = None
        if desktop is not None and not focus.password:
            try:
                before = desktop.uia.text_before_caret(max_chars=200, timeout=0.05)
            except Exception:
                before = None
        if before is None:
            return own
        last = self._dictation.last(key)
        if own and last is not None and last.text and time.monotonic() - last.t < _PENDING_TYPING_S and not before.endswith(last.text):
            # Our previous phrase is probably still being typed; the field has not caught up yet.
            return own
        return before

    def _format_dictation(self, words: str, focus: FocusInfo, *, formatter: str = "") -> str:
        cfg = self._ctx.settings.speech
        text = dictation_text(words, spoken_punctuation=cfg.spoken_punctuation, keep_recogniser_punctuation=cfg.auto_punctuation)
        if not text:
            return ""
        if formatter and formatter != "NOOP":
            text = apply_formatters(text, formatter)
        before = self._text_before(focus) if (cfg.smart_spacing or cfg.auto_capitalize) else None
        auto_cap = cfg.auto_capitalize and formatter in ("", "NOOP")
        return place_text(text, before, auto_cap=auto_cap, smart_spacing=cfg.smart_spacing)

    def _format_request(self, request: TypeRequest, focus: FocusInfo) -> str:
        if not request.prose:
            return request.text
        return self._format_dictation(request.text, focus, formatter=request.formatter)

    # ================================================================== actions

    def _register_actions(self) -> None:
        reg = self._ctx.actions
        reg.register("speech.mode", self._act_mode, description="Change how speech is used", undo=_undo_mode)
        reg.register("speech.scratch", self._act_scratch, description="Remove the last dictated words", undo=_undo_scratch)
        reg.register("speech.listen", self._act_listen, description="Turn the microphone on or off", undo=_undo_listen)
        # The suggestion's own actions are what "undo that" should reverse, so choosing is not remembered.
        reg.register("speech.choose", self._act_choose, description="Run one of the \"did you mean\" suggestions", history=False)

    def _act_mode(self, action: Action) -> str:
        mode = str(action.arg("mode", "")).strip().lower()
        try:
            new = SpeechMode(mode)
        except ValueError:
            raise ActionError(f"There is no speech mode called {mode or 'that'}") from None
        old = _mode_name(self._ctx.state.get("speech_mode"))
        self._ctx.state.update(speech_mode=new)
        return old

    def _act_listen(self, action: Action) -> bool:
        old = bool(self._ctx.state.get("listening"))
        on = action.arg("on")
        if on is None:
            on = not old
        elif isinstance(on, str):
            on = on.strip().lower() not in ("", "0", "false", "off", "no")
        self._ctx.state.update(listening=bool(on))  # _on_state opens or closes the device
        return old

    def _act_choose(self, action: Action) -> None:
        """A "did you mean" chip chosen by blink, dwell or the UI (spoken numbers are handled in _act)."""
        try:
            index = int(action.arg("index"))
        except (TypeError, ValueError):
            raise ActionError("Say which suggestion: one, two or three") from None
        alternatives: tuple[str, ...] = ()
        with self._lock:
            taken = self._take_choice(index)
            if taken is None:
                raise ActionError("That suggestion has gone; please say the command again")
            label, _chosen, choices = taken
            focus = self._current_focus()
            self._last_choice = None
            actions, shown = self._run_choice(taken, focus)
            offer = self._offer_alias("command", self._situation(focus), None, choices.heard)
            if offer is not None:
                self._choices = offer
                alternatives = (offer.items[0][0],)
        for a in actions:
            self._ctx.actions.dispatch(a)
        log.debug("Speech: chose suggestion %d", index)
        self._bus.publish(IntentResult(next(self._seq), choices.heard, "command", ", ".join(shown) or label, tuple(actions), 1.0, 0.0, alternatives))

    def _run_choice(self, taken: tuple[str, Match, _Choices], focus: FocusInfo) -> tuple[list[Action], list[str]]:
        """Run a numbered choice: a "did you mean" suggestion, or "yes" to remembering an alias."""
        label, chosen, choices = taken
        self._last_choice = (choices, chosen)
        if choices.offer is not None:
            return [], [self._remember_alias(*choices.offer)]
        more, captions = self._run_matches((chosen,), focus, choices.heard, choices.aim_t)
        return more, captions or [label]

    # ------------------------------------------------------------------ personal aliases (aliases.py)

    def _note_miss(self, decision: Decision, kind: str, situation: Situation, *, spoken: bool) -> None:
        """Remember a spoken utterance that was not understood, in case the user then gets the command another way."""
        if kind != "rejected" or not spoken or decision.reason or situation.password or situation.mode == "sleep":
            return
        if self._ctx.settings.speech.learn_aliases and decision.normalized and self._aliasable_heard(decision.normalized):
            self._miss = _Miss(decision.normalized, time.monotonic())

    def _offer_alias(self, kind: str, situation: Situation, matches: tuple[Match, ...] | None, text: str) -> _Choices | None:
        """After a miss, the command given another way: offer to remember what was heard as an alias for it."""
        if kind != "command" or situation.password or situation.mode == "sleep" or not self._ctx.settings.speech.learn_aliases:
            return None
        taken = self._last_choice
        if taken is not None:  # a "did you mean" choice: it says exactly what the miss meant
            choices, chosen = taken
            if choices.offer is not None or not choices.spoken:
                return None
            heard, found, t_miss = choices.heard_norm, (chosen,), choices.t
        else:
            miss = self._miss
            if miss is None or not matches:
                return None
            heard, found, t_miss = miss.heard, matches, miss.t
        self._miss = None  # one offer per miss, whatever comes of it
        if time.monotonic() - t_miss > ALIAS_WINDOW_S or not heard or not self._aliasable_heard(heard):
            return None
        target = self._alias_target(found, situation)
        if target is None or target == heard:
            return None
        label = f"Say yes to remember '{_clip(heard, 30)}' as '{_clip(target, 30)}'"
        return _Choices([(label, found[0])], text, None, time.monotonic(), heard_norm=heard, offer=(heard, target))

    def _aliasable_heard(self, heard: str) -> bool:
        """Short, not noise, not already an alias, and nothing like a command in any context.

        An alias must not hide a real command (a wizard's "next", the wake phrase, "faster"
        that only failed because nothing was scrolling), and the start of a command is a
        command cut short by a pause ("scroll" ... "down"), not a mishearing.
        """
        words = heard.split()
        if not 1 <= len(words) <= ALIAS_MAX_WORDS or heard in self._aliases or is_probably_noise(heard, None):
            return False
        return not self._grammar.prefix(heard, self._grammar.contexts()).possible

    def _alias_target(self, matches: tuple[Match, ...], situation: Situation) -> str | None:
        """The words that run these matches, when they may be aliased: never hard to undo, never free text."""
        contexts = self._interpreter.contexts(replace(situation, choices=0, offer=False))
        focus = FocusInfo(process=situation.process)
        phrases: list[str] = []
        for m in matches:
            spec = m.spec
            if spec.id in _NOT_ALIAS_TARGETS or spec.free_text or spec.exact_only or is_risky(m, focus):
                return None
            if not m.outcome.actions and m.outcome.type_request is None:
                return None
            phrase = m.text if m.method in ("exact", "fuzzy") else self._canonical_phrase(m, contexts)
            if not phrase:
                return None
            phrases.append(phrase)
        target = " ".join(phrases)
        parse = self._grammar.parse(target, contexts, self._build_ctx)
        if parse is None or parse.confidence < 0.99 or any(is_risky(m, focus) for m in parse.matches):
            return None
        return target

    def _canonical_phrase(self, match: Match, contexts: list[str]) -> str | None:
        """A plain way of saying a paraphrased command that does the same thing ("scroll down" for "go down the page")."""
        want = [(a.name, dict(a.args)) for a in match.outcome.actions]
        for phrase, _slots in self._grammar.canonical_phrases(match.spec.id, limit=40):
            parse = self._grammar.parse(phrase, contexts, self._build_ctx, allow_chain=False)
            if parse is None or len(parse.matches) != 1 or parse.matches[0].spec.id != match.spec.id:
                continue
            if [(a.name, dict(a.args)) for a in parse.matches[0].outcome.actions] == want:
                return phrase
        return None

    def _remember_alias(self, heard: str, target: str) -> str:
        try:
            alias_file.add(self._aliases_path, heard, target)
        except (OSError, ValueError) as e:
            log.warning("Could not save an alias: %s", e)
            self._bus.publish(Notice("Sightline could not save that", "warn", key="speech-aliases"))
            return "Could not remember that"
        aliases = dict(self._aliases)
        aliases[heard] = target
        self._aliases = aliases
        self._interpreter.aliases = dict(aliases)
        self._aliases_mtime = self._mtime(self._aliases_path)  # our own change needs no reload
        log.info("Speech: remembered an alias (%d in all)", len(aliases))
        return f"Remembered: '{_clip(heard, 30)}' means '{_clip(target, 30)}'"

    # ------------------------------------------------------------------ vocabulary (custom.py)

    def _add_vocabulary(self, match: Match, heard: str) -> str:
        """"add that to vocabulary" (the last phrase typed by voice) or "add <words> to vocabulary"."""
        spoken = str(match.slots.get("words", "")).strip()
        if spoken:
            entry = _original_words(heard, spoken) or spoken
        else:
            last = self._dictation.latest()
            entry = last.text if last is not None else ""
        if not entry.strip(_EDGE_PUNCTUATION):
            return "Dictate or spell the word first, then say add that to vocabulary"
        clean = custom.vocabulary_entry(entry)
        if clean is None:
            return "That is too long for the vocabulary.  Say add, then the word, then to vocabulary"
        try:
            added = custom.add_to_vocabulary(self._vocabulary_path, clean)
        except (OSError, ValueError) as e:
            log.warning("Could not add to the vocabulary: %s", e)
            return "Could not add that to the vocabulary"
        if not added:
            return f"'{clean}' is already in your vocabulary"
        self._load_vocabulary()
        log.info("Speech: added a word to the vocabulary (%d words)", len(self._vocabulary))
        return f"Added '{clean}' to your vocabulary"

    def _act_scratch(self, action: Action) -> tuple[str, str]:
        item = self._dictation.pop_last()
        if item is None:
            raise ActionError("Nothing to scratch")
        focus = self._current_focus()
        same_place = _focus_key(focus) == item.focus_key and not item.moved
        if same_place and item.text:
            desktop = self._ctx.desktop
            try:
                before = desktop.uia.text_before_caret(max_chars=max(50, item.length + 10), timeout=0.1) if desktop else None
            except Exception:
                before = None
            if before is not None and not before.endswith(item.text):
                same_place = False
        if same_place:
            remaining = item.length
            while remaining > 0:  # key.press repeats at most 100 times
                step = min(100, remaining)
                self._ctx.actions.execute_now(Action("key.press", {"combo": "backspace", "repeat": step}, source="voice"))
                remaining -= step
            return ("retype", item.text)
        # The caret moved or the field changed: let the app undo instead of deleting the wrong text.
        self._ctx.actions.execute_now(Action("key.press", {"combo": "ctrl+z"}, source="voice"))
        return ("redo", "")


# ---------------------------------------------------------------------- "undo that" for speech actions


def _undo_mode(action: Action, old: object) -> Action | None:
    return Action("speech.mode", {"mode": old}, source=action.source) if isinstance(old, str) and old else None


def _undo_listen(action: Action, old: object) -> Action | None:
    return Action("speech.listen", {"on": bool(old)}, source=action.source) if isinstance(old, bool) else None


def _undo_scratch(action: Action, result: object) -> Action | None:
    if not isinstance(result, tuple) or len(result) != 2:
        return None
    how, text = result
    if how == "retype" and text:
        return Action("text.type", {"text": text}, source=action.source)
    if how == "redo":
        return Action("key.press", {"combo": "ctrl+y"}, source=action.source)
    return None  # a password field: the text was never kept


_EDGE_PUNCTUATION = " \t\n.,;:!?'\"()[]"


def _original_words(raw: str, normalized: str) -> str | None:
    """The words as the recogniser wrote them ("Siobhan"), found in the raw text by their normalised form ("siobhan")."""
    tokens = clean_transcript(raw).split()
    target = normalized.split()
    n = len(target)
    for i in range(len(tokens) - n + 1):
        window = tokens[i : i + n]
        if normalize_command(" ".join(window)).split() == target:
            return " ".join(window).strip(_EDGE_PUNCTUATION)
    return None


def _clip(text: str, limit: int = _MAX_DISPLAY) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _bullets(count: int) -> str:
    return "•" * max(1, min(int(count), _MAX_BULLETS))


def _printed_keys(combo: object) -> int:
    """How many characters a key press types: letters, digits, symbols and space count; Enter, arrows and shortcuts do not."""
    modifiers, keys = split_combo(combo)
    if any(m in ("ctrl", "alt", "win") for m in modifiers):
        return 0
    return sum(
        1 for k in keys if k not in _NON_PRINTING_KEYS and not re.fullmatch(r"f\d{1,2}", k) and not k.startswith(_NON_PRINTING_PREFIXES)
    )


def _secret_display(actions: list[Action]) -> str | None:
    """The caption for what was typed or pressed into a password field ("Typed ••••••"), or None when nothing was."""
    typed = pressed = 0
    for a in actions:
        if a.name == "text.type":
            typed += len(str(a.arg("text") or ""))
        elif a.name == "key.press":
            try:
                repeat = max(1, int(a.arg("repeat", 1) or 1))
            except (TypeError, ValueError):
                repeat = 1
            pressed += _printed_keys(a.arg("combo") or a.arg("key")) * repeat
    if not typed and not pressed:
        return None
    return ("Typed " if typed else "Pressed ") + _bullets(typed + pressed)


__all__ = ["DictationLog", "SpeechService", "endpoint_ms"]
