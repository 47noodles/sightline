"""Deciding what an utterance means: a command, dictation, or nothing.

This is the arbitration described in ARCHITECTURE.md section 8, kept free of
threads and devices so it can be tested directly:

* sleep mode: only the wake phrase;
* dictation mode: a few escape commands, everything else is typed;
* auto mode: an utterance that is entirely a command runs as one; otherwise
  it is typed when a text field has focus, or reported as not understood;
* command mode: commands only.

Precision comes first.  Inside a text field (auto mode) only exact or
near-exact commands win over typing; paraphrase matching and unresolved
names never do.  When the speech seems not to be meant for the computer
(the user faced away), nothing is typed and only the safety commands run
(``SAFE_WHEN_AWAY``: stop, cancel, sleep, wake, pause or resume eyes, undo
that, help): a voice from a video or a call must not open a command prompt.
Actions that are hard to reverse (``core.risk``) never run from a
paraphrase; they are offered as "did you mean" instead, and so are near
misses of a rejected utterance.

The user's personal aliases (``aliases.py``) are tried before anything
else, except in text fields and dictation, where the words are usually
meant as text, and when the user faced away (an alias is learned, not
exact).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from sightline.core.risk import RISKY_ACTIONS, RISKY_KEYS, assess, normalize_combo
from sightline.core.types import FocusInfo

from .grammar import BuildContext, Outcome
from .matcher import Grammar, Match, Parse
from .normalize import is_probably_noise, normalize_command, strip_lead_in
from .semantic import SemanticIndex
from .vocab import FORMATTERS, PROSE_FORMATTERS

log = logging.getLogger(__name__)

OVERLAY_CONTEXTS = {
    "labels": "labels",
    "grid": "grid",
    "setup": "wizard",
    "calibration": "wizard",
    "help": "help",
    "settings": "settings",
    "chooser": "chooser",
    "menu": "menu",
    "confirm": "confirm",
}

# Fuzzy matches must be at least this close to a template phrase (0..1).
FUZZY_MIN = 0.86
FUZZY_MIN_IN_TEXT_FIELD = 0.92
SEMANTIC_MAX_WORDS = 10
PROSE_FORMAT_MAX_WORDS_IN_TEXT_FIELD = 4

# "Did you mean" candidates: looser than what runs on its own, because the user confirms them.
MAX_ALTERNATIVES = 3
SUGGEST_FUZZY_MIN = 72.0  # rapidfuzz ratio, 0..100
SUGGEST_SEMANTIC_MIN = 0.48  # cosine similarity
SUGGEST_MAX_WORDS = 8

# Commands about words in the focused text field; elsewhere "select the whole thing" means select all.
FIELD_ONLY = frozenset({"text.select", "text.select_range", "text.caret"})

# "Yes" to an offer: too common a word to take from talk that was not meant for the computer.
NOT_WHEN_AWAY = frozenset({"speech.remember"})

# The safety commands: they stop, pause, cancel or undo, so they are all that runs from speech the attention
# gate says was not for the computer (review S-01), and no command in commands.toml may take their words.
SAFE_WHEN_AWAY = frozenset(
    {
        "speech.sleep",
        "speech.wake",
        "key.stop",
        "key.release_all",
        "mouse.scroll_stop",
        "ui.cancel",
        "confirm.cancel",
        "chooser.cancel",
        "pointer.pause",
        "pointer.resume",
        "pointer.toggle",
        "action.undo",
        "ui.help.show",
        "ui.help.hide",
    }
)
SAFETY_COMMANDS = SAFE_WHEN_AWAY


@dataclass(frozen=True, slots=True)
class Situation:
    mode: str = "auto"  # auto, command, dictation, sleep
    overlay: str | None = None
    editable: bool = False
    password: bool = False
    away: str = ""  # why the speech seems not meant for the computer (the caption); empty when it is
    choices: int = 0  # "did you mean" alternatives waiting for "one", "two" or "three"
    offer: bool = False  # the waiting choice offers to remember an alias, so "yes" takes it
    process: str = ""  # the focused process ("cmd.exe"): Enter into a console is hard to reverse


@dataclass(frozen=True, slots=True)
class Decision:
    kind: str  # "command", "dictation", "ignored" or "rejected"
    text: str  # what was heard (cleaned)
    normalized: str = ""
    parse: Parse | None = None
    reason: str = ""  # for rejected and ignored: what to show
    method: str = ""  # exact, chain, fuzzy, semantic, names
    confidence: float = 1.0
    timings: dict[str, float] = field(default_factory=dict)
    alternatives: tuple[Match, ...] = ()  # "did you mean" candidates for a rejected utterance, best first

    @property
    def outcomes(self) -> tuple[Outcome, ...]:
        return tuple(m.outcome for m in self.parse.matches) if self.parse else ()


def is_risky(match: Match, focus: FocusInfo | None = None) -> bool:
    """True when running this match could close, delete, send or launch something (``core.risk``)."""
    if match.spec.risky:
        return True
    return any(assess(action, focus) is not None for action in match.outcome.actions)


def safe_when_away(parse: Parse) -> bool:
    """True when every command in the parse is a safety command (stop, cancel, sleep, wake, ...)."""
    return bool(parse.matches) and all(m.spec.id in SAFE_WHEN_AWAY for m in parse.matches)


def _focus(situation: Situation) -> FocusInfo:
    return FocusInfo(process=situation.process, editable=situation.editable, password=situation.password)


class Interpreter:
    def __init__(self, grammar: Grammar, semantic: SemanticIndex | None = None, aliases: dict[str, str] | None = None) -> None:
        self.grammar = grammar
        self.semantic = semantic
        self.aliases: dict[str, str] = dict(aliases or {})  # normalised heard words -> the command's words

    def contexts(self, situation: Situation) -> list[str]:
        if situation.mode == "sleep":
            return ["sleep"]
        out: list[str] = []
        if situation.choices and not situation.offer:
            out.append("choices")  # the newest thing on screen wins a bare "two"
        if situation.offer:
            out.append("offer")
        overlay = OVERLAY_CONTEXTS.get(situation.overlay or "")
        if overlay:
            out.append(overlay)
        if situation.choices and situation.offer:
            # "one" takes an offer to remember an alias too, but an overlay's numbers come first:
            # the corrected command may have been a click that opened the number chooser.
            out.append("choices")
        out.append("dictation" if situation.mode == "dictation" else "global")
        return out

    def decide(self, raw: str, situation: Situation, ctx: BuildContext, *, audio_ms: float | None = None, semantic: bool = True) -> Decision:
        started = time.perf_counter()
        norm = normalize_command(raw)
        timings: dict[str, float] = {}
        if is_probably_noise(norm, audio_ms):
            return Decision("ignored", raw, norm)
        contexts = self.contexts(situation)
        away = situation.away

        if situation.mode == "sleep":
            parse = self.grammar.parse(norm, contexts, ctx, allow_chain=False)
            if parse is None and not away:
                parse = self.grammar.fuzzy(norm, contexts, ctx, min_score=80.0)
            if parse is None or parse.confidence < 0.99 and parse.method != "fuzzy":
                return Decision("ignored", raw, norm)
            return Decision("command", raw, norm, parse, method=parse.method, confidence=parse.confidence)

        in_text = situation.mode == "dictation" or (situation.mode == "auto" and situation.editable)

        if situation.mode == "dictation":
            parse = self._parse(norm, contexts, situation, ctx, allow_chain=False)
            if parse is None and not away:
                parse = self.grammar.fuzzy(norm, contexts, ctx, min_score=FUZZY_MIN_IN_TEXT_FIELD * 100)
            timings["match_ms"] = _ms(started)
            if parse is not None and (parse.confidence >= 0.99 or parse.method == "fuzzy"):
                if away and not safe_when_away(parse):
                    return Decision("ignored", raw, norm, reason=away, timings=timings)
                return Decision("command", raw, norm, parse, method=parse.method, confidence=parse.confidence, timings=timings)
            if away:
                return Decision("ignored", raw, norm, reason=away, timings=timings)
            return Decision("dictation", raw, norm, timings=timings)

        focus = _focus(situation)
        if self.aliases and not in_text and not away:  # an alias is learned, not exact: not from talk to someone else
            aliased = self._aliased(norm, contexts, situation, ctx)
            if aliased is not None:
                timings["match_ms"] = _ms(started)
                risky = [m for m in aliased.matches if is_risky(m, focus)]
                if risky:
                    # A helper may have written one; it still waits for confirmation, like a paraphrase.
                    m = risky[0]
                    return Decision("rejected", raw, norm, reason=f"Say 1 to confirm: {m.outcome.display}", method="alias", confidence=m.confidence, timings=timings, alternatives=(m,))
                return Decision("command", raw, norm, aliased, method="alias", confidence=aliased.confidence, timings=timings)

        parse = self._parse(norm, contexts, situation, ctx)
        timings["match_ms"] = _ms(started)
        if parse is not None and away and any(m.spec.id in NOT_WHEN_AWAY for m in parse.matches):
            return Decision("ignored", raw, norm, reason=away, timings=timings)
        low_confidence: Parse | None = None
        if parse is not None:
            if parse.confidence >= 0.99:
                if away and not safe_when_away(parse):
                    # Not meant for the computer, as far as we can tell: a voice on a call or in a video
                    # may say "open command prompt" exactly, so only the safety commands run.
                    return Decision("ignored", raw, norm, reason=away, timings=timings)
                if in_text and _yields_to_dictation(parse):
                    if away:
                        return Decision("ignored", raw, norm, reason=away, timings=timings)
                    return Decision("dictation", raw, norm, timings=timings)
                return Decision("command", raw, norm, parse, method=parse.method, confidence=parse.confidence, timings=timings)
            low_confidence = parse

        if away:
            # Not meant for the computer: no near misses, paraphrases or typing either.
            return Decision("ignored", raw, norm, reason=away, timings=timings)

        if not in_text:
            # "let's scroll down please".  Never inside a text field, where "let's go back" is a sentence.
            inner = strip_lead_in(norm)
            if inner != norm:
                stripped = self._parse(inner, contexts, situation, ctx)
                if stripped is not None and stripped.confidence >= 0.99:
                    return Decision("command", raw, norm, stripped, method=stripped.method, confidence=stripped.confidence, timings=timings)

        fuzzy = self.grammar.fuzzy(norm, contexts, ctx, min_score=(FUZZY_MIN_IN_TEXT_FIELD if in_text else FUZZY_MIN) * 100)
        timings["fuzzy_ms"] = _ms(started)
        if fuzzy is not None:
            return Decision("command", raw, norm, fuzzy, method="fuzzy", confidence=fuzzy.confidence, timings=timings)

        if in_text:
            # Typing wins over guesses inside a text field.
            return Decision("dictation", raw, norm, timings=timings)

        use_semantic = semantic and self.semantic is not None and 2 <= len(norm.split()) <= SEMANTIC_MAX_WORDS
        if use_semantic:
            assert self.semantic is not None
            hit = self.semantic.match(norm)
            timings["semantic_ms"] = _ms(started)
            if hit is not None:
                m = self._semantic_match(hit.spec_id, hit.slots, hit.score, norm, contexts, ctx)
                if m is not None:
                    if is_risky(m, focus):
                        # Hard to reverse: ask first, never run from a paraphrase.
                        return Decision("rejected", raw, norm, reason=f"Say 1 to confirm: {m.outcome.display}", method="semantic", confidence=m.confidence, timings=timings, alternatives=(m,))
                    return Decision("command", raw, norm, Parse((m,), "semantic", m.confidence), method="semantic", confidence=m.confidence, timings=timings)

        if low_confidence is not None and _plausible_names(low_confidence):
            # An explicit verb with a name we could not find ("open zoom", "switch to crow"):
            # let the desktop look it up; it says so if nothing matches.
            return Decision("command", raw, norm, low_confidence, method="names", confidence=low_confidence.confidence, timings=timings)

        reason = self._explain(norm, contexts, ctx)
        alternatives = self.suggest(norm, contexts, ctx, semantic=use_semantic)
        timings["suggest_ms"] = _ms(started)
        return Decision("rejected", raw, norm, reason=reason, timings=timings, alternatives=alternatives)

    # ------------------------------------------------------------------ pieces

    def _parse(self, norm: str, contexts: list[str], situation: Situation, ctx: BuildContext, *, allow_chain: bool = True) -> Parse | None:
        parse = self.grammar.parse(norm, contexts, ctx, allow_chain=allow_chain)
        if parse is not None and "choices" in contexts and any(m.spec.id == "speech.choose" and int(m.slots.get("index", 0)) > situation.choices for m in parse.matches):
            # "three" with only two suggestions on screen: it means something else (a number label, say).
            parse = self.grammar.parse(norm, [c for c in contexts if c != "choices"], ctx, allow_chain=allow_chain)
        if parse is not None and not situation.editable and any(m.spec.id in FIELD_ONLY for m in parse.matches):
            return None
        return parse

    def _aliased(self, norm: str, contexts: list[str], situation: Situation, ctx: BuildContext) -> Parse | None:
        """The command an alias stands for, when it makes sense here (an alias for "12" needs the numbers up)."""
        target = self.aliases.get(norm)
        if not target:
            return None
        local = [c for c in contexts if c not in ("global", "dictation")]
        if local:
            # An alias may redirect a global phrase (a helper's choice), never what is on screen now:
            # "next" in the settings window, a number the chooser shows, "yes" to an offer.
            own = self.grammar.parse(norm, local, ctx, allow_chain=False)
            if own is not None and own.confidence >= 0.99:
                return None
        parse = self._parse(target, contexts, situation, ctx)
        if parse is None or parse.confidence < 0.99:
            return None
        return Parse(parse.matches, "alias", parse.confidence)

    def _semantic_match(self, spec_id: str, slots: dict, score: float, norm: str, contexts: Sequence[str], ctx: BuildContext) -> Match | None:
        spec = self.grammar.specs.get(spec_id)
        if spec is None or not any(c in spec.contexts for c in contexts):
            return None
        try:
            outcome = spec.build(slots, ctx)
        except Exception:
            log.exception("Builder for %s failed on a paraphrase", spec_id)
            return None
        if outcome.reject:
            return None
        return Match(spec, slots, outcome, "semantic", round(score, 3), norm)

    def suggest(self, norm: str, contexts: Sequence[str], ctx: BuildContext, *, semantic: bool = True) -> tuple[Match, ...]:
        """Up to three runnable commands close to a rejected utterance, best first."""
        if not norm or len(norm.split()) > SUGGEST_MAX_WORDS:
            return ()
        ranked: list[tuple[float, Match]] = []
        for score, m in self.grammar.near_misses(norm, [c for c in contexts if c != "choices"], ctx, min_score=SUGGEST_FUZZY_MIN, limit=MAX_ALTERNATIVES):
            # Put both kinds of evidence on one 0..1 scale: 0 at the suggestion floor, 1 where it would have run.
            ranked.append(((score - SUGGEST_FUZZY_MIN) / (FUZZY_MIN * 100 - SUGGEST_FUZZY_MIN), m))
        if semantic and self.semantic is not None and len(norm.split()) >= 2:
            for hit in self.semantic.near_misses(norm, min_score=SUGGEST_SEMANTIC_MIN, limit=MAX_ALTERNATIVES):
                m = self._semantic_match(hit.spec_id, hit.slots, hit.score, norm, contexts, ctx)
                if m is not None:
                    ranked.append(((hit.score - SUGGEST_SEMANTIC_MIN) / (self.semantic.threshold - SUGGEST_SEMANTIC_MIN), m))
        ranked.sort(key=lambda item: -item[0])
        out: list[Match] = []
        shown: set[str] = set()
        for _, m in ranked:
            label = m.outcome.display
            if not label or label in shown or not m.outcome.actions and m.outcome.type_request is None:
                continue
            shown.add(label)
            out.append(m)
            if len(out) == MAX_ALTERNATIVES:
                break
        return tuple(out)

    def _explain(self, norm: str, contexts: Sequence[str], ctx: BuildContext) -> str:
        """A specific reason when a template matched but its builder refused ("Not scrolling")."""
        from hassil import recognize_all

        for context in contexts:
            intents = self.grammar._intents.get(context)
            if intents is None:
                continue
            for result in recognize_all(norm, intents):
                spec = self.grammar.specs.get(result.intent.name)
                if spec is None:
                    continue
                slots = {name: e.value for name, e in result.entities.items()}
                try:
                    outcome = spec.build(slots, ctx)
                except Exception:
                    continue
                if outcome.reject and outcome.reject not in ("Not a web address",):
                    return outcome.reject
        return ""


NAME_SLOTS = ("app_name", "window_name", "element_name")
MAX_UNKNOWN_NAME_WORDS = 4


def _plausible_names(parse: Parse) -> bool:
    """Unknown names are passed on only when they are short, like real app and button names.

    "open the pod bay doors please" is a sentence, not an app.
    """
    for m in parse.matches:
        for slot in NAME_SLOTS:
            value = m.slots.get(slot)
            if value is not None and len(str(value).split()) > MAX_UNKNOWN_NAME_WORDS:
                return False
    return True


def _yields_to_dictation(parse: Parse) -> bool:
    """Free-text commands that read like ordinary sentences give way to typing in a text field."""
    for m in parse.matches:
        if m.spec.id in ("edit.find", "web.search") and m.slots.get("query"):
            return True
        if m.spec.id == "text.format":
            formatter = m.slots.get("formatter", "")
            words = str(m.slots.get("text", "")).split()
            if formatter in PROSE_FORMATTERS and len(words) > PROSE_FORMAT_MAX_WORDS_IN_TEXT_FIELD:
                return True
    return False


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000.0, 3)


__all__ = [
    "Decision",
    "FORMATTERS",
    "Interpreter",
    "OVERLAY_CONTEXTS",
    "RISKY_ACTIONS",
    "RISKY_KEYS",
    "SAFE_WHEN_AWAY",
    "SAFETY_COMMANDS",
    "Situation",
    "is_risky",
    "normalize_combo",
    "safe_when_away",
]
