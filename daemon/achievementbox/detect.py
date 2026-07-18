"""Game detection for the Mega Drive path.

The EverDrive menu copies the launched ROM into cart PSRAM, which the pi
bus exposes at USB address 0x0000000 -- so the daemon can read the running
game's Sega header (name, serial, region) and hash the ROM image the same
way RetroAchievements does (MD5 of the ROM file) without touching the SD
card or the console.

Caveat: we hash PSRAM up to the size the header declares. Commercial dumps
declare their true size; homebrew sometimes over-declares (SGDK pads to
1MB), which would change the hash vs the file on disk. Good enough to
identify commercial games for RA; revisit if a set ever fails to match.

SAFETY (learned the hard way, 2026-07-09): pi-bus reads of PSRAM preempt
the 68K's instruction fetches. Bulk-hashing the ROM while a game is
RUNNING starves the CPU and crashes it (observed: SGDK address error mid-
game). Reading the 0x100-byte header is brief enough to be tolerated, but
hash_rom() must only run in the seconds right after launch (boot logos,
before gameplay) or with heavy throttling. WRAM-shadow reads are immune --
the game never touches the SRAM chip. TODO: find a fetch-safe hashing
window or source the hash from the SD file instead.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .memory import EdlinkSession

HEADER_BASE = 0x100
CHUNK = 0x8000  # 32KB memrd chunks

REGION_NAMES = {"J": "Japan", "U": "USA", "E": "Europe", "D": "?D",
                "F": "France", "W": "World"}


@dataclass
class MdGameInfo:
    system: str            # "SEGA MEGA DRIVE" / "SEGA GENESIS" / ...
    title_domestic: str
    title_overseas: str
    copyright: str
    serial: str
    regions: str
    rom_size: int          # bytes, from header rom-end field
    md5: str | None = None # RA-style hash, filled by hash_rom()

    @property
    def title(self) -> str:
        return self.title_overseas or self.title_domestic


def read_header(session: EdlinkSession) -> MdGameInfo:
    hdr = session.read_mem(HEADER_BASE, 0x100)

    def text(lo: int, hi: int) -> str:
        return hdr[lo:hi].decode("ascii", "replace").strip()

    rom_end = int.from_bytes(hdr[0xA4:0xA8], "big")
    return MdGameInfo(
        system=text(0x00, 0x10),
        copyright=text(0x10, 0x20),
        title_domestic=text(0x20, 0x50),
        title_overseas=text(0x50, 0x80),
        serial=text(0x80, 0x8E),
        regions=text(0xF0, 0xF3),
        rom_size=rom_end + 1,
    )


def hash_rom(session: EdlinkSession, info: MdGameInfo) -> str:
    """MD5 of the ROM image, as rcheevos hashes plain Mega Drive dumps."""
    md5 = hashlib.md5()
    addr, remaining = 0, info.rom_size
    while remaining:
        n = min(CHUNK, remaining)
        md5.update(session.read_mem(addr, n))
        addr += n
        remaining -= n
    info.md5 = md5.hexdigest()
    return info.md5


RECENT_DAT = "MEGA/sys/recent.dat"
RECENT_SLOT = 512  # fixed-size slots, newest first, null-padded paths


def read_recent_games(session: EdlinkSession, scratch_dir: Path) -> list[str]:
    """Recently-played SD paths, newest first, via MEGA/sys/recent.dat.

    MCU-side read -- safe while a game is running. This is how menu-
    launched games are identified: the OS prepends the launched file's
    path to this list at every launch.
    """
    dest = Path(scratch_dir) / "recent.dat"
    session.read_sd_file(RECENT_DAT, dest)
    raw = dest.read_bytes()
    paths = []
    for off in range(0, len(raw), RECENT_SLOT):
        entry = raw[off:off + RECENT_SLOT].split(b"\0", 1)[0]
        if entry:
            paths.append(entry.decode("ascii", "replace"))
    return paths


def is_game_running(session: EdlinkSession) -> bool:
    """True if a game (not the EverDrive menu) owns the console.

    The menu doesn't populate a Sega header at PSRAM 0x100.
    """
    try:
        return read_header(session).system.startswith("SEGA")
    except IOError:
        return False
