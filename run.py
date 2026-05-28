#!/usr/bin/env python3
"""CRV Research — session runner.

Examples:
    python run.py                                        # interactive prompts
    python run.py --coord "374-2856 9"                   # explicit coord
    python run.py --pool ./targets                       # blinded draw from pool
    python run.py --pool ./targets --extended            # 95 min extended protocol
    python run.py --intention "investigate baseline drift"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from crv.session import Session
from crv.targets import is_valid_coord



def prompt_target() -> str:
    print()
    print("Enter a target identifier label for this session.")
    print("(This is a human-readable note; the actual blinded coordinate")
    print(" will be assigned automatically below.)")
    print()
    try:
        return input("Target label [optional, e.g. 'wikipool-A']: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def prompt_intention() -> str:
    print()
    print("Enter a one-line intention statement for this session.")
    print("Examples:")
    print("  'investigate whether Focus 12 is more stable than Focus 15 for me'")
    print("  'test whether Kokoro voice is less intrusive than espeak'")
    print("  'just relaxing, no specific goal'")
    print()
    try:
        return input("Intention: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def main():
    p = argparse.ArgumentParser(
        description="Run a CRV research session with biometric + binaural induction")
    p.add_argument("--mac", default=None,
                   help="Watch MAC address. If omitted, the watch is "
                        "auto-discovered by Bluetooth scan. Use this to skip "
                        "discovery when you know the address.")
    p.add_argument("--watch", choices=["yes", "no", "auto"], default="auto",
                   help="'auto' (default): discover a watch, or pass --mac. "
                        "'yes': require a watch (same as auto). "
                        "'no': run watch-less — skip all biometrics and "
                        "vitals, go straight into the guided CRV session.")
    p.add_argument("--target", default=None,
                   help="Target label (free-form). If omitted, will prompt.")
    p.add_argument("--coord", default=None,
                   help="Explicit target coordinate (SRI-style, e.g. '374-2856 9'). "
                        "If omitted and --pool is given, drawn from the pool. "
                        "If omitted entirely, randomly generated.")
    p.add_argument("--pool", default=None,
                   help="Path to target pool directory (default: no pool). "
                        "Build one first with `python -m crv.targets fetch`.")
    p.add_argument("--intention", default=None,
                   help="Pre-session intention statement. Prompted if omitted.")
    p.add_argument("--coord-interval", type=int, default=180,
                   help="Seconds between coordinate re-vocalizations during "
                        "active CRV stages (default: 180 = 3 min)")
    p.add_argument("--volume", type=float, default=0.35,
                   help="Initial binaural volume 0.0-1.0 (default: 0.35)")
    p.add_argument("--voice", default="af_heart",
                   help="Kokoro voice (default: af_heart). Try af_bella, "
                        "af_nicole, af_sarah, af_sky, bf_emma.")
    p.add_argument("--voice-speed", type=float, default=0.9)
    p.add_argument("--editor", default=None,
                   help="Auto-launch editor on notes.md (NOT recommended; "
                        "the editor will steal stdin from the dashboard).")
    p.add_argument("--sec-level", default="medium",
                   choices=["low", "medium", "high"])
    p.add_argument("--sessions-dir", default="sessions",
                   help="Output root for session folders (default: sessions)")
    p.add_argument("--protocol",
                   choices=["short", "standard", "long"],
                   default="standard",
                   help="Protocol timing tier. short=~60min, "
                        "standard=~95-150min (literature-grounded, default), "
                        "long=~150-220min (full SRI/Monroe spec)")
    p.add_argument("--extended", action="store_true",
                   help="Add Focus 21-27 journey stages before cooldown "
                        "(adds ~30-60 min depending on tier)")
    p.add_argument("--debug-log", default=None)
    args = p.parse_args()

    from crv import protocol
    if args.protocol == "short":
        protocol.use_short()
    elif args.protocol == "long":
        protocol.use_long()
    else:
        protocol.use_standard()
    if args.extended:
        protocol.use_extended(True)
    print(f"[Protocol: {args.protocol}"
          f"{' + extended' if args.extended else ''}: "
          f"{len(protocol.STAGES)} stages, "
          f"~{protocol.current_total_minutes()} min total]")

    sessions_root = Path(args.sessions_dir).resolve()
    sessions_root.mkdir(parents=True, exist_ok=True)

    pool_root = Path(args.pool).resolve() if args.pool else None
    if pool_root and not pool_root.exists():
        print(f"Pool directory not found: {pool_root}")
        print(f"Build one with: python -m crv.targets fetch --root {pool_root}")
        return 1

    if args.coord and not is_valid_coord(args.coord):
        print(f"Invalid coordinate format: {args.coord!r}")
        return 1

    target = args.target if args.target is not None else prompt_target()
    intention = args.intention if args.intention is not None else prompt_intention()
    editor = args.editor

    use_watch = (args.watch != "no")
    if not use_watch:
        print("[Watch disabled — running guided CRV session with no biometrics]")

    session = Session(
        mac=args.mac,
        sessions_root=sessions_root,
        target_id=target,
        coord=args.coord,
        intention=intention,
        pool_root=pool_root,
        coord_interval_sec=args.coord_interval,
        use_watch=use_watch,
        editor=editor,
        auto_launch_editor=(editor is not None),
        sec_level=args.sec_level,
        audio_volume=args.volume,
        voice=args.voice,
        voice_speed=args.voice_speed,
        debug_log=args.debug_log,
    )

    try:
        asyncio.run(session.run())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
