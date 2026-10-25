"""What Sightline learns about one person: aliases, the pause inside commands, the vocabulary (no hardware, no models)."""

from __future__ import annotations

import types
from pathlib import Path

import numpy as np
import pytest

from sightline.speech import aliases, custom
from sightline.speech import pauses as P
from sightline.speech.asr import SherpaOfflineEngine, bpe_vocab_for, hotword_variants
from sightline.speech.interpret import Situation
from sightline.speech.pipeline import EndpointConfig
from sightline.speech.service import endpoint_ms

# ---------------------------------------------------------------------- aliases.txt


def test_alias_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "aliases.txt"
    assert aliases.write_example(path)
    assert not aliases.write_example(path)  # never overwrites
    assert aliases.load(path) == ({}, [])
    aliases.add(path, "Scold down.", "scroll down")
    aliases.add(path, "skull town", "Scroll down")
    assert aliases.load(path) == ({"scold down": "scroll down", "skull town": "scroll down"}, [])
    aliases.add(path, "scold down", "scroll up")  # the same words again: replaced, not repeated
    text = path.read_text("utf-8")
    assert text.startswith("# Things Sightline heard")
    assert [line for line in text.splitlines() if line and not line.startswith("#")] == ["skull town = scroll down", "scold down = scroll up"]
    assert aliases.load(path)[0]["scold down"] == "scroll up"


def test_alias_problems_name_the_line(tmp_path: Path) -> None:
    path = tmp_path / "aliases.txt"
    path.write_text("# a helper's note\nscold down = scroll down\nno equals sign\n = copy\nsame = Same.\n", "utf-8")
    found, problems = aliases.load(path)
    assert found == {"scold down": "scroll down"}
    assert [p.split(":")[0] for p in problems] == ["Aliases file, line 3", "Aliases file, line 4", "Aliases file, line 5"]
    aliases.add(path, "new one", "copy")
    text = path.read_text("utf-8")
    assert "no equals sign" in text and "# a helper's note" in text  # their lines stay for them to fix
    with pytest.raises(ValueError):
        aliases.add(path, "copy", "Copy.")


def test_a_missing_alias_file_is_created_with_its_explanation(tmp_path: Path) -> None:
    path = tmp_path / "aliases.txt"
    aliases.add(path, "scold down", "scroll down")
    assert path.read_text("utf-8").startswith("# Things Sightline heard")
    assert aliases.load(path)[0] == {"scold down": "scroll down"}


# ---------------------------------------------------------------------- vocabulary.txt


@pytest.mark.parametrize(
    ("text", "entry"),
    [
        ("Siobhan.", "Siobhan"),
        ("  Royal  Prince Alfred ", "Royal Prince Alfred"),
        ("AC/DC", "AC DC"),  # "/" separates hotwords
        ("", None),
        ("...", None),
        ("one two three four five six seven", None),
        ("x" * 61, None),
        # Review S-16: the recogniser reads ":" as a boost and "@" as a tag; only letters, digits,
        # apostrophes, hyphens and spaces are kept.
        ("foo :99", "foo 99"),
        ("name@tag", "name tag"),
        ("Kubernetes:5.0/AC", "Kubernetes 5 0 AC"),
        ("O'Brien", "O'Brien"),
        ("O\u2019Neill", "O\u2019Neill"),
        ("Jean-Luc", "Jean-Luc"),
        ("Zo\u00eb", "Zo\u00eb"),
        ("Dr. Smith", "Dr Smith"),
        ("- -", None),
        ("::@@", None),
    ],
)
def test_vocabulary_entries(text: str, entry: str | None) -> None:
    assert custom.vocabulary_entry(text) == entry


def test_vocabulary_file(tmp_path: Path) -> None:
    path = tmp_path / "vocabulary.txt"
    assert custom.write_vocabulary_example(path)
    assert custom.load_vocabulary(path) == ([], [])
    assert custom.add_to_vocabulary(path, "Siobhan")
    assert not custom.add_to_vocabulary(path, "siobhan")  # already there, in any case
    assert custom.add_to_vocabulary(path, "Kubernetes.")
    assert custom.load_vocabulary(path) == (["Siobhan", "Kubernetes"], [])
    assert path.read_text("utf-8").startswith("# Words and names")
    with pytest.raises(ValueError):
        custom.add_to_vocabulary(path, "")
    assert custom.parse_vocabulary("# note\nNiamh\n\nniamh\n  Tadhg  \n") == ["Niamh", "Tadhg"]


# ---------------------------------------------------------------------- the pause inside commands


def test_nothing_is_learned_from_a_few_commands() -> None:
    m = P.PauseModel()
    for _ in range(P.MIN_COMMANDS - 1):
        m.add_command([600.0])
    assert m.learned_ms(250) is None
    m.add_command([600.0])
    assert m.learned_ms(250) == 720  # a fifth longer than the long pauses


def test_fluent_speakers_keep_their_endpoint() -> None:
    m = P.PauseModel()
    for _ in range(50):
        m.add_command([40.0, 64.0])  # the gaps between words: too short to matter
    assert m.samples == [] and m.learned_ms(250) == 250
    for _ in range(P.MIN_SAMPLES - 1):
        m.add_command([180.0])
    assert m.learned_ms(250) == 250  # too few long pauses to change anything


def test_long_pauses_set_the_wait_between_a_floor_and_a_ceiling() -> None:
    m = P.PauseModel()
    for i in range(40):
        m.add_command([300.0 + 10 * i])
    expected = 10 * round(P.quantile(sorted(m.samples), P.QUANTILE) * P.MARGIN / 10)
    assert 600 < expected < 900 and m.learned_ms(250) == expected
    assert m.learned_ms(950) == 950  # never less than the user's own command pause
    for _ in range(40):
        m.add_command([2400.0])
    assert m.learned_ms(250) == P.CEILING_MS


def test_recent_pauses_count_most() -> None:
    m = P.PauseModel()
    for _ in range(P.MAX_SAMPLES):
        m.add_command([900.0])
    for _ in range(P.MAX_SAMPLES):
        m.add_command([200.0])
    assert m.samples == [200.0] * P.MAX_SAMPLES and m.learned_ms(250) == 250


def test_a_pause_that_cut_a_command_in_two_counts() -> None:
    m = P.PauseModel(commands=P.MIN_COMMANDS)
    for _ in range(6):
        m.add_split(800.0)
    m.add_split(9000.0)  # that long apart, they were two utterances
    m.add_split(20.0)
    assert m.samples == [800.0] * 6 and m.learned_ms(250) == 960


def test_pauses_file(tmp_path: Path) -> None:
    path = tmp_path / "speech" / "pauses.json"
    m = P.PauseModel([500.0, 700.0], commands=30)
    m.dirty = True
    m.save(path)
    again = P.PauseModel.load(path)
    assert again.samples == [500.0, 700.0] and again.commands == 30
    assert not m.dirty and not again.dirty
    path.write_text("{broken", "utf-8")
    assert P.PauseModel.load(path).commands == 0
    assert P.PauseModel.load(tmp_path / "missing.json").commands == 0
    assert P.PauseModel.from_json({"version": 99, "commands": 5}).commands == 0
    assert P.PauseModel.from_json({"version": 1, "commands": "many"}).commands == 0


@pytest.mark.parametrize(
    "text",
    [
        '{"version": 1, "commands": 1e999, "pauses_ms": []}',
        '{"version": 1, "commands": Infinity, "pauses_ms": []}',
        '{"version": 1, "commands": NaN, "pauses_ms": []}',
        '{"version": 1, "commands": 30, "pauses_ms": [1e999, NaN]}',
        "[" * 100_000 + "]" * 100_000,
    ],
    ids=["1e999", "infinity", "nan", "bad-samples", "deep"],
)
def test_a_damaged_pauses_file_never_stops_speech_starting(tmp_path: Path, text: str) -> None:
    # Review S-06: these raised OverflowError or RecursionError inside SpeechService.__init__.
    path = tmp_path / "speech" / "pauses.json"
    path.parent.mkdir(parents=True)
    path.write_text(text, "utf-8")
    m = P.PauseModel.load(path)
    assert m.commands == 0 and m.samples == []
    assert (tmp_path / "speech" / "pauses.broken.json").exists() and not path.exists()


def test_a_huge_pauses_file_is_not_read(tmp_path: Path) -> None:
    path = tmp_path / "pauses.json"
    path.write_text('{"version": 1, "commands": 30, "pauses_ms": [' + ", ".join(["500.0"] * 60_000) + "]}", "utf-8")
    assert P.PauseModel.load(path).commands == 0
    assert P.PauseModel.from_json({"version": 1, "commands": float("inf")}).commands == 0


@pytest.mark.parametrize(
    ("learned", "enabled", "words"),
    [
        (None, True, "Not learned yet"),
        (250, True, "No longer than the pause that ends a command"),
        (820, True, "0.82 seconds"),
        (1000, True, "1 second"),
        (1500, True, "1.5 seconds"),
        (820, False, "Not learning"),
    ],
)
def test_the_learned_pause_in_words(learned: int | None, enabled: bool, words: str) -> None:
    assert P.describe(learned, 250, enabled=enabled) == words


# ---------------------------------------------------------------------- endpointing with the learned pause

PLAIN = EndpointConfig(command_ms=250, dictation_ms=650, early_ms=125)
LEARNED = EndpointConfig(command_ms=250, dictation_ms=650, early_ms=125, incomplete_ms=900)


@pytest.mark.parametrize(
    ("text", "plain", "learned"),
    [
        ("scroll down", 125, 125),  # a whole command: never waits longer
        ("tap", 125, 125),
        ("practice", 125, 125),
        ("click", 250, 250),  # a name may follow, but it is a command already
        ("reading mode", 250, 250),  # "off" often follows
        ("calibrate", 250, 250),  # so does "head"
        ("scroll", 250, 900),  # not a command yet: the rest is coming
        ("switch to", 650, 900),
        ("select all and", 650, 900),
        ("type", 650, 900),
        ("hello there", 250, 250),  # not a command at all
    ],
)
def test_only_unfinished_commands_wait_longer(interp, text: str, plain: int, learned: int) -> None:
    situation = Situation(mode="auto")
    assert endpoint_ms(text, interp, situation, PLAIN) == plain
    assert endpoint_ms(text, interp, situation, LEARNED) == learned


def test_sleep_ignores_the_learned_pause(interp) -> None:
    assert endpoint_ms("wake", interp, Situation(mode="sleep"), LEARNED) == 250


# ---------------------------------------------------------------------- the recogniser's hotwords


def test_hotword_variants() -> None:
    assert hotword_variants(["siobhan", "Kubernetes", " tired  eyes ", "AC/DC", "", "Siobhan"]) == [
        "siobhan",
        "Siobhan",
        "Kubernetes",
        "tired eyes",
        "Tired Eyes",
        "AC DC",
    ]


def test_hotwords_never_carry_the_recognisers_own_syntax() -> None:
    # Review S-16: "/" ends an entry, ":" sets a boost and "@" a tag; the file is written by hand.
    words = hotword_variants(["foo :99", "x/y@z", "b" * 70, "O'Brien"])
    assert words == ["foo 99", "Foo 99", "x y z", "X Y Z", "O'Brien"]
    assert not any(c in w for w in words for c in ":/@")


def test_bpe_vocabulary_from_tokens(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens.txt"
    tokens.write_text("<unk> 0\n▁t 1\n▁th 2\n▁ 3\nt 4\n<blk> 5\n", "utf-8")
    path = bpe_vocab_for(tokens, tmp_path / "cache")
    assert path.read_text("utf-8").splitlines() == ["<unk>\t0", "▁t\t-1", "▁th\t-2", "▁\t-3", "t\t-4", "<blk>\t-5"]
    assert bpe_vocab_for(tokens, tmp_path / "cache") == path  # made once per tokens file


class _Recognizer:
    """Records how streams were created; "decodes" to a fixed text."""

    def __init__(self) -> None:
        self.created: list[tuple] = []

    def create_stream(self, *args):
        self.created.append(args)
        return types.SimpleNamespace(accept_waveform=lambda rate, audio: None, result=types.SimpleNamespace(text=" hello "))

    def decode_stream(self, stream) -> None:
        pass


def test_hotwords_only_reach_a_recogniser_built_for_them() -> None:
    audio = np.zeros(1600, np.float32)
    greedy = SherpaOfflineEngine("parakeet", _Recognizer())
    greedy.set_hotwords(["Siobhan"])  # sherpa-onnx 2.0 crashes if a greedy recogniser gets hotwords
    assert greedy.decode(audio) == "hello"
    assert greedy._recognizer.created == [()]
    beam = SherpaOfflineEngine("parakeet", _Recognizer(), biasing=True)
    beam.decode(audio)
    beam.set_hotwords(["siobhan", "Nguyen"])
    beam.decode(audio)
    assert beam._recognizer.created == [(), ("siobhan/Siobhan/Nguyen",)]


class _FussyRecognizer(_Recognizer):
    """Refuses any hotwords, as a recogniser that cannot encode them might."""

    def create_stream(self, *args):
        if args:
            raise RuntimeError("cannot encode hotwords")
        return super().create_stream(*args)


def test_a_vocabulary_the_recogniser_refuses_falls_back_to_none(caplog) -> None:
    engine = SherpaOfflineEngine("parakeet", _FussyRecognizer(), biasing=True)
    engine.set_hotwords(["Siobhan"])
    audio = np.zeros(1600, np.float32)
    assert engine.decode(audio) == "hello"  # recognised without the vocabulary instead of failing
    assert engine.decode(audio) == "hello"
    assert engine._recognizer.created == [(), ()], "not asked again after the first refusal"
    assert "could not be used" in caplog.text
