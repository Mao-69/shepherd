"""crv/ui.py — Live session dashboard.

Compact Rich layout designed to be glanceable from a meditation cushion:

    ┌─ CRV Research — Session 2026-05-25_14-30-15 ──────────────────┐
    │  Stage III — Dimensions                  04:23 / 10:00         │
    │  ───────────────────────────────────────────────────────────   │
    │   Sketch the dimensional aspects of the target. Size           │
    │   relative to your body? Shape? Mass? Distance?                │
    │                                                                │
    │  ┌─ Heart Rate ───────────────────┐  ┌─ State ────────────┐    │
    │  │  ♥ 68 bpm                      │  │  COHERENT           │    │
    │  │  ▆▆▅▅▅▄▃▃▃▃▃▃▃▃▃               │  │  3:14 coherent      │    │
    │  │  60s: min 65  max 72  avg 67   │  │  Focus: focus_12    │    │
    │  └────────────────────────────────┘  └────────────────────┘    │
    │                                                                │
    │  BP 120/80   SpO₂ 98%   Bat 36%       baseline 75±4 bpm        │
    │                                                                │
    │  [Space] advance · [p] pause · [m] re-measure · [q] quit       │
    └────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from rich.align import Align
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import protocol as proto


# =============================================================================
# UIState — single source of truth for the dashboard
# =============================================================================

@dataclass
class UIState:
    session_id:        str   = ""
    stage_idx:         int   = 0
    stage_elapsed:     float = 0.0           # seconds in current stage
    stage_duration:    float = 1.0           # full duration of current stage
    paused:            bool  = False

    # Bio
    current_bpm:       Optional[int]    = None
    hr_history:        deque            = field(default_factory=lambda: deque(maxlen=120))
    baseline_bpm:      Optional[float]  = None
    baseline_std:      Optional[float]  = None
    state_label:       str              = "baseline"
    coherent_seconds:  int              = 0
    systolic:          Optional[int]    = None
    diastolic:         Optional[int]    = None
    spo2:              Optional[int]    = None
    battery:           Optional[int]    = None

    # Audio
    audio_available:   bool  = False
    audio_focus:       str   = "off"
    audio_volume:      float = 0.0
    audio_beat:        float = 0.0

    # Status text
    status_message:    str   = ""

    @property
    def stage(self):
        if 0 <= self.stage_idx < len(proto.STAGES):
            return proto.STAGES[self.stage_idx]
        return None


# =============================================================================
# Helpers
# =============================================================================

def _fmt_clock(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


def _hr_spark(values, width: int = 30) -> str:
    if not values:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    if len(values) > width:
        step = len(values) / width
        sampled = [values[int(i * step)] for i in range(width)]
    else:
        sampled = values
    mn, mx = min(sampled), max(sampled)
    span = max(1, mx - mn)
    return "".join(blocks[min(7, int((v - mn) / span * 7))]
                   for v in sampled)


def _state_styling(label: str):
    """Return (display_text, style) for a state label."""
    return {
        "baseline": ("BASELINE",  "bold cyan"),
        "coherent": ("COHERENT",  "bold green"),
        "active":   ("ACTIVE",    "bold yellow"),
        "stressed": ("STRESSED",  "bold red"),
    }.get(label, (label.upper(), "white"))


# =============================================================================
# Renderable
# =============================================================================

def make_dashboard(state: UIState) -> Layout:
    layout = Layout()
    layout.split(
        Layout(name="header",     size=3),
        Layout(name="stage_info", ratio=2, minimum_size=6),
        Layout(name="bio",        ratio=3, minimum_size=8),
        Layout(name="footer_bar", size=3),
        Layout(name="hotkeys",    size=3),
    )

    # ----- Header --------------------------------------------------------
    hdr = Table.grid(expand=True)
    hdr.add_column(justify="left",  ratio=1)
    hdr.add_column(justify="right", ratio=1)
    title = Text("CRV Research", style="bold cyan")
    title.append(f"   session {state.session_id}", style="dim")
    paused = Text("PAUSED", style="bold yellow") if state.paused else Text("LIVE", style="bold green")
    hdr.add_row(title, paused)
    layout["header"].update(Panel(hdr, border_style="cyan"))

    # ----- Stage info ----------------------------------------------------
    stage = state.stage
    stage_panel: Panel
    if stage is None:
        stage_panel = Panel(Text("(no stage)", style="dim"))
    else:
        clock = Text()
        clock.append(stage.name, style="bold magenta")
        clock.append("   ", style="")
        clock.append(_fmt_clock(state.stage_elapsed), style="bold")
        clock.append(" / ", style="dim")
        clock.append(_fmt_clock(stage.duration_sec), style="dim")
        clock.append(f"   (Focus: {stage.focus_level})", style="dim")

        # progress bar
        pct = min(1.0, state.stage_elapsed / max(1.0, stage.duration_sec))
        bar_w = 50
        filled = int(bar_w * pct)
        bar = Text()
        bar.append("█" * filled, style="magenta")
        bar.append("░" * (bar_w - filled), style="grey30")

        desc = Text(stage.description, style="white")

        stage_panel = Panel(
            Group(clock, Text(""), bar, Text(""), desc),
            border_style="magenta",
            title=Text(f"Stage {state.stage_idx + 1}/{len(proto.STAGES)}",
                       style="bold"),
        )
    layout["stage_info"].update(stage_panel)

    # ----- Bio row: HR panel + State panel ------------------------------
    bio_row = Layout()
    bio_row.split_row(
        Layout(name="hr",    ratio=2),
        Layout(name="state", ratio=1),
    )

    # HR panel
    hr_lines = []
    if state.current_bpm is not None:
        bpm_text = Text("♥ ", style="bold red blink")
        bpm_text.append(f"{state.current_bpm}", style="bold red")
        bpm_text.append(" bpm", style="dim")
        hr_lines.append(bpm_text)
    else:
        hr_lines.append(Text("♥ --- bpm", style="dim"))

    if state.hr_history:
        spark = _hr_spark(list(state.hr_history), width=40)
        hr_lines.append(Text(spark, style="red"))
        recent = list(state.hr_history)[-60:]
        if recent:
            stats = f"60s window: min {min(recent)}  max {max(recent)}  avg {sum(recent)//len(recent)}"
            hr_lines.append(Text(stats, style="dim"))

    if state.baseline_bpm is not None:
        bs_line = Text()
        bs_line.append(f"baseline {state.baseline_bpm:.0f}", style="dim")
        if state.baseline_std is not None:
            bs_line.append(f" ±{state.baseline_std:.1f}", style="dim")
        bs_line.append(" bpm", style="dim")
        hr_lines.append(bs_line)

    bio_row["hr"].update(Panel(Group(*hr_lines),
                                title="Heart Rate",
                                border_style="red"))

    # State panel
    state_text, state_style = _state_styling(state.state_label)
    state_lines = [
        Text(state_text, style=state_style, justify="center"),
        Text(""),
    ]
    if state.coherent_seconds > 0:
        state_lines.append(Text(
            f"{_fmt_clock(state.coherent_seconds)} coherent",
            style="green", justify="center"))
    state_lines.append(Text(
        f"Focus  {state.audio_focus}",
        style="dim", justify="center"))
    state_lines.append(Text(
        f"beat  {state.audio_beat:.1f} Hz",
        style="dim", justify="center"))

    # Audio status (helpful so users know if their headphones should be working)
    if not state.audio_available:
        audio_txt = Text("🔇 audio unavailable", style="bold red", justify="center")
    elif state.audio_focus == "off" or state.audio_volume < 0.01:
        audio_txt = Text("🔈 audio silent", style="dim", justify="center")
    else:
        audio_txt = Text(
            f"🔊 audio @ {int(state.audio_volume*100)}%",
            style="bold green", justify="center")
    state_lines.append(audio_txt)
    bio_row["state"].update(Panel(Group(*state_lines),
                                   title="State",
                                   border_style=state_style.split()[-1] if state_style else "cyan"))

    layout["bio"].update(bio_row)

    # ----- Footer bar with secondary vitals -----------------------------
    foot = Table.grid(expand=True, padding=(0, 2))
    foot.add_column(justify="center", ratio=1)
    foot.add_column(justify="center", ratio=1)
    foot.add_column(justify="center", ratio=1)
    foot.add_column(justify="center", ratio=2)

    def vital(label: str, value: str, style: str):
        t = Text()
        t.append(label + "  ", style="dim")
        t.append(value, style=style)
        return Align.center(t)

    bp_str = (f"{state.systolic}/{state.diastolic}"
              if state.systolic and state.diastolic else "—/—")
    spo2_str = f"{state.spo2}%" if state.spo2 else "—"
    bat_str  = f"{state.battery}%" if state.battery is not None else "—"

    foot.add_row(
        vital("BP",   bp_str,   "magenta"),
        vital("SpO₂", spo2_str, "blue"),
        vital("Bat",  bat_str,  "green"),
        Text(state.status_message, style="dim italic"),
    )
    layout["footer_bar"].update(Panel(foot, border_style="dim"))

    # ----- Hotkeys -------------------------------------------------------
    keys = Text(justify="center")
    keys.append("[space]", style="bold"); keys.append(" advance  ")
    keys.append("[p]",     style="bold"); keys.append(" pause  ")
    keys.append("[m]",     style="bold"); keys.append(" re-measure  ")
    keys.append("[n]",     style="bold"); keys.append(" mark event  ")
    keys.append("[q]",     style="bold"); keys.append(" quit")
    layout["hotkeys"].update(Panel(keys, border_style="dim"))

    return layout
