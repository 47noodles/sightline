"""Matching normalised utterances against the command grammar.

Three passes, most precise first:

1. Exact: hassil matches the whole utterance against the templates of the
   active contexts.  Several commands said in one breath ("select all
   copy") are split into a chain when the whole does not match.
2. Fuzzy: RapidFuzz compares the utterance with every expanded template
   phrase, to absorb small recognition slips ("scroll dawn").
3. Semantic (``semantic.py``): paraphrases.

``prefix`` tells the endpointer whether a partial transcript is already a
complete command that nothing longer could extend.
"""

from __future__ import annotations

import itertools
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from hassil import Intents, recognize_all
from hassil.expression import Alternative, Group, ListReference, Permutation, RuleReference, Sentence, TextChunk
from rapidfuzz import fuzz, process

from .grammar import EXPANSION_RULES, STATIC_LISTS, BuildContext, CommandSpec, Outcome
from .vocab import OPPOSITE_WORDS

log = logging.getLogger(__name__)

WILDCARD_LISTS = frozenset(name for name, spec in STATIC_LISTS.items() if spec.get("wildcard"))
NUMBER_LISTS = frozenset({"count", "cell", "choice"})
# Small lists that are spelled out when building fuzzy phrases.
_EXPAND_FOR_FUZZY = frozenset({"direction", "vertical", "place", "side", "click_type", "bare_key", "preset", "menu_item"})

MAX_CHAIN_TOKENS = 12
# Words people put between chained commands: "select all and copy", "copy then switch to notepad".
CONNECTORS = frozenset({"and", "then"})
MAX_CHAIN_SEGMENTS = 4


@dataclass(frozen=True, slots=True)
class Match:
    spec: CommandSpec
    slots: dict[str, Any]
    outcome: Outcome
    method: str  # "exact", "fuzzy" or "semantic"
    confidence: float
    text: str  # the words this match covers
    wildcard_chars: int = 0
    literal_chars: int = 0


@dataclass(frozen=True, slots=True)
class Parse:
    """One or more commands covering a whole utterance."""

    matches: tuple[Match, ...]
    method: str
    confidence: float

    @property
    def free_text(self) -> bool:
        return any(m.spec.free_text for m in self.matches)


@dataclass(frozen=True, slots=True)
class PrefixInfo:
    complete: bool  # the text is a whole command already
    name_follows: bool  # a longer command adds a name here ("click" -> "click save"); people pause before names
    free_text: bool  # the text is (or continues into) words that are typed or searched for
    possible: bool  # the text could still become a command
    complete_fixed: bool = False  # complete without free words at the end ("select all", not "select quick")


# --------------------------------------------------------------------------- template expansion


def _expand(expr: Any, rules: dict[str, Sentence], budget: list[int]) -> list[list[str]]:
    """All token sequences of an expression, slots as "{list}" tokens.  ``budget`` caps the work."""
    if budget[0] <= 0:
        return [[]]
    if isinstance(expr, TextChunk):
        return [expr.text.split()] if expr.text.strip() else [[]]
    if isinstance(expr, ListReference):
        return [["{" + expr.list_name + "}"]]
    if isinstance(expr, RuleReference):
        rule = rules.get(expr.rule_name)
        return _expand(rule.expression, rules, budget) if rule else [[]]
    if isinstance(expr, Alternative):
        out: list[list[str]] = []
        for item in expr.items:
            out.extend(_expand(item, rules, budget))
        if expr.is_optional and [] not in out:
            out.append([])
        return out
    if isinstance(expr, (Permutation, Group)):  # Sequence, and permutations treated as written
        out = [[]]
        for item in expr.items:
            options = _expand(item, rules, budget)
            out = [a + b for a, b in itertools.product(out, options)]
            budget[0] -= len(out)
            if budget[0] <= 0:
                break
        return out
    return [[]]


def _dedupe(seqs: Iterable[list[str]]) -> list[tuple[str, ...]]:
    seen: set[tuple[str, ...]] = set()
    out: list[tuple[str, ...]] = []
    for seq in seqs:
        t = tuple(seq)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _list_values(name: str) -> list[tuple[str, ...]]:
    spec = STATIC_LISTS.get(name, {})
    values = []
    for v in spec.get("values", []):
        text = v["in"] if isinstance(v, dict) else v
        values.append(tuple(str(text).split()))
    return values


# --------------------------------------------------------------------------- grammar


class Grammar:
    """Compiled templates for every context, plus the indexes used for chains, fuzzy matching and endpointing."""

    def __init__(self, specs: Sequence[CommandSpec]) -> None:
        self.specs: dict[str, CommandSpec] = {}
        for spec in specs:
            if spec.id in self.specs:
                raise ValueError(f"duplicate command id {spec.id}")
            self.specs[spec.id] = spec
        contexts = sorted({c for s in specs for c in s.contexts})
        self._intents: dict[str, Intents] = {}
        self._skeletons: dict[str, list[tuple[tuple[str, ...], str]]] = {}
        self._fixed: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
        self._fuzzy: dict[str, tuple[list[str], list[str]]] = {}
        self._first: dict[str, set[str]] = {}
        self._list_values = {name: _list_values(name) for name in STATIC_LISTS if name not in WILDCARD_LISTS}
        for context in contexts:
            members = [s for s in specs if context in s.contexts]
            self._intents[context] = Intents.from_dict(_intents_dict(members))
            self._build_indexes(context, members)

    # ------------------------------------------------------------------ building

    def _build_indexes(self, context: str, members: list[CommandSpec]) -> None:
        intents = self._intents[context]
        rules = intents.expansion_rules
        skeletons: list[tuple[tuple[str, ...], str]] = []
        for spec in members:
            intent = intents.intents[spec.id]
            for data in intent.data:
                for sentence in data.sentences:
                    budget = [2000]
                    for seq in _dedupe(_expand(sentence.expression, rules, budget)):
                        skeletons.append((seq, spec.id))
                        if data.slots:
                            self._fixed[(spec.id, seq)] = {k: v.value if hasattr(v, "value") else v for k, v in data.slots.items()}
        self._skeletons[context] = skeletons

        first: set[str] = set()
        for seq, _ in skeletons:
            tok = seq[0]
            if tok.startswith("{"):
                name = tok[1:-1]
                if name in NUMBER_LISTS:
                    first.add("#")
                elif name in WILDCARD_LISTS:
                    first.add("*")
                else:
                    first.update(v[0] for v in self._list_values.get(name, []) if v)
            else:
                first.add(tok)
        self._first[context] = first

        phrases: list[str] = []
        owners: list[str] = []
        seen: set[tuple[str, str]] = set()
        for seq, spec_id in skeletons:
            spec = self.specs[spec_id]
            if not spec.fuzzy or spec.exact_only:
                continue
            for phrase in self._fuzzy_phrases(seq):
                key = (phrase, spec_id)
                if key not in seen:
                    seen.add(key)
                    phrases.append(phrase)
                    owners.append(spec_id)
        self._fuzzy[context] = (phrases, owners)

    def _fuzzy_phrases(self, seq: tuple[str, ...]) -> list[str]:
        options: list[list[str]] = []
        for tok in seq:
            if not tok.startswith("{"):
                options.append([tok])
                continue
            name = tok[1:-1]
            if name in NUMBER_LISTS:
                options.append(["#"])
            elif name in _EXPAND_FOR_FUZZY:
                options.append([" ".join(v) for v in self._list_values.get(name, [])])
            else:
                return []  # free text, keys and modifiers are not fuzzy-matched
        if not options:
            return []
        out = []
        for combo in itertools.product(*options):
            out.append(" ".join(combo))
            if len(out) > 200:
                break
        return out

    def contexts(self) -> list[str]:
        return list(self._intents)

    def canonical_phrases(self, spec_id: str, limit: int = 6) -> list[tuple[str, dict[str, Any]]]:
        """Short example phrases for a command with the slots they imply (small lists spelled out)."""
        out: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()
        for context in ("global", *self._skeletons):
            for seq, sid in self._skeletons.get(context, []):
                if sid != spec_id:
                    continue
                fixed = self._fixed.get((sid, seq), {})
                options: list[list[tuple[str, dict[str, Any]]]] = []
                ok = True
                for tok in seq:
                    if not tok.startswith("{"):
                        options.append([(tok, {})])
                        continue
                    name = tok[1:-1]
                    if name not in _EXPAND_FOR_FUZZY or name == "bare_key":
                        ok = False
                        break
                    values = STATIC_LISTS[name]["values"]
                    options.append([((v["in"] if isinstance(v, dict) else v), {name: (v["out"] if isinstance(v, dict) else v)}) for v in values])
                if not ok:
                    continue
                for combo in itertools.product(*options):
                    phrase = " ".join(t for t, _ in combo)
                    if phrase in seen:
                        continue
                    seen.add(phrase)
                    slots = dict(fixed)
                    for _, sl in combo:
                        slots.update(sl)
                    out.append((phrase, slots))
        out.sort(key=lambda item: len(item[0]))
        # Keep variety across slot values (scroll up and scroll down) before trimming.
        picked: list[tuple[str, dict[str, Any]]] = []
        per_slots: dict[str, int] = {}
        for phrase, slots in out:
            key = repr(sorted(slots.items()))
            if per_slots.get(key, 0) >= max(1, limit // max(1, len({repr(sorted(s.items())) for _, s in out}))) + 1:
                continue
            per_slots[key] = per_slots.get(key, 0) + 1
            picked.append((phrase, slots))
            if len(picked) >= limit:
                break
        return picked

    # ------------------------------------------------------------------ exact

    def exact(self, text: str, context: str, ctx: BuildContext) -> list[Match]:
        """Every builder-approved whole-utterance match in one context, best first."""
        intents = self._intents.get(context)
        if intents is None or not text:
            return []
        found: dict[tuple[str, str], Match] = {}
        for result in recognize_all(text, intents):
            spec = self.specs.get(result.intent.name)
            if spec is None:
                continue
            slots = {name: entity.value for name, entity in result.entities.items()}
            wildcard_chars = sum(len(e.text or "") for e in result.entities.values() if e.is_wildcard)
            try:
                outcome = spec.build(slots, ctx)
            except Exception:
                log.exception("Builder for %s failed on %d characters of speech", spec.id, len(text))
                continue
            if outcome.reject:
                continue
            key = (spec.id, repr(sorted((k, repr(v)) for k, v in slots.items())))
            match = Match(spec, slots, outcome, "exact", outcome.confidence, text, wildcard_chars, result.text_chunks_matched)
            if key not in found or _rank(match) > _rank(found[key]):
                found[key] = match
        return sorted(found.values(), key=_rank, reverse=True)

    def can_start(self, token: str, context: str) -> bool:
        first = self._first.get(context, set())
        return token in first or "*" in first or (token.isdigit() and "#" in first)

    # ------------------------------------------------------------------ whole utterance, with chains

    def parse(self, text: str, contexts: Sequence[str], ctx: BuildContext, *, allow_chain: bool = True) -> Parse | None:
        """Best exact parse across contexts (earlier contexts win ties): one command, or a chain."""
        whole: Match | None = None
        for context in contexts:
            matches = self.exact(text, context, ctx)
            if matches:
                whole = matches[0]
                break
        if whole is not None and whole.confidence >= 0.99:
            if allow_chain and _free_chars(whole):
                # "select all and copy" is two commands, not "select" of the words "all and copy".
                chain = self._chain(text.split(), contexts, ctx)
                if chain is not None and chain.confidence >= 0.99 and sum(_free_chars(m) for m in chain.matches) < _free_chars(whole):
                    return chain
            return Parse((whole,), "exact", whole.confidence)
        chain = self._chain(text.split(), contexts, ctx) if allow_chain else None
        if chain is not None and (whole is None or chain.confidence > whole.confidence):
            return chain
        if whole is not None:
            return Parse((whole,), "exact", whole.confidence)
        return None

    def _chain(self, tokens: list[str], contexts: Sequence[str], ctx: BuildContext) -> Parse | None:
        n = len(tokens)
        if n < 2 or n > MAX_CHAIN_TOKENS:
            return None
        cache: dict[tuple[int, int], Match | None] = {}

        def segment(i: int, j: int) -> Match | None:
            key = (i, j)
            if key not in cache:
                best: Match | None = None
                if any(self.can_start(tokens[i], c) for c in contexts):
                    words = " ".join(tokens[i:j])
                    for context in contexts:
                        found = self.exact(words, context, ctx)
                        if found:
                            best = found[0]
                            break
                cache[key] = best
            return cache[key]

        # best[i]: (segments, min confidence, matches, characters swallowed by wildcards) for tokens[i:]
        best: dict[int, tuple[int, float, tuple[Match, ...], int] | None] = {n: (0, 1.0, (), 0)}
        for i in range(n - 1, -1, -1):
            choice: tuple[int, float, tuple[Match, ...], int] | None = None
            if i > 0 and tokens[i] in CONNECTORS:
                # Skipped between two commands; never at the start or the end.
                after = best.get(i + 1)
                best[i] = after if after is not None and after[0] > 0 else None
                continue
            for j in range(n, i, -1):
                rest = best.get(j)
                if rest is None:
                    continue
                m = segment(i, j)
                if m is None:
                    continue
                if j < n and not m.spec.chainable:
                    continue  # names and free text swallow everything after them, so only last
                segs = rest[0] + 1
                if segs > MAX_CHAIN_SEGMENTS:
                    continue
                cand = (segs, min(rest[1], m.confidence), (m,) + rest[2], rest[3] + _free_chars(m))
                # Most confident, then fewest words taken as free text, then fewest pieces.
                if choice is None or (cand[1], -cand[3], -cand[0]) > (choice[1], -choice[3], -choice[0]):
                    choice = cand
            best[i] = choice
        result = best.get(0)
        if result is None or result[0] < 2:
            return None
        return Parse(result[2], "chain", result[1])

    # ------------------------------------------------------------------ fuzzy

    def fuzzy(self, text: str, contexts: Sequence[str], ctx: BuildContext, *, min_score: float = 86.0, margin: float = 6.0) -> Parse | None:
        """Closest template phrase by edit similarity, then an exact parse of that phrase."""
        tokens = text.split()
        if not tokens or len(text) < 3 or len(tokens) > 8:
            return None
        numbers = [t for t in tokens if t.isdigit()]
        query = " ".join("#" if t.isdigit() else t for t in tokens)
        for context in contexts:
            phrases, owners = self._fuzzy.get(context, ([], []))
            if not phrases:
                continue
            hits = process.extract(query, phrases, scorer=fuzz.ratio, limit=12, score_cutoff=min_score - margin - 10)
            if not hits:
                continue
            best_phrase, best_score, best_idx = hits[0]
            best_owner = owners[best_idx]
            rivals = [(s, p) for p, s, i in hits[1:] if owners[i] != best_owner]
            runner = max((s for s, _ in rivals), default=0.0)
            needed = margin
            if any(_opposites(best_phrase, p) for s, p in rivals if best_score - s < OPPOSITE_MARGIN):
                needed = OPPOSITE_MARGIN  # "hot keyboard": show or hide?  Too close to call.
            if best_score < min_score or best_score - runner < needed:
                continue
            if best_phrase.count("#") != len(numbers):
                continue
            it = iter(numbers)
            corrected = " ".join(next(it) if tok == "#" else tok for tok in best_phrase.split())
            for m in self.exact(corrected, context, ctx):
                if m.spec.id == best_owner:
                    conf = round(best_score / 100.0 * m.confidence, 3)
                    fm = Match(m.spec, m.slots, m.outcome, "fuzzy", conf, corrected, m.wildcard_chars, m.literal_chars)
                    return Parse((fm,), "fuzzy", conf)
        return None

    def near_misses(self, text: str, contexts: Sequence[str], ctx: BuildContext, *, min_score: float = 72.0, limit: int = 3) -> list[tuple[float, Match]]:
        """Commands that sound close to ``text`` but not close enough to run: "did you mean" candidates.

        Returns (similarity 0..100, match) pairs, best first, one per command.
        """
        tokens = text.split()
        if not tokens or len(text) < 3 or len(tokens) > 8:
            return []
        numbers = [t for t in tokens if t.isdigit()]
        query = " ".join("#" if t.isdigit() else t for t in tokens)
        found: list[tuple[float, Match]] = []
        seen: set[str] = set()
        for context in contexts:
            phrases, owners = self._fuzzy.get(context, ([], []))
            if not phrases:
                continue
            for phrase, score, idx in process.extract(query, phrases, scorer=fuzz.ratio, limit=24, score_cutoff=min_score):
                owner = owners[idx]
                if owner in seen or phrase.count("#") != len(numbers):
                    continue
                it = iter(numbers)
                corrected = " ".join(next(it) if tok == "#" else tok for tok in phrase.split())
                for m in self.exact(corrected, context, ctx):
                    if m.spec.id == owner and not m.outcome.reject and m.confidence >= 0.99:
                        seen.add(owner)
                        found.append((float(score), Match(m.spec, m.slots, m.outcome, "fuzzy", round(score / 100.0, 3), corrected, m.wildcard_chars, m.literal_chars)))
                        break
        found.sort(key=lambda item: -item[0])
        return found[:limit]

    # ------------------------------------------------------------------ prefixes, for endpointing

    def prefix(self, text: str, contexts: Sequence[str]) -> PrefixInfo:
        tokens = text.split()
        if not tokens:
            return PrefixInfo(False, False, False, True)
        complete = name_follows = free_text = possible = complete_fixed = False
        for context in contexts:
            for seq, spec_id in self._skeletons.get(context, []):
                spec = self.specs[spec_id]
                for pos, in_wild in self._walk(seq, tokens):
                    possible = True
                    if in_wild and spec.free_text:
                        free_text = True
                    if pos == len(seq):
                        complete = True
                        complete_fixed = complete_fixed or not in_wild
                        if spec.wait_after:
                            name_follows = True
                        continue
                    nxt = seq[pos]
                    if nxt.startswith("{") and nxt[1:-1] in WILDCARD_LISTS:
                        if spec.free_text:
                            free_text = True
                        else:
                            name_follows = True
        return PrefixInfo(complete, name_follows, free_text, possible, complete_fixed)

    def _walk(self, seq: tuple[str, ...], tokens: list[str]) -> Iterable[tuple[int, bool]]:
        """Positions in ``seq`` reachable after consuming all of ``tokens`` (and whether a wildcard is still open)."""
        n = len(tokens)
        results: list[tuple[int, bool]] = []

        def go(si: int, ti: int) -> None:
            if len(results) > 8:
                return
            if ti == n:
                results.append((si, False))
                return
            if si == len(seq):
                return
            tok = seq[si]
            if not tok.startswith("{"):
                if tokens[ti] == tok:
                    go(si + 1, ti + 1)
                return
            name = tok[1:-1]
            if name in NUMBER_LISTS:
                if tokens[ti].isdigit() or tokens[ti] in ("to", "too", "for", "won", "ate"):
                    go(si + 1, ti + 1)
                return
            if name in WILDCARD_LISTS:
                results.append((si + 1, True))  # the wildcard can take everything that is left
                for k in range(ti + 1, n):
                    go(si + 1, k)
                return
            for value in self._list_values.get(name, []):
                k = len(value)
                if tuple(tokens[ti : ti + k]) == value:
                    go(si + 1, ti + k)
                elif n - ti < k and value[: n - ti] == tuple(tokens[ti:]):
                    results.append((si, False))  # partway through a multi-word value

        if seq and (seq[0].startswith("{") or seq[0] == tokens[0]):
            go(0, 0)
        return results


OPPOSITE_MARGIN = 12.0


def _opposites(a: str, b: str) -> bool:
    """True when two phrases differ only by one word and those words are opposites (show, hide)."""
    wa, wb = a.split(), b.split()
    if len(wa) != len(wb):
        return False
    diff = [(x, y) for x, y in zip(wa, wb) if x != y]
    return len(diff) == 1 and frozenset(diff[0]) in OPPOSITE_WORDS


def _free_chars(m: Match) -> int:
    """Characters taken as free text (typed, searched or selected words); key names and app names do not count."""
    return m.wildcard_chars if m.spec.free_text else 0


def _rank(m: Match) -> tuple[bool, int, int, float, int, int]:
    # Resolved before unresolved, user commands first, then fewer words swallowed by wildcards,
    # then more literal text matched.
    # Last, fewer slots: "alt f 4" is alt+F4, not alt+F four times.
    return (round(m.confidence, 3) >= 0.99, m.spec.priority, -m.wildcard_chars, m.confidence, m.literal_chars, -len(m.slots))


def _intents_dict(specs: Sequence[CommandSpec]) -> dict[str, Any]:
    intents: dict[str, Any] = {}
    for spec in specs:
        groups: dict[tuple[tuple[str, Any], ...], list[str]] = {}
        for item in spec.sentences:
            template, fixed = (item if isinstance(item, tuple) else (item, {}))
            key = tuple(sorted(fixed.items()))
            groups.setdefault(key, []).append(template)
        data = []
        for key, sentences in groups.items():
            block: dict[str, Any] = {"sentences": sentences}
            if key:
                block["slots"] = dict(key)
            data.append(block)
        intents[spec.id] = {"data": data}
    return {"language": "en", "intents": intents, "lists": STATIC_LISTS, "expansion_rules": EXPANSION_RULES}


_WORD = re.compile(r"\S+")
