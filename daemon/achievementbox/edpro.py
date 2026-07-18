"""Raw Mega EverDrive Pro USB protocol client.

Speaks the MCU command protocol directly over the CDC serial port
(pyserial), no edlink.exe. Framing and command set reverse-engineered
from references/mega-ed-pub/edio/everdrive.c and verified live:

  command:  '+', '+'^0xFF, cmd, cmd^0xFF
  strings:  u16 big-endian length + bytes
  status:   0xA5, error code (0 = ok)

Only commands verified to work over USB are implemented. CMD_MEM_RD
(0x19) is deliberately absent: sent raw it wedges the MCU's parser
(edlink precedes it with an undocumented mcumode handshake). Use the
edlink stdio session (memory.EdlinkSession) for FCI-bus reads; use this
client for MCU-side things: sys info, SD file/dir access.

Recovery: if the parser ever wedges (no reply), write ~32 zero bytes to
flush its argument reader, then retry status (see recover()).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports

# USB VID/PID the Mega EverDrive Pro MCU has been seen enumerating as.
# Firmware changes these (v26.0625 reports 38DF:0018, not the STM default
# 0483:5740), so treat them as a HINT for ranking -- identity is always
# confirmed by a live sys_info handshake, never by VID alone.
KNOWN_CART_IDS = {(0x0483, 0x5740), (0x38DF, 0x0018)}

CMD_STATUS = 0x10
CMD_MEM_WR = 0x1A
CMD_SYS_INF = 0x26
CMD_HOST_RST = 0x29
CMD_ROM_PATH = 0x31  # firmware v24+ (verified present on v26.0625)
CMD_F_DIR_LD = 0xC5
CMD_F_DIR_SIZE = 0xC6
CMD_F_DIR_GET = 0xC8
CMD_F_FOPN = 0xC9
CMD_F_FRD = 0xCA
CMD_F_FWR = 0xCC
CMD_F_FCLOSE = 0xCE
CMD_F_FINFO = 0xD0
CMD_F_DEL = 0xD3

FA_READ = 0x01
FA_WRITE = 0x02
FA_CREATE_ALWAYS = 0x08

# everdrive.h defines both 0xA5 and 0x5A status keys; older firmware
# replies 0xA5, newer may use 0x5A -- accept either.
STATUS_KEYS = (0xA5, 0x5A)

# FCI addresses used by edlink's Mega Drive ResetToMenu/AppInstall path.
ADDR_FCI_CFG = 0x01800000
ADDR_FCI_FIFO = 0x01810000


@dataclass
class SysInfo:
    boot_ctr: int
    game_ctr: int
    sw_ver: int
    device_id: int


def find_cart_port(preferred: str | None = None) -> str | None:
    """Auto-detect the cart's COM port by protocol handshake.

    VID/PID can't be trusted alone (firmware has changed it), so we rank
    candidate USB serial ports -- preferred first, then known cart IDs,
    then any other USB CDC device -- and confirm each by an actual
    sys_info reply. Bluetooth/virtual ports (no USB VID) are skipped so
    we never poke an unrelated device. Returns the port, or None.
    """
    def rank(p):
        if preferred and p.device == preferred:
            return 0
        if (p.vid, p.pid) in KNOWN_CART_IDS:
            return 1
        return 2

    candidates = sorted((p for p in list_ports.comports() if p.vid),
                        key=rank)
    for p in candidates:
        try:
            with EdProSerial(p.device, timeout=1.5) as dev:
                dev.recover()
                if dev.sys_info().sw_ver:  # a real EverDrive answered
                    return p.device
        except Exception:
            continue  # not the cart (or busy) -- try the next
    return None


class EdProSerial:
    def __init__(self, port: str = "COM5", timeout: float = 3.0):
        self._s = serial.Serial(port, 115200, timeout=timeout)

    def close(self):
        self._s.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # A cart that physically vanishes from USB mid-call (e.g. a Mega CD
    # title's BIOS taking the bus down right after a transient "game
    # running" mapper status) can leave a blocking pyserial read() stuck
    # past its configured per-read timeout -- observed live 2026-07-17,
    # wedging the hardware worker (and every web action behind it)
    # indefinitely. Force-close the port at the deadline so the blocked
    # read raises instead. Mirrors EdlinkSession's kill-the-child
    # watchdog (memory.py) for the raw-serial path. The port must not be
    # reused after the deadline fires -- open a fresh EdProSerial.
    def deadline(self, seconds: float):
        return _Deadline(self, seconds)

    # -- plumbing ---------------------------------------------------
    def _cmd(self, cmd: int):
        self._s.write(bytes([ord("+"), ord("+") ^ 0xFF, cmd, cmd ^ 0xFF]))

    def _read(self, n: int) -> bytes:
        data = self._s.read(n)
        if len(data) != n:
            raise IOError(f"short read: wanted {n}, got {len(data)}")
        return data

    def _tx_string(self, text: str):
        b = text.encode("ascii")
        self._s.write(len(b).to_bytes(2, "big") + b)

    def _rx_string(self) -> str:
        ln = int.from_bytes(self._read(2), "big")
        return self._read(ln).decode("ascii", "replace")

    def _check_status(self):
        self._cmd(CMD_STATUS)
        raw = self._read(2)
        if raw[0] not in STATUS_KEYS:
            raise IOError(f"bad status frame {raw.hex()}")
        if raw[1]:
            raise IOError(f"device error 0x{raw[1]:02x}")

    def recover(self):
        """Unwedge a half-fed command parser.

        Status reports the LAST command's result, which may be a stale
        error -- any well-formed 0xA5 reply means the parser is alive.
        """
        self._s.write(bytes(32))
        time.sleep(0.3)
        self._s.reset_input_buffer()
        self._cmd(CMD_STATUS)
        raw = self._read(2)
        if raw[0] not in STATUS_KEYS:
            raise IOError(f"parser still wedged: {raw.hex()}")

    def _mem_write(self, address: int, data: bytes):
        """Write an FCI config/FIFO block through the MCU.

        Unlike raw CMD_MEM_RD, CMD_MEM_WR needs no service-mode handshake.
        FCI config addresses also need no second DMA acknowledgement byte.
        """
        self._cmd(CMD_MEM_WR)
        self._s.write(address.to_bytes(4, "big"))
        self._s.write(len(data).to_bytes(4, "big"))
        self._s.write(b"\0")
        self._s.write(data)

    def launch_sd(self, sd_path: str):
        """Reset to the menu and ask it to launch an SD ROM immediately.

        This is edlink's Mega Drive ResetToMenu + AppInstall + AppStart
        sequence without its six-second menu-response timeout loop.  The
        install command is queued in the FPGA FIFO as soon as reset is
        released; the menu consumes it when ready and returns one result byte.
        """
        # Assert reset, clear the mapper config, then release reset.  These are
        # the same three operations performed by edlink ResetToMenu(soft).
        self._cmd(CMD_HOST_RST)
        self._s.write(b"\x01")
        time.sleep(0.05)
        self._mem_write(ADDR_FCI_CFG, bytes(256))
        self._cmd(CMD_HOST_RST)
        self._s.write(b"\0")

        path = sd_path if sd_path.startswith("sd:") else f"sd:{sd_path}"
        encoded = path.encode("ascii")
        self._mem_write(ADDR_FCI_FIFO, b"*i")
        self._mem_write(ADDR_FCI_FIFO, len(encoded).to_bytes(2, "big"))
        self._mem_write(ADDR_FCI_FIFO, encoded)
        # Normal acknowledgement is prompt, but retain edlink's tolerance for
        # a slow SD/menu boot without imposing that delay on the usual path.
        old_timeout = self._s.timeout
        self._s.timeout = 7
        try:
            result = self._read(1)[0]
        finally:
            self._s.timeout = old_timeout
        if result:
            raise IOError(f"menu rejected launch path (error 0x{result:02x})")
        self._mem_write(ADDR_FCI_FIFO, b"*s")

    # -- commands ---------------------------------------------------
    def sys_info(self) -> SysInfo:
        self._cmd(CMD_SYS_INF)
        raw = self._read(64)
        return SysInfo(
            boot_ctr=int.from_bytes(raw[28:32], "big"),
            game_ctr=int.from_bytes(raw[32:36], "big"),
            sw_ver=int.from_bytes(raw[44:46], "big"),
            device_id=raw[50],
        )

    def rom_path(self, path_type: int = 0) -> str:
        """SD path of the currently loaded ROM, straight from the MCU.

        The authoritative "what is running" answer -- unlike recent.dat,
        which can go stale (once made us evaluate the wrong game's
        achievements). path_type: 0=rom, 1=cue. Raises IOError on old
        firmware (<v24) so callers can fall back to recent.dat.
        """
        self._cmd(CMD_ROM_PATH)
        self._s.write(bytes([path_type]))
        path = self._rx_string()
        self._check_status()
        return path.split("\0", 1)[0]

    def file_size(self, path: str) -> int:
        """True size of an SD file (CMD_F_FINFO).

        Needed before read_file: CMD_F_FRD does NOT signal EOF and happily
        returns junk past the end (hashed 8MB of a 2MB ROM once).
        """
        self._cmd(CMD_F_FINFO)
        self._tx_string(path)
        resp = self._read(1)[0]
        if resp:
            raise IOError(f"file_info {path!r}: error 0x{resp:02x}")
        info = self._read(9)
        ln = int.from_bytes(self._read(2), "big")
        self._read(ln)  # name echo, unused
        return int.from_bytes(info[0:4], "big")

    def read_file(self, path: str, max_len: int | None = None,
                  block_size: int = 4096) -> bytes:
        """Read an SD file (MCU-side; safe while a game runs)."""
        if max_len is None:
            max_len = self.file_size(path)
        self._cmd(CMD_F_FOPN)
        self._s.write(bytes([FA_READ]))
        self._tx_string(path)
        self._check_status()

        chunks = []
        got = 0
        try:
            while got < max_len:
                # Hardware-probed on MED Pro firmware 26.0623: 4096-byte
                # requests are stable; 8192-byte requests corrupt framing.
                block = min(block_size, max_len - got)
                self._cmd(CMD_F_FRD)
                self._s.write(block.to_bytes(4, "big"))
                resp = self._read(1)[0]
                if resp:  # nonzero = EOF/error from device
                    break
                chunks.append(self._read(block))
                got += block
        finally:
            self._cmd(CMD_F_FCLOSE)
            self._check_status()
        return b"".join(chunks)

    def write_file(self, path: str, data: bytes):
        """Write an SD file (created/truncated). 1KB acked blocks."""
        self._cmd(CMD_F_FOPN)
        self._s.write(bytes([FA_CREATE_ALWAYS | FA_WRITE]))
        self._tx_string(path)
        self._check_status()
        try:
            self._cmd(CMD_F_FWR)
            self._s.write(len(data).to_bytes(4, "big"))
            off = 0
            while off < len(data):
                resp = self._read(1)[0]
                if resp:
                    raise IOError(f"write {path!r}: error 0x{resp:02x}")
                block = data[off:off + 1024]
                self._s.write(block)
                off += len(block)
        finally:
            self._cmd(CMD_F_FCLOSE)
            self._check_status()

    def delete_file(self, path: str):
        self._cmd(CMD_F_DEL)
        self._tx_string(path)
        self._check_status()

    def file_exists(self, path: str) -> bool:
        try:
            self.file_size(path)
            return True
        except IOError:
            return False

    def rename_file(self, src: str, dst: str):
        """No rename in the MCU protocol: copy through the PC, verify,
        delete the original."""
        data = self.read_file(src)
        self.write_file(dst, data)
        if self.file_size(dst) != len(data):
            raise IOError(f"rename verify failed: {dst} size mismatch")
        self.delete_file(src)

    def list_dir(self, path: str, args: int = 0) -> list[tuple[str, int, bool]]:
        """[(name, size, is_dir)] for an SD directory."""
        self._cmd(CMD_F_DIR_LD)
        self._s.write(bytes([args]))
        self._tx_string(path)
        self._check_status()

        self._cmd(CMD_F_DIR_SIZE)
        n = int.from_bytes(self._read(2), "big")

        out = []
        if n:
            self._cmd(CMD_F_DIR_GET)
            self._s.write((0).to_bytes(2, "big") + n.to_bytes(2, "big")
                          + (255).to_bytes(2, "big"))
            for _ in range(n):
                if self._read(1)[0]:
                    break
                info = self._read(9)
                size = int.from_bytes(info[0:4], "big")
                is_dir = bool(info[8] & 0x10) or info[8] == 1
                ln = int.from_bytes(self._read(2), "big")
                name = self._read(ln).decode("ascii", "replace")
                out.append((name, size, is_dir))
        return out


class _Deadline:
    """Force-closes an EdProSerial if the wrapped block outlives `seconds`."""

    def __init__(self, dev: EdProSerial, seconds: float):
        self._dev = dev
        self._seconds = seconds
        self._timer: threading.Timer | None = None

    def __enter__(self):
        self._timer = threading.Timer(self._seconds, self._force_close)
        self._timer.daemon = True
        self._timer.start()
        return self

    def _force_close(self):
        try:
            self._dev.close()
        except Exception:
            pass

    def __exit__(self, *exc):
        self._timer.cancel()
        return False
