"""crv/log.py — Persist session data to disk in research-friendly formats.

Output layout per session:
    sessions/<session_id>/
        metadata.json           — config + summary stats
        heartrate.csv           — timestamp, bpm, state
        stage_transitions.csv   — timestamp, stage_code, stage_name, focus
        audio_events.csv        — timestamp, focus_level, beat_hz, volume
        vitals.csv              — timestamp, kind, value (BP/SpO2 readings)
        notes.md                — user-edited prose notes
        summary.md              — auto-generated post-session report
"""

from __future__ import annotations

import csv
import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def make_session_id(prefix: str = "") -> str:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return f"{prefix}{stamp}" if prefix else stamp


class SessionLogger:
    """One instance per session. Owns the session output directory."""

    def __init__(self, root: Path, session_id: str,
                  metadata: Optional[Dict[str, Any]] = None):
        self.session_id = session_id
        self.dir = root / session_id
        self.dir.mkdir(parents=True, exist_ok=True)

        self._metadata: Dict[str, Any] = {
            "session_id":   session_id,
            "started_at":   datetime.now().isoformat(timespec="seconds"),
            "started_unix": time.time(),
            **(metadata or {}),
        }
        self._save_metadata()

        # Open CSV writers (one per file) for streaming writes
        self._hr_fh    = (self.dir / "heartrate.csv").open("w", newline="")
        self._st_fh    = (self.dir / "stage_transitions.csv").open("w", newline="")
        self._au_fh    = (self.dir / "audio_events.csv").open("w", newline="")
        self._vit_fh   = (self.dir / "vitals.csv").open("w", newline="")
        self._coord_fh = (self.dir / "coordinate_events.csv").open("w", newline="")

        self._hr    = csv.writer(self._hr_fh);    self._hr.writerow(("timestamp", "bpm", "state"))
        self._st    = csv.writer(self._st_fh);    self._st.writerow(("timestamp", "stage_code", "stage_name", "focus_level"))
        self._au    = csv.writer(self._au_fh);    self._au.writerow(("timestamp", "focus_level", "beat_hz", "volume"))
        self._vit   = csv.writer(self._vit_fh);   self._vit.writerow(("timestamp", "kind", "value"))
        self._coord = csv.writer(self._coord_fh); self._coord.writerow(("timestamp", "coord", "kind"))

        # Stage timing for the post-session summary
        self._stage_starts: Dict[str, float] = {}
        self._stage_order:  List[str]        = []

    # ---- single-event writes ----------------------------------------------

    def hr(self, bpm: int, state: str):
        self._hr.writerow((self._iso(), bpm, state))
        self._hr_fh.flush()

    def stage(self, stage_code: str, stage_name: str, focus_level: str):
        self._st.writerow((self._iso(), stage_code, stage_name, focus_level))
        self._st_fh.flush()
        self._stage_starts[stage_code] = time.time()
        self._stage_order.append(stage_code)

    def audio(self, focus_level: str, beat_hz: float, volume: float):
        self._au.writerow((self._iso(), focus_level, f"{beat_hz:.2f}",
                            f"{volume:.3f}"))
        self._au_fh.flush()

    def vitals(self, kind: str, value: str):
        """kind = 'BP' or 'SpO2'; value = the string like '120/80' or '98'."""
        self._vit.writerow((self._iso(), kind, value))
        self._vit_fh.flush()

    def coord_event(self, coord: str, kind: str = "periodic"):
        """Record a coordinate vocalization event. `kind` is 'initial'
        for the Stage I cue or 'periodic' for the repeating anchors."""
        self._coord.writerow((self._iso(), coord, kind))
        self._coord_fh.flush()

    # ---- notes.md helpers -------------------------------------------------

    def notes_path(self) -> Path:
        return self.dir / "notes.md"

    def init_notes(self, header: str):
        if self.notes_path().exists():
            return
        with self.notes_path().open("w") as f:
            f.write(f"# {header}\n\n")
            f.write(f"_Session: {self.session_id}_\n\n")
            md = self._metadata
            f.write(f"**Target coordinate:** `{md.get('coord', '(none)')}`\n\n")
            f.write(f"**Intention:** {md.get('intention', '(none)')}\n\n")
            f.write(f"**Session integrity hash:** "
                    f"`{md.get('session_integrity', '')[:16]}...`\n\n")
            f.write("---\n\n")

    def append_stage_header(self, stage_name: str, prompts: List[str]):
        with self.notes_path().open("a") as f:
            f.write(f"\n## {stage_name}\n\n")
            f.write(f"*Started at {datetime.now().strftime('%H:%M:%S')}*\n\n")
            for p in prompts:
                f.write(f"**{p}**\n\n\n\n")

    # ---- final summary ----------------------------------------------------

    def finalize(self, summary: Dict[str, Any]):
        """Update metadata and produce a markdown summary report."""
        self._metadata.update(summary)
        self._metadata["ended_at"]   = datetime.now().isoformat(timespec="seconds")
        self._metadata["ended_unix"] = time.time()
        self._save_metadata()

        self._write_summary_md()
        for fh in (self._hr_fh, self._st_fh, self._au_fh,
                    self._vit_fh, self._coord_fh):
            try:
                fh.close()
            except Exception:
                pass

    def _write_summary_md(self):
        out = self.dir / "summary.md"
        m = self._metadata
        with out.open("w") as f:
            f.write(f"# Session Summary — {m['session_id']}\n\n")
            f.write(f"- Started: `{m.get('started_at')}`\n")
            f.write(f"- Ended:   `{m.get('ended_at')}`\n")
            if "baseline_bpm" in m and m["baseline_bpm"] is not None:
                f.write(f"- Baseline HR: **{m['baseline_bpm']:.1f} bpm** "
                        f"(σ {m.get('baseline_std', 0):.1f})\n")
            if "final_bp" in m and m["final_bp"]:
                f.write(f"- Final BP: **{m['final_bp']} mmHg**\n")
            if "final_spo2" in m and m["final_spo2"]:
                f.write(f"- Final SpO₂: **{m['final_spo2']}%**\n")
            if "total_coherent_seconds" in m:
                mins = m["total_coherent_seconds"] // 60
                f.write(f"- Time in coherent state: **{mins} min "
                        f"({m['total_coherent_seconds']}s)**\n")
            f.write(f"- Target ID: `{m.get('target_id', '—')}`\n")
            f.write(f"- Actual target (if revealed): `{m.get('actual_target', '—')}`\n")
            f.write("\n## Per-stage HR statistics\n\n")
            f.write("| Stage | n | min | max | mean |\n")
            f.write("|-------|---|-----|-----|------|\n")
            for stage_code, stats in m.get("stage_stats", {}).items():
                if stats and stats.get("count", 0) > 0:
                    f.write(f"| {stage_code} | {stats['count']} | "
                            f"{stats['min']} | {stats['max']} | "
                            f"{stats['mean']:.1f} |\n")
            f.write(f"\nSee `notes.md` for your qualitative session notes.\n")

    def _save_metadata(self):
        with (self.dir / "metadata.json").open("w") as f:
            json.dump(self._metadata, f, indent=2, default=str)

    def _iso(self) -> str:
        return datetime.now().isoformat(timespec="milliseconds")
