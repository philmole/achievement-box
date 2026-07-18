"""Achievements mode toggle (CLI + library for the web UI).

Achievements need our sniffer core; the factory core has the in-game
menu + save states. The OS loads <games folder>/mapper.rbf as the core
for every launch from that folder, so the mode switch is just that file:

  ON:  mapper.rbf present (our core)     -> launches earn achievements
  OFF: renamed to mapper.rbf.bk          -> factory core, full IGM

Usage:
  python ra_toggle.py status|on|off [--dir "MEGA DRIVE"] [--port COM5]

`on` uploads our built core if neither file exists yet.
"""

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from achievementbox.edpro import EdProSerial
from config import CORE_RBF as SNIFFER_RBF, MD_ROM_DIR as DEFAULT_GAMES_DIR

ACTIVE_NAME = "mapper.rbf"
PARKED_NAME = "mapper.rbf.bk"
ORIGINAL_NAME = "mapper.rbf.original.bak"
LEGACY_CORE_SHA256 = {
    # 2026-07-14 prebuilt: WRAM sniffer + TH-write-dependent combo detector.
    "911340c105aa22292dd04a2e52069c3e80a50fff2bc1e948052a512ded48b169",
    # 2026-07-15 prebuilt: TH-row detector, before dual-port diagnostics.
    "806a61a512fc67fa09678dd2dd404d65a15db00e426943db5007b2415e0c08fc",
    # 2026-07-15 diagnostic: dual-port rows, before ignoring TH-high samples.
    "3156f9eb9c29a335439ccf983849d7b09c12d2f7ab70524193d1203d7de58cf1",
    # 2026-07-15 prebuilt: fixed combo detector, before PAL-region telemetry.
    "eae8b57b837ddf85c28805db0aa5cadc9b8877e19a8a83004149994898897189",
    # 2026-07-15/16 uncommitted intermediate build. Found live on the SD
    # 2026-07-17: an earlier `on` didn't recognise it and preserved it as
    # mapper.rbf.original.bak, so every later `off` restored a sniffer core
    # instead of the factory core.
    "a8ff9af34adfd9da80a9a149f11690df0ac7e5f21b7df80a9f6c6ed68318d233",
    # 2026-07-16 prebuilt shipped with the 0.1.0 release prep (e5d8164),
    # before frame_watch. Carts staged from that tree hold this core.
    "13d96ba13c762af61d2ecb56f6f4d9fb59d50df421baa406a53a9410ed31a993",
}


def _same_as_core(dev: EdProSerial, path: str, core: bytes) -> bool:
    """True only when the SD file is our exact shipped bitstream."""
    try:
        return dev.file_size(path) == len(core) and dev.read_file(path) == core
    except IOError:
        return False


def _is_achievement_core(dev: EdProSerial, path: str, core: bytes) -> bool:
    """Recognise the current bitstream and known shipped predecessors."""
    try:
        data = dev.read_file(path)
        return (data == core or
                hashlib.sha256(data).hexdigest() in LEGACY_CORE_SHA256)
    except IOError:
        return False


def list_mapper_dirs(dev: EdProSerial,
                     games_dir: str = DEFAULT_GAMES_DIR) -> list[str]:
    """The configured games folder and every descendant directory."""
    found = []
    pending = [games_dir.rstrip("/")]
    while pending:
        folder = pending.pop()
        found.append(folder)
        children = []
        for name, _size, is_dir in dev.list_dir(folder):
            if is_dir and not name.startswith("."):
                children.append(f"{folder}/{name}")
        pending.extend(reversed(sorted(children, key=str.lower)))
    return found


def get_mode(dev: EdProSerial, games_dir: str = DEFAULT_GAMES_DIR) -> bool:
    """True = achievements ON (our core stages the next launch)."""
    return dev.file_exists(f"{games_dir}/mapper.rbf")


def set_mode(dev: EdProSerial, on: bool,
             games_dir: str = DEFAULT_GAMES_DIR) -> str:
    """Switch mode; returns a human-readable description of what changed.
    Takes effect from the next menu launch."""
    core = SNIFFER_RBF.read_bytes()
    rbf = f"{games_dir}/{ACTIVE_NAME}"
    bak = f"{games_dir}/{PARKED_NAME}"
    original = f"{games_dir}/{ORIGINAL_NAME}"
    active = dev.file_exists(rbf)
    parked = dev.file_exists(bak)

    if on:
        if active and _same_as_core(dev, rbf, core):
            return "already ON"
        if active and _is_achievement_core(dev, rbf, core):
            dev.write_file(rbf, core)
            return "achievements ON (core upgraded)"
        if parked and not _is_achievement_core(dev, bak, core):
            raise IOError(f"refusing to overwrite unknown {bak}")
        if active:
            if dev.file_exists(original):
                raise IOError(f"refusing to overwrite existing {original}")
            dev.rename_file(rbf, original)
        if parked and _is_achievement_core(dev, bak, core):
            if _same_as_core(dev, bak, core):
                dev.rename_file(bak, rbf)
                return "achievements ON (core restored; original preserved)"
            dev.delete_file(bak)
            dev.write_file(rbf, core)
            return "achievements ON (parked core upgraded)"
        dev.write_file(rbf, core)
        return "achievements ON (core uploaded)"
    if active and not _is_achievement_core(dev, rbf, core):
        return "already OFF (non-Achievement Box mapper left untouched)"
    if not active and not dev.file_exists(original):
        return "already OFF"
    if active:
        if parked:
            if not _is_achievement_core(dev, bak, core):
                raise IOError(f"refusing to overwrite unknown {bak}")
            dev.delete_file(bak)
        dev.rename_file(rbf, bak)
    if dev.file_exists(original):
        if dev.file_exists(rbf):
            raise IOError(f"refusing to overwrite active {rbf}")
        dev.rename_file(original, rbf)
        return "achievements OFF (original mapper restored)"
    return "achievements OFF (factory core; in-game menu back)"


def set_mode_all(dev: EdProSerial, on: bool,
                 games_dir: str = DEFAULT_GAMES_DIR) -> str:
    """Apply the RA mapper state to the root and every subfolder."""
    folders = list_mapper_dirs(dev, games_dir)
    changed = 0
    for folder in folders:
        result = set_mode(dev, on, folder)
        if not result.startswith("already "):
            changed += 1
    state = "ON" if on else "OFF"
    return (f"achievements {state} across {len(folders)} folders"
            f" ({changed} changed)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["status", "on", "off"])
    ap.add_argument("--dir", default=DEFAULT_GAMES_DIR)
    ap.add_argument("--port", default="auto",
                    help='cart COM port, or "auto" to detect')
    args = ap.parse_args()

    if args.port == "auto":
        from achievementbox.edpro import find_cart_port
        found = find_cart_port()
        if not found:
            raise SystemExit("no Mega EverDrive Pro found; pass --port COMx")
        args.port = found

    with EdProSerial(args.port) as dev:
        dev.recover()
        if args.action == "status":
            active = get_mode(dev, args.dir)
            parked = dev.file_exists(f"{args.dir}/mapper.rbf.bk")
            mode = ("ACHIEVEMENTS (our core)" if active
                    else "NORMAL (factory core, in-game menu)")
            print(f"{args.dir}: {mode}"
                  + ("" if active or parked else " -- no core staged; "
                     "'on' will upload it"))
            return
        print(set_mode_all(dev, args.action == "on", args.dir))
        print("takes effect from the next game launch (menu) -- "
              "no power cycle needed")


if __name__ == "__main__":
    main()
