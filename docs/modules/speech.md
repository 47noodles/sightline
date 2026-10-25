# Speech subsystem

Microphone to recogniser to commands or typing.  Entry point:
`sightline.speech.create(ctx)` returns a `SpeechService` (`SpeechAPI` in
`core/interfaces.py`).  It publishes `MicLevel`, `TranscriptPartial`,
`TranscriptFinal`, `IntentResult` and `SpeechStatus`, registers
`speech.mode`, `speech.scratch`, `speech.listen` and `speech.choose`, and
dispatches the voice actions of ARCHITECTURE.md section 7.

## Files

| File | What it does |
|---|---|
| `service.py` | `SpeechService`: threads, model loading, arbitration hand-off, dictation placement, "did you mean", the attention gate, scratch, speech actions, alias offers, pause learning, the vocabulary; `endpoint_ms` (adaptive endpointing) |
| `audio.py` | `Microphone` (sounddevice, 16 kHz mono, resampling, stable device ids, watchdog reopen), `FileSource` (WAV or arrays through the same path), `BlockQueue`, `Resampler` |
| `vad.py` | Silero VAD v6 (ONNX, 32 ms frames, 0.2 ms each) and an energy fallback |
| `pipeline.py` | `UtterancePipeline`: pre-roll, VAD hysteresis, speculative decode at the start of a pause, adaptive endpoint, onset times, the pauses inside each utterance |
| `asr.py` | Recogniser wrappers: sherpa-onnx offline (Parakeet TDT, Moonshine), sherpa-onnx online, moonshine-voice streaming (bench only); key-term biasing (hotwords) |
| `aliases.py` | `aliases.txt`: what Sightline heard and the command the user meant |
| `pauses.py` | `PauseModel`: the pauses one person makes inside commands, and the wait it sets for an unfinished command |
| `models.py` | Pinned model files (URL, SHA-256, size, licence) and the recognisers offered in Settings |
| `grammar.py` | Every built-in command as hassil templates with builders, lists, help rows; key sequences, URLs |
| `matcher.py` | `Grammar`: exact parsing, chains, fuzzy matching, near misses, prefix analysis for endpointing |
| `interpret.py` | `Interpreter`: the arbitration rules (section 8), risky actions, suggestions |
| `normalize.py` | Recogniser text to matchable text: case, punctuation, fillers, numbers (text2num), known mishearings |
| `formatting.py` | Dictation: spoken punctuation, capitals, spacing, Talon formatters, spelling |
| `semantic.py` | Paraphrase matching with all-MiniLM-L6-v2 (int8 ONNX) and a polarity guard |
| `names.py` | Spoken names of apps, windows and on-screen elements (rapidfuzz) |
| `custom.py` | `commands.toml`: the user's own commands; `vocabulary.txt`: the user's words for the recogniser |
| `vocab.py` | Alphabets, keys, symbols, punctuation words, formatters, opposites |
| `config.py` | Speech settings |

Tools: `tools/speech_testset.py` renders the synthetic test set (SAPI, WAV
files only, never played), `tools/speech_bench.py` prints the bake-off,
latency, matcher and paraphrase tables while holding the `cpu` lock;
`--biasing`, `--vocabulary`, `--hotwords-score` and `--names` measure the
user's vocabulary.

## Threads and data flow

```
PortAudio callback -> BlockQueue -> sightline-speech: VAD -> UtterancePipeline -> recogniser
                                                   -> interpreter -> ActionRegistry.dispatch
sightline-speech-bg: paraphrase model, names refresh, the user's files, recogniser swaps, pauses.json
```

* The capture callback only resamples to 16 kHz and queues 20 ms blocks;
  it never blocks.  The queue drops the oldest audio if the speech thread
  stalls for 10 s.
* `sightline-speech` runs VAD per 32 ms frame, feeds the recogniser, decides
  the endpoint and handles each finished utterance under one lock, so
  utterances never interleave.
* `sightline-speech-bg` refreshes on-screen element names every 1.5 s and on
  `FocusChanged`, windows every 4 s, apps on `lists_changed()`, re-reads
  `commands.toml`, `aliases.txt` and `vocabulary.txt` within 2 s of a
  change, loads the paraphrase model, swaps the recogniser when the setting
  changes (or rebuilds it when biasing is turned on or off), writes the
  learned pauses at most every 10 s and shows the learned wait in the
  settings.
* `feed_text` runs the same interpretation and dispatch in the caller's
  thread (the typed command box, tests).
* `start()` loads models and opens the microphone; app.py calls it on a
  background thread.  `stop()` closes the device and joins the threads.
  Mute (`speech.listen`, the tray, the dock) closes the device, so the
  microphone light goes off.

Utterance ids come from one counter for spoken and typed utterances, so
captions can order them.

## Recognisers: the bake-off

All candidates ran on this machine (i5-12400, 6 cores) through the same
feeding code as the live pipeline.  "Model time after pause" is the time to
the final text once the audio up to a 250 ms pause has been fed, for 1-2 s
commands, without the speculative decode the live pipeline adds.  "Commands
understood" runs the recogniser's text through the real grammar and checks
the resulting actions against the reference text's actions (after the
mishearing fixes below; the bake-off log in `out/` has the earlier figures).

Test material: 177 command phrases (167 global, 10 overlay), each said by
SAPI David and Zira at rates -2 to +3 (354 clean clips) plus pink-noise and
babble versions at 12-18 dB SNR (177 each); 50 dictation sentences the same
way (200 clips); 80 LibriSpeech test-clean utterances and 250 Speech
Commands words (both CC-BY-4.0, used only for measurement and never
shipped).

| Recogniser | Model time after pause, median / p90 | Real-time factor | Command WER clean / noisy | Commands understood (wrong) | Clean commands understood | Dictation WER clean / noisy | LibriSpeech WER | Speech Commands words |
|---|---|---|---|---|---|---|---|---|
| **Parakeet TDT 0.6B v2 int8** (sherpa-onnx) | 156 / 211 ms | 0.088 | 5.5% / 19.4% | **88.1%** (2.0%) | **94.6%** | 0.2% / 2.0% | **2.6%** | 72.4% |
| Parakeet TDT-CTC 110M int8 (sherpa-onnx) | 49 / 73 ms | 0.029 | 10.5% / 21.6% | 83.2% (1.8%) | 90.4% | 0.4% / 1.7% | 3.6% | 70.4% |
| Moonshine Base (sherpa-onnx) | 35 / 53 ms | 0.023 | 8.7% / 43.5% | 80.2% (3.7%) | | 0.9% / 1.7% | 9.1% | 60.0% |
| Moonshine Tiny (sherpa-onnx) | 21 / 32 ms | 0.013 | 17.7% / 55.7% | 69.1% (4.0%) | | 0.7% / 3.2% | 7.4% | 66.8% |
| Moonshine Tiny streaming (moonshine-voice) | 141 / 317 ms | 0.326 | 8.5% / 87.6% | 66.2% (3.8%) | | 2.3% / 8.4% | 9.8% | 68.8% |
| Moonshine Small streaming (moonshine-voice) | 414 / 747 ms | 0.788 | 4.9% / 57.2% | 80.9% (1.9%) | | 2.3% / 1.9% | 6.5% | 70.0% |

Moonshine rows are from before the mishearing fixes (they gain a few points
too).  The streaming rows used 200 random clips.  Also tried and dropped
before the full run: moonshine-voice Medium streaming (real-time factor 1.27
here), NVIDIA Nemotron streaming (too slow on CPU), Kroko streaming
(truncated words, unclear licence).

**Choice.**  The rule is the best accuracy with a model share under 200 ms
(ARCHITECTURE.md section 10).  Parakeet 0.6B is the most accurate everywhere that matters (half
the clean-command errors of the 110M model, best on human speech) at a
156 ms median.  The live pipeline starts decoding 64 ms into the pause, so
most of that is hidden behind the endpoint wait.  Settings offers:

| Setting value | Label | Notes |
|---|---|---|
| `auto` | Recommended | Parakeet 0.6B if decoding a 1.5 s command takes under `AUTO_BUDGET_MS` (200 ms) on this computer, measured at start-up; otherwise Parakeet 110M |
| `parakeet-0.6b` | Most accurate (Parakeet 0.6B) | 482 MB download |
| `parakeet-110m` | Faster (Parakeet 110M) | 108 MB |
| `moonshine-base` | Fastest, less accurate (Moonshine Base) | 111 MB, optional download |

Both Parakeet models are `required`, so set-up downloads 590 MB of
recognisers.  If a chosen model is missing the service uses the next one
installed and says so in a toast; with none installed `SpeechStatus` says
"Speech models are not downloaded yet" and typed commands still work.

Model files (all fetched by `ModelManager`, pinned by SHA-256; nothing
downloads at runtime):

| Model | Licence | Size |
|---|---|---|
| Silero VAD v6.2.3 | MIT | 2.3 MB |
| Parakeet TDT 0.6B v2 int8 (sherpa-onnx export) | CC-BY-4.0 | 482 MB |
| Parakeet TDT-CTC 110M int8 (sherpa-onnx export) | CC-BY-4.0 | 108 MB |
| Moonshine Base int8 (sherpa-onnx export) | MIT | 111 MB |
| all-MiniLM-L6-v2 quint8 ONNX (commit 1110a24) | Apache-2.0 | 23.5 MB |

### Key-term biasing (the user's vocabulary)

The words in `vocabulary.txt` go to Parakeet as hotwords: during beam
search sherpa-onnx adds `HOTWORDS_SCORE` to each token of a hotword (`asr.py`).
The Parakeet exports ship only `tokens.txt`, so `bpe_vocab_for` writes a
sentencepiece-style vocabulary from it once, in the data folder, for
sherpa-onnx to split words into tokens.  Each entry goes in as written and
capitalised.  Greedy search cannot use hotwords (and sherpa-onnx 2.0
crashes when a greedy recogniser is given them), so a recogniser is built
for beam search only while the vocabulary has words; adding the first word
or removing the last rebuilds it in the background.  Moonshine takes no
hotwords.

`tools/speech_bench.py --names` adds 56 sentences with 15 names these
models do not know (`out/speech_names`, SAPI David and Zira, WAV files
only): each name as spelled, and respelled so SAPI says the real name
("Shivawn" for Siobhan).  Same clips and machine as the bake-off above, one
session:

| Recogniser | Model time after pause, median / p90 | Commands understood (wrong) | Command WER clean / noisy | Dictation WER clean / noisy | Speech Commands words | Names found, spelled / sounded |
|---|---|---|---|---|---|---|
| Parakeet 0.6B, greedy (no vocabulary) | 154 / 207 ms | 88.1% (2.0%) | 5.5% / 19.4% | 0.2% / 2.0% | 72.4% | 12 of 30 / 13 of 26 |
| Parakeet 0.6B, beam search | 166 / 223 ms | 88.6% (1.4%) | 5.2% / 16.8% | 0.2% / 0.8% | 65.2% | 12 of 30 / 13 of 26 |
| **Parakeet 0.6B, beam search + 15 names at 0.5** | 164 / 228 ms | **88.7% (1.6%)** | 5.1% / 16.2% | 0.2% / 0.8% | 67.6% | **18 of 30 / 17 of 26** |
| Parakeet 0.6B, beam search + 15 names at 2.0 | 155 / 221 ms | 82.1% (1.6%) | 13.9% / 26.7% | 0.5% / 1.2% | 48.8% | |
| Parakeet 110M, greedy (no vocabulary) | 51 / 74 ms | 83.2% (1.8%) | 10.5% / 21.6% | 0.4% / 1.7% | 70.4% | 8 of 30 / 5 of 26 |
| Parakeet 110M, beam search | 57 / 77 ms | 83.5% (1.3%) | 9.7% / 20.9% | 0.8% / 1.5% | 71.2% | 8 of 30 / 6 of 26 |
| **Parakeet 110M, beam search + 15 names at 0.5** | 59 / 80 ms | **83.5% (1.6%)** | 10.2% / 20.4% | 0.8% / 1.5% | 72.4% | **12 of 30 / 6 of 26** |
| Parakeet 110M, beam search + 15 names at 2.0 | 51 / 66 ms | 83.2% (2.1%) | 11.2% / 21.0% | 0.8% / 1.4% | 72.4% | |

The 2.0 rows are from an earlier run of the same bake-off.  On 200
command clips and the 56 name sentences the 0.6B went from 11.4% command
WER and 25 names (no vocabulary) to 11.4% and 35 at 0.5, and 21.8% and 45
at 2.0: the bigger boost turns commands into names.

**Choice.**  `HOTWORDS_SCORE` is 0.5: a third of the missed names come
through on the 0.6B (25 to 35 of 56) and 5 more on the 110M (13 to 18),
and commands understood do not drop on either model.  Beam search costs about 10 ms of model time on the 0.6B and
6 ms on the 110M, and only for people with a vocabulary.  Through the live
pipeline (`--pipeline --vocabulary`, 40 commands) end of speech to decision
was 222 / 290 ms with the names against 225 / 292 ms without, in the same
session.  Beam search also loses some isolated Speech Commands words on the
0.6B (72.4% to 65-68%), the weak spot already noted.

One more finding, not used: sherpa-onnx also takes a score per word
(`Siobhan :2`), and with the recogniser-wide score at 0.5 that found 21 of
30 / 20 of 26 names at 88.8% commands understood.  How the two scores
combine is not documented, so the plain 0.5 stays until that is understood
and tried with other vocabularies.

## Latency

End of speech to action, for short commands:

1. **Speculative decode.**  64 ms into a pause the pipeline decodes what it
   has.  If the speaker stays quiet until the endpoint, that text is the
   final text (offline recognisers get no new audio after it), so the
   recogniser costs nothing after the endpoint.
2. **Adaptive endpoint** (`endpoint_ms` in `service.py`), from the
   speculative text:
   * a complete command that nothing can extend ("tap", "copy", "scroll
     down"): `early_ms`, half the command pause (125 ms by default);
   * a complete command a name or more words may follow ("click", "find",
     "select all"): `command_endpoint_ms` (250 ms);
   * free text, a trailing "and", or dictation in a text field:
     `dictation_endpoint_ms` (650 ms);
   * the start of a command that is not finished yet ("scroll", "switch
     to"): the command pause (the dictation pause when a name follows), and
     never less than this person's learned wait (below).
3. Grammar matching takes 1-3 ms; paraphrase matching about 2 ms.

### The pause inside commands, per person

Some people pause in the middle of a command ("switch to ... notepad").  A
fixed endpoint cuts such a command in two, and the halves then fail or mean
something else ("scroll" ... "down" presses the Down key).  The pipeline
reports the gaps inside each utterance (`Utterance.pauses_ms`, in whole
32 ms frames, as the endpoint counts them), and `pauses.py` keeps them per
profile in `profiles\<name>\speech\pauses.json`:

* every spoken command adds the gaps inside it (gaps under 96 ms are left
  out);
* a command cut in two adds the gap between the halves: the first half was
  rejected as unfinished, the rest came within 2.5 s, and together they
  parse;
* after 20 spoken commands, the wait is the 95th percentile of the last 400
  gaps times 1.2, rounded to 10 ms, never below `command_endpoint_ms` and
  never above 1.5 s.  With fewer than 5 gaps it stays at the command pause.

The wait is used only while the prefix analysis says the words so far are
not a whole command, so "scroll down" and "tap" end as quickly as before;
only "scroll" waits longer for its "down".  Dictation, typed commands, talk
that was ignored and sleep teach nothing.  The value shows read-only in the
settings as "Your pauses inside commands" (`learned_command_pause`), and
`learn_pauses` (advanced) turns learning off.

Measured in real time through VAD, endpointing and recogniser (`tools/speech_bench.py --pipeline`,
40 clean command clips, end of speech to decision):

| Recogniser | Median | p90 |
|---|---|---|
| Parakeet 110M | 181 ms | 291 ms |
| Parakeet 0.6B | 201 ms | 285 ms |

Round 2 changed none of the endpoint decisions in this run (every clip
waited the same pause): back to back, Parakeet 0.6B took 211 / 287 ms
before and 212 / 288 ms after.

Through the whole service (`test_spoken_commands_end_to_end`: SAPI clips
paced like a microphone, Parakeet 0.6B chosen by "Recommended", grammar and
dispatch included) eleven commands took 158 to 286 ms, median 217 ms; the
slowest was a bare "click", which waits the full command pause in case a
name follows (say "tap" for the fast one).  `IntentResult.latency_ms`
reports end of speech to dispatch for every spoken utterance.

## Grammar

Commands are hassil templates in `grammar.py` (`build_commands`), one
`CommandSpec` per command: templates, a pure builder that turns slots into
an `Outcome` (actions, caption, optional text to type, state changes),
help rows, contexts and flags.  Templates use hassil syntax:
`(a|b)` alternatives, `[optional]`, `{list}` and `{list:slot}`, `<rule>`
expansion rules (`<times>`, `<it>`, `<window>`), and fixed slots
(`("scroll {direction} a bit", {"small": True})`).

* **Coverage.**  Every voice action in ARCHITECTURE.md section 7 has
  commands, except `mouse.drag_toggle` (from the eyes and the dock; voice
  has "drag" and "drop") and `window.place` (internal): 186 command
  definitions in twelve help categories, plus the user's own.  `test_speech_reaches_every_voice_action` checks them and
  warns about catalogue actions added since.
* **Contexts.**  `global`; `dictation` (escape commands only); `sleep` (the
  wake phrase); overlay contexts while `state.active_overlay` is set:
  `labels` and `grid` (bare numbers, "click 12"), `wizard` for setup and
  calibration ("next", "back", "skip"), `help`, `settings`, `chooser`,
  `menu` and `confirm` (below); `choices` while "did you mean" suggestions
  are up; and `offer` while an offer to remember an alias is up ("yes").
* **Newer catalogue actions.**  Anywhere: "practice" or "practice room"
  (`ui.practice.show`); "calibrate", "calibrate eyes", "calibrate gaze",
  "calibrate head" (`ui.calibrate` with that kind); "fix pointer" (`kind=drift`);
  "reading mode", "reading mode off", "stop reading mode"
  (`pointer.reading_scroll`); "menu", "show menu", "open menu", "action
  menu" (`ui.menu.show`, aimed at the speech onset like every spoken
  action); "close menu", "hide menu" (`ui.menu.hide`).
  * Settings window: "next" or "next setting", "back" or "previous",
    "more", "up" or "faster", "less", "down" or "slower" ("more 3 times"
    steps three), "set to 1.5 seconds" (`value` is a number when one was
    said, otherwise the words), and the presets "steady hands", "tremor",
    "tired eyes", "defaults".
  * Number chooser: 1 to 6, "number 2", "choose 2", "pick 2"; "double click
    2" adds `count=2`, "right click 2" and "middle click 2" add `button`;
    "none", "none of them", "cancel" run `ui.chooser.cancel`.
  * Action menu: 1 to 8, and the item words, sent as said in `item`:
    "double click", "right click", "context menu", "middle click", "drag",
    "drag from here", "drop", "drop here", "scroll", "scroll here", "zoom",
    "zoom here", "show numbers", "keyboard", "show keyboard", and for its
    scroll controls "stop scrolling", "up", "scroll up", "down", "scroll
    down".  The menu matches the words to its tiles.
  * Confirm countdown: "cancel", "no", "don't", "wait" run `ui.cancel` and
    nothing else is taken; "stop" and "never mind" cancel through their
    usual commands.

  Overlay words win over global commands only while their overlay is open;
  elsewhere "up" presses the Up key and "right click" right-clicks where
  you look.  The chooser takes no number above 6 and the menu none above 8.
* **Numbers.**  text2num turns spoken numbers into digits; homophones count
  where a number is expected ("tab for times", "scroll down to").
* **Chains.**  "select all and copy", "copy then switch to notepad": up to
  four commands in one utterance; commands that take a name or free text
  only come last.  A free-text command never swallows a chain ("select all
  and copy" is two commands, not a selection of the words "all and copy").
* **Repeats.**  "again", "repeat three times", "do that again".
* **Names.**  Apps (Start menu), windows (titles and process names) and
  on-screen element names are refreshed in the background and matched with
  rapidfuzz.  An unknown short name after an explicit verb ("open zoom") is
  passed to the desktop, which says if it cannot find it; sentences are
  never taken as names.
* **Dictation commands.**  "type ..." always types; "spell ..." takes plain
  letters, NATO and the Talon alphabet; formatters ("snake user name",
  "camel", "kebab", "constant", "title", "say") follow Talon.
* **Text by content** (text fields only): "select quick brown", "select from
  quick to lazy", "move after brown".  "undo" is the app's Ctrl+Z; "undo
  that" is `action.undo`.

### Matching an utterance

1. Normalise: lower case, recogniser punctuation and fillers removed,
   joiners split ("Alt-Tab"), numbers to digits, known mishearings fixed
   ("I'll tab", "undue", "write click", "Re-centered", "sideline").
2. Exact template match, or a chain of exact matches.
3. Outside text fields, the same after dropping lead-in and tail words
   ("let's scroll down please").
4. Fuzzy: the closest template phrase by rapidfuzz ratio, at least 86 (92 in
   a text field) and 6 points clear of any other command, 12 when the other
   command differs only by an opposite word ("hot keyboard": show or hide?).
5. Paraphrase (2 to 10 words, not in text fields): all-MiniLM-L6-v2 against
   seed phrases and canonical template phrases.  A match needs cosine 0.60
   and a 0.04 margin over any other command, or the same command with other
   slot values; a polarity guard drops candidates whose direction words
   contradict the query.
6. Otherwise: typed (text field), or rejected with up to three suggestions.

Paraphrase quality through the whole interpreter on 56 held-out
paraphrases and 50 everyday sentences (`tests/speech/test_models.py`):

| | Result |
|---|---|
| Precision of commands that ran | 97.6% (40 of 41) |
| Recall (ran, or offered for confirmation when risky) | 82.1% (46 of 56) |
| Everyday sentences that ran a command | 0 of 50 |
| Time per query | 2.7 ms |

The index on its own (`tools/speech_bench.py --semantic`, 384 phrases, 48
paraphrases that exact and fuzzy matching miss, 100 sentences that are not
commands) chose the threshold:

| Threshold | Margin | Precision | Recall | Non-commands accepted |
|---|---|---|---|---|
| 0.55 | 0.04 | 95.0% | 79.2% | 1 of 100 |
| **0.60** | **0.04** | **97.4%** | **79.2%** | **0 of 100** |
| 0.60 | 0.02 | 97.7% | 87.5% | 0 of 100 |
| 0.65 | 0.04 | 97.2% | 72.9% | 0 of 100 |

The 0.02 margin looks better on this small set; 0.04 stays because a near
miss now becomes a "did you mean" suggestion rather than a lost command.
Exact and fuzzy matching take 0.5 ms median, 2.2 ms p90, 4.1 ms worst over
273 test utterances.  The 21 command definitions added in round 2 cost
about 0.15 ms per utterance: back to back on a busier machine, 0.94 to
1.09 ms median and 4.4 to 5.1 ms p90.

## Arbitration (ARCHITECTURE.md section 8)

| Mode | Text field focused | Result |
|---|---|---|
| auto | no | exact, chain, fuzzy or paraphrase command; else "Not understood" with suggestions |
| auto | yes | exact or near-exact command (fuzzy 92) runs; "find ..." and long prose formatting type instead; everything else is typed |
| command | either | commands only; "type ..." still types |
| dictation | either | everything is typed except "stop dictation", "command mode", "scratch that", "go to sleep" |
| sleep | either | only the wake phrase; captions and partials are hidden |

**Attention gate.**  When an utterance ends the service asks
`ctx.vision.attention(voice onset, end of speech)`.  With
`attention_gate` on (default), face below 0.5 or facing below 0.5 means the
speech was probably not for the computer: only the safety commands run, and
only when said exactly (`SAFE_WHEN_AWAY` in `interpret.py`: sleep and wake,
stop, cancel, pause and resume eye control, "undo that", help).  Nothing is
typed, nothing else runs, paraphrases and aliases are off; the caption
reads "Ignored: you were looking away".  Before the security review (S-01)
every exact command ran while looking away, so a voice on a call or in a
video could say "open command prompt".  With `mouth_gate` on (default
off), a measured mouth movement below 0.2 does the same.  No vision, no
method, `None` or zero frames means no gate: voice-only users need it.  Both
gates are `protected` settings: voice can turn them on but not off.  An
echo check (holding speech that overlaps what the PC itself is playing,
through WASAPI loopback) would tell the loudspeaker from the owner; it is
left for later.

**Aim.**  Every action from a spoken utterance, chains and typed text
included, carries `aim_t`: the perf_counter time of the start of the audio
kept for that utterance, pre-roll included, so it is never late.

**Did you mean.**  A rejected utterance carries up to three suggestions in
`IntentResult.alternatives`, best first: near template phrases (ratio 72
and up) and close paraphrases (cosine 0.48 and up), one per command.  They
stay for 8 s.  "one", "two", "three" (or "1" to "3", "first", "number two",
"option 3") or `speech.choose index=n` runs one, aimed where the original
utterance was.  A real command or new dictation withdraws them; talk that
was ignored or another miss without suggestions leaves them up.

**Personal aliases** (`aliases.py`).  When a spoken utterance is not
understood and within 8 s the user gets the command another way (picks a
suggestion by voice, blink or dwell, or says it again and it runs), the
result offers one choice: "Say yes to remember 'scold down' as 'scroll
down'".  "yes" (or "one", or the chip) adds `scold down = scroll down` to
`aliases.txt` in the settings folder, a plain text file a helper can edit;
it is re-read within 2 s of a change and problems come back as a toast
naming the line.  Aliases are tried before any other matching, outside text
fields and dictation, never for speech the attention gate ignored, and only
where their command works.  A helper's alias may redirect a global phrase,
but never what the open overlay or the choices on screen take ("next" in
the settings window).  Nothing is offered for typed misses, password
fields, sleep, misses of more than five words, noise, or the start of a
real command cut short by a pause ("scroll" ... "scroll down"); nor when
the command is hard to undo, takes free text, or is help, cancel, stop,
undo, repeat or a speech mode change; nor with `learn_aliases` off.  An
alias a helper wrote for something hard to undo asks "Say 1 to confirm"
first.  Another command withdraws the offer, and a "yes" while looking away
is ignored.  An open overlay's numbers come before the offer's "one": the
corrected command may have been a click that opened the number chooser.

**Risky actions.**  What is hard to undo is decided by the shared policy in
`core/risk.py` (ARCHITECTURE.md section 8), the same one the countdown
uses: closing windows (`window.close`, `app.quit`), key combos that close,
delete, send or quit however they are spelled (alt+F4, ctrl+W,
ctrl+shift+W, ctrl+F4, Delete, shift+Delete, ctrl+shift+Delete,
ctrl+Enter, alt+S, ctrl+Q, Windows+R, ctrl+shift+Escape, Windows+X, with
extra keys still counted), Enter or a typed line into a command window,
opening a shell or system tool, and clicking a control named Delete, Send,
Pay and the like.  Commands marked `risky` (close tab, delete word or line,
scratch, microphone off) count too.  None of these runs from a paraphrase
or an alias: it becomes the only suggestion, "Say 1 to confirm: Close
window".  Exact commands run as usual (and the countdown then holds them).

**Privacy.**  In a password field nothing heard is shown or logged:
partials are dropped, `TranscriptFinal` and `IntentResult` carry empty
text, and anything typed or pressed there shows as bullets ("Typed
••••••••", "Pressed ••••••"): dictation, spelled keys ("press h u n t e r")
and snippets from `commands.toml` alike.  Those actions carry no utterance
and the `private` marker, so `Action.describe()` writes them as
`text.type(<private>)`.  Shortcuts that type nothing (ctrl+A, Enter) keep
their plain caption.

**The log.**  What was heard is never written at INFO.  At DEBUG each
utterance is logged as its kind, method, length and latency
("Speech: command (exact, 11 chars, 180 ms)"); the words and what they
became are written only when a developer turns on `general.log_speech`
(hidden; with DEBUG logging), and never from a password field.  Merged
commands, chosen suggestions, aliases and vocabulary words are logged as
counts.

## Dictation

`dictation_text` drops the recogniser's own punctuation (setting
`auto_punctuation` keeps it), turns punctuation words into marks ("comma",
"full stop", "new line", "open quote"), and numbers of ten and above into
digits.  `place_text` adds the leading space and capitals the text needs
after what is left of the caret: `uia.text_before_caret()` when the field
can be read, otherwise the service's own record of what it typed in that
field (also used while our previous phrase is still being typed).  A new
line starts a sentence.  Spacing and capitalisation rules and formatters
are adapted from talonhub/community (MIT).

"Scratch that" removes the last phrase (dictation, "type ...", or a
snippet from `commands.toml`) with backspaces when the focus and caret have
not moved since (checked against the field text when it can be
read), otherwise it sends Ctrl+Z.  `speech.mode`, `speech.listen` and
`speech.scratch` register undo functions for `action.undo`.

**Vocabulary.**  "add that to vocabulary" adds the last phrase typed by
voice (dictation, "type ...", "spell ..."); "add Kubernetes to my
vocabulary" adds the words as the recogniser wrote them.  Both go to
`vocabulary.txt` beside `commands.toml`: one word or phrase per line, up to
200 entries of up to six words and 60 characters each, `#` for comments.  A
helper can edit it; it is re-read within 2 s.  The list goes to the
recogniser as hotwords (key-term biasing, above).  Only letters, digits,
apostrophes, hyphens and spaces are kept ("foo :99" becomes "foo 99"):
sherpa-onnx reads "/" as the end of an entry, ":" as a boost and "@" as a
tag (review S-16), and `asr.hotword_variants` filters again.  If the
recogniser still refuses the hotwords, that utterance and the next are
recognised without them and a warning is logged.

## Adding commands

**Built in.**  Add a `_c(...)` entry to `build_commands()` in `grammar.py`:

```python
_c(
    "edit.save",                      # unique id
    E,                                # help category
    ["save <it>", "save [the] (file|document|changes)"],
    _seq("Save", "ctrl+s"),           # builder: caption and key combos
    [("save", "Save")],               # help rows: phrase, description, example
    semantic=("store my changes",),   # optional paraphrase seeds
)
```

Builders are pure: they read slots and the `BuildContext` and return an
`Outcome`; state changes go in `Outcome.state`.  Use `risky=True` for
anything hard to reverse, `wait_after=True` when a longer phrase often
follows, `free_text=True` for wildcards that capture dictated words, and
`chainable=False` for commands that must come last.  Add a line to `CASES`
in `tests/speech/test_grammar.py`; the coverage, help-example and
test-set tests pick up the rest.

**The user's own.**  `commands.toml` in the settings folder is created with
commented examples on first start and re-read within 2 s of a change:

```toml
[[command]]
phrase = "sign off"
text = "Kind regards,\nSam"

[[command]]
phrase = "(open|start) music"
app = "Spotify"
```

Each block has a `phrase` (hassil syntax allowed) and exactly one of
`keys`, `text`, `app` or `url`.  User commands win over built-in ones,
except the safety commands: a phrase that could be heard as stop, cancel,
sleep, wake (the user's own sleep and wake phrases included), undo that,
pause or resume eyes, or help is left out and the toast says why, so a
shared "command pack" cannot take away the way out of everything else.
Problems come back as a toast naming the command, at once and again when
speech starts (the first one comes before anything shows toasts).

Limits (review S-07): the file at most 256 KB, 200 commands, a phrase at
most 200 characters with at most 8 optional parts in square brackets and
500 ways of saying it (alternatives and optional parts multiplied out),
`text` 2,000 characters and `keys` 200.  Matching runs on every utterance
and each optional part about doubles its cost: 18 of them took 3 s to
reject one sentence.  A `url` must be a web page or an e-mail link (http,
https, mailto, or no scheme); anything else (`file:`, `ms-settings:`) is
refused with a problem, and `web.go` refuses it again.  Spoken addresses
need "http colon slash slash" said in full; a bare "http" no longer counts.

Beside it, also created with an explanation on first start:

```text
# aliases.txt: what Sightline heard = the command you meant
scold down = scroll down
open note bad = open notepad

# vocabulary.txt: words and names for Sightline to listen out for
Siobhan
Kubernetes
```

## Settings (`config.py`)

| Field | Default | Notes |
|---|---|---|
| `microphone` | "" | Stable id "HOSTAPI:name"; empty is the Windows default |
| `engine` | auto | Recogniser choices above |
| `command_endpoint_ms` | 250 | `early_ms` is half of it, at least 100 |
| `learned_command_pause` | "Not learned yet" | Read-only (`setting(readonly=True)`): the learned wait in words, such as "0.84 seconds" |
| `learn_pauses` | on | Advanced |
| `learn_aliases` | on | Offer to remember how you say commands |
| `dictation_endpoint_ms` | 650 | |
| `speech_threshold` | 0.5 | VAD onset; offset is 0.15 lower |
| `wake_phrase`, `sleep_phrase` | "wake up", "go to sleep" | |
| `attention_gate` | on | Protected: voice can turn it on, not off |
| `mouth_gate` | off | Protected: voice can turn it on, not off |
| `semantic_matching` | on | |
| `spoken_punctuation`, `auto_capitalize`, `smart_spacing` | on | |
| `auto_punctuation` | off | |

## Tests

`.venv\Scripts\python.exe -m pytest tests/speech -q`

* Unit tests (no hardware, no models): normalisation, formatting, about
  300 utterance-to-action cases, overlays, chains, homophones, arbitration,
  the attention gate with `FakeVision`, "did you mean", risky actions,
  scratch, password privacy, user commands, endpointing and onset times
  with a fake recogniser, resampling and the file source.
  `test_personal.py` covers `aliases.txt`, `vocabulary.txt`, the pause
  model, the learned endpoint and hotword handling; `test_service.py` the
  alias offers, pause learning per profile and the vocabulary commands.
* Shared helpers (the bag-of-words paraphrase index, `FakeMicrophone`) live
  in `tests/speech/speech_helpers.py`, not in `conftest.py`: another
  folder's `conftest` module would otherwise shadow them in a full run.
* `models`: Silero VAD, paraphrase precision and recall with the real
  embedder, an end-to-end run (SAPI WAV clips paced like a microphone
  through the real VAD, endpointing, recogniser and grammar), and a name
  that Parakeet 110M gets right only with it in the vocabulary (`slow`
  too).
  They read the developer's models folder and skip when it is empty.
* Nothing in the suite plays audio, opens a microphone, moves the pointer
  or types.  The opt-in live capture test (`SIGHTLINE_LIVE_AUDIO=1`) and
  the real-voice checks are in `docs/TESTING.md`.

Regenerate the test set with `python tools/speech_testset.py` (WAV files
only) and the tables with `python tools/speech_bench.py` (holds the `cpu`
lock).

## Known limits

* The accuracy figures come from SAPI voices.  Real voices, accents,
  dysarthric speech and real microphones are still to be measured; isolated
  one-word commands ("up", "end", "home") are the weakest spot.
* The Talon alphabet ("air bat cap") is often merged by these recognisers;
  NATO words are more reliable.
* "tap" (click) and "tab" (the key) can be confused by the recogniser.
* The attention thresholds are priors until tried on a real face.
* Facing the screen is not proof that the owner spoke: a video or a call
  the owner is watching still reaches the microphone.  The safety-only rule
  limits what such speech can do; an echo check against what the PC plays
  is still to come.
* `pauses.json` that cannot be read is moved aside as `pauses.broken.json`
  and learning starts again.
* Aliases are learned only from speech followed by speech or a "did you
  mean" chip.  A miss followed by the same command from the dock, a gaze
  gesture or the keyboard teaches nothing, because the service does not see
  where those actions came from.  Aliases are removed only by editing
  `aliases.txt`.
* A command cut in two by a pause is learned from, but not repaired: before
  the wait is learned, "scroll" ... "down" still presses the Down key.
* The pause figures are from fake audio and synthetic voices; real slow or
  breath-limited speakers are still to be measured.
* The vocabulary figures come from SAPI voices saying names they do not
  know (and phonetic respellings); real speakers saying their own names are
  still to be measured.
