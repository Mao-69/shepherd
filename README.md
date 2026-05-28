# CRV Research

A research-grade data-collection environment for **Controlled Remote Viewing
(CRV)** sessions augmented with layered binaural audio induction in the
Hemi-Sync® style, driven by live biometrics from a P58 / MOY-DBT5 smartwatch.

> **Not affiliated with the Monroe Institute.** "Hemi-Sync" is their
> registered trademark. This project is an independent open implementation
> in a similar layered style for personal research use.

## What the audio engine produces

For each Focus level, the engine layers in real time:

1. **Pink-noise bed** (Voss-McCartney 1/f) — the meditative "shhh" backdrop
2. **Binaural carrier stack** — multiple carriers (root + 2nd, 3rd, 5th
   harmonics) each pair contributing to the perceived beat at Δf
3. **Amplitude modulation** at Δf — audible pulsing for stronger entrainment
4. **Voice guidance** via `espeak-ng` — calm announcements at each transition
5. **30-second crossfades** between Focus levels — no jarring jumps

Each Focus level uses a perceptually distinct carrier frequency, so you can
actually hear when you've moved between stages.

| Focus | Δf  | Carrier | State (per open literature) |
|-------|-----|---------|-----------------------------|
| 10    | 4 Hz  | 100 Hz | Mind awake, body asleep |
| 12    | 7 Hz  | 120 Hz | Expanded awareness |
| 15    | 10 Hz | 150 Hz | "No time" |
| 21    | 16 Hz | 200 Hz | Bridge state |
| 22    | 14 Hz | 220 Hz | Liminal |
| 23    | 12 Hz | 230 Hz | Recently arrived non-physical |
| 24    | 10 Hz | 240 Hz | Belief System Territories (early) |
| 25    | 9 Hz  | 250 Hz | Belief System Territories (mid) |
| 26    | 8 Hz  | 260 Hz | Belief System Territories (late) |
| 27    | 6 Hz  | 270 Hz | The "park" / reception center |

## Protocol references

- **CRV (Controlled Remote Viewing)**: Ingo Swann's six-stage protocol,
  developed at SRI International (formerly Stanford Research Institute) under
  the Star Gate / SCANATE programs. Partially declassified DIA *Coordinate
  Remote Viewing Manual* (1985).
- **Gateway / Focus levels**: 1983 INSCOM analytical report by Lt Col Wayne
  McDonnell; Bob Monroe's *Journeys Out of the Body*, *Far Journeys*, and
  *Ultimate Journey*; *Hemi-Sync Sourcebook*.

## Prerequisites

```bash
# System packages
sudo apt install bluez libportaudio2 espeak-ng

# Watch paired+bonded with bluetoothctl (one time)
bluetoothctl
> trust  FF:26:0B:1A:38:7D
> pair   FF:26:0B:1A:38:7D

# Python deps
pip install -r requirements.txt
```

**Headphones are required** for the binaural effect.

**First run downloads the Kokoro TTS model files** (~106MB total: 80MB
quantized model + 26MB voices) into `~/.cache/crv_research/kokoro/`. After
that, voice synthesis is fast and runs entirely on your CPU.

Available voices (American Female unless noted): `af_heart` (default,
calm), `af_bella`, `af_nicole`, `af_sarah`, `af_sky`, `bf_emma` (British
Female). Select with `--voice`. Speed control via `--voice-speed`
(default 0.9 = slightly slower than normal for meditative pace).

If `kokoro-onnx` isn't installed, voice falls back to `espeak-ng`
automatically. If neither is available, voice cues are silently skipped.

## Pre-download the voice model

If you want to fetch the model files before running a real session (e.g.
on a fast network connection), use the standalone audio test:

```bash
python -m crv.audio --download-voices-only
```

## Building the target pool

Before running blinded sessions, build a pool of sealed targets. Images
are pulled from Wikimedia Commons Featured Pictures (CC-licensed, no API
key needed) and encrypted into `targets/pool/`:

```bash
# Default: 50 targets with images
python -m crv.targets fetch --count 50

# Slower, more polite (if you hit rate-limiting)
python -m crv.targets fetch --count 50 --delay 5

# Smaller thumbnails (faster, lower bandwidth)
python -m crv.targets fetch --count 50 --thumb-width 640

# No images at all (just title+description for now; refetch images later)
python -m crv.targets fetch --count 50 --no-images
```

Other pool commands:

```bash
python -m crv.targets list              # show pool contents + coverage
python -m crv.targets refetch-images    # backfill images for no-image targets
python -m crv.targets reveal "374-2856 9"   # manually unseal a target
```

The pool needs at least 4 targets before blinded decoy-ranking works
(1 real + 3 decoys). More targets = better blinding.

## Running a session

```bash
# Standard ~103 min protocol; auto-discovers your watch over Bluetooth
python run.py

# Skip discovery if you know the MAC
python run.py --mac FF:26:0B:1A:38:7D

# Run WITHOUT a watch — skips all biometrics/vitals, goes straight into
# the guided CRV session with audio induction only
python run.py --watch no

# Blinded session drawing a target from the pool
python run.py --pool ./targets

# Timing tiers and the Focus 21-27 journey (see "Protocol timing tiers" below)
python run.py --protocol short            # ~58 min
python run.py --protocol standard         # ~103 min (default)
python run.py --protocol long             # ~152 min
python run.py --protocol long --extended  # ~217 min, full deep journey
```

### Watch discovery

By default the program scans for a compatible watch by Bluetooth name
(matching `P58`, `MOY`, `MOYOUNG`, `DBT5`, etc.). It first checks devices
already paired/bonded via bluetoothctl (instant), then does a ~12-second
active scan if needed. If it can't find one, you'll be offered the choice
to continue watch-less or abort.

For discovery and connection to work, the watch must be paired+bonded
once via bluetoothctl (see Prerequisites). If your watch advertises an
unusual name, pass `--mac` explicitly.

The session opens a notes.md file (path shown in terminal — open it in a
**separate** terminal/editor window; don't open it in the dashboard's
terminal or it'll steal stdin and break hotkeys).

Hotkeys during a session:

| Key     | Action |
|---------|--------|
| Space   | Advance to next stage early |
| p       | Pause / resume timer |
| m       | Re-measure BP and SpO₂ now |
| n       | Mark an event in notes.md |
| q       | Abort session (data still saved) |

## Protocol timing tiers

Three timing tiers control how long each stage lasts:

- `--protocol short` (~58 min) — for quick practice or constrained schedules
- `--protocol standard` (~103 min) — closer to the open Monroe/SRI literature; the new sensible default
- `--protocol long` (~152 min) — full SRI/Monroe spec, for deep work

| Flag | Total | Use |
|------|-------|-----|
| `--protocol short` | ~58 min | Time-pressed practice, the old default |
| `--protocol standard` (new default) | ~103 min | Closer to open Monroe/SRI literature |
| `--protocol long` | ~152 min | Full SRI/Monroe-spec deep work |

Compose with `--extended` to add the Focus 21-27 journey stages before
cooldown:

| Combination | Total |
|-------------|-------|
| `--protocol short` | 58 min |
| `--protocol short --extended` | 87 min |
| `--protocol standard` (default) | 103 min |
| `--protocol standard --extended` | 144 min |
| `--protocol long` | 152 min |
| `--protocol long --extended` | 217 min |

The biggest difference between tiers is the **Focus 10 induction**:
5 min (short), 20 min (standard), 30 min (long). Open Monroe literature
is clear that genuine Focus 10 takes 20+ minutes for most practitioners
to drop into reliably — the short tier's 5 minutes is closer to "settling
into the chair" time. Stages V (Probing) and VI (Modeling) are also
substantially longer in the standard and long tiers, matching reports
that the deepest CRV data often emerges after the first ten minutes of
a stage.

Examples:

```bash
# First real session at the new standard timing (~103 min)
python run.py --pool ./targets

# Or compose
python run.py --protocol long --extended   # full ~217 min deep journey
python run.py --protocol short             # quick practice
```

## Session output

Each session creates a timestamped folder under `sessions/`:

```
sessions/2026-05-25_14-30-15/
├── metadata.json            # config + summary stats
├── heartrate.csv            # timestamp, bpm, state
├── stage_transitions.csv    # timestamp, code, name, focus_level
├── audio_events.csv         # timestamp, focus_level, beat_hz, volume
├── vitals.csv               # timestamp, kind (BP/SpO2), value
├── notes.md                 # your prose notes
└── summary.md               # auto-generated post-session report
```

Load the CSVs into pandas/R/JASP/spreadsheet for analysis.

## Honest caveats

- **This is a personal research tool, not a clinical device.** Watch readings
  are from cheap consumer hardware.
- **CRV and Gateway protocols are themselves unproven.** This project takes
  no position on their efficacy; it provides infrastructure for self-
  experimentation, nothing more.
- **Binaural-beat entrainment is contested in the literature.** Recent
  meta-analyses (Garcia-Argibay et al., 2019) find small effects on mood
  and attention but mixed evidence for the underlying EEG-entrainment
  mechanism. Some studies suggest amplitude-modulated/isochronic tones
  produce stronger effects than pure binaural; this engine combines both.
- **Stop immediately and consult a doctor** if you experience seizures,
  severe anxiety, dissociation, or any adverse reaction. Binaural audio
  may affect some people unpredictably.

## License

MIT. Hemi-Sync® is a registered trademark of the Monroe Institute and is
referenced only descriptively.
