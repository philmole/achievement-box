"""Console memory backends for the Achievement Box daemon.

A MemoryBackend exposes the console's memory in CONSOLE address space;
rcheevos memrefs address memory the same way, so the rc_client bridge can
pass addresses straight through.

MedProBackend reads the Mega Drive WRAM shadow maintained by our FPGA
sniffer (fpga/wram_sniffer.sv) out of the Mega EverDrive Pro's cart SRAM,
over a persistent `edlink .stdio` session (one USB handshake, then
low-latency request/response on stdin/stdout).
"""

from __future__ import annotations

import subprocess
import sys
import threading
from abc import ABC, abstractmethod
from pathlib import Path


def edlink_cmd(edlink_exe: str | Path, *args: str) -> list[str]:
    """Command line for edlink on any platform.

    edlink.exe is a .NET assembly; krikzz's own cross-platform wrapper
    (mega-ed-pub/edlink.py) runs it under mono on non-Windows -- so the
    Pi uses the exact same transport as the PC."""
    base = [str(edlink_exe)] if sys.platform == "win32" \
        else ["mono", str(edlink_exe)]
    return base + list(args)

# Pi/USB address map (see fpga/mapper/lib_base/pi_map.sv)
PI_SRAM_BASE = 0x1000000       # cart SRAM chip, 512KB
SHADOW_BASE = PI_SRAM_BASE + 0x40000   # WRAM shadow (wram_sniffer.sv)
PI_MCFG_BASE = 0x183FF00       # mapper config page
PI_MST_ADDR = 0x1800200        # mapper status (ce_mst)
WRAM_BASE = 0xFF0000           # 68K work RAM, canonical mirror
WRAM_SIZE = 0x10000

# Mapper status values (probed 2026-07-09; menu/game differ in bit 0).
# mst polls are MCU-side and proven safe during menu SD->PSRAM loads,
# unlike PSRAM reads which block them.
MST_GAME = b"\xa0\xa0"
MST_MENU = b"\xa1\xa1"


class MemoryBackend(ABC):
    """Reads console memory by console address."""

    @abstractmethod
    def read(self, address: int, length: int) -> bytes:
        """Read `length` bytes at console address `address`."""

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class EdlinkSession:
    """Persistent `edlink .stdio` session.

    Response framing (probed empirically, edlink v1.0.0.1): for
    `memrd ... --file -`, stdout carries an ASCII decimal byte-count line
    terminated by \\n, then exactly that many raw payload bytes.
    """

    def __init__(self, edlink_exe: str | Path):
        self._proc = subprocess.Popen(
            edlink_cmd(edlink_exe, ".stdio"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._connected = False

    # A wedged edlink child (cart dropping off USB mid-transfer) otherwise
    # blocks the hardware worker forever on a pipe read, and every web
    # action queues behind it. Kill the child at the deadline so the read
    # returns short/EOF and raises IOError, which callers already handle.
    # The first read carries edlink's USB connection setup, which has been
    # observed at ~5.2s (2026-07-17) -- give it a longer leash.
    READ_DEADLINE = 5.0
    FIRST_READ_DEADLINE = 15.0

    def read_mem(self, pi_address: int, length: int) -> bytes:
        cmd = f"memrd --addr 0x{pi_address:x} --len 0x{length:x} --file -\n"
        self._proc.stdin.write(cmd.encode("ascii"))
        self._proc.stdin.flush()

        deadline = self.READ_DEADLINE if self._connected \
            else self.FIRST_READ_DEADLINE
        watchdog = threading.Timer(deadline, self._proc.kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            stated = int(self._readline())
            data = self._proc.stdout.read(stated)
        finally:
            watchdog.cancel()
        if stated != length or len(data) != length:
            raise IOError(
                f"memrd 0x{pi_address:x}: wanted {length}, "
                f"stated {stated}, got {len(data)}"
            )
        self._connected = True
        return data

    def read_sd_file(self, sd_path: str, dest: Path) -> Path:
        """Copy a file off the cart SD (MCU-side; safe during gameplay).

        dest is deleted first: a leftover copy from a previous read has a
        stable size and would satisfy the completion poll with STALE data
        (bit us live -- fixed-size recent.dat showed the previous game).
        """
        dest.unlink(missing_ok=True)
        cmd = f'cp --src "sd:/{sd_path.lstrip("/")}" --dst "{dest}"\n'
        self._proc.stdin.write(cmd.encode("ascii"))
        self._proc.stdin.flush()
        # cp emits no stdout in stdio mode; poll for the file to finish
        import time
        last = -1
        for _ in range(120):
            time.sleep(0.25)
            size = dest.stat().st_size if dest.exists() else -1
            if size == last and size >= 0:
                return dest
            last = size
        raise IOError(f"cp of sd:/{sd_path} did not complete")

    def _readline(self) -> str:
        line = b""
        while not line.endswith(b"\n"):
            ch = self._proc.stdout.read(1)
            if not ch:
                raise IOError("edlink stdio session closed unexpectedly")
            line += ch
        return line.decode("ascii").strip()

    def close(self) -> None:
        # Best-effort cleanup: the read watchdog above can already have
        # killed self._proc (e.g. the cart vanishing mid-read during a
        # power cycle -- hardware-confirmed 2026-07-17), leaving stdin in a
        # state where closing it raises OSError even though poll() still
        # reports the process alive. That OSError escaping close() killed
        # the caller's thread (webapp.py's HwWorker) with no self-recovery.
        try:
            if self._proc.poll() is None:
                self._proc.stdin.close()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        except OSError:
            self._proc.kill()


class MedProBackend(MemoryBackend):
    """Mega Drive WRAM via the FPGA sniffer shadow on a Mega EverDrive Pro.

    Console WRAM $FF0000-$FFFFFF (and its $E00000+ mirrors) is served from
    the shadow at pi 0x1040000. Anything outside WRAM is unreadable by
    design -- the sniffer only shadows work RAM.
    """

    def __init__(self, edlink_exe: str | Path):
        self._session = EdlinkSession(edlink_exe)

    def read(self, address: int, length: int) -> bytes:
        if length <= 0:
            raise ValueError("length must be positive")
        # accept any $E00000+ mirror, normalise to 16-bit offset
        if address >> 21 != 0b111:
            raise ValueError(f"0x{address:06x} is not Mega Drive work RAM")
        offset = address & (WRAM_SIZE - 1)
        if offset + length > WRAM_SIZE:
            raise ValueError("read crosses end of work RAM")
        return self._session.read_mem(SHADOW_BASE + offset, length)

    def read_ra(self, ra_address: int, length: int) -> bytes:
        """Read WRAM in RetroAchievements byte order.

        RA's Mega Drive address space mirrors how emulator cores store
        68K RAM: little-endian 16-bit words, i.e. RA byte N == console
        byte N^1. (Proven live: Sonic's game mode lives at console
        $FFF600 but achievement triggers check RA 0xF601.) The shadow
        holds true console byte order, so swap pairs here.
        """
        base = ra_address & ~1
        span = (ra_address & 1) + length
        span += span & 1
        raw = self.read(WRAM_BASE + base, span)
        swapped = bytearray(span)
        swapped[0::2] = raw[1::2]
        swapped[1::2] = raw[0::2]
        off = ra_address & 1
        return bytes(swapped[off:off + length])

    def read_write_counter(self) -> int:
        """Sniffer telemetry: total WRAM writes since game launch."""
        raw = self._session.read_mem(PI_MCFG_BASE, 4)
        return int.from_bytes(raw, "little")

    def read_drop_count(self) -> int:
        """Sniffer telemetry: FIFO overflows. Nonzero = shadow diverged."""
        raw = self._session.read_mem(PI_MCFG_BASE + 4, 2)
        return int.from_bytes(raw, "little")

    def read_combo_flag(self) -> bool:
        """IGM-lite: sticky flag set by pad_watch.sv when the player holds
        Start+Down in-game. Cleared by mapper reset (returning to menu)."""
        raw = self._session.read_mem(PI_MCFG_BASE + 6, 1)
        return bool(raw[0] & 1)

    def read_console_region(self) -> str | None:
        """Active Mega Drive hardware region captured from $A10001.

        Returns ``PAL``, ``NTSC-U``, ``NTSC-J``, or None until the running
        game has read the hardware version register.
        """
        raw = self._session.read_mem(PI_MCFG_BASE + 8, 2)
        if not (raw[1] & 1):
            return None
        value = raw[0]
        if value & 0x40:
            return "PAL"
        return "NTSC-U" if value & 0x80 else "NTSC-J"

    def read_vint_count(self) -> int:
        """Free-wrapping 8-bit vblank vector-fetch counter (frame_watch.sv).

        Increments once per level-6 autovector fetch, i.e. once per frame
        (~60/s NTSC, ~50/s PAL). Deltas mod 256 over a known interval give
        the live frame rate; a zero delta means v-interrupts are disabled
        (or pre-frame_watch gateware) and carries no region verdict.
        """
        raw = self._session.read_mem(PI_MCFG_BASE + 10, 1)
        return raw[0]

    def in_game(self) -> bool | None:
        """True in game, False at menu, None if the value is unrecognised.

        Safe to poll at any time, including during menu game loads.
        """
        raw = self._session.read_mem(PI_MST_ADDR, 2)
        if raw == MST_GAME:
            return True
        if raw == MST_MENU:
            return False
        return None

    def close(self) -> None:
        self._session.close()
