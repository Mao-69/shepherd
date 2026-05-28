"""crv/watch.py — gatttool-backed driver for P58 / MOY-DBT5 watches.

Extracted from the standalone monitor and made standalone-importable.
Spawns gatttool as a subprocess and parses notifications.

Public API:
    async with WatchDriver(state, mac, sec_level="medium") as w:
        await w.trigger_bp()
        await w.trigger_spo2()
        await w.trigger_hr()
        # incoming readings are posted to the callbacks registered with
        # `on_hr`, `on_bp`, `on_spo2`, `on_battery`.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from typing import Callable, List, Optional, Tuple


# GATT handles for the P58 / MOY-DBT5 firmware family
HANDLE_MOY_WRITE       = 0x004f
HANDLE_MOY_NOTIFY      = 0x0051
HANDLE_MOY_CCCD        = 0x0052
HANDLE_STD_HR_NOTIFY   = 0x005b
HANDLE_STD_HR_CCCD     = 0x005c
HANDLE_BATTERY_NOTIFY  = 0x001a
HANDLE_BATTERY_CCCD    = 0x001b

# MOYOUNG-V2 frames (no-space hex strings, the format gatttool wants)
CMD_QUERY_HR      = "feea20056d"
CMD_TRIGGER_BP    = "feea200869000000"
CMD_TRIGGER_SPO2  = "feea20066b00"

NOTIF_RE = re.compile(
    r"Notification handle\s*=\s*0x([0-9a-fA-F]+)\s+value:\s*([0-9a-fA-F ]+)")

# Device-name substrings that identify a compatible watch. The P58 family
# reports a few different names depending on firmware revision.
KNOWN_NAME_HINTS = ("P58", "MOY", "MOYOUNG", "DBT5", "DA FIT", "DAFIT")

# bluetoothctl "Device AA:BB:.. Name" line
_BTCTL_DEVICE_RE = re.compile(
    r"Device\s+([0-9A-Fa-f:]{17})\s+(.*)")


async def discover_watch(timeout: float = 12.0,
                          name_hints: Tuple[str, ...] = KNOWN_NAME_HINTS,
                          verbose: bool = True) -> Optional[str]:
    """Scan for a compatible watch using bluetoothctl and return its MAC,
    or None if not found.

    Strategy:
      1. List already-known/paired devices first (instant — if you've
         bonded the watch before, it shows up here).
      2. If none match, run an active scan for `timeout` seconds and
         match device names against `name_hints`.

    Matching is by advertised name substring (case-insensitive). The P58
    family advertises names like 'P58', 'MOY-DBT5', 'MOYOUNG-V2', etc.
    """
    # --- 1. check already-known devices (paired/bonded) ---
    known = _list_known_devices()
    for mac, name in known:
        if _name_matches(name, name_hints):
            if verbose:
                print(f"[watch] found known device: {name} [{mac}]")
            return mac

    # --- 2. active scan ---
    if verbose:
        print(f"[watch] scanning for a compatible watch "
              f"(up to {timeout:.0f}s)... make sure it's awake.")
    found = await _scan_for_devices(timeout)
    # Merge with known, prefer name match
    candidates = found + known
    for mac, name in candidates:
        if _name_matches(name, name_hints):
            if verbose:
                print(f"[watch] discovered: {name} [{mac}]")
            return mac

    if verbose:
        print("[watch] no compatible watch found by name.")
        if candidates:
            print("[watch] devices seen during scan:")
            for mac, name in candidates:
                print(f"          {mac}  {name or '(no name)'}")
            print("[watch] if your watch is listed above under a different "
                  "name, pass it explicitly with --mac <address>.")
    return None


def _name_matches(name: str, hints: Tuple[str, ...]) -> bool:
    if not name:
        return False
    upper = name.upper()
    return any(h.upper() in upper for h in hints)


def _list_known_devices() -> List[Tuple[str, str]]:
    """Return [(mac, name)] for devices bluetoothctl already knows about."""
    try:
        out = subprocess.run(["bluetoothctl", "devices"],
                              capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    devices = []
    for line in out.stdout.splitlines():
        m = _BTCTL_DEVICE_RE.search(line)
        if m:
            devices.append((m.group(1), m.group(2).strip()))
    return devices


async def _scan_for_devices(timeout: float) -> List[Tuple[str, str]]:
    """Run `bluetoothctl scan on` for `timeout` seconds, then collect the
    device list. Returns [(mac, name)]."""
    # Start a scanning session. We drive bluetoothctl in interactive mode
    # so we can turn scan on, wait, then read the device list.
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        print("[watch] bluetoothctl not found — install bluez.")
        return []

    try:
        proc.stdin.write(b"scan on\n")
        await proc.stdin.drain()
        await asyncio.sleep(timeout)
        proc.stdin.write(b"scan off\ndevices\nquit\n")
        await proc.stdin.drain()
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            stdout = b""
    except Exception as e:
        print(f"[watch] scan error: {e}")
        try:
            proc.kill()
        except Exception:
            pass
        return []

    devices = []
    seen = set()
    for line in stdout.decode(errors="replace").splitlines():
        m = _BTCTL_DEVICE_RE.search(line)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            devices.append((m.group(1), m.group(2).strip()))
    return devices


class WatchDriver:
    def __init__(self, mac: str, sec_level: str = "medium",
                  debug_file: Optional[str] = None):
        self.mac        = mac
        self.sec_level  = sec_level
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock      = asyncio.Lock()
        self._connected = False

        # Callbacks — set by the session orchestrator
        self.on_hr:      Callable[[int], None]    = lambda v: None
        self.on_bp:      Callable[[int, int], None] = lambda s, d: None
        self.on_spo2:    Callable[[int], None]    = lambda v: None
        self.on_battery: Callable[[int], None]    = lambda v: None
        self.on_status:  Callable[[str], None]    = lambda s: None

        self._dbg_fh = open(debug_file, "w", buffering=1) if debug_file else None

    @property
    def connected(self) -> bool:
        return self._connected

    async def __aenter__(self) -> "WatchDriver":
        await self._spawn()
        await self._setup()
        return self

    async def __aexit__(self, *exc):
        if self._proc is not None:
            try:
                self._proc.stdin.write(b"disconnect\nexit\n")
                await asyncio.wait_for(self._proc.stdin.drain(), 2.0)
            except Exception:
                pass
            try:
                self._proc.terminate()
            except Exception:
                pass
        if self._dbg_fh:
            self._dbg_fh.close()

    async def _spawn(self):
        self.on_status("Spawning gatttool...")
        self._proc = await asyncio.create_subprocess_exec(
            "gatttool", "-b", self.mac, "-t", "public",
            f"--sec-level={self.sec_level}", "-I",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        asyncio.create_task(self._read_output())

    async def _setup(self):
        self.on_status("Connecting to watch...")
        await self._cmd("connect")
        for _ in range(150):
            if self._connected:
                break
            await asyncio.sleep(0.1)
        # Enable all the notification streams we care about
        await self._cmd(f"char-write-req 0x{HANDLE_MOY_CCCD:04x} 0100")
        await asyncio.sleep(0.3)
        await self._cmd(f"char-write-req 0x{HANDLE_STD_HR_CCCD:04x} 0100")
        await asyncio.sleep(0.3)
        await self._cmd(f"char-write-req 0x{HANDLE_BATTERY_CCCD:04x} 0100")
        await asyncio.sleep(0.3)
        self.on_status("Watch connected, notifications enabled")

    # -- low-level send --------------------------------------------------

    async def _cmd(self, line: str):
        async with self._lock:
            if self._proc and not self._proc.stdin.is_closing():
                if self._dbg_fh:
                    self._dbg_fh.write(f">>> {line}\n")
                self._proc.stdin.write((line + "\n").encode())
                await self._proc.stdin.drain()

    # -- public triggers -------------------------------------------------

    async def trigger_hr(self):
        await self._cmd(f"char-write-cmd 0x{HANDLE_MOY_WRITE:04x} {CMD_QUERY_HR}")

    async def trigger_bp(self):
        await self._cmd(f"char-write-cmd 0x{HANDLE_MOY_WRITE:04x} {CMD_TRIGGER_BP}")

    async def trigger_spo2(self):
        await self._cmd(f"char-write-cmd 0x{HANDLE_MOY_WRITE:04x} {CMD_TRIGGER_SPO2}")

    # -- output parser ---------------------------------------------------

    async def _read_output(self):
        assert self._proc and self._proc.stdout
        while True:
            line_b = await self._proc.stdout.readline()
            if not line_b:
                break
            line = line_b.decode(errors="replace").rstrip("\r\n")
            if self._dbg_fh:
                self._dbg_fh.write(f"<<< {line}\n")
            self._parse_line(line)

    def _parse_line(self, line: str):
        if "Connection successful" in line:
            self._connected = True
            return
        m = NOTIF_RE.search(line)
        if not m:
            return
        handle = int(m.group(1), 16)
        try:
            data = bytes(int(x, 16) for x in m.group(2).strip().split())
        except ValueError:
            return

        if handle == HANDLE_MOY_NOTIFY and len(data) >= 5 \
                and data[0] == 0xfe and data[1] == 0xea:
            cmd, payload = data[4], data[5:]
            if cmd == 0x69 and len(payload) >= 2:
                # BP frame layout used by this firmware: [stop_flag, sys, dia]
                # — payload[0] is a status byte, payload[1] is systolic,
                # payload[2] is diastolic.
                s = payload[1] if len(payload) > 1 else payload[0]
                d = payload[2] if len(payload) > 2 else 0
                if 60 <= s <= 250 and 30 <= d <= 160:
                    self.on_bp(s, d)
            elif cmd == 0x6b and payload:
                v = payload[0]
                if 50 <= v <= 100:
                    self.on_spo2(v)
            elif cmd == 0x6d and payload:
                if 30 <= payload[0] <= 220:
                    self.on_hr(payload[0])

        elif handle == HANDLE_STD_HR_NOTIFY and len(data) >= 2:
            flags = data[0]
            if (flags & 0x01) and len(data) >= 3:
                bpm = data[1] | (data[2] << 8)
            else:
                bpm = data[1]
            if 30 <= bpm <= 220:
                self.on_hr(bpm)

        elif handle == HANDLE_BATTERY_NOTIFY and data:
            self.on_battery(data[0])
