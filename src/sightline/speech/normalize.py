"""Turning raw recogniser output into text the command grammar can match.

The command path wants lowercase words, digits for numbers and no
punctuation; dictation keeps the original text and formats it separately
(``formatting.py``).  The same ``normalize_command`` is applied to dynamic
names (apps, windows, buttons) so both sides of a comparison agree.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from text_to_num import alpha2digit

from .vocab import FILLER_WORDS

# Annotations some recognisers emit for non-speech: "[BLANK_AUDIO]", "(coughs)", "<unk>", "*music*".
_ANNOTATION = re.compile(r"\[[^\]]*\]|\([^)]*\)|<[^>]*>|\*[^*]*\*")
# Separators that join words: ctrl+c, alt-tab, re-center, x-ray.
_JOINERS = re.compile(r"(?<=\w)[+\-_](?=\w)")
_SPACES = re.compile(r"\s+")
# Punctuation stripped from the edges of each token.  Dots, slashes, colons and
# @ inside a token survive ("google.com", "3.5", "a@b.com").
_EDGE_PUNCT = "\"'`.,!?;:()[]{}<>\u2026\u2014\u2013-"
_INNER_KEEP = re.compile(r"[^\w.'/:@#%&$]+")

# Whole-phrase fixes for words recognisers often get wrong in commands.  Applied
# after lowercasing, before numbers are converted.  Kept small and specific: a
# wrong rewrite would change what the user said.
_PHRASE_FIXES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), repl)
    for pattern, repl in (
        (r"\b(?:write|rite|wright|ride) click\b", "right click"),
        (r"\bright clique\b", "right click"),
        (r"\bdouble clique\b", "double click"),
        (r"\bre cent(?:er|re)\b", "recenter"),
        (r"\brecentre\b", "recenter"),
        (r"\bscrawl\b", "scroll"),
        (r"\bscroll(?:ed|s)\b", "scroll"),
        (r"\bclicked\b", "click"),
        (r"\bok\b", "okay"),
        (r"\bnew tap\b", "new tab"),
        (r"\bclose tap\b", "close tab"),
        (r"\bnext tap\b", "next tab"),
        (r"\bdictation mod\b", "dictation mode"),
        (r"\bcommand mod\b", "command mode"),
        # Misheard command words seen in the bake-off (tools/speech_bench.py) that are not
        # ordinary sentences, so fixing them cannot turn dictation into a command.
        (r"\bi'?ll (?:tab|tap)\b", "alt tab"),
        (r"\baltab\b", "alt tab"),
        (r"\bundue\b", "undo"),
        (r"\bre ?due\b|\bredu\b", "redo"),
        (r"\b(?:hold|whole) (?:ship|chip)\b|\bwhole shift\b", "hold shift"),
        (r"\brelease (?:ship|chip)\b", "release shift"),
        (r"\bre ?cent(?:ere|re)d\b|\bresentered\b", "recenter"),
        (r"\b(pause|resume) ice\b", r"\1 eyes"),
        (r"\b(?:notch|nutch|nej|nudged)\b(?= (?:the )?(?:mouse |pointer )?(?:up|down|left|right)\b)", "nudge"),
        (r"\bword ?write\b", "word right"),
        (r"\b(?:side ?lines?|sight line|site line)\b", "sightline"),
    )
)

# Words people (and recognisers) put before or after a command: "let's scroll down", "save please".
_LEAD_IN = frozenset({"its", "it's", "lets", "let's", "just", "please", "so", "now", "and", "then"})
_TAIL = frozenset({"please", "now", "thanks", "thank"})


def clean_transcript(text: str) -> str:
    """Remove recogniser annotations and odd whitespace, keeping case and punctuation."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'")
    text = _ANNOTATION.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


@lru_cache(maxsize=4096)
def normalize_command(text: str) -> str:
    """Lowercase words, digits for numbers, no punctuation.  Cached: the same names are normalised often."""
    text = clean_transcript(text).lower()
    if not text:
        return ""
    text = _JOINERS.sub(" ", text)
    tokens: list[str] = []
    for raw in text.split():
        token = raw.strip(_EDGE_PUNCT)
        token = _INNER_KEEP.sub("", token)
        token = token.strip(_EDGE_PUNCT)
        if token and token not in FILLER_WORDS:
            tokens.append(token)
    text = " ".join(tokens)
    for pattern, repl in _PHRASE_FIXES:
        text = pattern.sub(repl, text)
    if any(ch.isalpha() for ch in text):
        try:
            text = alpha2digit(text, "en", threshold=0)
        except Exception:  # text2num raises on a few odd inputs; leave the words alone
            pass
    return _SPACES.sub(" ", text).strip()


def tokens(text: str) -> list[str]:
    return text.split()


def strip_lead_in(normalized: str) -> str:
    """The command inside "let's scroll down please"; the text unchanged when there is nothing to strip."""
    words = normalized.split()
    while len(words) > 1 and words[0] in _LEAD_IN:
        words = words[1:]
    while len(words) > 1 and words[-1] in _TAIL:
        words = words[:-1]
        if words and words[-1] == "thank":
            words = words[:-1]
    return " ".join(words)


_CAMEL_SPLIT = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")
_NAME_NOISE = re.compile(r"\((?:x86|x64|64-bit|32-bit|preview|beta)\)|[®™©]", re.IGNORECASE)
_VERSIONISH = re.compile(r"\b\d+(?:\.\d+){1,3}\b")


def spoken_forms(name: str) -> list[str]:
    """Ways someone might say an app, window or button name, already normalised.

    "OneNote" -> ["onenote", "1 note"]; "Visual Studio Code" -> ["visual studio code"];
    "Microsoft Edge" -> ["microsoft edge", "edge"].  The caller decides how to use
    shorter forms (they are listed after the full one).
    """
    if not name:
        return []
    base = _NAME_NOISE.sub(" ", name)
    base = _VERSIONISH.sub(" ", base)
    base = base.replace("&", " and ")
    forms: list[str] = []

    def add(value: str) -> None:
        norm = normalize_command(value)
        if norm and norm not in forms:
            forms.append(norm)

    add(base)
    split = _CAMEL_SPLIT.sub(" ", base)
    if split != base:
        add(split)
    # Drop a leading vendor word: "Microsoft Edge" -> "edge", "Google Chrome" -> "chrome".
    words = normalize_command(split).split()
    if len(words) >= 2 and words[0] in _VENDOR_WORDS:
        add(" ".join(words[1:]))
    return forms


_VENDOR_WORDS = {
    "microsoft",
    "google",
    "mozilla",
    "adobe",
    "apple",
    "windows",
}

# Short recogniser outputs that are usually noise (a cough, a breath, a click) rather than words.
NOISE_TRANSCRIPTS = {
    "",
    "you",
    "thank you",
    "thanks",
    "bye",
    "the",
    "a",
    "i",
    "oh",
    "uh",
    "um",
    "hmm",
    "mm",
    "huh",
    "ha",
    "yeah",
    "so",
    "and",
}


def is_probably_noise(normalized: str, audio_ms: float | None) -> bool:
    """True for empty text, or for a typical noise transcript from a very short sound."""
    if not normalized:
        return True
    if not any(ch.isalnum() for ch in normalized):
        return True
    if audio_ms is not None and audio_ms < 450 and normalized in NOISE_TRANSCRIPTS:
        return True
    return False
