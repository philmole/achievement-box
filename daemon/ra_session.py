"""Achievement session v2: right-game guarantee + game-follow.

Design (after the After Burner 2 incident, where a stale recent.dat made
v1 relaunch and evaluate the WRONG game):

  - NEVER relaunches anything. The sniffer core comes from the toggle
    (`ra_toggle.py on` stages mapper.rbf in the games folder); every menu
    launch then uses it. If the factory core is running instead, the
    liveness guard refuses to evaluate (garbage memory = false unlocks).
  - Identifies the running game by asking the MCU (CMD_ROM_PATH, fw v24+),
    falling back to recent.dat only on old firmware, then hashes the ROM
    file off the SD (MCU-side, safe while the game runs).
  - Follows game changes: when the console returns to the menu (Start+Down
    or reset), the set is unloaded immediately; when the next game boots,
    it is identified and its set loaded -- one long session, one login.

Credentials come from RA_USER / RA_PASS environment variables, or from
daemon/.env (KEY=VALUE lines; see .env.example). No CLI args: passwords
on a command line land in shell history and the process list.

Usage:
  python ra_session.py [--port COM5]
"""

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from achievementbox.edpro import EdProSerial
from achievementbox.memory import MedProBackend
from achievementbox.rcbridge import RcClient
# EDLINK/ENV_FILE/load_env_file re-exported here so importers (webapp) keep
# their `from ra_session import ...`; the real home is config.py.
from config import EDLINK, ENV_FILE, ROOT, load_env_file

RECENT_DAT = "MEGA/sys/recent.dat"
TARGET_FPS = 30.0
SETTLE_SECONDS = 1.0  # rom_path is MCU-side and valid as soon as it boots


def stamp() -> str:
    return time.strftime("%H:%M:%S")


def identify_running_game(port: str,
                          on_path=None) -> tuple[str, str]:
    """(sd_path, md5) for the game running RIGHT NOW.

    rom_path comes from the MCU's own record of what it loaded --
    recent.dat is only a fallback for pre-v24 firmware, since a stale
    entry there once pointed us at the wrong game entirely.

    The exact file is read and hashed on every launch. A path/size cache is not
    safe here: a replacement ROM can keep both while changing its contents.
    Every caller passes the freshly computed hash to rc_client.load_game(hash).
    on_path (optional) is called with sd_path as soon as it's known,
    so a UI can show which game is coming before hashing finishes.
    """
    # A Mega CD title's BIOS can take the cart off USB moments after a
    # transient "game running" mapper status caught this identify path
    # starting (2026-07-17, hardware-confirmed) -- deadline guards every
    # blocking call below so a vanished cart raises instead of hanging
    # the caller (and everything queued behind it) forever.
    IDENTIFY_DEADLINE = 20.0
    with EdProSerial(port) as dev, dev.deadline(IDENTIFY_DEADLINE):
        dev.recover()
        try:
            sd_path = dev.rom_path()
            source = "rom_path"
        except IOError as e:
            raw = dev.read_file(RECENT_DAT, max_len=512)
            sd_path = raw.split(b"\0", 1)[0].decode("ascii", "replace")
            source = f"recent.dat fallback (rom_path: {e})"
        if not sd_path:
            raise SystemExit("device reports no loaded ROM -- "
                             "launch a game from the console menu first")
        print(f"[{stamp()}] running game ({source}): {sd_path}")
        if on_path:
            on_path(sd_path)

        size = dev.file_size(sd_path.lstrip("/"))
        print(f"[{stamp()}] reading launched ROM from SD for hashing "
              f"(MCU-side, game keeps running)...", flush=True)
        t0 = time.monotonic()
        rom = dev.read_file(sd_path.lstrip("/"), max_len=size)
        if len(rom) != size:
            raise IOError(f"short ROM read: expected {size:,} bytes, "
                          f"received {len(rom):,}")
        md5 = hashlib.md5(rom).hexdigest()
        print(f"[{stamp()}] {len(rom):,} bytes in "
              f"{time.monotonic()-t0:.0f}s, md5={md5}")
    return sd_path, md5


def verify_sniffer_core(backend: MedProBackend):
    """Refuse to evaluate unless OUR core is demonstrably running.

    The factory core (toggle OFF) leaves the shadow window as stale SRAM:
    rc_client would see frozen garbage and could fire false unlocks. A
    live game writes WRAM thousands of times per second, so a static
    write counter means the sniffer isn't there.
    """
    c0 = backend.read_write_counter()
    time.sleep(0.5)
    c1 = backend.read_write_counter()
    if c1 == c0:
        raise SystemExit(
            "sniffer core is NOT active (WRAM write counter static) -- "
            "run `ra_toggle.py on` and relaunch the game from the menu")
    print(f"[{stamp()}] sniffer core live "
          f"(+{c1 - c0:,} WRAM writes/0.5s)", flush=True)


def wait_for_menu(backend: MedProBackend):
    while backend.in_game() is not False:
        time.sleep(1.0)


def run_game_session(client: RcClient, backend: MedProBackend, md5: str):
    """Load the set for md5 and evaluate frames until the console leaves
    the game. Returns when the console is back at the menu."""
    try:
        # Always identify/start the RA session, even when md5 came from cache.
        client.load_game(md5)
    except RuntimeError as e:
        print(f"[{stamp()}] no RA set for this ROM ({e}) -- "
              f"idling until the console returns to the menu", flush=True)
        wait_for_menu(backend)
        return

    info = client.game_info()
    s = client.summary()
    print(f"[{stamp()}] RA game #{info.get('id')}: {info.get('title')}")
    print(f"[{stamp()}] achievements: {s['unlocked']}/{s['total']} "
          f"unlocked, {s['points']} points possible")
    print(f"[{stamp()}] watching memory -- go earn something! "
          f"(Start+Down for menu, Ctrl+C to stop)", flush=True)

    frame_budget = 1.0 / TARGET_FPS
    frames = 0
    t_report = time.monotonic()
    t_check = time.monotonic()
    try:
        while True:
            t0 = time.monotonic()
            client.do_frame()
            frames += 1
            if time.monotonic() - t_check >= 0.5:
                t_check = time.monotonic()
                if backend.in_game() is False:
                    print(f"[{stamp()}] console returned to menu",
                          flush=True)
                    break
            if time.monotonic() - t_report >= 30:
                print(f"[{stamp()}] alive: "
                      f"{frames/(time.monotonic()-t_report):.0f} fps, "
                      f"drops {backend.read_drop_count()}", flush=True)
                frames, t_report = 0, time.monotonic()
            dt = time.monotonic() - t0
            if dt < frame_budget:
                time.sleep(frame_budget - dt)
    finally:
        # menu owns WRAM now -- stop evaluating IMMEDIATELY
        client.unload_game()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="auto",
                    help='cart COM port, or "auto" to detect')
    args = ap.parse_args()

    if args.port == "auto":
        from achievementbox.edpro import find_cart_port
        found = find_cart_port()
        if not found:
            raise SystemExit("no Mega EverDrive Pro found -- plug it in "
                             "(and not in a Mega CD game), or pass --port")
        args.port = found
        print(f"[{stamp()}] cart detected on {args.port}")

    load_env_file(ENV_FILE)
    user = os.environ.get("RA_USER")
    password = os.environ.get("RA_PASS")
    if not user or not password:
        raise SystemExit("set RA_USER and RA_PASS (environment or "
                         f"{ENV_FILE} -- see .env.example)")

    backend: MedProBackend | None = None

    def read_wram(ra_addr: int, length: int) -> bytes:
        if backend is None:
            raise IOError("backend not connected")
        return backend.read_ra(ra_addr, length)  # RA byte order (N^1 swap)

    def on_event(kind: str, info: dict):
        if kind == "unlock":
            print("\n" + "=" * 60)
            print(f"  ACHIEVEMENT UNLOCKED!  [{info['points']} pts]")
            print(f"  {info['title']}")
            print(f"  {info['description']}")
            print("=" * 60 + "\n", flush=True)
        elif kind == "mastered":
            print(f"\n[{stamp()}] *** GAME MASTERED ***\n", flush=True)

    client = RcClient(read_wram, on_event)
    try:
        print(f"[{stamp()}] logging in as {user} (softcore)...")
        client.login(user, password)
        print(f"[{stamp()}] login ok", flush=True)

        backend = MedProBackend(EDLINK)
        while True:
            if backend.in_game() is not True:
                print(f"[{stamp()}] waiting for a game launch...",
                      flush=True)
                while backend.in_game() is not True:
                    time.sleep(1.0)
                time.sleep(SETTLE_SECONDS)

            # identification needs the raw serial port -- hand it over
            backend.close()
            try:
                _, md5 = identify_running_game(args.port)
            except SystemExit as e:
                # "No loaded ROM" can be a transient empty CMD_ROM_PATH
                # reply while a game is genuinely running (hardware-
                # observed 2026-07-17), not just "nothing launched yet" --
                # treat it as retryable rather than ending the session.
                print(f"[{stamp()}] identify failed ({e}) -- retrying",
                      flush=True)
                backend = MedProBackend(EDLINK)
                time.sleep(1.0)
                continue
            backend = MedProBackend(EDLINK)

            verify_sniffer_core(backend)
            run_game_session(client, backend, md5)
    except KeyboardInterrupt:
        print(f"\n[{stamp()}] session ended")
    finally:
        client.close()
        if backend is not None:
            backend.close()


if __name__ == "__main__":
    main()
