"""crv/biometrics.py — HR baseline, rolling stats, coherence + HRV estimation.

Two-tier biometric analysis:

    1. Bpm-derived (always available): mean, std-dev, label classification.
       Used live for the coherent/active/stressed dashboard indicator.

    2. RR-interval-derived (estimated from bpm samples): pseudo-RMSSD,
       pseudo-SDNN. Approximate, since the watch only reports bpm and not
       true beat-to-beat intervals. Useful for relative comparisons
       within a session and across sessions; not for clinical claims.

If the watch is later replaced with one that exposes RR intervals
directly (chest straps via BLE Heart Rate Service notification flag
bit 4 = "RR-Interval present"), call `record_rr_ms(ms)` instead of
`record(bpm)` and the same downstream consumers work unchanged.
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Tuple


@dataclass
class BioState:
    baseline_bpm:     Optional[float] = None
    baseline_std:     Optional[float] = None
    baseline_rmssd:   Optional[float] = None
    current_bpm:      Optional[int]   = None
    rolling_mean:     Optional[float] = None
    rolling_std:      Optional[float] = None
    rolling_rmssd:    Optional[float] = None
    rolling_sdnn:     Optional[float] = None
    state_label:      str             = "baseline"
    coherent_seconds: int             = 0
    samples:          Deque[Tuple[float, int]]   = field(
        default_factory=lambda: deque(maxlen=7200))     # 2 hours @ 1 Hz
    rr_intervals_ms:  Deque[Tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=7200))     # (timestamp, ms)


class BiometricsTracker:
    ROLLING_WINDOW_SEC = 30      # how much recent data feeds rolling stats
    HRV_MIN_INTERVALS  = 5       # need at least this many RRs for RMSSD

    def __init__(self):
        self.state = BioState()
        self._coherent_started: Optional[float] = None

    # ---- input ---------------------------------------------------------
    def record(self, bpm: int):
        """Record a heart-rate sample (bpm). Also estimates the implied
        RR interval (60000/bpm ms) and stashes it for HRV estimation."""
        self.state.current_bpm = bpm
        now = time.time()
        self.state.samples.append((now, bpm))
        if bpm > 0:
            self.state.rr_intervals_ms.append((now, 60000.0 / bpm))
        self._update_rolling()
        self._update_label()

    def record_rr_ms(self, ms: float):
        """For watches that expose real RR intervals (chest straps).
        Pushes the interval directly and derives bpm from it."""
        now = time.time()
        self.state.rr_intervals_ms.append((now, ms))
        if ms > 0:
            bpm = int(round(60000.0 / ms))
            self.state.current_bpm = bpm
            self.state.samples.append((now, bpm))
        self._update_rolling()
        self._update_label()

    def lock_baseline(self, last_n_seconds: int = 180):
        """Call after baseline-collection period."""
        cutoff = time.time() - last_n_seconds
        recent: List[int] = [v for t, v in self.state.samples if t >= cutoff]
        if not recent:
            return
        self.state.baseline_bpm = statistics.mean(recent)
        self.state.baseline_std = (statistics.pstdev(recent)
                                    if len(recent) > 1 else 0.0)
        # Pseudo-RMSSD baseline
        recent_rr = [ms for t, ms in self.state.rr_intervals_ms if t >= cutoff]
        if len(recent_rr) >= self.HRV_MIN_INTERVALS:
            self.state.baseline_rmssd = _rmssd(recent_rr)

    # ---- accessors for the UI ------------------------------------------
    def stage_stats(self, since_ts: float):
        recent = [v for t, v in self.state.samples if t >= since_ts]
        if not recent:
            return {"count": 0, "min": None, "max": None,
                    "mean": None, "rmssd": None, "sdnn": None}
        recent_rr = [ms for t, ms in self.state.rr_intervals_ms if t >= since_ts]
        return {
            "count": len(recent),
            "min":   min(recent),
            "max":   max(recent),
            "mean":  statistics.mean(recent),
            "rmssd": _rmssd(recent_rr) if len(recent_rr) >= self.HRV_MIN_INTERVALS else None,
            "sdnn":  statistics.pstdev(recent_rr) if len(recent_rr) > 1 else None,
        }

    # ---- internal ------------------------------------------------------
    def _update_rolling(self):
        cutoff = time.time() - self.ROLLING_WINDOW_SEC
        recent: List[int] = [v for t, v in self.state.samples if t >= cutoff]
        if not recent:
            return
        self.state.rolling_mean = statistics.mean(recent)
        self.state.rolling_std  = (statistics.pstdev(recent)
                                    if len(recent) > 1 else 0.0)

        recent_rr = [ms for t, ms in self.state.rr_intervals_ms if t >= cutoff]
        if len(recent_rr) >= self.HRV_MIN_INTERVALS:
            self.state.rolling_rmssd = _rmssd(recent_rr)
            self.state.rolling_sdnn  = statistics.pstdev(recent_rr)

    def _update_label(self):
        bs = self.state.baseline_bpm
        if bs is None or self.state.rolling_mean is None:
            self.state.state_label = "baseline"
            return
        cur_mean = self.state.rolling_mean
        cur_std  = self.state.rolling_std or 0.0
        bs_std   = self.state.baseline_std or 5.0

        if cur_mean < bs - 2 and cur_std < max(2.0, bs_std * 0.7):
            self.state.state_label = "coherent"
            if self._coherent_started is None:
                self._coherent_started = time.time()
            self.state.coherent_seconds = int(time.time() - self._coherent_started)
            return
        if cur_mean > bs + 5 and cur_std > bs_std * 1.5:
            self.state.state_label = "stressed"
        elif cur_mean > bs + 3:
            self.state.state_label = "active"
        else:
            self.state.state_label = "baseline"
        self._coherent_started = None
        self.state.coherent_seconds = 0


# ---- standard HRV math ----

def _rmssd(rr_ms: List[float]) -> float:
    """Root mean square of successive differences. Standard HRV time-domain
    metric, sensitive to parasympathetic tone."""
    if len(rr_ms) < 2:
        return 0.0
    diffs = [(rr_ms[i+1] - rr_ms[i]) ** 2 for i in range(len(rr_ms) - 1)]
    return (sum(diffs) / len(diffs)) ** 0.5
