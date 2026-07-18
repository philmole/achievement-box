"""Achievement Box web app: live session + phone-facing UI.

One process owns the hardware (COM port rule: edlink stdio and the raw
MCU client can't coexist, so everything is marshalled through a single
worker thread) and serves the web UI on top:

  worker thread   ra_session v2 supervisor loop (identify via
                  CMD_ROM_PATH, core-liveness guard, game-follow) plus a
                  request queue for web actions that need the port
                  (toggle, SD library scan, launch).
  FastAPI/asyncio REST + WebSocket; state snapshots and unlock events
                  are pushed to every connected browser.

The achievement list comes from the patch/startsession responses
rc_client already fetches (response_log tap) -- no extra API calls.
Badges and box art are cached on disk after first fetch so WiFi blips
never blank the UI.

Usage:
  .venv\\Scripts\\python daemon\\webapp.py [--port COM5] [--http 8000]
  (RA_USER/RA_PASS from environment or daemon/.env, as ra_session)

Do NOT run ra_session.py at the same time -- this replaces it.
"""

import argparse
import asyncio
import base64
import binascii
import faulthandler
import hmac
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from achievementbox import gamelib
from achievementbox.edpro import EdProSerial
from achievementbox.memory import MedProBackend
from achievementbox.rcbridge import RcClient
from achievementbox.region_watch import RegionMonitor
from achievementbox.version import USER_AGENT
import ra_toggle
from ra_session import (EDLINK, ENV_FILE, TARGET_FPS, SETTLE_SECONDS,
                        identify_running_game, load_env_file, stamp)

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocket, WebSocketDisconnect

from achievementbox.statehub import Hub

ROOT = Path(__file__).parent.parent
WEBUI = ROOT / "webui"
BADGE_DIR = gamelib.CACHE_DIR / "badges"
SYSTEM_ICON_DIR = gamelib.CACHE_DIR / "system-icons"
VAPID_FILE = gamelib.CACHE_DIR / "vapid.json"
PUSH_SUBS_FILE = gamelib.CACHE_DIR / "push_subs.json"
TOGGLE_PREF_FILE = gamelib.CACHE_DIR / "ra_enabled.json"
MEDIA_HOST = "https://media.retroachievements.org"
RA_SYSTEM_ICON_HOST = (
    "https://raw.githubusercontent.com/RetroAchievements/RAWeb/"
    "master/public/assets/images/system"
)
GAMES_DIR = ra_toggle.DEFAULT_GAMES_DIR
CD_DIR = "MEGA CD"  # --cd-dir overrides; Mega CD launches are
                    # fire-and-forget: the cart LEAVES the USB bus while
                    # a CD game runs (MCU becomes the CD drive), so no
                    # achievements and no live state until it's back
SCAN_DIRS: list[tuple[str, str]] = []  # filled in main()
CD_RESUME_GRACE = 10.0  # seconds USB must stay gone before a remembered CD
                        # selection is shown as a resumed cd-session; shorter
                        # drops are cartridge ROM switches / transient errors


hub = Hub()


# ---------------------------------------------------------------------
# HTTPS: box-generated CA (install its root on the phone once -> real
# secure context: proper PWA install, push without flags or spam labels)
# ---------------------------------------------------------------------

HTTPS_DIR = gamelib.CACHE_DIR / "https"


def _cert_names() -> tuple[list[str], list[str]]:
    """(dns_names, ip_strings) the server cert must cover right now."""
    import socket
    dns = [f"{MDNS_NAME}.local", socket.gethostname(), "localhost"]
    ips = ["127.0.0.1"] + lan_ips()
    return dns, ips


def ensure_https_certs() -> tuple[Path, Path, Path]:
    """(ca_cert, server_cert, server_key). The root CA is generated once
    and its key kept, so the phone-installed rootca.pem stays valid for
    the box's lifetime; the server cert is re-issued under it whenever
    the LAN IPs/hostname drift out of its SANs or it nears expiry."""
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from ipaddress import ip_address

    ca_crt = HTTPS_DIR / "rootca.pem"
    ca_key_f = HTTPS_DIR / "ca.key"
    srv_crt = HTTPS_DIR / "server.pem"
    srv_key_f = HTTPS_DIR / "server.key"
    HTTPS_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    name = lambda cn: x509.Name(
        [x509.NameAttribute(x509.NameOID.COMMON_NAME, cn)])
    pem = serialization.Encoding.PEM

    def write_key(path: Path, key):
        path.write_bytes(key.private_bytes(
            pem, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        try:
            path.chmod(0o600)
        except OSError:
            pass

    # -- root CA: create once, or load; pre-ca.key installs regenerate
    #    (their phones must reinstall rootca.pem -- say so loudly)
    if ca_crt.exists() and ca_key_f.exists():
        ca = x509.load_pem_x509_certificate(ca_crt.read_bytes())
        ca_key = serialization.load_pem_private_key(
            ca_key_f.read_bytes(), password=None)
    else:
        if ca_crt.exists():
            print(f"[{stamp()}] HTTPS: CA key missing (pre-upgrade certs) "
                  f"-- new root CA; reinstall /rootca.pem on phones",
                  flush=True)
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca = (x509.CertificateBuilder()
              .subject_name(name("Achievement Box Root CA"))
              .issuer_name(name("Achievement Box Root CA"))
              .public_key(ca_key.public_key())
              .serial_number(x509.random_serial_number())
              .not_valid_before(now)
              .not_valid_after(now + timedelta(days=3650))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0),
                             critical=True)
              .add_extension(x509.KeyUsage(
                  digital_signature=False, content_commitment=False,
                  key_encipherment=False, data_encipherment=False,
                  key_agreement=False, key_cert_sign=True, crl_sign=True,
                  encipher_only=False, decipher_only=False), critical=True)
              .sign(ca_key, hashes.SHA256()))
        ca_crt.write_bytes(ca.public_bytes(pem))
        write_key(ca_key_f, ca_key)
        srv_crt.unlink(missing_ok=True)   # was signed by the old CA

    # -- server cert: reuse only while it covers today's names/IPs
    dns_names, ip_strs = _cert_names()
    if srv_crt.exists() and srv_key_f.exists():
        crt = x509.load_pem_x509_certificate(srv_crt.read_bytes())
        try:
            san = crt.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value
            have_dns = set(san.get_values_for_type(x509.DNSName))
            have_ips = {str(ip) for ip in
                        san.get_values_for_type(x509.IPAddress)}
        except x509.ExtensionNotFound:
            have_dns, have_ips = set(), set()
        fresh = (set(dns_names) <= have_dns and set(ip_strs) <= have_ips
                 and crt.not_valid_after_utc > now + timedelta(days=30))
        if fresh:
            return ca_crt, srv_crt, srv_key_f
        print(f"[{stamp()}] HTTPS: server cert stale "
              f"(IPs/hostname changed or expiring) -- re-issuing under "
              f"the same CA", flush=True)

    sans = ([x509.DNSName(d) for d in dns_names]
            + [x509.IPAddress(ip_address(ip)) for ip in ip_strs])
    key = ec.generate_private_key(ec.SECP256R1())
    crt = (x509.CertificateBuilder()
           .subject_name(name(f"{MDNS_NAME}.local"))
           .issuer_name(ca.subject)
           .public_key(key.public_key())
           .serial_number(x509.random_serial_number())
           .not_valid_before(now).not_valid_after(now + timedelta(days=825))
           .add_extension(x509.SubjectAlternativeName(sans), critical=False)
           .add_extension(x509.ExtendedKeyUsage(
               [x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
           .sign(ca_key, hashes.SHA256()))

    srv_crt.write_bytes(crt.public_bytes(pem))
    write_key(srv_key_f, key)
    print(f"[{stamp()}] HTTPS certs ready in {HTTPS_DIR}", flush=True)
    return ca_crt, srv_crt, srv_key_f


def start_https(https_port: int) -> bool:
    """Uvicorns serving the same app over TLS: one on https_port, and a
    best-effort one on 443 so a bare https://<host> URL works (typing
    https:// against the plain-http port gives Chrome's scary
    'unsupported protocol or cipher suite' page). Returns True if 443
    was claimed."""
    ca, crt, key = ensure_https_certs()

    def serve(port: int):
        cfg = uvicorn.Config(app, host="0.0.0.0", port=port,
                             log_level="warning",
                             ssl_certfile=str(crt), ssl_keyfile=str(key))
        threading.Thread(target=uvicorn.Server(cfg).run, daemon=True,
                         name=f"https-server-{port}").start()

    serve(https_port)
    if https_port == 443:
        return True
    import socket
    try:  # probe first: uvicorn reports bind failures loudly + async
        socket.create_server(("0.0.0.0", 443)).close()
    except OSError as e:
        print(f"[{stamp()}] https on :443 unavailable ({e}); "
              f"use the :{https_port} URLs", flush=True)
        return False
    serve(443)
    return True


# ---------------------------------------------------------------------
# naming: mDNS + startup banner (the box needs a URL, not an IP)
# ---------------------------------------------------------------------

MDNS_NAME = "achievementbox"


def lan_ips() -> list[str]:
    """Real LAN IPv4s (skips loopback/link-local/virtual 192.168.56.*)."""
    import socket
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))       # no traffic sent; picks the route
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ip = info[4][0]
            if not (ip.startswith("127.") or ip.startswith("169.254.")
                    or ip.startswith("192.168.56.")):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def start_mdns(http_port: int):
    """Advertise http://achievementbox.local:<port> via mDNS/zeroconf.

    Resolves out-of-the-box on iOS/macOS/Windows/Linux. (Android Chrome
    can't resolve .local -- Android users get the IP/QR from the banner,
    or the router's device-name DNS.)"""
    try:
        import socket
        from zeroconf import Zeroconf, ServiceInfo
        ips = lan_ips()
        if not ips:
            return None
        zc = Zeroconf()
        info = ServiceInfo(
            "_http._tcp.local.",
            f"Achievement Box._http._tcp.local.",
            addresses=[socket.inet_aton(ip) for ip in ips],
            port=http_port,
            server=f"{MDNS_NAME}.local.",
            properties={"path": "/"})
        zc.register_service(info)
        return zc
    except Exception as e:
        print(f"[{stamp()}] mDNS unavailable: {e}", flush=True)
        return None


def start_port80_redirect(http_port: int):
    """Answer plain http://achievementbox.local (no port) with a
    redirect to the real port. Skipped quietly if 80 is taken/blocked."""
    if http_port == 80:
        return
    import socketserver

    class Redirect(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                request = self.rfile.readline().decode("latin1")
                host = "achievementbox.local"
                for line in iter(self.rfile.readline, b"\r\n"):
                    if line.lower().startswith(b"host:"):
                        host = line.split(b":", 1)[1].strip().decode(
                            "latin1").split(":")[0]
                path = request.split(" ")[1] if " " in request else "/"
                self.wfile.write(
                    f"HTTP/1.1 301 Moved Permanently\r\n"
                    f"Location: http://{host}:{http_port}{path}\r\n"
                    f"Content-Length: 0\r\nConnection: close\r\n\r\n"
                    .encode("latin1"))
            except Exception:
                pass

    try:
        srv = socketserver.ThreadingTCPServer(("0.0.0.0", 80), Redirect)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv
    except OSError as e:
        print(f"[{stamp()}] port-80 redirect unavailable ({e}); "
              f"use the :{http_port} URLs", flush=True)
        return None


def print_banner(http_port: int, https_port: int = 0,
                 https_443: bool = False):
    """All the ways to reach the box, plus a scan-me QR for phones."""
    import socket
    ips = lan_ips()
    urls = []
    if https_port:  # lead with https -- it's the real (trusted) origin
        if https_443:
            urls += [f"https://{MDNS_NAME}.local"]
            urls += [f"https://{ip}" for ip in ips]
        else:
            urls += [f"https://{MDNS_NAME}.local:{https_port}"]
            urls += [f"https://{ip}:{https_port}" for ip in ips]
    urls += [f"http://{MDNS_NAME}.local:{http_port}",
             f"http://{socket.gethostname()}:{http_port}"]
    urls += [f"http://{ip}:{http_port}" for ip in ips]
    print(f"\n[{stamp()}] Achievement Box is up:", flush=True)
    for u in urls:
        print(f"    {u}", flush=True)
    if https_port:
        print(f"    (for trusted https: install http://{ips[0] if ips else 'IP'}"
              f":{http_port}/rootca.crt on the phone once, as a "
              f"CA certificate)", flush=True)
    if ips:
        try:
            import qrcode
            qr = qrcode.QRCode(border=1)
            qr.add_data(f"http://{ips[0]}:{http_port}")
            qr.make()
            qr.print_ascii(invert=True)
            print("    (scan with the phone camera)", flush=True)
        except Exception:
            pass


# ---------------------------------------------------------------------
# web push (PWA notifications for unlocks, app closed or open)
# ---------------------------------------------------------------------

def _vapid() -> dict:
    """Load or create the VAPID keypair (identifies this box to the
    browser push services)."""
    if VAPID_FILE.exists():
        return json.loads(VAPID_FILE.read_text(encoding="utf-8"))
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    import base64
    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_numbers().private_value.to_bytes(32, "big")
    pub = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    keys = {"private": b64(priv), "public": b64(pub)}
    gamelib.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    VAPID_FILE.write_text(json.dumps(keys), encoding="utf-8")
    return keys


def _push_subs() -> list[dict]:
    try:
        return json.loads(PUSH_SUBS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_push_subs(subs: list[dict]):
    gamelib.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PUSH_SUBS_FILE.write_text(json.dumps(subs), encoding="utf-8")


def _toggle_preference() -> bool | None:
    """Return the user's saved RA choice, independently of ROM/core state."""
    try:
        value = json.loads(TOGGLE_PREF_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, bool) else None
    except Exception:
        return None


def _save_toggle_preference(on: bool):
    gamelib.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    TOGGLE_PREF_FILE.write_text(json.dumps(bool(on)), encoding="utf-8")


def push_to_phones(payload: dict):
    """Best-effort web push to every subscribed phone (worker thread;
    dead subscriptions are pruned)."""
    subs = _push_subs()
    if not subs:
        return
    from pywebpush import webpush, WebPushException
    keys = _vapid()
    alive = []
    for sub in subs:
        try:
            webpush(subscription_info=sub, data=json.dumps(payload),
                    vapid_private_key=keys["private"],
                    vapid_claims={"sub": "mailto:box@achievementbox.local"})
            alive.append(sub)
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", 0)
            if code not in (404, 410):   # gone = pruned; else keep trying
                alive.append(sub)
            print(f"[{stamp()}] push failed ({code}): {e}", flush=True)
        except Exception as e:
            alive.append(sub)
            print(f"[{stamp()}] push error: {e}", flush=True)
    if len(alive) != len(subs):
        _save_push_subs(alive)


# ---------------------------------------------------------------------
# RA response parsing (achievement list without extra API calls)
# ---------------------------------------------------------------------

def parse_session_responses(log: list[tuple[str, bytes]]) -> tuple[list, dict]:
    """(achievements, game_media) from rc_client's patch/startsession
    traffic. Requests all hit dorequest.php, so classify by body keys."""
    patch, unlocked_ids = None, set()
    for _url, body in log:
        try:
            data = json.loads(body)
        except Exception:
            continue
        if "PatchData" in data:
            patch = data["PatchData"]
        for key in ("Unlocks", "HardcoreUnlocks"):
            for u in data.get(key) or []:
                uid = u.get("ID") if isinstance(u, dict) else u
                if uid is not None:
                    unlocked_ids.add(uid)
    if not patch:
        return [], {}
    achievements = []
    for a in patch.get("Achievements", []):
        if a.get("Flags") != 3:  # core set only
            continue
        achievements.append({
            "id": a.get("ID"),
            "title": a.get("Title", ""),
            "description": a.get("Description", ""),
            "points": a.get("Points", 0),
            "badge": str(a.get("BadgeName", "")),
            "unlocked": a.get("ID") in unlocked_ids,
        })
    icon = patch.get("ImageIcon") or patch.get("ImageIconURL") or ""
    if icon and not icon.startswith("http"):
        icon = MEDIA_HOST + (icon if icon.startswith("/")
                             else f"/Images/{icon}.png")
    media = {"icon": icon, "title": patch.get("Title", ""),
             "game_id": patch.get("ID")}
    return achievements, media


# ---------------------------------------------------------------------
# hardware worker
# ---------------------------------------------------------------------

class HwRequest:
    def __init__(self, action: str, payload=None):
        self.action = action
        self.payload = payload
        self.done = threading.Event()
        self.result = None
        self.error: str | None = None

    def finish(self, result=None, error=None):
        self.result, self.error = result, error
        self.done.set()


class HwWorker(threading.Thread):
    """Owns the COM port. Runs the achievement session; services web
    requests (toggle/scan/launch) by borrowing the port between frames."""

    def __init__(self, com_port: str, user: str, password: str):
        super().__init__(daemon=True, name="hw-worker")
        self._port_arg = com_port      # "auto" or a fixed COMx
        self.com_port = None if com_port == "auto" else com_port
        self.user = user
        self.password = password
        self.requests: queue.Queue[HwRequest] = queue.Queue()
        self.backend: MedProBackend | None = None
        self.client: RcClient | None = None
        self.evaluating_achievements = False
        self.toggle_in_flight = False
        # A Mega CD launch temporarily removes the cart from USB. Remember
        # that disappearance so a later reconnect directly into a Mega Drive
        # game is not mistaken for the still-running CD session.
        self._cd_cart_gone = False
        # CD title the MCU still reported selected after the console came
        # back to the menu. A later USB drop with this set means the title
        # was relaunched from the console menu -- resume the session display.
        self._last_cd_path: str | None = None
        # When USB first failed detection. A cartridge ROM switch restores
        # USB within a few seconds; only a drop that outlives
        # CD_RESUME_GRACE is treated as a CD relaunch (see _supervise).
        self._usb_gone_since: float | None = None
        # The mapper status flickers to "menu" for a moment while a CD
        # title boots; a single menu read must not tear the session down.
        self._cd_menu_reads = 0
        # One-shot log guard for the mapper-says-game / MCU-has-nothing
        # state (console powered off, or pre-classification undecided).
        self._unidentified_noted = False

    def submit(self, action: str, payload=None, timeout=90) -> HwRequest:
        req = HwRequest(action, payload)
        self.requests.put(req)
        if not req.done.wait(timeout):
            req.error = "hardware worker timeout"
        return req

    # -- raw-serial helpers (port must be free) ----------------------
    def _with_serial(self, fn, deadline: float | None = None):
        """deadline (seconds), when given, force-closes the port if fn
        hangs -- for calls that can race a cart vanishing off USB
        mid-transaction (e.g. a Mega CD boot). Scans/toggles that can
        legitimately run long leave this unset."""
        with EdProSerial(self.com_port) as dev:
            dev.recover()
            if deadline is None:
                return fn(dev)
            with dev.deadline(deadline):
                return fn(dev)

    def _service_requests(self) -> bool:
        """Run queued web actions. Closes the edlink session around raw
        serial / edlink CLI use; caller's loop reopens on demand.
        Returns True if a game was launched (caller must re-identify)."""
        launched = False
        while True:
            try:
                req = self.requests.get_nowait()
            except queue.Empty:
                return launched
            self._close_backend()
            try:
                if req.action == "toggle_get":
                    req.finish(self._with_serial(
                        lambda d: ra_toggle.get_mode(d, GAMES_DIR)))
                elif req.action == "toggle_set":
                    if isinstance(req.payload, tuple):
                        on, games_dir = req.payload
                    else:  # compatibility with callers predating folder scope
                        on, games_dir = bool(req.payload), GAMES_DIR
                    toggle_fn = (ra_toggle.set_mode_all
                                 if games_dir == GAMES_DIR
                                 else ra_toggle.set_mode)
                    msg = self._with_serial(
                        lambda d: toggle_fn(d, bool(on), games_dir))
                    if games_dir == GAMES_DIR:
                        _save_toggle_preference(bool(on))
                        hub.update(toggle=bool(on))
                    req.finish(msg)
                elif req.action == "discover":
                    dirs = self._with_serial(gamelib.discover_dirs)
                    # keep launch validation / CD detection in sync with
                    # whatever folders actually hold ROMs right now
                    SCAN_DIRS[:] = dirs
                    req.finish(dirs)
                elif req.action == "scan_folder":
                    folder, system = req.payload
                    req.finish(self._with_serial(
                        lambda d: gamelib.scan_folder(d, folder, system)))
                elif req.action == "launch":
                    req.finish(self._launch(str(req.payload)))
                    # Mega CD intentionally removes the cart from USB. Keep
                    # its dedicated state instead of entering MD identify.
                    launched = (req.error is None
                                and not hub.state.get("cd_session"))
                else:
                    req.finish(error=f"unknown action {req.action}")
            except Exception as e:
                req.finish(error=str(e))

    def _launch(self, sd_path: str) -> str:
        import subprocess
        from achievementbox.memory import edlink_cmd
        # CD launches take the cart off the USB bus -- tell every browser
        # why the box is about to go dark
        cd = any(s == "mcd" and sd_path.startswith(f"{d}/")
                 for d, s in SCAN_DIRS)
        # edlink owns both sides of the menu's FCI FIFO exchange. The raw MCU
        # serial protocol can write the install request, but the menu's reply
        # is returned through the opposite FCI FIFO rather than USB serial.
        # Reading the serial stream directly therefore mistakes an unrelated
        # byte for the launch result (commonly 0x72, ASCII "r").
        launch_args = ["run", "--file", f"sd:{sd_path}"]
        # `edlink run` bypasses the menu's per-folder mapper selection. When
        # achievements mode is on, explicitly load the same mapper already
        # staged beside the ROM; otherwise web-launched games fall back to the
        # factory core and Start+Down disappears. Do not do this for Mega CD.
        if not cd and _toggle_preference() is True:
            launch_args.extend(["--fpga", str(ra_toggle.SNIFFER_RBF)])
        r = subprocess.run(
            edlink_cmd(EDLINK, *launch_args),
            capture_output=True, text=True, timeout=120)
        if "ok" not in r.stdout.lower():
            raise IOError(
                f"launch failed: {r.stdout}{r.stderr}".strip())
        self._last_cd_path = None
        if cd:
            self._cd_cart_gone = False
            self._show_cd_session(sd_path)
        else:
            hub.update(cd_session=False)
        return f"launched {sd_path}"

    # -- backend lifecycle -------------------------------------------
    def _open_backend(self) -> MedProBackend:
        if self.backend is None:
            self.backend = MedProBackend(EDLINK)
        return self.backend

    def _close_backend(self):
        if self.backend is not None:
            self.backend.close()
            self.backend = None

    def _show_cd_session(self, sd_path: str | None = None):
        """Present a CD/MD+ path without sending it through ROM hashing."""
        if not sd_path:
            hub.update(cd_session=True, connection="cd-session",
                       achievements=[], summary=None, rich_presence=None)
            return
        sd_path = sd_path.lstrip("/")
        entry = next((g for g in gamelib.cached_library()
                      if g.get("path") == sd_path), {})
        hub.update(cd_session=True, connection="cd-session",
                   game={"path": sd_path,
                         "title": entry.get("title") or Path(sd_path).stem,
                         "system": "mcd",
                         "stem": entry.get("stem") or Path(sd_path).stem},
                   achievements=[], summary=None, rich_presence=None)

    # -- session -------------------------------------------------------
    def run(self):
        unlock_sink = []

        def read_wram(addr, length):
            if self.backend is None:
                raise IOError("backend not connected")
            return self.backend.read_ra(addr, length)

        def on_event(kind, info):
            if (kind in ("unlock", "mastered")
                    and not self.evaluating_achievements):
                print(f"[{stamp()}] ignored {kind} event outside a recognised "
                      "game session", flush=True)
                return
            if kind == "unlock":
                print(f"[{stamp()}] UNLOCK: {info['title']} "
                      f"[{info['points']} pts]", flush=True)
                unlock_sink.append(info)
                game = (hub.state.get("game") or {}).get("title", "")
                threading.Thread(target=push_to_phones, daemon=True, args=({
                    # plain, informative text -- emoji-led titles score
                    # badly with Chrome's notification-spam classifier
                    "title": f"Achievement unlocked: {info['title']}",
                    "body": f"{info['description']} (+{info['points']} pts"
                            + (f", {game})" if game else ")"),
                    "icon": "https://media.retroachievements.org/Badge/"
                            f"{info['badge']}.png",
                    "tag": f"unlock-{info['id']}",
                },)).start()
            elif kind == "mastered":
                hub.event({"type": "mastered"})

        self.client = RcClient(read_wram, on_event)
        self.client.response_log = []
        try:
            hub.update(connection="logging-in", user=self.user,
                       ra_mode=self.client.mode)
            self.client.login(self.user, self.password)
            print(f"[{stamp()}] RA login ok ({self.user})", flush=True)
        except Exception as e:
            hub.update(connection="login-failed", error=str(e))
            print(f"[{stamp()}] RA login FAILED: {e}", flush=True)
            self.client.close()
            self.client = None
            return

        # The switch reflects the user's choice, not whether this particular
        # ROM was recognised or successfully loaded the achievement reader.
        on = _toggle_preference()
        if on is not None:
            hub.update(toggle=on)

        while True:
            try:
                self._supervise(unlock_sink)
            except Exception as e:
                print(f"[{stamp()}] worker error: {e}; retrying", flush=True)
                # Best-effort: a broken backend's own close() has raised here
                # before (OSError from an already-killed edlink child during
                # a power cycle, 2026-07-17) and silently ended this thread
                # with no self-recovery. Cleanup must never be allowed to
                # escape this handler.
                try:
                    self._close_backend()
                except Exception as close_err:
                    print(f"[{stamp()}] backend cleanup failed ({close_err}) "
                          "-- continuing anyway", flush=True)
                    self.backend = None
                if self._port_arg == "auto":
                    self.com_port = None  # re-detect: port may have moved
                connection = ("cd-session" if hub.state.get("cd_session")
                              else "offline")
                hub.update(connection=connection, game=None,
                           achievements=[], summary=None,
                           rich_presence=None)
                time.sleep(2)

    def _supervise(self, unlock_sink: list):
        if self.com_port is None:  # cart not seen yet / gone -- re-detect
            from achievementbox.edpro import find_cart_port
            found = find_cart_port()
            if not found:
                if self._usb_gone_since is None:
                    self._usb_gone_since = time.monotonic()
                if hub.state.get("cd_session"):
                    self._cd_cart_gone = True
                elif (self._last_cd_path is not None
                      and time.monotonic() - self._usb_gone_since
                      >= CD_RESUME_GRACE):
                    # USB dropping while a CD title is still the MCU's
                    # selection means it was relaunched from the console
                    # menu -- bring the session display back. A cartridge
                    # ROM switch also drops USB but restores it within a
                    # few seconds, so only a drop that outlives the grace
                    # period counts (a booting cartridge game must never
                    # resurrect the old CD title).
                    print(f"[{stamp()}] cart gone with CD title selected -- "
                          f"resuming cd-session ({self._last_cd_path})",
                          flush=True)
                    self._cd_cart_gone = True
                    self._show_cd_session(self._last_cd_path)
                connection = ("cd-session" if hub.state.get("cd_session")
                              else "offline")
                if hub.state.get("connection") != connection:
                    hub.update(connection=connection, game=None,
                               achievements=[], summary=None,
                               rich_presence=None)
                time.sleep(2)
                return
            self._usb_gone_since = None
            self.com_port = found
            print(f"[{stamp()}] cart detected on {found}", flush=True)
        # A CD launch is not identifiable through the cartridge protocol.
        # Keep the in-memory session until the mapper reports the menu again.
        if hub.state.get("cd_session"):
            # The cart answering USB means the phone may launch a cartridge
            # game right now (a playing CD keeps the cart off the bus) --
            # service queued web actions before re-evaluating the session.
            if self._service_requests():
                return  # cartridge game launched; next pass identifies it
            try:
                backend = self._open_backend()
                in_game = backend.in_game()
                if in_game is not False:
                    self._cd_menu_reads = 0
                if in_game is False:
                    # The status register reads "menu" transiently while a
                    # CD title boots (observed 2026-07-17: a single flicker
                    # killed a fresh session). Only two consecutive menu
                    # reads end the session.
                    self._cd_menu_reads += 1
                    if self._cd_menu_reads < 2:
                        time.sleep(1.0)
                        return
                    self._cd_menu_reads = 0
                    print(f"[{stamp()}] mapper reports menu -- cd-session "
                          "over", flush=True)
                    self._cd_cart_gone = False
                    hub.update(cd_session=False, connection="menu", game=None)
                elif self._cd_cart_gone:
                    # USB returning after it disappeared means the Mega CD
                    # disc stopped, but the console may already be in another
                    # CD/MD+ title. Ask the MCU what was selected. A plain
                    # console reset leaves the CD title selected while the
                    # console sits at the menu, so an mcd selection alone is
                    # NOT proof of play: only keep the session when the
                    # mapper itself says a game is running (an MD+ title
                    # keeps USB alive; a playing CD never answers here).
                    self._close_backend()
                    sd_path = self._with_serial(lambda d: d.rom_path(),
                                                 deadline=5.0)
                    clean_path = sd_path.lstrip("/")
                    system = next((s for d, s in SCAN_DIRS
                                   if clean_path.startswith(f"{d}/")), None)
                    self._cd_cart_gone = False
                    if system == "mcd" and in_game is True:
                        self._show_cd_session(clean_path)
                        time.sleep(1.0)
                        return
                    # Reset back to the menu. Remember an mcd selection so a
                    # later USB drop restores the session display.
                    self._last_cd_path = (clean_path if system == "mcd"
                                          else None)
                    print(f"[{stamp()}] USB back, no game running "
                          f"(selected: {clean_path or '?'}) -- cd-session "
                          "over, back to menu", flush=True)
                    hub.update(cd_session=False, connection="menu", game=None)
                else:
                    hub.update(connection="cd-session", achievements=[],
                               summary=None, rich_presence=None)
                    time.sleep(1.0)
                    return
            except Exception as e:
                print(f"[{stamp()}] cd-session poll failed ({e}) -- "
                      "treating cart as gone", flush=True)
                self._close_backend()
                self._cd_cart_gone = True
                if self._port_arg == "auto":
                    self.com_port = None
                hub.update(connection="cd-session", achievements=[],
                           summary=None, rich_presence=None)
                time.sleep(2.0)
                return
        # Migrate existing installs once, now that automatic COM detection has
        # supplied a usable port. Subsequent starts use only the saved choice.
        if _toggle_preference() is None:
            try:
                on = self._with_serial(
                    lambda d: ra_toggle.get_mode(d, GAMES_DIR))
                _save_toggle_preference(on)
                hub.update(toggle=on)
            except Exception as e:
                print(f"[{stamp()}] could not read initial RA preference: {e}",
                      flush=True)
        self._service_requests()
        # A serviced Mega CD launch sets cd_session but deliberately does not
        # count as a Mega Drive launch. Do not fall through and try to identify
        # its .cue/.iso as a cartridge ROM before USB has disappeared.
        if hub.state.get("cd_session"):
            self._close_backend()
            return
        backend = self._open_backend()
        in_game = backend.in_game()
        if in_game is not True:
            self._unidentified_noted = False
            if hub.state.get("connection") != "menu":
                hub.update(connection="menu", game=None,
                           achievements=[], summary=None)
            time.sleep(1.0)
            return

        time.sleep(SETTLE_SECONDS)
        self._close_backend()
        # Only a cartridge load gives the MCU a rom path. A Mega CD title
        # launched from the console menu keeps the cart on USB with the
        # mapper reporting "game" but an EMPTY rom path -- its .cue sits in
        # the MCU's cue slot instead (hardware-confirmed 2026-07-17 with
        # Batman Returns: rom_path(0)=='', rom_path(1)==the .cue path).
        # Classify that here; cartridge identification would otherwise
        # retry the empty path forever. recent.dat is NOT consulted: the
        # same probe showed it stale (previous cartridge title).
        try:
            probed_rom = self._with_serial(lambda d: d.rom_path(),
                                           deadline=5.0)
        except IOError:
            # pre-v24 firmware has no CMD_ROM_PATH; let
            # identify_running_game do its recent.dat fallback.
            probed_rom = None
        if probed_rom == "":
            try:
                cue = self._with_serial(lambda d: d.rom_path(1),
                                        deadline=5.0)
            except IOError:
                cue = ""
            clean = cue.lstrip("/")
            system = next((s for d, s in SCAN_DIRS
                           if clean.startswith(f"{d}/")), None)
            if system == "mcd":
                print(f"[{stamp()}] console-launched CD title detected "
                      f"({clean}) -- cd-session", flush=True)
                self._unidentified_noted = False
                self._cd_cart_gone = False
                self._last_cd_path = clean
                self._show_cd_session(clean)
                return
            # Mapper says game, MCU has neither a rom path nor a CD
            # selection -- e.g. the console is powered off while the cart
            # stays USB-powered. Idle quietly; hammering identification
            # here spammed the log every 6s for 20 minutes (2026-07-17).
            if not self._unidentified_noted:
                print(f"[{stamp()}] mapper reports a game but the MCU has "
                      "no loaded ROM or CD selection -- idling until that "
                      "changes", flush=True)
                self._unidentified_noted = True
            if hub.state.get("connection") != "menu":
                hub.update(connection="menu", game=None,
                           achievements=[], summary=None)
            time.sleep(5.0)
            return
        self._unidentified_noted = False
        # A cartridge game is running -- any remembered CD selection is
        # obsolete (a later USB drop must read as offline, not cd-session).
        self._last_cd_path = None
        hub.update(connection="identifying")
        try:
            sd_path, md5 = identify_running_game(
                self.com_port,
                on_path=lambda p: hub.update(
                    connection="identifying",
                    game={"path": p,
                          "title": Path(p).name.rsplit(".", 1)[0]
                          .split("(")[0].strip()}))
        except SystemExit as e:
            # identify_running_game raises SystemExit (meant for one-shot
            # CLI callers) when the MCU reports no loaded ROM -- observed
            # live 2026-07-17 as a transient empty CMD_ROM_PATH reply while
            # a game was genuinely running. SystemExit isn't an Exception,
            # so it silently killed this worker thread and the whole
            # daemon looked "stuck" until a manual restart. Treat it as
            # retryable instead: back to menu detection, try again shortly.
            # Since the empty-path pre-classification above, reaching this
            # means the path vanished between the probe and the hash read;
            # rare, so a slower retry keeps the log readable.
            print(f"[{stamp()}] identify failed ({e}) -- retrying", flush=True)
            hub.update(connection="menu", game=None,
                       achievements=[], summary=None, rich_presence=None)
            self._open_backend()
            time.sleep(5.0)
            return
        backend = self._open_backend()

        # RA toggled off: report honestly instead of running the hardware
        # checks (the factory core is loaded, so they would produce
        # misleading core-inactive / stale-region errors). Still show what
        # is playing; no rc_client session is created.
        if _toggle_preference() is False:
            entry = next((g for g in gamelib.cached_library()
                          if g.get("path") == sd_path.lstrip("/")), {})
            hub.update(connection="ra-disabled",
                       game={"path": sd_path,
                             "title": entry.get("title")
                             or Path(sd_path).name.rsplit(".", 1)[0]
                             .split("(")[0].strip()},
                       achievements=[], summary=None, rich_presence=None)
            self._wait_for_menu_or_launch()
            return

        # No achievement callback from a previous or failed session may cross
        # the identification boundary. Evaluation is enabled only after RA has
        # supplied a real, supported game set and the sniffer is verified.
        self.evaluating_achievements = False
        unlock_sink.clear()
        self.client.response_log = []
        try:
            # Always identify/start the RA session, even on a ROM hash cache hit.
            self.client.load_game(md5)
        except RuntimeError as e:
            self.client.unload_game()
            unlock_sink.clear()
            # Unknown ROMs don't need the sniffer, but recording its liveness
            # here lets hardware tests verify custom-folder mapper loading.
            try:
                c0 = backend.read_write_counter()
                time.sleep(0.5)
                c1 = backend.read_write_counter()
                core_note = "active" if c1 != c0 else "not active"
            except Exception as core_error:
                core_note = f"check failed: {core_error}"
            print(f"[{stamp()}] no RA set ({e}); sniffer core {core_note}",
                  flush=True)
            hub.update(connection="no-set", game={"path": sd_path},
                       achievements=[], summary=None, rich_presence=None)
            self._wait_for_menu_or_launch()
            return

        # Only require the sniffer after RA confirms the ROM has a set. This
        # lets unknown ROMs report "no set" accurately even when their folder
        # did not load mapper.rbf. Never evaluate a known set against stale
        # SRAM from the factory core.
        c0 = backend.read_write_counter()
        time.sleep(0.5)
        c1 = backend.read_write_counter()
        if c1 == c0:
            print(f"[{stamp()}] sniffer core NOT active for {sd_path} -- "
                  f"refusing to evaluate", flush=True)
            self.client.unload_game()
            unlock_sink.clear()
            hub.update(connection="core-inactive",
                       game={"path": sd_path}, achievements=[], summary=None,
                       rich_presence=None)
            self._wait_for_menu_or_launch()
            return

        drops = backend.read_drop_count()
        if drops:
            print(f"[{stamp()}] WRAM capture invalid ({drops} dropped "
                  "writes) -- refusing to evaluate", flush=True)
            self.client.unload_game()
            unlock_sink.clear()
            hub.update(connection="capture-invalid",
                       game={"path": sd_path}, achievements=[], summary=None,
                       rich_presence=None)
            self._wait_for_menu_or_launch()
            return

        info = self.client.game_info()
        console_region = backend.read_console_region()
        if console_region == "PAL":
            print(f"[{stamp()}] PAL console mode detected -- refusing to "
                  "present/evaluate an NTSC RetroAchievements session",
                  flush=True)
            self.client.unload_game()
            unlock_sink.clear()
            hub.update(connection="unsupported-region",
                       game={"title": info.get("title"),
                             "id": info.get("id"),
                             "icon": info.get("icon", ""),
                             "path": sd_path,
                             "region": console_region},
                       achievements=[], summary=None, rich_presence=None)
            self._wait_for_menu_or_launch()
            return
        # RA maps known-bad revisions to a pseudo game whose title begins
        # "Unsupported Game Version". It can contain warning achievements,
        # but it is not a playable achievement set and must never enter the
        # game's progress screen.
        if info.get("title", "").casefold().startswith(
                "unsupported game version"):
            print(f"[{stamp()}] unsupported ROM revision: "
                  f"{info.get('title')} (RA #{info.get('id')})", flush=True)
            self.client.unload_game()
            unlock_sink.clear()
            hub.update(connection="no-set", game={"path": sd_path},
                       achievements=[], summary=None, rich_presence=None)
            self._wait_for_menu_or_launch()
            return

        achievements = self.client.achievement_list()
        s = self.client.summary()
        print(f"[{stamp()}] RA game #{info.get('id')}: {info.get('title')} "
              f"({s['unlocked']}/{s['total']}, {len(achievements)} "
              f"achievements)", flush=True)
        hub.update(connection="playing",
                   game={"title": info.get("title"), "id": info.get("id"),
                         "icon": info.get("icon", ""), "path": sd_path},
                   summary=s, achievements=achievements,
                   rich_presence=None)

        frame_budget = 1.0 / TARGET_FPS
        t_check = time.monotonic()
        last_rich_presence = ""
        capture_invalid = False
        region_changed = False
        # The $A10001 latch above only proves the region at boot; a console
        # whose NTSC/PAL switch is flipped mid-game keeps running with
        # nothing on the bus to re-latch. frame_watch's vblank counter gives
        # the live cadence -- PAL sessions never reach this point, so the
        # expected verdict is always NTSC. Old gateware without the counter
        # yields a frozen register (delta 0 = no verdict), which never trips.
        region_monitor = RegionMonitor(expected="NTSC")
        self.evaluating_achievements = True
        try:
            while True:
                t0 = time.monotonic()

                # A console reset or Start+Down is a hard session boundary.
                # Check both signals BEFORE rc_client_do_frame: evaluating even
                # one frame of the next game's WRAM against this set can cause
                # a false unlock. combo_hit is sticky until mapper reset; if
                # reset has already cleared it, the menu-state check catches it.
                if self.backend.read_combo_flag():
                    print(f"[{stamp()}] Start+Down detected -- stopping "
                          "achievement evaluation", flush=True)
                    break
                if self.backend.in_game() is not True:
                    print(f"[{stamp()}] console left game -- stopping "
                          "achievement evaluation", flush=True)
                    break

                drops = self.backend.read_drop_count()
                if drops:
                    capture_invalid = True
                    print(f"[{stamp()}] WRAM capture invalid ({drops} "
                          "dropped writes) -- stopping achievement "
                          "evaluation", flush=True)
                    break

                self.client.do_frame()
                while unlock_sink:
                    u = unlock_sink.pop(0)
                    for a in achievements:
                        if a["id"] == u["id"]:
                            a["unlocked"] = True
                    s = self.client.summary()
                    hub.update(summary=s, achievements=achievements)
                    hub.event(dict(u, type="unlock"))
                if time.monotonic() - t_check >= 0.5:
                    t_check = time.monotonic()
                    if region_monitor.feed(self.backend.read_vint_count(),
                                           time.monotonic()):
                        region_changed = True
                        print(f"[{stamp()}] PAL frame cadence detected "
                              "mid-game (region switch flipped?) -- "
                              "stopping achievement evaluation", flush=True)
                        break
                    rich_presence = self.client.rich_presence()
                    if rich_presence != last_rich_presence:
                        last_rich_presence = rich_presence
                        print(f"[{stamp()}] Rich Presence: "
                              f"{rich_presence or '(none)'}", flush=True)
                        hub.update(rich_presence=rich_presence or None)
                    if not self.requests.empty():
                        if self._service_requests():
                            print(f"[{stamp()}] game launched from UI -- "
                                  f"re-identifying", flush=True)
                            self._open_backend()
                            break
                        self._open_backend()
                dt = time.monotonic() - t0
                if dt < frame_budget:
                    time.sleep(frame_budget - dt)
        finally:
            self.evaluating_achievements = False
            unlock_sink.clear()
            self.client.unload_game()  # menu owns WRAM now
        if capture_invalid:
            hub.update(connection="capture-invalid",
                       game={"title": info.get("title"),
                             "id": info.get("id"),
                             "icon": info.get("icon", ""),
                             "path": sd_path},
                       achievements=[], summary=None, rich_presence=None)
            self._wait_for_menu_or_launch()
            return
        if region_changed:
            hub.update(connection="region-changed",
                       game={"title": info.get("title"),
                             "id": info.get("id"),
                             "icon": info.get("icon", ""),
                             "path": sd_path,
                             "region": "PAL"},
                       achievements=[], summary=None, rich_presence=None)
            self._wait_for_menu_or_launch()
            return
        hub.update(connection="menu", game=None,
                   achievements=[], summary=None, rich_presence=None)

    def _wait_for_menu_or_launch(self):
        """Park (no-set / wrong-core) until the console leaves the game
        OR the UI launches another -- either way the supervisor loop
        re-identifies next pass."""
        backend = self._open_backend()
        while backend.in_game() is not False:
            if self._service_requests():
                self._open_backend()
                return
            backend = self._open_backend()
            time.sleep(1.0)


# ---------------------------------------------------------------------
# web app
# ---------------------------------------------------------------------

app = FastAPI(title="Achievement Box")
worker: HwWorker | None = None

# The web password is deliberately separate from the RetroAchievements login.
# It protects the LAN control plane; RA_USER/RA_PASS never leave this process.
WEB_USER = ""
WEB_PASSWORD = ""
WEB_REQUIRE_HTTPS = True
WEB_HTTPS_PORT = 8443
WEB_HTTPS_443 = False


def _headers(scope) -> dict[bytes, bytes]:
    return {key.lower(): value for key, value in scope.get("headers", [])}


def _web_authorized(scope) -> bool:
    """Validate Basic auth, or allow trusted-LAN mode when unset."""
    if not WEB_PASSWORD:
        return True
    value = _headers(scope).get(b"authorization", b"")
    if not value.startswith(b"Basic "):
        return False
    try:
        raw = base64.b64decode(value[6:], validate=True).decode("utf-8")
        username, password = raw.split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    return (hmac.compare_digest(username, WEB_USER)
            and hmac.compare_digest(password, WEB_PASSWORD))


def _same_origin(scope) -> bool:
    """Reject browser cross-origin writes while allowing native clients."""
    headers = _headers(scope)
    origin = headers.get(b"origin")
    if not origin:
        return True
    try:
        parsed = urlsplit(origin.decode("ascii"))
        host = headers.get(b"host", b"").decode("ascii")
    except (UnicodeDecodeError, ValueError):
        return False
    return parsed.scheme in ("http", "https") and parsed.netloc == host


def _loopback_client(scope) -> bool:
    client = scope.get("client")
    return bool(client and client[0] in ("127.0.0.1", "::1"))


def _https_location(scope) -> str:
    headers = _headers(scope)
    host = headers.get(b"host", b"achievementbox.local").decode(
        "latin1").split(":", 1)[0]
    authority = host if WEB_HTTPS_443 else f"{host}:{WEB_HTTPS_PORT}"
    raw_path = scope.get("raw_path")
    path = (raw_path.decode("latin1") if raw_path is not None
            else scope.get("path", "/"))
    query = scope.get("query_string", b"")
    return f"https://{authority}{path}" + (
        f"?{query.decode('latin1')}" if query else "")


class WebSecurityMiddleware:
    """Authenticate the LAN UI and protect its hardware-control routes."""

    PUBLIC_CERTS = {"/rootca.pem", "/rootca.crt"}
    SECURITY_HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    ]

    def __init__(self, app):
        self.app = app

    async def _http_response(self, send, status: int, body: bytes,
                             extra_headers=()):
        headers = [(b"content-type", b"text/plain; charset=utf-8"),
                   (b"content-length", str(len(body)).encode()),
                   (b"cache-control", b"no-store")]
        headers.extend(extra_headers)
        headers.extend(self.SECURITY_HEADERS)
        await send({"type": "http.response.start", "status": status,
                    "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        is_public_cert = kind == "http" and path in self.PUBLIC_CERTS

        # Never send a reusable password over a clear-text LAN connection.
        # Loopback remains usable for a local kiosk; certificate downloads are
        # public so a phone can establish trust before its first HTTPS login.
        if (kind == "http" and WEB_PASSWORD and WEB_REQUIRE_HTTPS
                and scope.get("scheme") != "https"
                and not _loopback_client(scope) and not is_public_cert):
            return await self._http_response(
                send, 307, b"Use HTTPS\n",
                [(b"location", _https_location(scope).encode("latin1"))])

        if not is_public_cert and not _web_authorized(scope):
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008,
                            "reason": "authentication required"})
                return
            return await self._http_response(
                send, 401, b"Authentication required\n",
                [(b"www-authenticate",
                  b'Basic realm="Achievement Box", charset="UTF-8"')])

        unsafe = (kind == "websocket"
                  or scope.get("method", "GET")
                  not in ("GET", "HEAD", "OPTIONS"))
        if unsafe and not _same_origin(scope):
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008,
                            "reason": "cross-origin request blocked"})
                return
            return await self._http_response(
                send, 403, b"Cross-origin request blocked\n")

        async def secure_send(message):
            if message.get("type") == "http.response.start":
                message.setdefault("headers", []).extend(self.SECURITY_HEADERS)
            await send(message)

        await self.app(scope, receive, secure_send)


app.add_middleware(WebSecurityMiddleware)


@app.get("/api/state")
def api_state():
    return hub.snapshot()


@app.get("/api/games")
def api_games():
    return {"games": gamelib.cached_library(),
            "openvgdb": gamelib.openvgdb_ready(),
            "launchbox": gamelib.launchbox_ready()}


@app.post("/api/games/refresh")
def api_games_refresh():
    if not gamelib.openvgdb_ready():
        try:
            gamelib.sync_openvgdb()
        except Exception as e:
            print(f"openvgdb sync failed: {e}")
    # Split the rescan into bounded worker jobs so no single USB pass trips the
    # hardware-worker timeout: one quick discover, then one scan per folder,
    # then a local (no-USB) metadata merge.
    dreq = worker.submit("discover", timeout=90)
    if dreq.error:
        return JSONResponse({"error": dreq.error}, status_code=500)
    all_games: list = []
    for folder, system in dreq.result:
        sreq = worker.submit("scan_folder", (folder, system), timeout=120)
        if sreq.error:
            # skip & continue: one flaky folder shouldn't fail the whole rescan
            print(f"scan of {folder} skipped: {sreq.error}")
            continue
        all_games.extend(sreq.result)
    games = gamelib.enrich_library(all_games)
    return {"games": games, "openvgdb": gamelib.openvgdb_ready(),
            "launchbox": gamelib.launchbox_ready()}


@app.post("/api/toggle")
def api_toggle(body: dict):
    if worker.com_port is None:
        return JSONResponse(
            {"error": "cart offline -- power the console on first"},
            status_code=503)
    # The toggle only takes effect at the next launch; changing it mid-game
    # would silently do nothing to the running session, so refuse.
    if (hub.state.get("connection") in ("playing", "identifying")
            or worker.evaluating_achievements):
        return JSONResponse(
            {"error": "game session active -- return to the EverDrive menu "
                      "to change achievements mode"}, status_code=409)
    games_dir = str(body.get("dir") or GAMES_DIR).replace("\\", "/").strip("/")
    if (games_dir != GAMES_DIR
            and not games_dir.startswith(f"{GAMES_DIR}/")) \
            or ".." in games_dir.split("/"):
        return JSONResponse({"error": "bad games folder"}, status_code=400)
    on = bool(body.get("on"))
    if worker.toggle_in_flight:
        return JSONResponse({"error": "another toggle is already running"},
                            status_code=409)
    worker.toggle_in_flight = True
    try:
        req = worker.submit("toggle_set", (on, games_dir), timeout=300)
    finally:
        worker.toggle_in_flight = False
    if req.error:
        return JSONResponse({"error": req.error}, status_code=500)
    return {"message": req.result, "on": on, "dir": games_dir}


@app.post("/api/launch")
def api_launch(body: dict):
    # A playing CD keeps the cart off USB; if the cart answers, the console
    # is back at the menu (or an MD+ title) and launching is safe even while
    # the session display lingers.
    if hub.state.get("cd_session") and worker.com_port is None:
        return JSONResponse(
            {"error": "Mega CD game running -- quit it on the console before "
                      "launching another"}, status_code=409)
    if worker.toggle_in_flight:
        return JSONResponse(
            {"error": "achievements switch is being applied -- wait for it "
                      "to finish"}, status_code=409)
    if worker.com_port is None:
        return JSONResponse(
            {"error": "cart offline -- power the console on first"},
            status_code=503)
    path = body.get("path", "")
    system = next((s for d, s in SCAN_DIRS if path.startswith(f"{d}/")), None)
    if system is None:
        return JSONResponse({"error": "bad path"}, status_code=400)
    req = worker.submit("launch", path, timeout=150)
    if req.error:
        return JSONResponse({"error": req.error}, status_code=500)
    note = ""
    if system == "mcd":
        note = ("Mega CD game: the cart leaves USB while it plays -- "
                "no achievements, box reconnects when you quit")
    return {"message": req.result, "note": note}


@app.get("/api/art/{system}/{face}/{stem}")
def api_art(system: str, face: str, stem: str):
    """Box face (front|back|spine) for a scanned game. LaunchBox art when
    synced (MEDIA_REGION preference), libretro front otherwise."""
    if system not in gamelib.LIBRETRO_SYSTEMS or face not in (
            "front", "back", "spine", "snap", "title"):
        return JSONResponse({"error": "bad request"}, status_code=400)
    game = next((g for g in gamelib.cached_library()
                 if g.get("system") == system and g.get("stem") == stem),
                {"stem": stem, "system": system})
    p = gamelib.fetch_face(game, face)
    if p is None:
        return JSONResponse({"error": "no art"}, status_code=404)
    media = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(p, media_type=media,
                        headers={"Cache-Control": "max-age=86400"})


@app.get("/rootca.pem")
def api_rootca():
    """The box's root CA -- install it on the phone once and the https
    URLs become fully trusted (real PWA install, unflagged push)."""
    ca = HTTPS_DIR / "rootca.pem"
    if not ca.exists():
        return JSONResponse({"error": "https not initialised"},
                            status_code=404)
    return FileResponse(ca, media_type="application/x-pem-file",
                        filename="achievementbox-rootca.pem")


@app.get("/rootca.crt")
def api_rootca_der():
    """Same CA in DER -- the form Android's cert installer (and
    Windows' double-click import) reliably accepts; .pem can fail to
    register as a CA without any visible error."""
    ca = HTTPS_DIR / "rootca.pem"
    if not ca.exists():
        return JSONResponse({"error": "https not initialised"},
                            status_code=404)
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding
    der = x509.load_pem_x509_certificate(ca.read_bytes()).public_bytes(
        Encoding.DER)
    return Response(der, media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition":
                             'attachment; filename="achievementbox-rootca.crt"'})


@app.get("/api/push/vapid")
def api_vapid():
    return {"publicKey": _vapid()["public"]}


@app.post("/api/push/subscribe")
def api_push_subscribe(body: dict):
    if not body.get("endpoint"):
        return JSONResponse({"error": "bad subscription"}, status_code=400)
    subs = _push_subs()
    if not any(s.get("endpoint") == body["endpoint"] for s in subs):
        subs.append(body)
        _save_push_subs(subs)
    return {"subscribed": True, "count": len(subs)}


@app.post("/api/push/test")
def api_push_test():
    push_to_phones({"title": "Achievement Box connected",
                    "body": "Unlock notifications are working.",
                    "tag": "push-test"})
    return {"sent": len(_push_subs())}


@app.get("/api/lbimg/{fname}")
def api_lbimg(fname: str):
    """Cached LaunchBox image by FileName (modal screenshots)."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.(jpg|jpeg|png)", fname):
        return JSONResponse({"error": "bad filename"}, status_code=400)
    p = gamelib.fetch_launchbox_image(fname)
    if p is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    media = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(p, media_type=media,
                        headers={"Cache-Control": "max-age=86400"})


@app.get("/media/badge/{name}")
def api_badge(name: str, locked: int = 0):
    """Cached proxy for RA badge art (offline-safe after first fetch)."""
    name = "".join(ch for ch in name if ch.isalnum() or ch in "_-")
    fname = f"{name}{'_lock' if locked else ''}.png"
    p = BADGE_DIR / fname
    if not p.exists():
        BADGE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            req = urllib.request.Request(
                f"{MEDIA_HOST}/Badge/{fname}",
                headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as r:
                p.write_bytes(r.read())
        except Exception:
            return JSONResponse({"error": "badge fetch failed"},
                                status_code=404)
    return FileResponse(p, media_type="image/png",
                        headers={"Cache-Control": "max-age=86400"})


@app.get("/media/system/{name}")
def api_system_icon(name: str):
    """Cached same-origin proxy for RA's console icons."""
    if not re.fullmatch(r"[a-z0-9-]+", name):
        return JSONResponse({"error": "bad system icon"}, status_code=400)
    p = SYSTEM_ICON_DIR / f"{name}.png"
    if not p.exists():
        SYSTEM_ICON_DIR.mkdir(parents=True, exist_ok=True)
        try:
            req = urllib.request.Request(
                f"{RA_SYSTEM_ICON_HOST}/{name}.png",
                headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as r:
                p.write_bytes(r.read())
        except Exception:
            return JSONResponse({"error": "system icon fetch failed"},
                                status_code=404)
    return FileResponse(p, media_type="image/png",
                        headers={"Cache-Control": "max-age=86400"})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    q = hub.subscribe()
    try:
        await ws.send_json(hub.snapshot())
        while True:
            await ws.send_json(await q.get())
    except (WebSocketDisconnect, Exception):
        pass  # client gone (phone slept, etc.) -- drop it quietly
    finally:
        hub.unsubscribe(q)


class FreshStaticFiles(StaticFiles):
    """UI files must never go stale on phones: revalidate every load
    (the files are tiny; art/badges keep their own long cache)."""
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/", FreshStaticFiles(directory=str(WEBUI), html=True),
          name="webui")


def main():
    global worker, CD_DIR, WEB_USER, WEB_PASSWORD, WEB_REQUIRE_HTTPS
    global WEB_HTTPS_PORT, WEB_HTTPS_443
    # Game titles and Rich Presence commonly contain glyphs outside the
    # Windows console code page. Logging must never tear down an otherwise
    # valid achievement session.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    # Windows ProactorEventLoop has crashed this daemon with native access
    # violations (overlapped IO completing after its buffer is gone, e.g. on
    # abrupt phone/browser disconnects -- see webapp-crash.log history). The
    # selector loop handles our websocket/HTTP load fine and avoids the
    # whole class; asyncio subprocesses are not used anywhere.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # a native fault (bad ctypes struct etc.) kills the process with no
    # Python traceback -- leave one in a crash log we can read afterwards.
    # faulthandler entries carry no timestamps: date them by marking every
    # daemon start, so a dump reads as "after the <date> start".
    crash_log = open(Path(__file__).parent / "webapp-crash.log", "a")
    crash_log.write(f"=== daemon start "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    crash_log.flush()
    faulthandler.enable(crash_log)

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="auto",
                    help='cart COM port, or "auto" to detect')
    ap.add_argument("--http", type=int, default=8000)
    ap.add_argument("--https", type=int, default=8443,
                    help="HTTPS port (0 disables)")
    ap.add_argument("--cd-dir", default=CD_DIR,
                    help="SD folder holding Mega CD games")
    args = ap.parse_args()
    CD_DIR = args.cd_dir
    # Seed launch-validation folders from the last scan's folder tags so
    # launches work before the first rescan; a rescan (discover_dirs) then
    # replaces this with whatever folders currently hold ROMs. Fall back to
    # the classic two folders for a pre-folder-tag cache.
    seeded: dict[str, str] = {}
    for g in gamelib.cached_library():
        f = g.get("folder")
        if f and f not in seeded:
            seeded[f] = g.get("system", "md")
    SCAN_DIRS[:] = (list(seeded.items())
                    or [(GAMES_DIR, "md"), (CD_DIR, "mcd")])

    # cart detection happens in the worker (and re-runs whenever the cart
    # drops off USB) -- the web UI must come up even with the console off

    load_env_file(ENV_FILE)
    user, password = os.environ.get("RA_USER"), os.environ.get("RA_PASS")
    if not user or not password:
        raise SystemExit("set RA_USER and RA_PASS (environment or "
                         f"{ENV_FILE} -- see .env.example)")

    WEB_USER = os.environ.get("WEB_USER", "achievementbox")
    WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
    WEB_REQUIRE_HTTPS = os.environ.get("WEB_REQUIRE_HTTPS", "1") != "0"
    if WEB_PASSWORD and len(WEB_PASSWORD) < 12:
        raise SystemExit("WEB_PASSWORD must be at least 12 characters")
    if WEB_PASSWORD and WEB_REQUIRE_HTTPS and not args.https:
        raise SystemExit("HTTPS is required for LAN authentication; remove "
                         "--https 0 or explicitly set WEB_REQUIRE_HTTPS=0 "
                         "only on an isolated development machine")

    worker = HwWorker(args.port, user, password)
    worker.start()

    config = uvicorn.Config(app, host="0.0.0.0", port=args.http,
                            log_level="warning")
    server = uvicorn.Server(config)

    @app.on_event("startup")
    async def _grab_loop():
        loop = asyncio.get_running_loop()
        hub.loop = loop

        def _quiet_disconnect(loop, context):
            # Windows ProactorEventLoop logs a full traceback every time a
            # client (phone/browser) drops a TCP connection abruptly -- e.g. a
            # long /api/games/refresh fetch or an SSE stream during a scan.
            # Harmless (WinError 10054); swallow it, delegate everything else.
            if isinstance(context.get("exception"), ConnectionResetError):
                return
            loop.default_exception_handler(context)

        loop.set_exception_handler(_quiet_disconnect)

    zc = start_mdns(args.http)
    start_port80_redirect(args.http)
    https_443 = False
    if args.https:
        try:
            https_443 = start_https(args.https)
        except Exception as e:
            if WEB_PASSWORD and WEB_REQUIRE_HTTPS:
                raise SystemExit(f"HTTPS unavailable: {e}") from e
            print(f"[{stamp()}] https unavailable: {e}", flush=True)
            args.https = 0
    WEB_HTTPS_PORT = args.https
    WEB_HTTPS_443 = https_443
    print_banner(args.http, args.https, https_443)
    try:
        server.run()
    finally:
        if zc:
            zc.close()


if __name__ == "__main__":
    main()
