# Hearing EQ

Personal hearing-compensation EQ for music, built from your own audiogram.

1. **Measure** your hearing thresholds per ear with a pure-tone test.
2. **Compensate**: apply a per-ear, linear-phase EQ that boosts the bands you've
   lost by a fraction of the measured loss. Pitch, tuning and instruments stay
   exactly as mixed; only the spectral balance changes.
3. **Use it** either by batch-rendering your music library to FLAC, or in real
   time on Windows (foobar2000 convolver / Equalizer APO).

There's also a **simulate** mode that does the reverse: it attenuates the lost
bands so a normal-hearing person can hear music the way you do.

> **Not a medical device.** The hearing test is relative (thresholds are in
> dBFS and depend on your headphones and system volume). It's meant for mild to
> moderate high-frequency loss with usable residual hearing, not as a
> substitute for a clinical audiogram or hearing aids.

## Requirements

- Windows (the hearing test uses `msvcrt` for keyboard input)
- Python 3.9+
- [ffmpeg](https://ffmpeg.org/) on `PATH` (for decoding MP3 / M4A)

```
pip install -r requirements.txt
```

## Quick start

Run the GUI, which wraps all three tools:

```
python hearing_eq_gui.py
```

Or use the scripts directly from the command line:

```
python audiogram.py                     # 1. take the hearing test -> audiogram.json
python hearing_eq.py music/ out/        # 2a. render compensated FLACs
python export_eq.py                     # 2b. or export real-time filters -> windows_eq/
```

**Keep the same headphones and system volume** between the test and listening,
since the thresholds are relative to them.

## Tools

### `audiogram.py`: hearing test

Adaptive pure-tone audiometry (Hughson-Westlake staircase: 10 dB down / 5 dB
up) with randomly interleaved frequencies and ears. Saves thresholds to JSON
and plots an audiogram.

| Option | Description |
|---|---|
| `--freqs HZ …` | Test frequencies (default: standard audiometric set) |
| `--ear left\|right\|both` | Which ear(s) to test (default: both) |
| `--output FILE` | Output JSON (default: `audiogram.json`) |
| `--fast` | Fewer reversals, quicker test |
| `--no-calibration` | Skip the reference tone / volume-setting step |
| `--no-plot` | Don't plot the audiogram |

### `hearing_eq.py`: batch processor

Reads `.mp3`, `.m4a`, `.wav` and `.flac` files from a folder and writes
compensated FLACs (tags and cover art are preserved).

| Option | Description |
|---|---|
| `--audiogram FILE` | Audiogram JSON (default: `./audiogram.json`) |
| `--strength F` | Fraction of the measured loss to apply (default 0.5, the half-gain rule; 1.0 in simulate mode) |
| `--max-boost DB` | Ceiling on per-band gain (default 18 dB; 60 dB in simulate mode) |
| `--taps N` | FIR length (default 4095) |
| `--simulate` | Attenuate instead of boost, to hear how the listener hears |
| `--plot` | Save the applied EQ curve as a PNG |

### `export_eq.py`: real-time filters for Windows

Uses the same filter design as `hearing_eq.py`, so live playback matches the
rendered FLACs. Writes to `windows_eq/`:

- `hearing_ir_44100.wav` / `hearing_ir_48000.wav`: stereo impulse responses
  for foobar2000's Convolver DSP (left channel = left ear, right = right ear).
- `equalizer_apo_config.txt`: per-ear GraphicEQ + preamp for
  [Equalizer APO](https://sourceforge.net/projects/equalizerapo/), which applies
  system-wide (Spotify, browser, games…).
- `README.txt`: step-by-step install instructions for both.

Options: `--audiogram`, `--out-dir`, `--strength`, `--max-boost`, `--taps`,
`--rates HZ …` (default 44100 48000).

### `hearing_eq_gui.py`: GUI

A Tkinter front-end for the three tools. It's also the single entry point for
the packaged executable: the GUI re-invokes itself with `--run audiogram`,
`--run process` or `--run export`.

## Audiogram format

```json
{
  "metadata": { "date": "…", "device": "…", "n_reversals": 6, "note": "…" },
  "thresholds": {
    "left":  { "500": -74.0, "1000": -71.5, "4000": -60.0, "8000": -45.0 },
    "right": { "500": -75.0, "1000": -71.0, "4000": -55.0, "8000": -50.0 }
  }
}
```

Thresholds are in dBFS per frequency (Hz). `null` means "not audible even at
the maximum test level". Your loss at each frequency is measured relative to
your own baseline (the median threshold at ≤ 2 kHz).

## Building the executable

```
pip install pyinstaller
pyinstaller HearingEQ.spec
```

This produces a single `dist/HearingEQ.exe` containing the GUI and all three
tools. ffmpeg still needs to be on `PATH`. (`audiogram.spec` and
`hearing_eq.spec` are older per-tool builds.)

## Download

A prebuilt `HearingEQ.exe` (Windows) is available on the
[Releases](https://github.com/ThePerramus/hearing_eq/releases) page.

## License

[MIT](LICENSE)
