"""SD game library: scan, metadata, box art.

- scan: list ROM files in the games folder over the raw MCU protocol
  (MCU-side, safe while a game runs).
- metadata (title/year/description): OpenVGDB, a one-time ~30MB download
  (github.com/OpenVGDB/OpenVGDB) queried locally with sqlite3 -- no API
  key, works offline after the first sync. Matched by No-Intro filename,
  then by normalised title. LaunchBox (below) fills any gaps.
- box art: the LaunchBox Games Database when synced -- it carries FRONT,
  BACK and SPINE scans, each region-tagged, so MEDIA_REGION can select the
  preferred release. Built once by `sync_art.py` into a compact
  local index; images are UUID.jpg at images.launchbox-app.com, cached
  on disk. Without it we fall back to thumbnails.libretro.com fronts.
  Cached on disk after first fetch (WiFi blips must not blank the UI).

Everything degrades gracefully: no DB -> filename-derived titles only;
no LaunchBox index -> libretro fronts only; no art -> placeholder label.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from config import MEDIA_REGION
from achievementbox.version import USER_AGENT

CACHE_DIR = Path(__file__).parent.parent / "cache"
ART_DIR = CACHE_DIR / "art"
DB_PATH = CACHE_DIR / "openvgdb.sqlite"
LIB_INDEX = CACHE_DIR / "games.json"
LB_INDEX = CACHE_DIR / "launchbox.json"
LB_REGION_FILE = CACHE_DIR / "launchbox-region.txt"

OPENVGDB_ZIP = ("https://github.com/OpenVGDB/OpenVGDB/releases/"
                "download/v29.0/openvgdb.zip")
LIBRETRO_SYSTEMS = {
    "md": "Sega - Mega Drive - Genesis",
    "mcd": "Sega - Mega-CD - Sega CD",
}
# -- LaunchBox Games Database (front/back/spine, region-preferred) -----
LAUNCHBOX_ZIP = "https://gamesdb.launchbox-app.com/Metadata.zip"
LAUNCHBOX_IMG = "https://images.launchbox-app.com/"
# LaunchBox platform names -> our system code
LB_PLATFORMS = {
    "Sega Genesis": "md", "Sega Mega Drive": "md",
    "Sega CD": "mcd", "Sega Mega-CD": "mcd", "Sega Mega CD": "mcd",
}
# image Type -> face; reconstructed variants are worse (higher penalty)
LB_TYPE_FACE = {
    "Box - Front": ("front", 0), "Box - Front - Reconstructed": ("front", 5),
    "Box - Back": ("back", 0), "Box - Back - Reconstructed": ("back", 5),
    "Box - Spine": ("spine", 0),
}
# screenshot Types worth showing in the game modal, best first
LB_SHOT_TYPES = ("Screenshot - Gameplay", "Screenshot - Game Title",
                 "Screenshot - Game Select", "Screenshot - High Scores")
LB_MAX_SHOTS = 4
MEDIA_REGION_ALIASES = {
    "pal": "Europe", "eu": "Europe", "europe": "Europe",
    "ntsc-u": "North America", "ntsc-u/c": "North America",
    "usa": "North America", "us": "North America",
    "north america": "North America",
    "ntsc-j": "Japan", "jp": "Japan", "japan": "Japan",
    "world": "World",
}
MEDIA_REGION_CANON = MEDIA_REGION_ALIASES.get(
    MEDIA_REGION.casefold(), MEDIA_REGION)
MEDIA_REGION_SLUG = re.sub(
    r"[^a-z0-9]+", "-", MEDIA_REGION_CANON.casefold()).strip("-") or "world"

REGION_GROUPS = {
    "Europe": ("Europe", "United Kingdom", "Australia", "France",
               "Germany", "Spain", "Italy"),
    "North America": ("North America", "United States", "Canada", "Mexico"),
    "Japan": ("Japan", "Asia"),
    "World": ("World", ""),
}


def _region_rank(region: str) -> int:
    """Rank a LaunchBox image region against MEDIA_REGION."""
    order = [MEDIA_REGION_CANON, "World", "Europe", "North America", "Japan"]
    order = list(dict.fromkeys(order))
    for group_rank, group in enumerate(order):
        members = REGION_GROUPS.get(group, (group,))
        if region in members:
            return group_rank * 10 + members.index(region)
    return 90

# per-system ROM extensions ('.bin' is deliberately absent for mcd:
# there it's CD track data behind a .cue, not a game entry)
ROM_EXTS = {
    "md": {".md", ".bin", ".gen", ".smd", ".68k"},
    "mcd": {".cue", ".iso", ".chd"},
}


def _norm(title: str) -> str:
    """Normalise for matching: strip region tags, articles, punctuation."""
    t = title.split("(")[0].split("[")[0]
    t = re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()
    # No-Intro puts articles last ("Addams Family, The"); LaunchBox puts
    # them first ("The Addams Family") -- strip both ends
    for art in (" the", " a", " an"):
        if t.endswith(art):
            t = t[: -len(art)].strip()
    for art in ("the ", "a ", "an "):
        if t.startswith(art):
            t = t[len(art):].strip()
    return t


def _fetch(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# -- SD scan ---------------------------------------------------------

def scan_sd_games(dev, games_dir: str, system: str = "md") -> list[dict]:
    """List ROM files in games_dir (recursing one level into subdirs).
    dev is an open, recovered EdProSerial."""
    out = []
    exts = ROM_EXTS[system]

    def walk(path: str, depth: int):
        for name, size, is_dir in dev.list_dir(path):
            if name.startswith("."):
                continue
            full = f"{path}/{name}"
            if is_dir and depth > 0:
                walk(full, depth - 1)
            elif size > 0 and Path(name).suffix.lower() in exts:
                out.append({"path": full, "file": name, "system": system,
                            "stem": Path(name).stem, "size": size})

    walk(games_dir, 1)
    out.sort(key=lambda g: g["stem"].lower())
    return out


def _classify_folder(dev, folder: str) -> str | None:
    """Classify a top-level SD folder without walking more than necessary.

    Returns 'mcd' if it holds a disc image (.cue/.iso/.chd), else 'md' if it
    holds any Mega Drive ROM, else None. Walks one level of subdirs deep (same
    reach as scan_sd_games) but bails the instant it sees a disc image, since
    mcd wins over md -- a .bin inside a Mega CD folder is track data, not a
    game. md folders still need a full pass to rule out a stray disc image."""
    disc_exts = ROM_EXTS["mcd"]
    md_exts = ROM_EXTS["md"]
    has_md = False

    def walk(path: str, depth: int) -> bool:  # True == found a disc image
        nonlocal has_md
        for name, size, is_dir in dev.list_dir(path):
            if name.startswith("."):
                continue
            if is_dir and depth > 0:
                if walk(f"{path}/{name}", depth - 1):
                    return True
            elif size > 0:
                ext = Path(name).suffix.lower()
                if ext in disc_exts:
                    return True
                if ext in md_exts:
                    has_md = True
        return False

    if walk(folder, 1):
        return "mcd"
    return "md" if has_md else None


def discover_dirs(dev, log=print) -> list[tuple[str, str]]:
    """Auto-detect every top-level SD folder that holds ROMs.

    A folder is classified 'mcd' if it contains a disc image
    (.cue/.iso/.chd), else 'md' if it contains any Mega Drive ROM, else
    skipped. Classifying mcd by its disc images (not by .bin) preserves the
    rule that a .bin inside a Mega CD folder is CD track data, not a game.
    Returns [(folder, system)] sorted by folder name.
    """
    try:
        root = dev.list_dir("")
    except IOError:
        root = dev.list_dir("/")
    dirs: list[tuple[str, str]] = []
    for name, _size, is_dir in root:
        # "MEGA" (not "MEGA DRIVE") is the EverDrive OS's own reserved
        # folder (mappers/, sys/, gamedata/ -- internal caches, not games).
        # gamedata/ holds zero-byte per-ROM marker files that would
        # otherwise be picked up as a phantom, unlaunchable game folder.
        if not is_dir or name.startswith(".") or name.upper() == "MEGA":
            continue
        system = _classify_folder(dev, name)
        if system:
            dirs.append((name, system))
    dirs.sort()
    log(f"discovered {len(dirs)} ROM folder(s): "
        + (", ".join(f"{d} [{s}]" for d, s in dirs) or "none"))
    return dirs


# -- OpenVGDB metadata -----------------------------------------------

def openvgdb_ready() -> bool:
    return DB_PATH.exists()


def sync_openvgdb(log=print):
    """One-time download of the metadata DB (~30MB zipped)."""
    if openvgdb_ready():
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log("downloading OpenVGDB (one-time, ~30MB)...")
    zpath = CACHE_DIR / "openvgdb.zip"
    zpath.write_bytes(_fetch(OPENVGDB_ZIP, timeout=300))
    with zipfile.ZipFile(zpath) as z:
        member = next(n for n in z.namelist()
                      if n.lower().endswith(".sqlite"))
        DB_PATH.write_bytes(z.read(member))
    zpath.unlink()
    log(f"OpenVGDB ready at {DB_PATH}")


class Metadata:
    """Local OpenVGDB lookups for the Mega Drive library."""

    def __init__(self):
        self._db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        ids = [r[0] for r in self._db.execute(
            "SELECT systemID FROM SYSTEMS WHERE systemName LIKE '%Genesis%'"
            " OR systemName LIKE '%Mega Drive%'"
            " OR systemName LIKE '%Sega CD%'"
            " OR systemName LIKE '%Mega-CD%'")]
        self._sys = ",".join(str(i) for i in ids) or "-1"
        self._by_file: dict[str, tuple] | None = None
        self._by_title: dict[str, tuple] | None = None

    def _index(self):
        if self._by_file is not None:
            return
        self._by_file, self._by_title = {}, {}
        rows = self._db.execute(
            f"SELECT r.romFileName, rel.releaseTitleName,"
            f" rel.releaseDescription, rel.releaseDate,"
            f" rel.releaseDeveloper, rel.releaseGenre, rel.releasePublisher"
            f" FROM ROMs r JOIN RELEASES rel ON rel.romID = r.romID"
            f" WHERE r.systemID IN ({self._sys})")
        for fname, title, desc, date, dev_, genre, pub in rows:
            rec = (title, desc, date, dev_, genre, pub)
            if fname:
                self._by_file.setdefault(Path(fname).stem.lower(), rec)
            if title:
                self._by_title.setdefault(_norm(title), rec)

    def lookup(self, stem: str) -> dict:
        """Best-effort metadata for a ROM filename stem."""
        self._index()
        rec = (self._by_file.get(stem.lower())
               or self._by_title.get(_norm(stem)))
        if not rec:
            return {}
        title, desc, date, dev_, genre, pub = rec
        year = ""
        if date:
            m = re.search(r"(19|20)\d\d", str(date))
            year = m.group(0) if m else ""
        return {"title": title or "", "description": desc or "",
                "year": year, "developer": dev_ or "", "genre": genre or "",
                "publisher": pub or ""}


# -- LaunchBox art index (front/back/spine, region-preferred) ----------

def launchbox_ready() -> bool:
    try:
        indexed_region = LB_REGION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        # Pre-MEDIA_REGION indexes were always built Europe-first.
        return LB_INDEX.exists() and MEDIA_REGION_CANON == "Europe"
    return LB_INDEX.exists() and indexed_region == MEDIA_REGION_CANON


def sync_launchbox(log=print, fresh: bool = False):
    """Build the LaunchBox art index.

    Downloads Metadata.zip (~200MB) unless a cached copy exists (pass
    fresh=True to force a re-download), streams the ~1.7GB Metadata.xml
    with two iterparse passes (games, then their images), keeps only
    Mega Drive / Mega CD titles, and records the best FRONT/BACK/SPINE
    image per game preferring MEDIA_REGION, plus screenshots + video.
    The zip is KEPT so index-logic tweaks rebuild without re-downloading;
    only the extracted XML is deleted.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    zpath = CACHE_DIR / "launchbox-metadata.zip"
    xpath = CACHE_DIR / "Metadata.xml"
    if fresh or not zpath.exists():
        log("downloading LaunchBox Metadata.zip (~200MB)...")
        zpath.write_bytes(_fetch(LAUNCHBOX_ZIP, timeout=900))
    else:
        log(f"using cached {zpath.name}")
    with zipfile.ZipFile(zpath) as z:
        member = next(n for n in z.namelist()
                      if n.lower().endswith("metadata.xml"))
        with z.open(member) as src, open(xpath, "wb") as dst:
            while chunk := src.read(1 << 20):
                dst.write(chunk)

    index = build_launchbox_index(xpath, log)
    xpath.unlink()
    LB_INDEX.write_text(json.dumps(index), encoding="utf-8")
    LB_REGION_FILE.write_text(MEDIA_REGION_CANON, encoding="utf-8")
    n = sum(len(v) for v in index.values())
    log(f"LaunchBox index ready: {n} games at {LB_INDEX} "
        f"(media region: {MEDIA_REGION_CANON})")


def build_launchbox_index(xml_path, log=print) -> dict:
    """Two streaming passes over Metadata.xml -> compact art/meta index
    {system: {norm_title: {name, year, overview, front?, back?, spine?}}}.
    Separated from the download so it can be unit-tested."""
    xml_path = str(xml_path)

    def stream(tag):
        """Yield each <tag> element, keeping memory bounded by clearing the
        root after each one. (Clearing children as they end -- the naive
        way -- would wipe their text before the parent is read.)"""
        it = ET.iterparse(xml_path, events=("start", "end"))
        _, root = next(it)
        for ev, el in it:
            if ev == "end" and el.tag == tag:
                yield el
                root.clear()

    # pass 1: games we care about -> {dbid: {system, name, year, overview}}
    log("indexing games...")
    games: dict[str, dict] = {}
    for el in stream("Game"):
        system = LB_PLATFORMS.get((el.findtext("Platform") or "").strip())
        dbid = (el.findtext("DatabaseID") or "").strip()
        if system and dbid:
            games[dbid] = {
                "system": system,
                "name": (el.findtext("Name") or "").strip(),
                "year": (el.findtext("ReleaseYear") or "").strip(),
                "overview": (el.findtext("Overview") or "").strip(),
                "publisher": (el.findtext("Publisher") or "").strip(),
                "video": (el.findtext("VideoURL") or "").strip(),
                "_art": {},     # face -> (score, filename)
                "_shots": {}}   # shot type -> [filenames]

    # pass 2: their images -> best per face by region rank (+ recon penalty)
    log(f"indexing images for {len(games)} games...")
    for el in stream("GameImage"):
        g = games.get((el.findtext("DatabaseID") or "").strip())
        if g:
            img_type = (el.findtext("Type") or "").strip()
            fname = (el.findtext("FileName") or "").strip()
            face_pen = LB_TYPE_FACE.get(img_type)
            if face_pen and fname:
                face, penalty = face_pen
                score = _region_rank(
                    (el.findtext("Region") or "").strip()) + penalty
                cur = g["_art"].get(face)
                if cur is None or score < cur[0]:
                    g["_art"][face] = (score, fname)
            elif fname and img_type in LB_SHOT_TYPES:
                g["_shots"].setdefault(img_type, []).append(fname)

    def _rec_quality(rec: dict, raw_name: str) -> tuple:
        """Rank colliding records: official releases beat mods/hacks.

        _norm strips parentheticals, so "Game (Batadvantage)" collides
        with "Game" -- first-in-XML-order used to win, sometimes handing
        a real game's key to a fan mod's art. Prefer the record that
        looks like the official entry."""
        return (
            "(" not in raw_name,               # no "(...)" mod/beta suffix
            "front" in rec,                    # has a real box front
            bool(rec.get("year")),             # official entries have years
            ("back" in rec) + ("spine" in rec),
            len(rec.get("shots", [])),
            len(rec.get("overview", "")),
        )

    index: dict[str, dict] = {"md": {}, "mcd": {}}
    for g in games.values():
        art = {face: fn for face, (_s, fn) in g["_art"].items()}
        if not (g["name"] and (art or g["overview"])):
            continue
        # up to LB_MAX_SHOTS screenshots, best types first
        shots = []
        for t in LB_SHOT_TYPES:
            shots.extend(g["_shots"].get(t, []))
        rec = {"name": g["name"], "year": g["year"],
               "overview": g["overview"], "publisher": g.get("publisher", ""),
               **art}
        if shots:
            rec["shots"] = shots[:LB_MAX_SHOTS]
        if g.get("video"):
            rec["video"] = g["video"]
        key = _norm(g["name"])
        bucket = index[g["system"]]
        cur = bucket.get(key)
        if cur is None or (_rec_quality(rec, g["name"])
                           > _rec_quality(cur, cur["name"])):
            bucket[key] = rec
    return index


_lb_cache: dict | None = None


def _launchbox() -> dict:
    global _lb_cache
    if _lb_cache is None:
        _lb_cache = (json.loads(LB_INDEX.read_text(encoding="utf-8"))
                     if launchbox_ready() else {"md": {}, "mcd": {}})
    return _lb_cache


def launchbox_lookup(stem: str, system: str) -> dict:
    """LaunchBox record ({name, year, overview, front?, back?, spine?})
    for a ROM stem, matched by normalised title."""
    return _launchbox().get(system, {}).get(_norm(stem), {})


# -- box art ---------------------------------------------------------

def _libretro_name(stem: str) -> str:
    # libretro thumbnail names replace these characters with underscores
    return re.sub(r'[&*/:`<>?\\|"]', "_", stem)


def fetch_launchbox_image(filename: str) -> Path | None:
    """Cache + serve a LaunchBox image (UUID.jpg) by its FileName."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    p = ART_DIR / f"lb--{safe}"
    if p.exists():
        return p if p.stat().st_size else None
    ART_DIR.mkdir(parents=True, exist_ok=True)
    try:
        p.write_bytes(_fetch(LAUNCHBOX_IMG + urllib.parse.quote(filename),
                             timeout=25))
        return p
    except Exception:
        p.write_bytes(b"")  # negative-cache
        return None


def art_path(stem: str, system: str = "md") -> Path:
    safe = re.sub(r'[^A-Za-z0-9 ()\[\].,!+&_-]', "_", stem)
    return ART_DIR / f"{MEDIA_REGION_SLUG}--{system}--{safe}.png"


def _media_name_variants(stem: str) -> list[str]:
    """No-Intro thumbnail names in configured-region preference order."""
    base = stem.split("(")[0].strip()
    if MEDIA_REGION_CANON == "Europe":
        tags = ("Europe", "USA, Europe", "Japan, Europe", "World")
    elif MEDIA_REGION_CANON == "North America":
        tags = ("USA", "USA, Europe", "World")
    elif MEDIA_REGION_CANON == "Japan":
        tags = ("Japan", "Japan, USA", "World")
    elif MEDIA_REGION_CANON == "World":
        tags = ("World",)
    else:
        tags = (MEDIA_REGION_CANON, "World")
    variants = [f"{base} ({tag})" for tag in tags]
    if stem not in variants:
        variants.append(stem)  # the ROM's own region, last resort
    return variants


def fetch_libretro(stem: str, system: str = "md",
                   kind: str = "Named_Boxarts") -> Path | None:
    """Libretro thumbnail (Named_Boxarts front / Named_Snaps in-game
    screenshot), configured-region-first, disk-cached by region and stem."""
    suffix = "" if kind == "Named_Boxarts" else f"--{kind}"
    p = art_path(stem + suffix, system)
    if p.exists():
        return p if p.stat().st_size else None  # empty file = known miss
    ART_DIR.mkdir(parents=True, exist_ok=True)
    base_url = ("https://thumbnails.libretro.com/"
                + urllib.parse.quote(LIBRETRO_SYSTEMS.get(
                    system, LIBRETRO_SYSTEMS["md"])) + f"/{kind}/")
    for name in _media_name_variants(stem):
        try:
            p.write_bytes(_fetch(
                base_url + urllib.parse.quote(_libretro_name(name) + ".png"),
                timeout=20))
            return p
        except Exception:
            continue
    p.write_bytes(b"")  # negative-cache the miss
    return None


def fetch_libretro_front(stem: str, system: str = "md") -> Path | None:
    return fetch_libretro(stem, system, "Named_Boxarts")


def fetch_face(game: dict, face: str) -> Path | None:
    """Serve a box face (front|back|spine) for a library record.

    LaunchBox first (configured-region front/back/spine); libretro is the
    front-only fallback. Returns a cached local path or None.
    """
    if face in ("snap", "title"):  # screenshots (libretro only)
        kind = "Named_Snaps" if face == "snap" else "Named_Titles"
        return fetch_libretro(game.get("stem", ""),
                              game.get("system", "md"), kind)
    fname = (game.get("art") or {}).get(face)
    if fname:
        p = fetch_launchbox_image(fname)
        if p:
            return p
    if face == "front":
        return fetch_libretro_front(game.get("stem", ""),
                                    game.get("system", "md"))
    return None


# -- library assembly ------------------------------------------------

def folder_label(path: str, system: str) -> str:
    """Display folder for the library filter, derived from the ROM path.
    MD: the subfolder under the top level, or the top level for root games.
    MCD: always the top level (each disc lives in its own directory)."""
    parts = (path or "").replace("\\", "/").split("/")
    if not parts or not parts[0]:
        return ""
    if system == "mcd":
        return parts[0]
    return parts[1] if len(parts) >= 3 else parts[0]


def scan_folder(dev, folder: str, system: str, log=print) -> list[dict]:
    """Raw USB scan of ONE SD folder: list its ROMs and tag each with its
    display folder. No metadata merge -- see enrich_library. Split out of
    build_library so the web layer can scan folders as separate bounded jobs
    (each under its own hardware-worker timeout) instead of one long scan."""
    scanned = scan_sd_games(dev, folder, system)
    for g in scanned:
        g["folder"] = folder_label(g["path"], system)
    return scanned


def enrich_library(games: list[dict], log=print) -> list[dict]:
    """Merge OpenVGDB/LaunchBox metadata into raw scanned games and write the
    LIB_INDEX cache. Pure local work (no USB) -- runs after all folders are
    scanned. Art is fetched lazily per game by the web layer."""
    meta = Metadata() if openvgdb_ready() else None
    for g in games:
        info = meta.lookup(g["stem"]) if meta else {}
        lb = launchbox_lookup(g["stem"], g["system"])
        g["title"] = (info.get("title") or lb.get("name")
                      or g["stem"].split("(")[0].strip())
        g["year"] = info.get("year") or lb.get("year", "")
        g["description"] = info.get("description") or lb.get("overview", "")
        g["genre"] = info.get("genre", "")
        g["developer"] = info.get("developer", "")
        g["publisher"] = info.get("publisher") or lb.get("publisher", "")
        # LaunchBox image FileNames per face (front/back/spine) for the UI
        g["art"] = {face: lb[face] for face in ("front", "back", "spine")
                    if lb.get(face)}
        g["shots"] = lb.get("shots", [])
        g["video"] = lb.get("video", "")
    LIB_INDEX.parent.mkdir(parents=True, exist_ok=True)
    LIB_INDEX.write_text(json.dumps(games, indent=1), encoding="utf-8")
    folders = sorted({g["folder"] for g in games if g.get("folder")})
    log(f"library: {len(games)} games across {', '.join(folders) or 'none'}")
    return games


def build_library(dev, dirs: list[tuple[str, str]], log=print) -> list[dict]:
    """Scan the SD and merge metadata in one shot. dirs = [(sd_folder, system)].
    Thin wrapper over scan_folder + enrich_library; the web layer splits these
    into separate hardware-worker jobs, but callers that hold the port can use
    this."""
    games: list[dict] = []
    for folder, system in dirs:
        try:
            games.extend(scan_folder(dev, folder, system, log))
        except IOError as e:
            log(f"scan of {folder} skipped: {e}")
    return enrich_library(games, log)


def cached_library() -> list[dict]:
    if LIB_INDEX.exists():
        games = json.loads(LIB_INDEX.read_text(encoding="utf-8"))
        for g in games:  # backfill folder for pre-folder-tag caches (no rescan)
            if not g.get("folder"):
                g["folder"] = folder_label(g.get("path", ""), g.get("system", "md"))
        return games
    return []
