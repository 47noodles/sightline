"""The user's own files in the settings folder: commands (``commands.toml``) and vocabulary (``vocabulary.txt``).

Both are created with commented examples on first start and re-read when
they change, so a command or word added by a helper (or by voice) works a
few seconds later without restarting Sightline.  Personal aliases live
beside them in ``aliases.txt`` (``aliases.py``).

Limits (review S-07).  Matching runs on every utterance, and a phrase with
many optional parts costs about twice as much for each one more ("[a] [a]
... [a]" with 18 of them took 3 s to reject a sentence), so a phrase may
have at most ``MAX_OPTIONALS`` optional parts and ``MAX_EXPANSIONS`` ways of
being said; the file, the number of commands and the length of a text are
capped too.  No command may take the words of a safety command (stop,
cancel, sleep, wake, undo that, pause, help): ``without_safety_phrases``
leaves it out and says so.  A ``url`` must be a web page or an e-mail link
(``core.urls``).  Each problem comes back in plain words for a notice.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hassil import parse_sentence
from hassil.expression import Alternative, Group, ListReference, Permutation, RuleReference, TextChunk

from sightline.core.types import Action
from sightline.core.urls import web_address

from .grammar import EXPANSION_RULES, BuildContext, CommandSpec, HelpRow, Outcome
from .matcher import _expand
from .normalize import normalize_command

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 256 * 1024
MAX_COMMANDS = 200
MAX_PHRASE_CHARS = 200
MAX_TEXT_CHARS = 2000
MAX_KEYS_CHARS = 200
MAX_OPTIONALS = 8  # "[a]" parts in one phrase: 8 cost about 3 ms per utterance, each one more doubles it
MAX_EXPANSIONS = 500  # ways of saying one phrase, alternatives and optional parts multiplied out

EXAMPLE = """\
# Your own voice commands for Sightline.
#
# Each [[command]] block adds one command.  "phrase" is what you say.  It can
# offer alternatives in round brackets and optional words in square brackets:
#     phrase = "(sign off|sign-off) [please]"
# Then give one of these:
#     keys = "ctrl+shift+n"          keys to press; several: "ctrl+a ctrl+c"
#     text = "Kind regards,\\nSam"     text to type (\\n starts a new line)
#     app  = "Spotify"               an app to open
#     url  = "https://www.bbc.co.uk" a web page to open in the browser
# Your commands take priority over the built-in ones.  Sightline notices
# changes to this file within a few seconds; no restart needed.
#
# Remove the # at the start of the lines below to try the examples.

# [[command]]
# phrase = "sign off"
# text = "Kind regards,\\nSam"

# [[command]]
# phrase = "new incognito window"
# keys = "ctrl+shift+n"

# [[command]]
# phrase = "(open|start) music"
# app = "Spotify"

# [[command]]
# phrase = "check the news"
# url = "https://www.bbc.co.uk/news"
"""

_SYNTAX = re.compile(r"[()\[\]{}<>|;]")


@dataclass(frozen=True, slots=True)
class CustomCommand:
    phrase: str
    keys: str = ""
    text: str = ""
    app: str = ""
    url: str = ""


def write_example(path: Path) -> bool:
    """Create the commented example file if there is no file yet.  Returns True if it wrote one."""
    if path.exists():
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(EXAMPLE, "utf-8")
        return True
    except OSError as e:
        log.warning("Could not write %s: %s", path, e)
        return False


def load(path: Path, *, protected: AbstractSet[str] | None = None) -> tuple[list[CustomCommand], list[str]]:
    """Parse the file.  Returns the commands and plain-English problems (one per bad entry).

    ``protected`` is every way of saying the safety commands (``safety_phrases``, the default uses
    the usual wake and sleep phrases); a command that would take one of them is left out.
    """
    if not path.exists():
        return [], []
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return [], [f"Your commands file is larger than {MAX_FILE_BYTES // 1024} KB, so Sightline did not read it"]
        data = tomllib.loads(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        return [], [f"Could not read your commands file: {e}"]
    except tomllib.TOMLDecodeError as e:
        return [], [f"Your commands file has a mistake: {e}"]
    except RecursionError:
        return [], ["Your commands file has a mistake: it is nested too deeply"]
    entries = data.get("command", [])
    if not isinstance(entries, list):
        return [], ["Your commands file should use [[command]] blocks"]
    commands: list[CustomCommand] = []
    problems: list[str] = []
    for i, entry in enumerate(entries, 1):
        if len(commands) >= MAX_COMMANDS:
            problems.append(f"Only the first {MAX_COMMANDS} commands in your commands file are used")
            break
        if not isinstance(entry, dict):
            problems.append(f"Command {i} is not a [[command]] block")
            continue
        phrase = str(entry.get("phrase", "")).strip()
        if not phrase:
            problems.append(f"Command {i} has no phrase")
            continue
        if len(phrase) > MAX_PHRASE_CHARS:
            problems.append(f"Command {i}: the phrase is longer than {MAX_PHRASE_CHARS} characters")
            continue
        fields = {k: str(entry.get(k, "")) for k in ("keys", "text", "app", "url")}
        given = [k for k, v in fields.items() if v.strip()]
        if len(given) != 1:
            problems.append(f'"{phrase}": give exactly one of keys, text, app or url')
            continue
        problem = _field_problem(fields)
        if problem:
            problems.append(f'"{phrase}": {problem}')
            continue
        template = template_for(phrase)
        try:
            sentence = parse_sentence(template)
        except Exception as e:  # hassil raises its own parse errors
            problems.append(f'"{phrase}": the phrase could not be understood ({e})')
            continue
        optionals, expansions = template_size(sentence)
        if optionals > MAX_OPTIONALS:
            problems.append(f'"{phrase}": the phrase has more than {MAX_OPTIONALS} optional parts in square brackets')
            continue
        if expansions > MAX_EXPANSIONS:
            problems.append(f'"{phrase}": the phrase can be said in more than {MAX_EXPANSIONS} ways; use fewer choices')
            continue
        commands.append(CustomCommand(phrase, **fields))
    commands, shadowing = without_safety_phrases(commands, safety_phrases() if protected is None else protected)
    return commands, shadowing + problems


def _field_problem(fields: Mapping[str, str]) -> str:
    """What is wrong with a command's keys, text or url, in plain words; empty when nothing is."""
    if len(fields["text"]) > MAX_TEXT_CHARS:
        return f"the text is longer than {MAX_TEXT_CHARS} characters"
    if len(fields["keys"]) > MAX_KEYS_CHARS:
        return f"the keys are longer than {MAX_KEYS_CHARS} characters"
    if fields["url"].strip() and web_address(fields["url"]) is None:
        return "the url must be a web page (https://...) or an e-mail link (mailto:...)"
    return ""


# --------------------------------------------------------------------------- the size of a phrase


def _rules() -> dict[str, Any]:
    global _PARSED_RULES
    if _PARSED_RULES is None:
        _PARSED_RULES = {name: parse_sentence(text) for name, text in EXPANSION_RULES.items()}
    return _PARSED_RULES


_PARSED_RULES: dict[str, Any] | None = None


def template_size(sentence: Any) -> tuple[int, int]:
    """(optional parts, ways of saying it) for a parsed phrase template.

    The count stops just past ``MAX_EXPANSIONS``, so a phrase built to be enormous costs no more
    to measure than one that is merely too big.  Lists ({name}) count as one way.
    """
    expression = getattr(sentence, "expression", sentence)
    return _optionals(expression, 0), _expansions(expression, _rules(), 0)


def _optionals(expr: Any, depth: int) -> int:
    if depth > 64 or not isinstance(expr, Group):
        return 0
    own = 1 if isinstance(expr, Alternative) and expr.is_optional else 0
    return own + sum(_optionals(item, depth + 1) for item in expr.items)


def _expansions(expr: Any, rules: Mapping[str, Any], depth: int) -> int:
    cap = MAX_EXPANSIONS + 1
    if depth > 64:
        return cap
    if isinstance(expr, (TextChunk, ListReference)):
        return 1
    if isinstance(expr, RuleReference):
        rule = rules.get(expr.rule_name)
        return _expansions(rule.expression, rules, depth + 1) if rule is not None else 1
    if isinstance(expr, Alternative):
        return min(cap, sum(_expansions(item, rules, depth + 1) for item in expr.items))
    if isinstance(expr, Group):  # a sequence, or a permutation (every order of its parts)
        total = 1
        if isinstance(expr, Permutation):
            for n in range(2, len(expr.items) + 1):
                total = min(cap, total * n)
        for item in expr.items:
            total = min(cap, total * _expansions(item, rules, depth + 1))
        return total
    return 1


# --------------------------------------------------------------------------- safety commands keep their words


def phrase_forms(template: str) -> list[str]:
    """The ways of saying a phrase template, word by word, with each list left as "{name}"."""
    try:
        sentence = parse_sentence(template)
    except Exception:
        return []
    budget = [MAX_EXPANSIONS * 4]
    return sorted({" ".join(seq) for seq in _expand(sentence.expression, _rules(), budget) if seq})


def spoken_forms(specs: Iterable[CommandSpec]) -> set[str]:
    """Every way of saying these commands (lists left out), normalised as speech is."""
    out: set[str] = set()
    for spec in specs:
        for item in spec.sentences:
            template = item[0] if isinstance(item, tuple) else item
            out.update(form for form in phrase_forms(template) if "{" not in form)
    return out


def _form_pattern(form: str) -> re.Pattern[str]:
    words = [".+" if word.startswith("{") else re.escape(word) for word in form.split()]
    return re.compile("^" + " ".join(words) + "$")


@lru_cache(maxsize=8)
def safety_phrases(wake_phrase: str | None = None, sleep_phrase: str | None = None) -> frozenset[str]:
    """Every way of saying the safety commands: stop, cancel, sleep, wake, undo that, pause and resume eyes, help."""
    from .config import SpeechConfig
    from .grammar import build_commands, sleep_and_wake
    from .interpret import SAFETY_COMMANDS  # here, not at the top: interpret needs the semantic model's module

    defaults = SpeechConfig()
    wake = defaults.wake_phrase if wake_phrase is None else wake_phrase
    sleep = defaults.sleep_phrase if sleep_phrase is None else sleep_phrase
    specs = [s for s in build_commands() + sleep_and_wake(wake, sleep) if s.id in SAFETY_COMMANDS]
    return frozenset(spoken_forms(specs))


def without_safety_phrases(commands: list[CustomCommand], protected: AbstractSet[str]) -> tuple[list[CustomCommand], list[str]]:
    """The commands that leave the safety words alone, and a problem for each one that does not."""
    kept: list[CustomCommand] = []
    problems: list[str] = []
    for cmd in commands:
        clash = None
        for form in phrase_forms(template_for(cmd.phrase)):
            if "{" not in form:
                clash = form if form in protected else None
            else:
                pattern = _form_pattern(form)
                clash = next((p for p in sorted(protected) if pattern.match(p)), None)
            if clash is not None:
                break
        if clash is None:
            kept.append(cmd)
        else:
            problems.append(f'"{cmd.phrase}" was left out: "{clash}" is one of Sightline\'s safety commands, and always does what it says')
    return kept, problems


def template_for(phrase: str) -> str:
    """Normalise a phrase the way spoken text is normalised; template syntax is kept as written."""
    if _SYNTAX.search(phrase):
        return phrase.lower().replace("-", " ")
    return normalize_command(phrase) or phrase.lower()


def to_specs(commands: list[CustomCommand]) -> list[CommandSpec]:
    specs: list[CommandSpec] = []
    for i, cmd in enumerate(commands):
        specs.append(
            CommandSpec(
                id=f"custom.{i}",
                category="My commands",
                sentences=(template_for(cmd.phrase),),
                build=_builder(cmd),
                help=(HelpRow(cmd.phrase, _describe(cmd)),),
                priority=10,
            )
        )
    return specs


def _describe(cmd: CustomCommand) -> str:
    if cmd.keys:
        return f"Press {cmd.keys}"
    if cmd.text:
        preview = cmd.text.replace("\n", " ")
        return f'Type "{preview[:40]}{"..." if len(preview) > 40 else ""}"'
    if cmd.app:
        return f"Open {cmd.app}"
    return f"Open {cmd.url}"


def _builder(cmd: CustomCommand):  # noqa: ANN202
    def build(slots: Mapping[str, Any], ctx: BuildContext) -> Outcome:
        if cmd.keys:
            combos = [c for c in re.split(r"[\s,]+", cmd.keys.strip().lower()) if c]
            actions = tuple(Action("key.press", {"combo": c}, source="voice") for c in combos)
            return Outcome(actions, _describe(cmd))
        if cmd.text:
            text = cmd.text.replace("\\n", "\n")
            return Outcome((Action("text.type", {"text": text}, source="voice"),), "Typed: " + text.replace("\n", " ")[:40])
        if cmd.app:
            return Outcome((Action("app.open", {"name": cmd.app}, source="voice"),), f"Open {cmd.app}")
        return Outcome((Action("web.go", {"url": cmd.url}, source="voice"),), f"Go to {cmd.url}")

    return build


# --------------------------------------------------------------------------- vocabulary.txt

VOCABULARY_EXAMPLE = """\
# Words and names for Sightline to listen out for (concept review item 9).
#
# One word or phrase per line, written the way it should be typed:
#     Siobhan
#     Kubernetes
# Say "add that to vocabulary" just after dictating or spelling a word, or
# "add <word> to vocabulary", and Sightline adds a line here.  A helper can
# add, change or delete lines; Sightline notices changes within a few
# seconds.  Lines that start with # are ignored.
"""

MAX_VOCABULARY = 200  # sherpa-onnx takes them all with every utterance; a few dozen is typical
MAX_ENTRY_WORDS = 6
MAX_ENTRY_CHARS = 60
_WORD_MARKS = "'’-"  # kept inside an entry: O'Brien, Jean-Luc


def vocabulary_entry(text: str) -> str | None:
    """A word or short phrase cleaned for the vocabulary ("Siobhan." -> "Siobhan"); None when it is empty or too long.

    Only letters, digits, apostrophes, hyphens and spaces are kept (review S-16): the recogniser reads
    "/" as the end of an entry, ":" as a boost and "@" as a tag, so "foo :99" must not reach it as written.
    """
    kept = "".join(c if c.isalnum() or c in _WORD_MARKS else " " for c in str(text))
    entry = " ".join(kept.split()).strip(" " + _WORD_MARKS)
    if not entry or len(entry) > MAX_ENTRY_CHARS or len(entry.split()) > MAX_ENTRY_WORDS:
        return None
    return entry


def parse_vocabulary(text: str) -> list[str]:
    """Entries from the file's text: one per line, # comments skipped, repeats (in any case) dropped."""
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entry = vocabulary_entry(stripped)
        if entry is None or entry.lower() in seen:
            continue
        seen.add(entry.lower())
        out.append(entry)
        if len(out) >= MAX_VOCABULARY:
            break
    return out


def write_vocabulary_example(path: Path) -> bool:
    """Create the commented vocabulary file if there is none yet.  Returns True if it wrote one."""
    if path.exists():
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(VOCABULARY_EXAMPLE, "utf-8")
        return True
    except OSError as e:
        log.warning("Could not write %s: %s", path, e)
        return False


def load_vocabulary(path: Path) -> tuple[list[str], list[str]]:
    """The entries and plain-English problems."""
    if not path.exists():
        return [], []
    try:
        return parse_vocabulary(path.read_text("utf-8")), []
    except (OSError, UnicodeDecodeError) as e:
        return [], [f"Could not read your vocabulary file: {e}"]


def add_to_vocabulary(path: Path, entry: str) -> bool:
    """Append an entry.  False when it is already there (in any case); raises ValueError when it cannot be an entry."""
    clean = vocabulary_entry(entry)
    if clean is None:
        raise ValueError("not a word or short phrase")
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        text = VOCABULARY_EXAMPLE
    existing = parse_vocabulary(text)
    if clean.lower() in {e.lower() for e in existing}:
        return False
    if len(existing) >= MAX_VOCABULARY:
        raise ValueError(f"the vocabulary already has {MAX_VOCABULARY} entries")
    if text and not text.endswith("\n"):
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text + clean + "\n", "utf-8")
    os.replace(tmp, path)
    return True
