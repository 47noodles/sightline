"""Personal aliases: what Sightline heard, and the command the user meant.

Learned from corrections (concept review item 16a).  When an utterance is
rejected and, within a few seconds, the user gets the command another way
(picks a "did you mean" choice, or says it again and it is understood),
Sightline offers to remember what it heard.  Saying "yes" adds a line to
``aliases.txt``, next to ``commands.toml``, and from then on that phrase
runs the command.  Open recognisers are weakest on atypical speech, and a
small closed command set with personal aliases copes with that better than
retraining the recogniser.

The file is plain text so a helper can read and fix it: one alias per line,
what was heard, an equals sign, then the command as it is normally said.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .normalize import normalize_command

log = logging.getLogger(__name__)

EXAMPLE = """\
# Things Sightline heard, and the command you meant.
#
# When Sightline does not understand you and you then give the command
# another way, it offers to remember what it heard.  Saying "yes" adds a
# line here, and from then on saying it that way runs the command.
#
# A helper can add, change or delete lines: what was heard, an equals sign,
# then the command as you would normally say it.  Sightline notices changes
# within a few seconds.  Lines that start with # are ignored.  For example:
#
#     scold down = scroll down
#     open note bad = open notepad
"""

MAX_ALIASES = 500  # a guard against a runaway file; real lists are a few dozen lines


def write_example(path: Path) -> bool:
    """Create the file with its explanation if there is none yet.  Returns True if it wrote one."""
    if path.exists():
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(EXAMPLE, "utf-8")
        return True
    except OSError as e:
        log.warning("Could not write %s: %s", path, e)
        return False


def parse_line(line: str) -> tuple[str, str] | None:
    """"scold down = scroll down" -> ("scold down", "scroll down"), both normalised; None for comments and blanks."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    heard, sep, means = text.partition("=")
    if not sep:
        raise ValueError("needs an equals sign between what was heard and the command")
    heard_n, means_n = normalize_command(heard), normalize_command(means)
    if not heard_n or not means_n:
        raise ValueError("needs words on both sides of the equals sign")
    if heard_n == means_n:
        raise ValueError("says the same thing on both sides")
    return heard_n, means_n


def load(path: Path) -> tuple[dict[str, str], list[str]]:
    """The aliases (heard -> command, normalised) and plain-English problems, one per bad line."""
    if not path.exists():
        return {}, []
    try:
        lines = path.read_text("utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        return {}, [f"Could not read your aliases file: {e}"]
    aliases: dict[str, str] = {}
    problems: list[str] = []
    for number, line in enumerate(lines, 1):
        try:
            pair = parse_line(line)
        except ValueError as e:
            problems.append(f"Aliases file, line {number}: {e}")
            continue
        if pair is not None and len(aliases) < MAX_ALIASES:
            aliases[pair[0]] = pair[1]  # a later line wins, so a helper can override by adding a line
    return aliases, problems


def add(path: Path, heard: str, means: str) -> None:
    """Remember an alias.  Replaces an existing line for the same words; keeps everything else as it is."""
    heard_n, means_n = normalize_command(heard), normalize_command(means)
    if not heard_n or not means_n or heard_n == means_n:
        raise ValueError("an alias needs two different phrases")
    try:
        lines = path.read_text("utf-8").splitlines()
    except FileNotFoundError:
        lines = EXAMPLE.splitlines()
    out: list[str] = []
    for line in lines:
        try:
            pair = parse_line(line)
        except ValueError:
            pair = None  # keep a helper's broken line as it is; it is reported when the file loads
        if pair is not None and pair[0] == heard_n:
            continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    out.append(f"{heard_n} = {means_n}")
    _write(path, "\n".join(out) + "\n")


def _write(path: Path, text: str) -> None:
    """Write through a temporary file, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, path)
