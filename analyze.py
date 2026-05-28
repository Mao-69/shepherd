#!/usr/bin/env python3
"""analyze.py — Multi-session aggregate analysis for CRV Research sessions.

Walks the sessions/ directory and produces:
    - aggregate.csv      One row per session with summary metrics
    - aggregate.md       Human-readable summary of hit/miss rates and trends
    - per-stage charts   PNG of HR around each stage transition (matplotlib)
    - drift.png          Baseline HR over session date — visualize practice
                          drift over time

Usage:
    python analyze.py                              # default: sessions/, output to ./analysis/
    python analyze.py --sessions ./sessions --out ./analysis
    python analyze.py --no-plots                   # skip matplotlib output

This is a research aid, not a statistical inference tool. With small
session counts (n<20) the hit/miss rates have very wide confidence
intervals; treat them as observations, not p-values.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def load_session(session_dir: Path) -> Optional[Dict[str, Any]]:
    """Read all artifacts from one session directory; return summary dict
    or None if the session is incomplete."""
    meta_p = session_dir / "metadata.json"
    if not meta_p.exists():
        return None
    try:
        meta = json.loads(meta_p.read_text())
    except Exception:
        return None

    feedback = None
    fb_p = session_dir / "feedback.json"
    if fb_p.exists():
        try:
            feedback = json.loads(fb_p.read_text())
        except Exception:
            pass

    # Load HR samples
    hr_samples: List[tuple] = []
    hr_p = session_dir / "heartrate.csv"
    if hr_p.exists():
        with hr_p.open() as f:
            for row in csv.DictReader(f):
                try:
                    hr_samples.append((row["timestamp"], int(row["bpm"]),
                                         row["state"]))
                except (KeyError, ValueError):
                    continue

    # Stage transitions
    stages: List[tuple] = []
    st_p = session_dir / "stage_transitions.csv"
    if st_p.exists():
        with st_p.open() as f:
            for row in csv.DictReader(f):
                stages.append((row["timestamp"], row["stage_code"],
                                row["stage_name"], row["focus_level"]))

    return {
        "id":            session_dir.name,
        "dir":           session_dir,
        "meta":          meta,
        "feedback":      feedback,
        "hr_count":      len(hr_samples),
        "hr_min":        min((v for _, v, _ in hr_samples), default=None),
        "hr_max":        max((v for _, v, _ in hr_samples), default=None),
        "hr_mean":       statistics.mean(v for _, v, _ in hr_samples) if hr_samples else None,
        "baseline_bpm":  meta.get("baseline_bpm"),
        "baseline_std":  meta.get("baseline_std"),
        "baseline_rmssd": meta.get("baseline_rmssd"),
        "coherent_sec":  meta.get("total_coherent_seconds", 0),
        "stage_count":   len(stages),
        "verdict":       feedback.get("verdict") if feedback else None,
        "real_rank":     feedback.get("real_target_rank") if feedback else None,
        "started_at":    meta.get("started_at"),
        "coord":         meta.get("coord"),
    }


def write_aggregate_csv(sessions: List[Dict[str, Any]], out_path: Path):
    fields = ["id", "started_at", "coord", "hr_count", "hr_mean",
              "hr_min", "hr_max", "baseline_bpm", "baseline_std",
              "baseline_rmssd", "coherent_sec", "verdict", "real_rank"]
    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for s in sessions:
            w.writerow([s.get(k) for k in fields])


def write_aggregate_md(sessions: List[Dict[str, Any]], out_path: Path):
    hits         = [s for s in sessions if s["verdict"] == "hit"]
    partial      = [s for s in sessions if s["verdict"] == "partial_hit"]
    misses       = [s for s in sessions if s["verdict"] == "miss"]
    judged       = hits + partial + misses
    unjudged     = [s for s in sessions if s["verdict"] is None]

    with out_path.open("w") as f:
        f.write("# CRV Research — Aggregate Analysis\n\n")
        f.write(f"Generated: {datetime.now().isoformat(timespec='seconds')}\n\n")
        f.write(f"**Total sessions:** {len(sessions)}  \n")
        f.write(f"**Judged (with feedback):** {len(judged)}  \n")
        f.write(f"**Unjudged:** {len(unjudged)}  \n\n")

        if judged:
            hit_rate = len(hits) / len(judged) * 100
            partial_rate = len(partial) / len(judged) * 100
            f.write(f"## Hit / miss rates\n\n")
            f.write(f"- **Hits (rank 1 of 4):** {len(hits)} ({hit_rate:.1f}%)  ")
            f.write(f"_(chance = 25.0%)_\n")
            f.write(f"- **Partial hits (rank 2 of 4):** {len(partial)} ({partial_rate:.1f}%)\n")
            f.write(f"- **Misses (rank 3-4 of 4):** {len(misses)} "
                    f"({len(misses)/len(judged)*100:.1f}%)\n\n")
            f.write(f"With {len(judged)} sessions, the 95% CI on a 25% null hit "
                    f"rate is approximately ±{(1.96*((0.25*0.75)/len(judged))**0.5)*100:.1f} "
                    f"percentage points. Confidence intervals are wide at small n.\n\n")

        # Baseline drift
        baselines = [(s["started_at"], s["baseline_bpm"])
                     for s in sessions if s.get("baseline_bpm")]
        if baselines:
            baselines.sort()
            f.write("## Baseline HR over time\n\n")
            f.write("| Session | Baseline (bpm) | Coherent time |\n")
            f.write("|---------|---------------|---------------|\n")
            for s in sorted(sessions, key=lambda x: x.get("started_at") or ""):
                bs = s.get("baseline_bpm")
                cs = s.get("coherent_sec", 0)
                f.write(f"| {s['id']} | "
                        f"{bs:.1f if bs else '—'} | "
                        f"{cs//60} min |\n")
            f.write("\n")

        # Session table
        f.write("## All sessions\n\n")
        f.write("| Session | Coord | Verdict | Rank | Baseline | Coherent |\n")
        f.write("|---------|-------|---------|------|---------|---------|\n")
        for s in sorted(sessions, key=lambda x: x.get("started_at") or ""):
            verdict = s.get("verdict") or "—"
            rank = s.get("real_rank")
            bs = s.get("baseline_bpm")
            cs = s.get("coherent_sec", 0)
            f.write(f"| {s['id']} | `{s.get('coord', '—')}` | "
                    f"{verdict} | {rank if rank is not None else '—'} | "
                    f"{bs:.0f if bs else '—'} bpm | "
                    f"{cs//60} min |\n")
        f.write("\n")

        if unjudged:
            f.write(f"\n## Sessions without feedback\n\n")
            f.write(f"{len(unjudged)} sessions ran without target reveal / ranking. "
                    "Add targets with `python -m crv.targets fetch` and enable the "
                    "pool with `python run.py --pool ./targets` to get blinded "
                    "feedback on future sessions.\n")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sessions", default="sessions",
                   help="Sessions root directory")
    p.add_argument("--out", default="analysis",
                   help="Output directory for analysis artifacts")
    p.add_argument("--no-plots", action="store_true",
                   help="Skip matplotlib plot generation")
    args = p.parse_args()

    sessions_root = Path(args.sessions)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not sessions_root.exists():
        print(f"No sessions directory: {sessions_root}")
        return 1

    sessions = []
    for sd in sorted(sessions_root.iterdir()):
        if not sd.is_dir():
            continue
        info = load_session(sd)
        if info:
            sessions.append(info)

    if not sessions:
        print(f"No valid sessions found under {sessions_root}")
        return 1

    print(f"Loaded {len(sessions)} sessions")

    write_aggregate_csv(sessions, out_dir / "aggregate.csv")
    write_aggregate_md (sessions, out_dir / "aggregate.md")
    print(f"  Wrote {out_dir/'aggregate.csv'}")
    print(f"  Wrote {out_dir/'aggregate.md'}")

    if not args.no_plots:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            _plot_baseline_drift(sessions, out_dir / "drift.png", plt)
            _plot_hit_rate(sessions, out_dir / "hit_rate.png", plt)
            print(f"  Wrote plots in {out_dir}/")
        except ImportError:
            print("  matplotlib not installed; skipping plots "
                  "(pip install matplotlib)")
        except Exception as e:
            print(f"  Plot generation failed: {e}")

    return 0


def _plot_baseline_drift(sessions, out_path, plt):
    data = [(s["started_at"], s["baseline_bpm"])
            for s in sessions if s.get("baseline_bpm")]
    if not data:
        return
    data.sort()
    labels = [d[0][:10] for d in data]
    values = [d[1] for d in data]
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(range(len(values)), values, marker="o")
    ax.set_xticks(range(len(values)))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Baseline HR (bpm)")
    ax.set_title("Baseline heart rate across sessions")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def _plot_hit_rate(sessions, out_path, plt):
    judged = [s for s in sessions if s.get("verdict")]
    if not judged:
        return
    counts = {"hit": 0, "partial_hit": 0, "miss": 0}
    for s in judged:
        v = s["verdict"]
        if v in counts:
            counts[v] += 1
    fig, ax = plt.subplots(figsize=(6, 4))
    labels = ["Hit\n(rank 1)", "Partial\n(rank 2)", "Miss\n(rank 3-4)"]
    values = [counts["hit"], counts["partial_hit"], counts["miss"]]
    colors = ["#4CAF50", "#FFC107", "#F44336"]
    ax.bar(labels, values, color=colors)
    ax.set_ylabel("Sessions")
    ax.set_title(f"Hit rate over {len(judged)} judged sessions "
                  f"(chance: {len(judged)/4:.1f} per category)")
    ax.axhline(y=len(judged)/4, color="black", linestyle="--",
                alpha=0.5, label="Chance line")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


if __name__ == "__main__":
    sys.exit(main())
