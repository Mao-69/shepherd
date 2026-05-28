"""crv/session.py — orchestrates a full CRV research session.

Wires together:
    - WatchDriver         (BLE biometrics input)
    - BiometricsTracker   (baseline, rolling stats, HRV, state classification)
    - BinauralEngine      (audio induction with voice guidance)
    - SessionLogger       (data persistence)
    - TargetPool          (blinded target source)
    - UI dashboard        (live display)

Research-grade additions (beyond basic walkthrough):

    * Coordinate-based target identification (SRI-style 8-digit), spoken
      at Stage I start and periodically during stages I-VI
    * Cryptographic commit-then-reveal: SHA-256 of notes pre-reveal,
      stored in metadata.json so post-hoc edits are detectable
    * Pre-session intention statement (locked at start)
    * Blinded decoy ranking after the session
    * Session integrity lock: hash of protocol + audio config
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import random
import statistics
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.live import Live

from . import protocol as proto
from .audio       import BinauralEngine, FOCUS_PROFILES
from .biometrics  import BiometricsTracker
from .log         import SessionLogger, make_session_id
from .targets     import TargetPool, generate_coordinate, is_valid_coord
from .ui          import UIState, make_dashboard
from .watch       import WatchDriver, discover_watch


# =============================================================================
# Stdin / hotkeys (same pattern as the standalone monitor)
# =============================================================================

@contextlib.contextmanager
def _raw_stdin():
    if not sys.stdin.isatty():
        yield
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


async def _read_keys(queue: asyncio.Queue):
    if not sys.stdin.isatty():
        # No interactive stdin (e.g. piped input). Don't return — that would
        # complete this task and, under asyncio.wait(FIRST_COMPLETED), tear
        # down the whole session. Just idle forever instead.
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        return
    loop = asyncio.get_running_loop()
    def _on_stdin():
        try:
            ch = os.read(sys.stdin.fileno(), 1).decode(errors="ignore")
            if ch:
                queue.put_nowait(ch)
        except Exception:
            pass
    loop.add_reader(sys.stdin.fileno(), _on_stdin)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        loop.remove_reader(sys.stdin.fileno())


# =============================================================================
# Session
# =============================================================================

class Session:
    def __init__(self,
                  mac: Optional[str],
                  sessions_root: Path,
                  target_id: str = "",
                  coord: Optional[str] = None,
                  intention: str = "",
                  pool_root: Optional[Path] = None,
                  coord_interval_sec: int = 180,
                  use_watch: bool = True,
                  editor: Optional[str] = None,
                  auto_launch_editor: bool = False,
                  sec_level: str = "medium",
                  audio_volume: float = 0.35,
                  voice: str = "af_heart",
                  voice_speed: float = 0.9,
                  debug_log: Optional[str] = None):
        self.mac                = mac
        self.use_watch          = use_watch
        self.sec_level          = sec_level
        self.audio_volume       = audio_volume
        self.voice              = voice
        self.voice_speed        = voice_speed
        self.intention          = intention or "(none stated)"
        self.coord_interval_sec = coord_interval_sec
        self.session_id         = make_session_id()

        # Target pool integration. If pool_root is given, we draw a
        # coordinate from the pool when --coord isn't explicitly provided.
        # This is what enables true blinding for solo work.
        self.pool: Optional[TargetPool] = None
        self.coord: Optional[str] = coord
        if pool_root is not None:
            self.pool = TargetPool(pool_root)
            if not self.coord:
                picked = self.pool.pick_random()
                if picked:
                    self.coord = picked

        # Fallback: generate a fresh random coordinate even without a pool,
        # so the viewer always has a "target ID" to anchor to.
        if not self.coord:
            self.coord = generate_coordinate()

        self.logger = SessionLogger(
            sessions_root, self.session_id,
            metadata={
                "target_id":    target_id,
                "coord":        self.coord,
                "mac":          mac or ("none" if not use_watch else "auto-discover"),
                "use_watch":    use_watch,
                "voice":        voice,
                "intention":    self.intention,
                "pool_root":    str(pool_root) if pool_root else None,
                "from_pool":    pool_root is not None and self.coord in (
                    self.pool.coordinates() if self.pool else []),
                "session_integrity": _compute_session_integrity_hash(voice,
                                                                       voice_speed,
                                                                       audio_volume),
            })
        self.editor             = editor
        self.auto_launch_editor = auto_launch_editor and editor is not None
        self.debug_log          = debug_log

        self.bio    = BiometricsTracker()
        self.audio  = BinauralEngine(voice=voice, voice_speed=voice_speed)
        self.ui     = UIState(session_id=self.session_id)
        self.console = Console()

        self._editor_proc: Optional[subprocess.Popen] = None
        self._advance_event = asyncio.Event()
        self._abort_event   = asyncio.Event()

        # Stage timing
        self._stage_started_at: float = 0.0
        self._paused: bool = False
        self._pause_started_at: Optional[float] = None
        self._total_pause_time: float = 0.0
        self._total_coherent_seconds: int = 0
        self._coherent_was_active: bool = False
        self._coherent_segment_start: Optional[float] = None
        self._vitals_in_progress: bool = False
        # Coordinate vocalization
        self._last_coord_spoken_at: float = 0.0
        # Track which stage codes get periodic coordinate repetition
        self._coord_active_stages = {"stage1", "stage2", "stage3",
                                       "stage4", "stage5", "stage6"}

    # ---- main entry point ------------------------------------------------

    async def run(self):
        self.console.print(
            f"[bold cyan]CRV Research[/]   session [bold]{self.session_id}[/]")
        self.console.print(f"Output: [dim]{self.logger.dir}[/]")
        self.console.print(f"Target coordinate: [bold yellow]{self.coord}[/]")
        if self.pool is not None:
            entry = self.pool.get_manifest_entry(self.coord)
            if entry:
                self.console.print(
                    f"[dim]Drawn from pool[/]: {self.pool.root} "
                    f"({len(self.pool.available_coordinates())} available, "
                    f"{len(self.pool.coordinates())} total)")
            else:
                self.console.print(
                    "[dim]Manual coord (not from pool — no reveal possible)[/]")
        else:
            self.console.print(
                "[dim]No target pool configured. "
                "Run [bold]python -m crv.targets fetch[/] to build one.[/]")
        self.console.print(f"Intention: [dim]{self.intention}[/]\n")

        # Prepare notes.md
        self.logger.init_notes(f"CRV Session {self.session_id}")

        # Notes-file instructions (no auto-launch; the editor would steal stdin
        # from the dashboard's keyboard reader and break hotkeys).
        self._show_notes_instructions()

        # Start audio engine and play a brief test chime so the user can
        # verify their headphones are working BEFORE the silent baseline.
        if self.audio.start():
            self.audio.set_volume(self.audio_volume)
            self.ui.audio_available = True
            self.console.print(
                f"[green]✓[/] Audio engine started "
                f"(volume {self.audio_volume*100:.0f}%)")
            self.console.print(
                f"[green]✓[/] Voice backend: [bold]{self.audio.voice_backend}[/]")
            # Pre-synthesize all stage voice prompts so transitions don't
            # stall the audio thread waiting on the TTS model.
            self._prewarm_voices()
            self.console.print(
                "[bold yellow]→ Test tone:[/] you should hear a brief chime and a voice cue. "
                "If you don't, check your headphones and volume.")
            self.audio.chime(freq_hz=440, duration_sec=0.6)
            time.sleep(1.0)
            self.audio.speak("CRV research session ready. Headphones on.")
            time.sleep(3.5)
        else:
            self.ui.audio_available = False
            self.console.print(
                f"[bold red]✗ Audio unavailable:[/] {self.audio.availability_error}\n"
                f"  Install with: [bold]pip install sounddevice[/]\n"
                f"  Session will run silently (no Gateway induction tones)."
            )

        # Watch discovery (unless running watch-less or a MAC was given).
        if self.use_watch and not self.mac:
            self.console.print("\n[bold]Looking for your watch...[/]")
            self.mac = await discover_watch(timeout=12.0, verbose=True)
            if self.mac:
                self.console.print(f"[green]✓[/] Watch found: [bold]{self.mac}[/]")
            else:
                self.console.print(
                    "[yellow]![/] No watch auto-discovered. You can:\n"
                    "    • make sure the watch is awake and bonded (bluetoothctl pair), then retry\n"
                    "    • pass the address explicitly: [bold]--mac AA:BB:CC:DD:EE:FF[/]\n"
                    "    • run without a watch: [bold]--watch no[/]")
                try:
                    choice = input(
                        "\nContinue without a watch for this session? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    choice = "n"
                if choice == "y":
                    self.use_watch = False
                    self.console.print("[dim]Proceeding without biometrics.[/]")
                else:
                    self.console.print("[yellow]Aborting — no watch.[/]")
                    self.audio.stop()
                    return

        # Last chance to bail out before the dashboard takes over the screen
        try:
            input("\nPress Enter to begin the session (Ctrl-C to abort)... ")
        except (EOFError, KeyboardInterrupt):
            self.console.print("[yellow]Aborted before start.[/]")
            self.audio.stop()
            return

        # Run the session inside a raw-stdin context for hotkeys
        with _raw_stdin():
            try:
                if self.use_watch:
                    async with WatchDriver(self.mac,
                                            sec_level=self.sec_level,
                                            debug_file=self.debug_log) as watch:
                        self._wire_watch_callbacks(watch)
                        await self._main_loop(watch)
                else:
                    # Watch-less session: no biometrics, no vitals, just the
                    # guided CRV protocol with audio.
                    await self._main_loop(None)
            finally:
                self.audio.stop()
                self._finalize_session()

        # After session: commit-then-reveal flow if we have a target pool
        if self.pool is not None and self.pool.get_manifest_entry(self.coord):
            self._reveal_and_judge()

    def _show_notes_instructions(self):
        path = self.logger.notes_path()
        self.console.print(
            f"\n[bold yellow]→ Notes file:[/] {path}")
        self.console.print(
            "[dim]Open this in a separate terminal or editor window. "
            "Each stage will append a header with prompts you can fill in. "
            "Don't open it in the SAME terminal — that breaks the dashboard hotkeys.[/]"
        )
        if self.auto_launch_editor and self.editor:
            # User explicitly asked for auto-launch via --editor. Honor it
            # but warn that it'll interfere with hotkeys.
            try:
                self._editor_proc = subprocess.Popen(
                    [self.editor, str(path)])
                self.console.print(
                    f"[yellow]![/] Launched {self.editor} as a subprocess — "
                    "hotkeys may not work while it has focus.")
            except Exception as e:
                self.console.print(
                    f"[yellow]![/] Could not launch '{self.editor}': {e}")
                self._editor_proc = None

    # ---- watch wiring ---------------------------------------------------

    def _wire_watch_callbacks(self, watch: WatchDriver):
        def on_hr(bpm: int):
            self.bio.record(bpm)
            self.ui.current_bpm     = bpm
            self.ui.hr_history.append(bpm)
            self.ui.state_label     = self.bio.state.state_label
            self.ui.coherent_seconds = self.bio.state.coherent_seconds
            self.ui.baseline_bpm    = self.bio.state.baseline_bpm
            self.ui.baseline_std    = self.bio.state.baseline_std
            self.logger.hr(bpm, self.bio.state.state_label)
            self._maybe_chime_coherent()

        def on_bp(s: int, d: int):
            self.ui.systolic, self.ui.diastolic = s, d
            self.logger.vitals("BP", f"{s}/{d}")
            self.ui.status_message = f"BP recorded: {s}/{d} mmHg"

        def on_spo2(v: int):
            self.ui.spo2 = v
            self.logger.vitals("SpO2", str(v))
            self.ui.status_message = f"SpO₂ recorded: {v}%"

        def on_battery(v: int):
            self.ui.battery = v

        def on_status(msg: str):
            self.ui.status_message = msg

        watch.on_hr      = on_hr
        watch.on_bp      = on_bp
        watch.on_spo2    = on_spo2
        watch.on_battery = on_battery
        watch.on_status  = on_status

    # ---- main session loop ---------------------------------------------

    async def _main_loop(self, watch):
        keys: asyncio.Queue[str] = asyncio.Queue()
        if watch is None:
            self.ui.status_message = "No watch — guided CRV only (no biometrics)"

        async def refresh_loop(live: Live):
            while True:
                self._update_ui_from_audio()
                live.update(make_dashboard(self.ui))
                await asyncio.sleep(0.2)

        async def keyboard_loop():
            while True:
                ch = (await keys.get()).lower()
                if ch == " ":
                    self._advance_event.set()
                elif ch == "p":
                    self._toggle_pause()
                elif ch == "m":
                    if watch is not None:
                        asyncio.create_task(self._manual_vitals(watch))
                    else:
                        self.ui.status_message = "No watch — vitals unavailable"
                elif ch == "n":
                    self._mark_event()
                elif ch == "q":
                    self._abort_event.set()
                    raise KeyboardInterrupt

        async def stage_loop():
            if watch is not None:
                await self._initial_vitals(watch)
            for idx, stage in enumerate(proto.STAGES):
                self.ui.stage_idx = idx
                await self._run_stage(idx, stage, watch)
                if stage.code == "baseline" and watch is not None:
                    self.bio.lock_baseline()
                    self.console.bell()  # short audible signal
            # After all stages: end the session
            self._advance_event.set()
            self._abort_event.set()

        async def hr_poker():
            """The P58 doesn't auto-stream HR — we have to keep asking.
            Poke every 5 seconds, but skip while BP or SpO2 is in flight.
            The watch's MOY firmware is single-threaded internally; sending
            a 0x6d during a 0x69/0x6b run aborts the in-progress job."""
            if watch is None:
                return
            while True:
                await asyncio.sleep(5.0)
                if self._abort_event.is_set():
                    return
                if self._vitals_in_progress:
                    continue
                try:
                    await watch.trigger_hr()
                except Exception:
                    pass

        async def coord_speaker():
            """Periodically re-announce the target coordinate during
            active CRV stages (I-VI), as SRI protocols did — the viewer
            uses the coordinate as a continuous focusing anchor."""
            while True:
                await asyncio.sleep(5.0)
                if self._abort_event.is_set():
                    return
                # Only speak during the active CRV stages, and never while
                # vitals are being measured (would step on the cue)
                stage = self.ui.stage
                if stage is None:
                    continue
                if stage.code not in self._coord_active_stages:
                    continue
                if self._vitals_in_progress:
                    continue
                now = time.time()
                if now - self._last_coord_spoken_at >= self.coord_interval_sec:
                    self._last_coord_spoken_at = now
                    self.logger.coord_event(self.coord)
                    self.ui.status_message = f"Coord cue: {self.coord}"
                    speak_coordinate(self.audio, self.coord)

        with Live(make_dashboard(self.ui),
                   console=self.console,
                   refresh_per_second=15,
                   screen=True) as live:
            # The stage loop is the one task whose completion means the
            # session is over. Support tasks (refresh, keys, pokers) run
            # for the whole duration and are cancelled at the end.
            stage_task = asyncio.create_task(stage_loop())
            support_tasks = [
                asyncio.create_task(refresh_loop(live)),
                asyncio.create_task(_read_keys(keys)),
                asyncio.create_task(keyboard_loop()),
                asyncio.create_task(coord_speaker()),
            ]
            # Only poll the watch when we actually have one. In watch-less
            # mode hr_poker would return immediately, and with
            # FIRST_COMPLETED that used to tear the whole session down
            # before any stage ran — which is the bug this guards against.
            if watch is not None:
                support_tasks.append(asyncio.create_task(hr_poker()))

            all_tasks = [stage_task] + support_tasks
            try:
                # Wait specifically for the stage loop to finish (or for a
                # support task to crash). We don't use a bare FIRST_COMPLETED
                # over all tasks, because a support task that legitimately
                # returns early (or a keyboard 'q' raising) should be handled
                # explicitly.
                done, pending = await asyncio.wait(
                    all_tasks, return_when=asyncio.FIRST_COMPLETED)
                # Surface any real exception (but not KeyboardInterrupt from 'q')
                for t in done:
                    exc = t.exception()
                    if exc and not isinstance(exc, KeyboardInterrupt):
                        raise exc
            finally:
                for t in all_tasks:
                    t.cancel()
                await asyncio.gather(*all_tasks, return_exceptions=True)

    # ---- per-stage logic ------------------------------------------------

    async def _initial_vitals(self, watch: WatchDriver):
        """Take one BP and one SpO2 measurement before baseline begins, so
        we have a starting reference. Holds the vitals lock so the HR
        poker doesn't interrupt — the watch firmware can only run one
        sensor command at a time."""
        await self._run_vitals_sequence(watch, label_prefix="Initial vitals")

    async def _manual_vitals(self, watch: WatchDriver):
        """Triggered by the 'm' hotkey. Same lock-protected BP→SpO2
        sequence as the auto-run at session start."""
        if self._vitals_in_progress:
            self.ui.status_message = "Vitals already in progress, ignoring"
            return
        await self._run_vitals_sequence(watch, label_prefix="Manual re-measure")

    async def _run_vitals_sequence(self, watch: WatchDriver,
                                    label_prefix: str = "Vitals"):
        """Sequentially trigger BP then SpO2, with the vitals lock held
        the whole time so the HR poker can't interfere."""
        self._vitals_in_progress = True
        try:
            self.ui.status_message = f"{label_prefix}: BP (~45s)..."
            await watch.trigger_bp()
            await asyncio.sleep(45)
            self.ui.status_message = f"{label_prefix}: SpO₂ (~30s)..."
            await watch.trigger_spo2()
            await asyncio.sleep(30)
            self.ui.status_message = f"{label_prefix}: complete"
        finally:
            self._vitals_in_progress = False

    async def _wait_for_speech_idle(self, timeout: float = 5.0):
        """Poll until the audio engine reports no active speech, or
        timeout. Used between Focus-level announcement and stage-specific
        cue+description so they don't overlap."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = self.audio.snapshot()
            if not snap.speaking:
                return
            await asyncio.sleep(0.1)

    def _prewarm_voices(self):
        """Pre-synthesize every stage's cue and description before the
        session begins. Each Kokoro inference is ~1-2s the first time;
        without prewarming, the first stage transition would stutter as
        the model runs against the full description text. Synthesized
        WAVs are persisted to disk so subsequent runs of this same
        protocol skip the synthesis step entirely."""
        if not self.audio.voice_available:
            return
        from .audio import FOCUS_PROFILES
        all_texts = []
        for stage in proto.STAGES:
            if stage.cue_message:
                all_texts.append(stage.cue_message)
            if stage.description:
                all_texts.append(stage.description)
        for prof in FOCUS_PROFILES.values():
            if prof.announce:
                all_texts.append(prof.announce)
        # Plus the test cue and the coordinate "Target. X Y Z" message
        all_texts.append("CRV research session ready. Headphones on.")

        # Synthesize each. The VoiceSynth disk cache makes already-cached
        # ones instant; only first-run or changed-text ones are slow.
        from_cache = 0
        fresh      = 0
        failed     = 0
        with self.console.status("[dim]Preparing voice prompts...[/]") as st:
            for i, text in enumerate(all_texts, 1):
                # Was this already on disk?
                disk_path = self.audio._voice._disk_cache_path_for(text)
                was_cached = disk_path is not None and disk_path.exists()
                result = self.audio._voice.synthesize(text)
                if result is None:
                    failed += 1
                elif was_cached:
                    from_cache += 1
                else:
                    fresh += 1
                st.update(f"[dim]Preparing voice prompts... "
                          f"{i}/{len(all_texts)} "
                          f"({from_cache} cached, {fresh} synthesized)[/]")
        msg = f"[green]✓[/] Voice prompts ready: "
        msg += f"[bold]{from_cache}[/] from cache"
        if fresh > 0:
            msg += f", [bold]{fresh}[/] newly synthesized"
        if failed > 0:
            msg += f", [yellow]{failed}[/] failed"
        self.console.print(msg)

    async def _run_stage(self, idx: int, stage: proto.Stage,
                          watch: WatchDriver):
        # Transition: audio, log, notes
        self.audio.set_focus(stage.focus_level)
        # set_focus already speaks the Focus-level announcement.
        # Let that finish before layering the stage-specific guidance.
        self.audio.chime(freq_hz=660 if idx % 2 == 0 else 880,
                          duration_sec=0.5)
        self.logger.stage(stage.code, stage.name, stage.focus_level)
        self.logger.append_stage_header(stage.name, stage.prompts)

        # Wait briefly for the Focus-level announcement to finish so we
        # don't pile speech onto speech. Cap the wait so a hung TTS can't
        # stall the session.
        await self._wait_for_speech_idle(timeout=5.0)
        if stage.cue_message:
            self.ui.status_message = stage.cue_message
            self.audio.speak(stage.cue_message, post_pause_sec=0.8)
        if stage.description:
            # Queue the full description so it plays right after the cue.
            self.audio.speak(stage.description, post_pause_sec=0.5)

        # First speaking of the coordinate happens at Stage I — this is the
        # SRI moment where the viewer receives the target identifier and
        # makes their immediate reflexive ideogram. After this initial cue,
        # the periodic coord_speaker task repeats the coordinate every
        # `coord_interval_sec` during stages I-VI.
        if stage.code == "stage1":
            speak_coordinate(self.audio, self.coord)
            self._last_coord_spoken_at = time.time()
            self.logger.coord_event(self.coord, kind="initial")

        # Time-keeping
        self._stage_started_at  = time.time()
        self._total_pause_time  = 0.0
        self._pause_started_at  = None
        self.ui.stage_duration  = stage.duration_sec
        self._advance_event.clear()

        # Run the stage until duration expires or user advances
        while True:
            if self._abort_event.is_set():
                return
            effective_elapsed = (time.time() - self._stage_started_at
                                  - self._total_pause_time)
            if self._paused and self._pause_started_at:
                effective_elapsed = (self._pause_started_at
                                      - self._stage_started_at
                                      - self._total_pause_time)
            self.ui.stage_elapsed = effective_elapsed
            self.ui.paused = self._paused

            if effective_elapsed >= stage.duration_sec:
                break
            if self._advance_event.is_set():
                self._advance_event.clear()
                break

            # Mid-stage re-measurement: every 5 min of stage time, if it's
            # a long stage and we're not in baseline/cool-down. (Tunable.)
            await asyncio.sleep(0.5)

    def _toggle_pause(self):
        if self._paused:
            # Resume: add elapsed pause to total
            if self._pause_started_at:
                self._total_pause_time += time.time() - self._pause_started_at
            self._pause_started_at = None
            self._paused = False
            self.ui.status_message = "Resumed"
        else:
            self._paused = True
            self._pause_started_at = time.time()
            self.ui.status_message = "Paused"

    def _mark_event(self):
        ts = time.strftime("%H:%M:%S")
        # Append a timestamped marker to notes.md
        with self.logger.notes_path().open("a") as f:
            f.write(f"\n*[Event marker @ {ts}]*\n\n")
        self.ui.status_message = f"Event marked at {ts}"

    def _update_ui_from_audio(self):
        snap = self.audio.snapshot()
        if (self.ui.audio_focus != snap.focus_level
                or abs(self.ui.audio_volume - snap.volume) > 0.01
                or abs(self.ui.audio_beat   - snap.delta_f) > 0.05):
            # Log only on meaningful change
            self.logger.audio(snap.focus_level, snap.delta_f, snap.volume)
        self.ui.audio_focus  = snap.focus_level
        self.ui.audio_volume = snap.volume
        self.ui.audio_beat   = snap.delta_f

    def _maybe_chime_coherent(self):
        """Trigger a subtle chime the first moment the user enters a
        coherent state, and again when they leave it."""
        is_coherent = self.bio.state.state_label == "coherent"
        if is_coherent and not self._coherent_was_active:
            self.audio.chime(freq_hz=523, duration_sec=0.6)   # C5
            self._coherent_segment_start = time.time()
        elif not is_coherent and self._coherent_was_active:
            if self._coherent_segment_start is not None:
                duration = int(time.time() - self._coherent_segment_start)
                self._total_coherent_seconds += duration
            self._coherent_segment_start = None
        self._coherent_was_active = is_coherent

    # ---- finalization ---------------------------------------------------

    def _finalize_session(self):
        # Close any in-flight coherent segment
        if self._coherent_segment_start is not None:
            self._total_coherent_seconds += int(
                time.time() - self._coherent_segment_start)

        # Capture the notes hash BEFORE target reveal. This is the
        # commit half of commit-then-reveal: lets you prove later that the
        # notes file existed in its current state at session end, before
        # you saw the target.
        notes_hash = hash_notes_file(self.logger.notes_path())

        summary = {
            "baseline_bpm":           self.bio.state.baseline_bpm,
            "baseline_std":           self.bio.state.baseline_std,
            "baseline_rmssd":         self.bio.state.baseline_rmssd,
            "final_bp":               (f"{self.ui.systolic}/{self.ui.diastolic}"
                                       if self.ui.systolic and self.ui.diastolic
                                       else None),
            "final_spo2":             self.ui.spo2,
            "total_coherent_seconds": self._total_coherent_seconds,
            "stage_stats":            {},
            "notes_sha256_pre_reveal": notes_hash,
            "coord":                  self.coord,
            "intention":              self.intention,
        }
        self.logger.finalize(summary)
        self.console.print(
            f"\n[bold green]Session complete.[/] "
            f"Data saved to [bold]{self.logger.dir}[/]")
        self.console.print(
            f"  Total coherent time: "
            f"[bold]{self._total_coherent_seconds // 60} min "
            f"({self._total_coherent_seconds}s)[/]")
        self.console.print(
            f"  Notes SHA-256 (pre-reveal): [dim]{notes_hash[:16]}...[/]")

    def _reveal_and_judge(self):
        """After the session, present 4 targets (1 real + 3 decoys) in
        random order, ask the viewer to rank them by similarity to their
        notes, then reveal the actual target and write the ranking +
        reveal to disk. This is the SRI-style blinded feedback step.

        Skipped automatically if there are fewer than 4 targets in the
        pool (you can't rank against decoys you don't have)."""
        if self.pool is None:
            return
        all_coords = self.pool.coordinates()
        if len(all_coords) < 4:
            self.console.print(
                "\n[yellow]![/] Pool has fewer than 4 targets; "
                "skipping decoy ranking. Add more with "
                "[bold]python -m crv.targets fetch[/].")
            self._do_reveal_only()
            return

        self.console.print(
            "\n[bold cyan]═══ BLINDED FEEDBACK STEP ═══[/]\n")
        self.console.print(
            "You'll now see 4 targets in random order: the real one and "
            "3 decoys. Read each one's title and description, then rank "
            "them from most-similar to least-similar to your session notes. "
            "The actual target is revealed only after you've ranked them.\n")
        try:
            input("Press Enter to continue with the blinded ranking step... ")
        except (EOFError, KeyboardInterrupt):
            self.console.print("[yellow]Skipped ranking.[/]")
            self._do_reveal_only()
            return

        # Pull the four targets
        decoys = self.pool.pick_decoys(exclude=self.coord, count=3)
        candidates = [self.coord] + decoys
        rng = random.SystemRandom()
        rng.shuffle(candidates)
        # We need to peek each candidate's content to display it. Use
        # the pool's underlying decrypt — but DO NOT mark them as revealed.
        from .targets import coord_to_filename, decrypt, key_from_b64
        contents = []
        for c in candidates:
            fn = coord_to_filename(c)
            enc = (self.pool.pool_dir / f"{fn}.enc").read_bytes()
            key = key_from_b64((self.pool.keys_dir / f"{fn}.key").read_text())
            data = json.loads(decrypt(enc, key).decode("utf-8"))
            contents.append((c, data))

        self.console.print("\nCandidate targets:\n")
        for i, (c, data) in enumerate(contents, 1):
            self.console.print(f"[bold]Target #{i}[/]: {data.get('title', '?')}")
            self.console.print(f"  {data.get('description', '')[:300]}\n")

        # Ask for ranking — retry on invalid input rather than bailing to
        # a bare reveal (which loses the whole point of the blinded judge).
        self.console.print(
            "Enter your ranking as 4 comma-separated numbers, most-similar "
            "first.\nExample: 3,1,4,2  means target #3 is most similar to "
            "your notes, then #1, then #4, then #2.")
        ranking = None
        for _attempt in range(5):
            try:
                ranking_str = input("Your ranking (or 'skip' to skip judging): ").strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[yellow]Skipping the ranking step.[/]")
                self._do_reveal_only()
                return
            if ranking_str.lower() in ("skip", "s", "q", "quit"):
                self.console.print("[yellow]Skipping the ranking step.[/]")
                self._do_reveal_only()
                return
            try:
                parsed = [int(x) for x in ranking_str.replace(",", " ").split()]
            except ValueError:
                self.console.print(
                    "[yellow]Please enter four numbers (e.g. 3,1,4,2).[/]")
                continue
            if sorted(parsed) != [1, 2, 3, 4]:
                self.console.print(
                    "[yellow]Ranking must use each of 1, 2, 3, 4 exactly "
                    f"once. You entered: {parsed}. Try again.[/]")
                continue
            ranking = parsed
            break

        if ranking is None:
            self.console.print(
                "[yellow]No valid ranking after several tries; "
                "revealing without a score.[/]")
            self._do_reveal_only()
            return

        # Compute rank of the real target (1 = best, 4 = worst)
        real_idx_in_candidates = next(
            i for i, (c, _) in enumerate(contents) if c == self.coord)
        real_position_label = real_idx_in_candidates + 1   # the user-facing number
        real_rank = ranking.index(real_position_label) + 1  # 1-indexed rank

        # Reveal
        content = self.pool.reveal(self.coord)
        self.console.print(
            f"\n[bold green]═══ REVEAL ═══[/]")
        self.console.print(
            f"\n[bold]The real target was Target #{real_position_label}:[/]")
        self.console.print(f"  Title: {content.get('title')}")
        self.console.print(f"  {content.get('description', '')[:400]}\n")

        if real_rank == 1:
            verdict = "[bold green]HIT (rank 1 of 4 — better than chance)[/]"
        elif real_rank == 2:
            verdict = "[bold yellow]Partial hit (rank 2 of 4)[/]"
        else:
            verdict = f"[bold red]Miss (real target ranked {real_rank} of 4)[/]"
        self.console.print(f"Verdict: {verdict}\n")

        # Save the ranking + reveal to a feedback file
        feedback = {
            "candidates":           [c for c, _ in contents],
            "candidate_titles":     [data.get("title", "") for _, data in contents],
            "user_ranking":         ranking,
            "real_target_position": real_position_label,
            "real_target_rank":     real_rank,
            "verdict":              ["hit", "partial_hit", "miss", "miss"][real_rank - 1],
            "notes_sha256_at_reveal": hash_notes_file(self.logger.notes_path()),
            "revealed_target":      content,
        }
        (self.logger.dir / "feedback.json").write_text(
            json.dumps(feedback, indent=2))
        self.console.print(
            f"Feedback saved to [bold]{self.logger.dir/'feedback.json'}[/]")
        if content.get("revealed_image_path"):
            self.console.print(
                f"Target image: [bold]{content['revealed_image_path']}[/]")

    def _do_reveal_only(self):
        """Fallback when there aren't enough decoys: just reveal."""
        content = self.pool.reveal(self.coord)
        if content is None:
            return
        self.console.print(f"\n[bold]Target reveal:[/] {content.get('title')}")
        self.console.print(f"  {content.get('description', '')[:400]}")
        if content.get("revealed_image_path"):
            self.console.print(
                f"  Image: [bold]{content['revealed_image_path']}[/]")

    def _launch_editor(self):
        """Kept for backward compatibility; logic moved to
        _show_notes_instructions and is gated behind auto_launch_editor."""
        pass


# _delayed helper removed — was only used by the buggy `m` hotkey path.
# The new _manual_vitals method holds the vitals lock for the whole BP→SpO2
# sequence, which is the safe way to do it on this firmware.


def _compute_session_integrity_hash(voice: str, voice_speed: float,
                                      audio_volume: float) -> str:
    """Hash the configuration that would change session output. Stored in
    metadata.json at session start. Lets you verify a session ran with
    the protocol and audio settings you committed to."""
    h = hashlib.sha256()
    for s in proto.STAGES:
        h.update(f"{s.code}|{s.duration_sec}|{s.focus_level}|{s.cue_message}".encode())
        h.update((s.description or "").encode())
    for name, prof in sorted(FOCUS_PROFILES.items()):
        h.update(f"{name}|{prof.delta_f}|{prof.carrier}|{prof.am_depth}|{prof.pink_level}".encode())
    h.update(f"voice={voice}|speed={voice_speed}|vol={audio_volume}".encode())
    return h.hexdigest()


def hash_notes_file(path: Path) -> str:
    """SHA-256 of a notes file. Used for commit-then-reveal: capturing
    this hash before the target is revealed proves the notes existed in
    that state at that moment."""
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def speak_coordinate(audio_engine, coord: str):
    """Render a coordinate suitably for speech. The format "374-2856 9"
    should be spoken digit-by-digit, with hyphen as 'dash'."""
    # Spell out digits, keep dash and the trailing single digit as separate
    # words. Reads naturally as "three seven four dash two eight five six
    # ... nine"
    spoken = []
    for ch in coord:
        if ch.isdigit():
            spoken.append(ch)
        elif ch == "-":
            spoken.append("dash")
        # whitespace just acts as a pause naturally
    audio_engine.speak("Target. " + " ".join(spoken), post_pause_sec=1.0)

