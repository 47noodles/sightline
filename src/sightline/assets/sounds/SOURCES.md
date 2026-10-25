# Sounds

Every file here is synthesised by `tools/ui_make_sounds.py` (NumPy, no
samples from anywhere else) and is part of Sightline under Apache-2.0.
Rebuild them with:

    .venv\Scripts\python.exe tools\ui_make_sounds.py

44.1 kHz, 16-bit, mono WAV, so `QSoundEffect` can play them with no decoder.

| File | When it plays | Sound |
|---|---|---|
| hold_1.wav | Both eyes held shut long enough to arm a click | one clear wooden tap |
| hold_2.wav | Held long enough to arm a double click | two higher taps |
| click.wav | A left click happened | a low, dry tap |
| double_click.wav | A double click happened | two low taps |
| right_click.wav | A right click happened | tap, then a lower tap |
| middle_click.wav | A middle click happened | tap, then a higher tap |
| recognised.wav | A voice command was understood | two soft bells, rising |
| not_understood.wav | Speech was heard but not understood | two soft bells, falling |
| sleep.wav | Voice went to sleep | three bells, falling |
| wake.wav | Voice woke up | three bells, rising |
| eyes_paused.wav | Eye control paused | two wooden taps, falling |
| eyes_resumed.wav | Eye control resumed | two wooden taps, rising |
| error.wav | An action failed | a low, soft "uh-uh" |
| calibration_point.wav | A calibration target was collected | one short high bell |
| success.wav | Setup or calibration finished | a rising four-note arpeggio |

Wood taps are for the pointer, bells are for the voice; rising means on,
awake or done, falling means off, asleep or not understood.
