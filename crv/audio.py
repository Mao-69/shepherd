"""crv/audio.py — Layered, Hemi-Sync-inspired binaural induction engine.

NOT affiliated with the Monroe Institute. "Hemi-Sync" is their trademark;
this is an independent, open implementation in the same general style for
personal research use.

What this engine produces per Focus level:

    1. PINK NOISE BED      — 1/f noise generated via Voss-McCartney, low volume
    2. BINAURAL CARRIERS   — multiple stacked sine pairs, each L/R differing
                             by the target Δf so several carriers contribute
                             to the perceived beat
    3. HARMONICS           — 2x, 3x, 5x carrier overtones at falling amplitudes,
                             giving the tones "warmth" instead of sterile sine
    4. AMPLITUDE MODULATION — slow volume oscillation at the target Δf
                             (audible pulsing; better entrainment evidence
                             than pure binaural beats alone)
    5. VOICE GUIDANCE      — espeak-ng synthesizes calm stage announcements,
                             played as a one-shot overlay at transitions

Focus level frequencies follow open Monroe Institute literature (Hemi-Sync
Sourcebook, Lewis et al. open papers). Carrier frequencies are chosen so each
Focus level is *perceptually* distinguishable (higher Focus = brighter tone)
while Δf moves into the theta/alpha range as Focus increases.

Threading model:
    - One sounddevice OutputStream callback runs in PortAudio's audio thread.
    - The main thread calls set_focus() / set_volume() / speak() to change
      state. Transitions glide smoothly via per-block linear interpolation.
"""

from __future__ import annotations

import hashlib
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from typing import Optional

import numpy as np

try:
    import sounddevice as sd
    _HAVE_SD = True
    _SD_ERR  = ""
except (ImportError, OSError) as _e:
    _HAVE_SD = False
    _SD_ERR  = str(_e)


SAMPLE_RATE = 44100
BLOCK_SIZE  = 1024


# -----------------------------------------------------------------------------
# Focus level definitions
# -----------------------------------------------------------------------------
# Each entry specifies the layered audio profile for that Focus level. Values
# come from open Monroe Institute literature for Δf (the binaural beat) and
# state description. Carrier choices are picked to make each level
# perceptually distinct: higher Focus → higher carrier → brighter tone.
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class FocusProfile:
    name:              str
    delta_f:           float           # binaural beat frequency (Hz)
    carrier:           float           # root carrier (Hz)
    harmonic_levels:   tuple           # amplitudes of 2nd, 3rd, 5th harmonics
    pink_level:        float           # pink-noise volume (0..1 ratio of master)
    am_depth:          float           # amplitude modulation depth (0..1)
    announce:          str             # short voice-prompt text


# Δf rationale:
#   4 Hz = δ/θ border, classic deep-relaxation onset
#   7 Hz = theta peak, imagery & intuition band
#   10 Hz = alpha (Berger rhythm), relaxed awareness
#   16 Hz = low beta, focused analytical bridge
#   6-9 Hz = mid theta, integrative/exploratory
FOCUS_PROFILES = {
    "off": FocusProfile(
        name="Off (silent)",
        delta_f=0.0, carrier=0.0, harmonic_levels=(0, 0, 0),
        pink_level=0.0, am_depth=0.0, announce=""),

    "c1": FocusProfile(
        name="C-1 — Ordinary waking",
        delta_f=0.0, carrier=80.0, harmonic_levels=(0.10, 0.05, 0.02),
        pink_level=0.10, am_depth=0.0,
        announce="C one. Ordinary waking awareness."),

    "focus_10": FocusProfile(
        name="Focus 10 — Mind awake, body asleep",
        delta_f=4.0, carrier=100.0, harmonic_levels=(0.20, 0.10, 0.05),
        pink_level=0.18, am_depth=0.30,
        announce="Beginning Focus ten. Mind awake. Body asleep."),

    "focus_12": FocusProfile(
        name="Focus 12 — Expanded awareness",
        delta_f=7.0, carrier=120.0, harmonic_levels=(0.20, 0.10, 0.05),
        pink_level=0.16, am_depth=0.35,
        announce="Moving to Focus twelve. Expanded awareness."),

    "focus_15": FocusProfile(
        name="Focus 15 — No time",
        delta_f=10.0, carrier=150.0, harmonic_levels=(0.18, 0.10, 0.04),
        pink_level=0.14, am_depth=0.35,
        announce="Focus fifteen. The state of no time."),

    "focus_21": FocusProfile(
        name="Focus 21 — Bridge",
        delta_f=16.0, carrier=200.0, harmonic_levels=(0.15, 0.08, 0.04),
        pink_level=0.13, am_depth=0.30,
        announce="Focus twenty-one. The bridge."),

    "focus_22": FocusProfile(
        name="Focus 22 — Liminal",
        delta_f=14.0, carrier=220.0, harmonic_levels=(0.15, 0.08, 0.04),
        pink_level=0.13, am_depth=0.30,
        announce="Focus twenty-two."),

    "focus_23": FocusProfile(
        name="Focus 23",
        delta_f=12.0, carrier=230.0, harmonic_levels=(0.13, 0.07, 0.03),
        pink_level=0.12, am_depth=0.28,
        announce="Focus twenty-three."),

    "focus_24": FocusProfile(
        name="Focus 24 — Belief territories",
        delta_f=10.0, carrier=240.0, harmonic_levels=(0.13, 0.07, 0.03),
        pink_level=0.12, am_depth=0.28,
        announce="Focus twenty-four."),

    "focus_25": FocusProfile(
        name="Focus 25",
        delta_f=9.0, carrier=250.0, harmonic_levels=(0.12, 0.06, 0.03),
        pink_level=0.12, am_depth=0.28,
        announce="Focus twenty-five."),

    "focus_26": FocusProfile(
        name="Focus 26",
        delta_f=8.0, carrier=260.0, harmonic_levels=(0.12, 0.06, 0.03),
        pink_level=0.11, am_depth=0.28,
        announce="Focus twenty-six."),

    "focus_27": FocusProfile(
        name="Focus 27 — The park",
        delta_f=6.0, carrier=270.0, harmonic_levels=(0.10, 0.05, 0.02),
        pink_level=0.10, am_depth=0.25,
        announce="Focus twenty-seven. The park."),
}


# -----------------------------------------------------------------------------
# Voss-McCartney pink noise generator (smooth 1/f spectrum)
# -----------------------------------------------------------------------------

class PinkNoiseGenerator:
    """Continuous pink-noise generator using the Voss-McCartney algorithm.

    Pink noise has equal energy per octave (1/f spectrum) — perceptually
    "warm" like distant rainfall, unlike white noise which sounds harsh.
    Voss-McCartney sums multiple white-noise sources updated at decreasing
    rates; the result is a very good 1/f approximation."""

    NUM_ROWS = 16        # number of summed sources

    def __init__(self, seed: Optional[int] = None):
        self._rng     = np.random.default_rng(seed)
        self._rows    = self._rng.standard_normal(self.NUM_ROWS).astype(np.float32)
        self._counter = 0
        self._max_key = (1 << self.NUM_ROWS) - 1

    def generate(self, n: int) -> np.ndarray:
        """Produce n pink-noise samples in roughly [-1, 1]."""
        out = np.empty(n, dtype=np.float32)
        running_sum = float(self._rows.sum())
        for i in range(n):
            self._counter = (self._counter + 1) & self._max_key
            # Find the lowest bit that changed in counter; that row updates
            diff = self._counter ^ (self._counter - 1)
            row  = (diff.bit_length() - 1) if diff else 0
            if row < self.NUM_ROWS:
                new_val = float(self._rng.standard_normal())
                running_sum += new_val - float(self._rows[row])
                self._rows[row] = new_val
            white_jitter = float(self._rng.standard_normal()) * 0.3
            out[i] = (running_sum + white_jitter) / (self.NUM_ROWS + 1)
        # Normalize — pink samples are smaller than white at same row count
        return out * 0.5


# -----------------------------------------------------------------------------
# Voice synthesis — Kokoro-82M (ONNX, high quality) with espeak-ng fallback
# -----------------------------------------------------------------------------

# Kokoro-onnx model files. Downloaded on first use and cached.
KOKORO_MODEL_URL  = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx"
KOKORO_VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
KOKORO_MODEL_FILE  = "kokoro-v1.0.int8.onnx"
KOKORO_VOICES_FILE = "voices-v1.0.bin"
KOKORO_SAMPLE_RATE = 24000

# Default voice: "af_heart" is one of the calmer American-female voices in
# Kokoro's catalog, suited for meditative announcements. Alternatives worth
# trying: af_bella, af_nicole, af_sarah, af_sky, bf_emma (British female).
DEFAULT_KOKORO_VOICE = "af_heart"


def _cache_dir() -> str:
    """Where Kokoro model files live. Honors XDG_CACHE_HOME / falls back
    to ~/.cache/crv_research/kokoro."""
    base = os.environ.get("XDG_CACHE_HOME",
                          os.path.expanduser("~/.cache"))
    path = os.path.join(base, "crv_research", "kokoro")
    os.makedirs(path, exist_ok=True)
    return path


def _voice_cache_dir() -> str:
    """Persistent cache for synthesized voice WAVs. Indexed by a hash of
    (text, voice, speed, backend) — voice/speed changes invalidate
    automatically; text changes do too. Lives across sessions so the
    second run never has to re-synthesize anything that hasn't changed."""
    base = os.environ.get("XDG_CACHE_HOME",
                          os.path.expanduser("~/.cache"))
    path = os.path.join(base, "crv_research", "voices")
    os.makedirs(path, exist_ok=True)
    return path


def _voice_cache_key(text: str, voice: str, speed: float, backend: str) -> str:
    h = hashlib.sha256()
    h.update(text.encode("utf-8"))
    h.update(f"|{voice}|{speed:.3f}|{backend}".encode("utf-8"))
    return h.hexdigest()[:24]   # 24 hex chars is plenty for uniqueness


class VoiceSynth:
    """Synthesizes short stage announcements using Kokoro-82M (preferred)
    or falls back to espeak-ng if Kokoro isn't installed or its model
    files aren't downloaded yet. If neither is available, voice synth
    silently degrades to no-op."""

    def __init__(self,
                  voice: str = DEFAULT_KOKORO_VOICE,
                  speed: float = 0.9,             # 0.9 = slightly slower than default
                  espeak_voice: str = "en+f3",
                  espeak_rate: int = 130,
                  espeak_pitch: int = 35,
                  auto_download: bool = True,
                  verbose: bool = True):
        self.voice         = voice
        self.speed         = speed
        self.auto_download = auto_download
        self.verbose       = verbose

        # espeak parameters (fallback path)
        self.espeak_voice = espeak_voice
        self.espeak_rate  = espeak_rate
        self.espeak_pitch = espeak_pitch

        # State
        self._cache:    dict[str, np.ndarray] = {}
        self._tmpdir    = tempfile.mkdtemp(prefix="crv_voice_")
        self._kokoro    = None
        self._espeak_ok = shutil.which("espeak-ng") is not None

        # Try Kokoro first
        self._kokoro = self._try_init_kokoro()

    @property
    def available(self) -> bool:
        return (self._kokoro is not None) or self._espeak_ok

    @property
    def backend_name(self) -> str:
        if self._kokoro is not None:
            return f"kokoro-onnx ({self.voice})"
        if self._espeak_ok:
            return f"espeak-ng ({self.espeak_voice})"
        return "none"

    # ---- Kokoro initialization ----------------------------------------

    def _try_init_kokoro(self):
        """Returns a Kokoro instance, or None if unavailable.
        Auto-downloads model files on first run if necessary."""
        try:
            from kokoro_onnx import Kokoro
        except ImportError:
            if self.verbose:
                print("[VoiceSynth] kokoro-onnx not installed — "
                      "pip install kokoro-onnx for HQ voice. "
                      "Falling back to espeak-ng.")
            return None

        cache = _cache_dir()
        model_path  = os.path.join(cache, KOKORO_MODEL_FILE)
        voices_path = os.path.join(cache, KOKORO_VOICES_FILE)

        if not (os.path.exists(model_path) and os.path.exists(voices_path)):
            if not self.auto_download:
                if self.verbose:
                    print(f"[VoiceSynth] Kokoro model files not found in "
                          f"{cache}. Re-run with auto_download=True or "
                          f"fetch them manually.")
                return None
            if not self._download_models(model_path, voices_path):
                return None

        try:
            if self.verbose:
                print(f"[VoiceSynth] Loading Kokoro model from {cache} ...")
            return Kokoro(model_path, voices_path)
        except Exception as e:
            if self.verbose:
                print(f"[VoiceSynth] Kokoro init failed: {e}. "
                      f"Falling back to espeak-ng.")
            return None

    def _download_models(self, model_path: str, voices_path: str) -> bool:
        """One-time download of Kokoro model files. ~80MB + ~26MB."""
        import urllib.request
        import urllib.error

        targets = [
            (KOKORO_MODEL_URL,  model_path,  "Kokoro model (~80MB)"),
            (KOKORO_VOICES_URL, voices_path, "Kokoro voices (~26MB)"),
        ]
        for url, dest, label in targets:
            if os.path.exists(dest):
                continue
            if self.verbose:
                print(f"[VoiceSynth] Downloading {label} ...")
                print(f"  {url}")
                print(f"  -> {dest}")
            try:
                last_pct = [-1]
                def hook(blocks, blocksize, total):
                    if total <= 0:
                        return
                    pct = min(100, int(100 * blocks * blocksize / total))
                    if pct >= last_pct[0] + 5:
                        last_pct[0] = pct
                        if self.verbose:
                            print(f"    {pct}%  ", end="\r", flush=True)
                urllib.request.urlretrieve(url, dest, reporthook=hook)
                if self.verbose:
                    print(f"    done.            ")
            except (urllib.error.URLError, OSError) as e:
                if self.verbose:
                    print(f"[VoiceSynth] Download failed: {e}")
                # Clean up partial file
                try: os.unlink(dest)
                except Exception: pass
                return False
        return True

    # ---- public API ---------------------------------------------------

    def synthesize(self, text: str) -> Optional[np.ndarray]:
        """Return a float32 mono numpy array of the spoken text at the
        engine's sample rate, or None if synthesis isn't available.

        Lookup order:
            1. In-memory cache (fastest; warm per process)
            2. On-disk cache (~/.cache/crv_research/voices/*.wav)
            3. Kokoro synthesis
            4. espeak-ng fallback synthesis

        After fresh synthesis, the result is persisted to both caches.
        Cache key includes voice/speed/backend so changing any of those
        invalidates automatically."""
        if not text:
            return None
        if text in self._cache:
            return self._cache[text]

        # Disk cache check
        disk_path = self._disk_cache_path_for(text)
        if disk_path is not None and disk_path.exists():
            audio = self._load_wav(disk_path)
            if audio is not None:
                self._cache[text] = audio
                return audio

        # Fresh synthesis
        audio: Optional[np.ndarray] = None
        if self._kokoro is not None:
            audio = self._synthesize_kokoro(text)
        if audio is None and self._espeak_ok:
            audio = self._synthesize_espeak(text)

        if audio is not None:
            self._cache[text] = audio
            # Persist to disk for next session
            if disk_path is not None:
                self._save_wav(disk_path, audio)
        return audio

    def _disk_cache_path_for(self, text: str):
        """Compute the on-disk cache path for a given utterance, or None
        if no backend is active (can't form a key without knowing which
        engine would have rendered it)."""
        from pathlib import Path
        backend = self.backend_name
        if backend == "none":
            return None
        key = _voice_cache_key(text, self.voice, self.speed, backend)
        return Path(_voice_cache_dir()) / f"{key}.wav"

    @staticmethod
    def _load_wav(path) -> Optional[np.ndarray]:
        """Load a 16-bit mono WAV at SAMPLE_RATE into float32 [-1,1]."""
        try:
            with wave.open(str(path), "rb") as wf:
                if wf.getnchannels() != 1 or wf.getframerate() != SAMPLE_RATE:
                    return None
                frames = wf.readframes(wf.getnframes())
                if wf.getsampwidth() != 2:
                    return None
            audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            return audio
        except Exception:
            return None

    @staticmethod
    def _save_wav(path, audio: np.ndarray):
        """Save float32 mono audio as 16-bit WAV at SAMPLE_RATE.
        Writes to a temp file in the same directory then renames, so a
        crash mid-write never leaves a half-written cache file."""
        try:
            tmp_path = path.with_suffix(".wav.tmp")
            samples = np.clip(audio, -1.0, 1.0)
            int16   = (samples * 32767.0).astype(np.int16)
            with wave.open(str(tmp_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(int16.tobytes())
            os.replace(tmp_path, path)
        except Exception as e:
            print(f"[VoiceSynth] disk cache save failed: {e}")

    # ---- Kokoro synthesis ---------------------------------------------

    def _synthesize_kokoro(self, text: str) -> Optional[np.ndarray]:
        try:
            samples, sample_rate = self._kokoro.create(
                text, voice=self.voice, speed=self.speed, lang="en-us")
            audio = np.asarray(samples, dtype=np.float32)
            if sample_rate != SAMPLE_RATE:
                audio = self._resample(audio, sample_rate, SAMPLE_RATE)
            # Gentle fade-in/out to avoid clicks
            self._apply_edge_fade(audio, fade_ms=30)
            return audio
        except Exception as e:
            if self.verbose:
                print(f"[VoiceSynth] Kokoro synthesis failed for {text!r}: {e}")
            return None

    # ---- espeak fallback ----------------------------------------------

    def _synthesize_espeak(self, text: str) -> Optional[np.ndarray]:
        wav_path = os.path.join(self._tmpdir,
                                  f"speech_{abs(hash(text)) & 0xffffffff:08x}.wav")
        try:
            subprocess.run(
                ["espeak-ng",
                 "-v", self.espeak_voice,
                 "-s", str(self.espeak_rate),
                 "-p", str(self.espeak_pitch),
                 "-w", wav_path,
                 text],
                check=True, capture_output=True, timeout=10)
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            if self.verbose:
                print(f"[VoiceSynth] espeak-ng failed: {e}")
            self._espeak_ok = False
            return None

        try:
            with wave.open(wav_path, "rb") as wf:
                sr = wf.getframerate()
                frames = wf.readframes(wf.getnframes())
                sampwidth = wf.getsampwidth()
            if sampwidth == 2:
                audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            else:
                audio = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
            if sr != SAMPLE_RATE:
                audio = self._resample(audio, sr, SAMPLE_RATE)
            self._apply_edge_fade(audio, fade_ms=30)
            return audio
        except Exception as e:
            if self.verbose:
                print(f"[VoiceSynth] WAV read failed: {e}")
            return None
        finally:
            try: os.unlink(wav_path)
            except Exception: pass

    # ---- helpers ------------------------------------------------------

    @staticmethod
    def _resample(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
        """Linear-interpolation resample. Good enough for voice; not for
        critical music applications, but no scipy/librosa dep needed."""
        if src_sr == dst_sr:
            return audio
        ratio = dst_sr / src_sr
        new_len = int(round(len(audio) * ratio))
        if new_len <= 0:
            return audio
        old_idx = np.arange(len(audio), dtype=np.float64)
        new_idx = np.linspace(0, len(audio) - 1, new_len, dtype=np.float64)
        return np.interp(new_idx, old_idx, audio).astype(np.float32)

    @staticmethod
    def _apply_edge_fade(audio: np.ndarray, fade_ms: int = 30):
        """In-place linear fade-in/out to suppress click at edges.
        Modifies the array directly."""
        n = int(SAMPLE_RATE * fade_ms / 1000)
        if len(audio) < 2 * n:
            return
        audio[:n]  *= np.linspace(0.0, 1.0, n, dtype=np.float32)
        audio[-n:] *= np.linspace(1.0, 0.0, n, dtype=np.float32)


# -----------------------------------------------------------------------------
# Main engine
# -----------------------------------------------------------------------------

@dataclass
class AudioStateSnapshot:
    focus_level:    str   = "off"
    focus_name:     str   = "Off (silent)"
    delta_f:        float = 0.0
    carrier:        float = 0.0
    pink_level:     float = 0.0
    volume:         float = 0.0
    speaking:       bool  = False
    chime_active:   bool  = False


class BinauralEngine:
    """Layered binaural induction engine with smooth Focus-level glides,
    pink-noise bed, harmonic carriers, AM modulation, and voice overlay."""

    def __init__(self,
                  sample_rate:    int   = SAMPLE_RATE,
                  beat_glide_sec: float = 30.0,    # crossfade time between levels
                  vol_glide_sec:  float = 1.0,
                  voice_volume:   float = 0.8,
                  master_volume:  float = 0.35,
                  voice:          str   = DEFAULT_KOKORO_VOICE,
                  voice_speed:    float = 0.9,
                  voice_verbose:  bool  = True):
        self.sr            = sample_rate
        self.beat_glide_sec= beat_glide_sec
        self.vol_glide_sec = vol_glide_sec
        self.voice_volume  = voice_volume

        # State (locked)
        self._lock           = threading.Lock()
        self._focus_level    = "off"
        self._target_profile = FOCUS_PROFILES["off"]
        self._cur_profile    = FOCUS_PROFILES["off"]
        self._target_volume  = master_volume
        self._cur_volume     = 0.0
        self._blend          = 1.0    # 1.0 = fully cur, 0.0 = fully target
                                       # decreases towards 0 during a glide,
                                       # then we snap target→cur and reset to 1

        # Continuous phase accumulators (one per oscillator to avoid clicks)
        self._phases_l = np.zeros(4, dtype=np.float64)
        self._phases_r = np.zeros(4, dtype=np.float64)
        self._phases_l_target = np.zeros(4, dtype=np.float64)
        self._phases_r_target = np.zeros(4, dtype=np.float64)
        self._am_phase = 0.0
        self._am_phase_target = 0.0

        # Pink noise
        self._pink = PinkNoiseGenerator()

        # Voice — queue of utterances. The audio callback consumes them
        # back-to-back so multiple speak() calls chain naturally.
        self._voice = VoiceSynth(voice=voice,
                                  speed=voice_speed,
                                  verbose=voice_verbose)
        self._voice_queue: list = []                    # list[np.ndarray]
        self._voice_pos = 0                              # offset into queue[0]

        # Chime (state transitions, coherence cue)
        self._chime_remaining = 0
        self._chime_freq      = 880.0
        self._chime_phase     = 0.0

        # Stream
        self._stream: Optional[sd.OutputStream] = None
        self._available = _HAVE_SD

    # ---- lifecycle ----------------------------------------------------

    @property
    def available(self) -> bool:        return self._available
    @property
    def availability_error(self) -> str: return "" if _HAVE_SD else _SD_ERR
    @property
    def voice_available(self) -> bool:  return self._voice.available
    @property
    def voice_backend(self) -> str:     return self._voice.backend_name

    def start(self) -> bool:
        if not _HAVE_SD or self._stream is not None:
            return self._stream is not None
        try:
            self._stream = sd.OutputStream(
                samplerate=self.sr, channels=2,
                blocksize=BLOCK_SIZE, dtype="float32",
                callback=self._audio_callback)
            self._stream.start()
            return True
        except Exception as e:
            print(f"[BinauralEngine] stream open failed: {e}")
            self._available = False
            return False

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    # ---- public state setters -----------------------------------------

    def set_focus(self, level: str, announce: bool = True):
        """Switch to a new Focus level with a smooth crossfade.
        If `announce` is True and voice is available, also speak the
        transition announcement defined in the profile. Clears any
        in-flight or pending speech first."""
        if level not in FOCUS_PROFILES:
            raise ValueError(f"unknown focus level: {level}")
        prof = FOCUS_PROFILES[level]
        with self._lock:
            self._focus_level    = level
            self._target_profile = prof
            self._blend          = 1.0   # start of crossfade
        if announce and prof.announce:
            self.speak(prof.announce, immediate=True)

    def set_volume(self, vol: float):
        with self._lock:
            self._target_volume = max(0.0, min(1.0, vol))

    def speak(self, text: str, immediate: bool = False,
                post_pause_sec: float = 0.4) -> bool:
        """Queue a voice utterance. Plays after anything already in the
        queue. If `immediate=True`, clears the queue first (preempts any
        in-flight speech). Returns True if the text was successfully
        synthesized and queued. Adds `post_pause_sec` of trailing silence
        to space utterances apart."""
        audio = self._voice.synthesize(text)
        if audio is None:
            return False
        if post_pause_sec > 0:
            pad = np.zeros(int(post_pause_sec * self.sr), dtype=np.float32)
            audio = np.concatenate([audio, pad])
        with self._lock:
            if immediate:
                self._voice_queue.clear()
                self._voice_pos = 0
            self._voice_queue.append(audio)
        return True

    def speak_clear(self):
        """Cancel any in-flight speech and clear all queued utterances."""
        with self._lock:
            self._voice_queue.clear()
            self._voice_pos = 0

    def prewarm(self, text: str) -> bool:
        """Synthesize `text` and cache it without playing. Useful at
        session start to avoid first-utterance synthesis stalls."""
        return self._voice.synthesize(text) is not None

    def chime(self, freq_hz: float = 880.0, duration_sec: float = 0.4):
        with self._lock:
            self._chime_remaining = int(duration_sec * self.sr)
            self._chime_freq      = freq_hz
            self._chime_phase     = 0.0

    def snapshot(self) -> AudioStateSnapshot:
        with self._lock:
            p = self._cur_profile
            return AudioStateSnapshot(
                focus_level=self._focus_level,
                focus_name=p.name,
                delta_f=p.delta_f,
                carrier=p.carrier,
                pink_level=p.pink_level,
                volume=self._cur_volume,
                speaking=bool(self._voice_queue),
                chime_active=self._chime_remaining > 0)

    # ---- audio callback -----------------------------------------------

    def _audio_callback(self, outdata: np.ndarray, frames: int, t, status):
        block_dur = frames / self.sr

        with self._lock:
            cur_prof    = self._cur_profile
            tgt_prof    = self._target_profile
            blend_start = self._blend
            target_vol  = self._target_volume
            cur_vol     = self._cur_volume
            vol_step    = block_dur / max(self.vol_glide_sec, 0.01)

            # Volume interpolation
            if cur_vol < target_vol:
                cur_vol = min(target_vol, cur_vol + vol_step)
            elif cur_vol > target_vol:
                cur_vol = max(target_vol, cur_vol - vol_step)
            self._cur_volume = cur_vol

            # Blend (crossfade) interpolation. blend decreases from 1 → 0
            # over self.beat_glide_sec; when it hits 0, the target profile
            # becomes the current and blend resets to 1 for next change.
            blend_step = block_dur / max(self.beat_glide_sec, 0.01)
            if cur_prof is not tgt_prof:
                new_blend = max(0.0, blend_start - blend_step)
                self._blend = new_blend
                if new_blend == 0.0:
                    self._cur_profile = tgt_prof
                    self._blend = 1.0
                    # Also reset the "target" phase accumulators back into
                    # the main ones for the next transition
                    self._phases_l = self._phases_l_target.copy()
                    self._phases_r = self._phases_r_target.copy()
                    self._am_phase = self._am_phase_target
            else:
                new_blend = 1.0

            # Snapshot for the synthesis path (release lock before heavy work)
            phases_l   = self._phases_l.copy()
            phases_r   = self._phases_r.copy()
            phases_l_t = self._phases_l_target.copy()
            phases_r_t = self._phases_r_target.copy()
            am_phase   = self._am_phase
            am_phase_t = self._am_phase_target

            voice_queue = list(self._voice_queue)     # shallow copy
            voice_pos   = self._voice_pos
            voice_initial_len = len(voice_queue)
            chime_rem   = self._chime_remaining
            chime_freq  = self._chime_freq
            chime_phase = self._chime_phase

        # ---- generate current-profile layer ----
        cur_buf, new_phases_l, new_phases_r, new_am_phase = \
            self._synth_profile(cur_prof, frames, phases_l, phases_r, am_phase)

        # ---- generate target-profile layer if mid-crossfade ----
        if cur_prof is not tgt_prof:
            tgt_buf, new_phases_l_t, new_phases_r_t, new_am_phase_t = \
                self._synth_profile(tgt_prof, frames, phases_l_t, phases_r_t,
                                     am_phase_t)
            mix = new_blend * cur_buf + (1.0 - new_blend) * tgt_buf
        else:
            tgt_buf, new_phases_l_t, new_phases_r_t, new_am_phase_t = \
                cur_buf, phases_l_t, phases_r_t, am_phase_t
            mix = cur_buf

        # ---- apply master volume ----
        mix *= cur_vol

        # ---- voice overlay (mono → stereo, consume queue across the block) ----
        # The voice queue may roll over from one utterance to the next within
        # a single audio block; loop until we've filled `frames` samples or
        # the queue empties.
        if voice_queue:
            written = 0
            while written < frames and voice_queue:
                cur = voice_queue[0]
                remaining = len(cur) - voice_pos
                if remaining <= 0:
                    voice_queue.pop(0)
                    voice_pos = 0
                    continue
                n = min(frames - written, remaining)
                chunk = cur[voice_pos:voice_pos + n] * self.voice_volume
                mix[written:written + n, 0] += chunk
                mix[written:written + n, 1] += chunk
                voice_pos += n
                written   += n
                if voice_pos >= len(cur):
                    voice_queue.pop(0)
                    voice_pos = 0

        # ---- chime overlay ----
        if chime_rem > 0:
            n = min(frames, chime_rem)
            t_arr = np.arange(n, dtype=np.float32) / self.sr
            envelope = np.linspace(1.0, 0.0, n, dtype=np.float32) ** 2
            chime = 0.30 * envelope * np.sin(
                2 * np.pi * chime_freq * t_arr + chime_phase)
            mix[:n, 0] += chime
            mix[:n, 1] += chime
            new_chime_phase = (chime_phase + 2 * np.pi * chime_freq * n / self.sr) % (2 * np.pi)
            new_chime_rem   = chime_rem - n
        else:
            new_chime_phase = chime_phase
            new_chime_rem   = 0

        # ---- soft-clip to prevent overdrive on layered peaks ----
        np.tanh(mix, out=mix)

        outdata[:] = mix

        # ---- write back phase accumulators ----
        with self._lock:
            self._phases_l = new_phases_l
            self._phases_r = new_phases_r
            self._phases_l_target = new_phases_l_t
            self._phases_r_target = new_phases_r_t
            self._am_phase  = new_am_phase
            self._am_phase_target = new_am_phase_t
            # Voice sync: we popped `voice_initial_len - len(voice_queue)`
            # items from the head during this block. New items appended by
            # the main thread (via speak()) are at the tail and untouched.
            # speak_clear() would have emptied the live queue entirely; we
            # detect that by checking total size.
            consumed = voice_initial_len - len(voice_queue)
            if consumed > 0 and len(self._voice_queue) >= consumed:
                self._voice_queue = self._voice_queue[consumed:]
            self._voice_pos = voice_pos if self._voice_queue else 0
            self._chime_remaining = new_chime_rem
            self._chime_phase     = new_chime_phase

    # ---- per-profile synthesis ---------------------------------------

    def _synth_profile(self, prof: FocusProfile, frames: int,
                        phases_l: np.ndarray, phases_r: np.ndarray,
                        am_phase: float):
        """Render one block of audio for a single Focus profile.

        Layers, into one (frames, 2) float32 buffer:
            - root carrier as L/R binaural pair
            - 2nd, 3rd, 5th harmonic carriers as binaural pairs at lower vol
            - AM modulation at delta_f
            - pink noise bed
        Returns (buffer, new_phases_l, new_phases_r, new_am_phase).
        """
        out = np.zeros((frames, 2), dtype=np.float32)

        if prof.carrier == 0.0:
            # "off" — just pink noise at the bed level (could be silent too)
            if prof.pink_level > 0:
                noise = self._pink.generate(frames) * prof.pink_level
                out[:, 0] = noise
                out[:, 1] = noise
            return out, phases_l, phases_r, am_phase

        t_arr = np.arange(frames, dtype=np.float64) / self.sr
        block_dur = frames / self.sr

        # ---- AM modulation envelope (cosine, 0..1) ----
        if prof.am_depth > 0 and prof.delta_f > 0:
            am_env = 1.0 - prof.am_depth * 0.5 * (
                1.0 - np.cos(2 * np.pi * prof.delta_f * t_arr + am_phase))
            new_am_phase = (am_phase + 2 * np.pi * prof.delta_f * block_dur) % (2 * np.pi)
        else:
            am_env = np.ones(frames, dtype=np.float64)
            new_am_phase = am_phase

        # ---- 4 oscillators per ear: root + 3 harmonic multipliers ----
        # multipliers: 1x (root), 2x, 3x, 5x
        # amplitudes for harmonics from the profile; root has amplitude 1.
        multipliers = np.array([1.0, 2.0, 3.0, 5.0])
        amps        = np.array([1.0,
                                  prof.harmonic_levels[0],
                                  prof.harmonic_levels[1],
                                  prof.harmonic_levels[2]])

        f_left  = prof.carrier * multipliers
        f_right = (prof.carrier + prof.delta_f) * multipliers

        new_phases_l = phases_l.copy()
        new_phases_r = phases_r.copy()

        left  = np.zeros(frames, dtype=np.float64)
        right = np.zeros(frames, dtype=np.float64)

        for i in range(4):
            if amps[i] <= 0:
                continue
            left  += amps[i] * np.sin(2 * np.pi * f_left[i]  * t_arr + phases_l[i])
            right += amps[i] * np.sin(2 * np.pi * f_right[i] * t_arr + phases_r[i])
            new_phases_l[i] = (phases_l[i] + 2 * np.pi * f_left[i]  * block_dur) % (2 * np.pi)
            new_phases_r[i] = (phases_r[i] + 2 * np.pi * f_right[i] * block_dur) % (2 * np.pi)

        # Normalize so total amplitude per channel ≤ 1, then apply AM env
        total_amp = float(amps.sum())
        if total_amp > 0:
            left  *= (am_env / total_amp) * 0.7
            right *= (am_env / total_amp) * 0.7

        # ---- pink noise bed ----
        if prof.pink_level > 0:
            noise = self._pink.generate(frames) * prof.pink_level
            left  += noise
            right += noise

        out[:, 0] = left.astype(np.float32)
        out[:, 1] = right.astype(np.float32)
        return out, new_phases_l, new_phases_r, new_am_phase


# ---- standalone smoke test -------------------------------------------------
if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        description="CRV layered binaural induction engine smoke test")
    ap.add_argument("--voice", default=DEFAULT_KOKORO_VOICE,
                    help=f"Kokoro voice name (default: {DEFAULT_KOKORO_VOICE}). "
                         "Try af_bella, af_nicole, af_sarah, af_sky, bf_emma.")
    ap.add_argument("--speed", type=float, default=0.9,
                    help="Voice speed multiplier (default: 0.9, slightly slow)")
    ap.add_argument("--volume", type=float, default=0.4,
                    help="Master volume 0..1 (default: 0.4)")
    ap.add_argument("--levels", nargs="+",
                    default=["focus_10", "focus_12", "focus_15",
                             "focus_21", "focus_27", "off"],
                    help="Focus levels to cycle through")
    ap.add_argument("--dwell", type=float, default=15.0,
                    help="Seconds per level (default: 15)")
    ap.add_argument("--download-voices-only", action="store_true",
                    help="Just download Kokoro model files and exit")
    args = ap.parse_args()

    if args.download_voices_only:
        synth = VoiceSynth(voice=args.voice, auto_download=True, verbose=True)
        print(f"\nBackend: {synth.backend_name}")
        sys.exit(0)

    eng = BinauralEngine(beat_glide_sec=8.0,
                          voice=args.voice,
                          voice_speed=args.speed)
    if not eng.start():
        sys.exit(f"sounddevice unavailable: {eng.availability_error}")
    eng.set_volume(args.volume)

    print(f"\n  Voice backend: {eng.voice_backend}")
    print(f"  Master volume: {args.volume*100:.0f}%")
    print(f"  Levels: {args.levels}")
    print(f"  Dwell: {args.dwell}s each\n")

    try:
        for level in args.levels:
            if level not in FOCUS_PROFILES:
                print(f"  (skipping unknown level {level!r})")
                continue
            prof = FOCUS_PROFILES[level]
            print(f"→ {prof.name}  (Δf={prof.delta_f} Hz, "
                  f"carrier={prof.carrier} Hz)")
            eng.set_focus(level)
            time.sleep(args.dwell)
    finally:
        eng.stop()
