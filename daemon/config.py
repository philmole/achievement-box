"""Central daemon config: dev-machine paths from the environment / .env.

Every machine-specific path -- the krikzz USB tool, the sniffer core, the
Mega Drive games folder -- comes from an environment variable, so the daemon
runs unchanged on any box: your dev PC, the Pi, or a stranger's fresh clone.

Values may be set as real environment variables or in daemon/.env
(KEY=VALUE lines; see .env.example). Real environment variables win. This
module loads .env *on import*, before the path constants below are resolved,
so a .env override takes effect even for module-level constants.
"""

import os
from pathlib import Path

DAEMON = Path(__file__).parent
ROOT = DAEMON.parent
ENV_FILE = DAEMON / ".env"


def load_env_file(path: Path = ENV_FILE) -> None:
    """Minimal .env loader (no dependency; the same file works on the Pi).
    Real environment variables win over file entries."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


load_env_file()


def _path_env(key: str, default: Path) -> Path:
    value = os.environ.get(key)
    return Path(value) if value else default


# krikzz edlink.exe -- USB link tool, an EXTERNAL dependency
# (github.com/krikzz/mega-ed-pub). The default path is provided by the pinned
# references/mega-ed-pub submodule; set MED_EDLINK only for another location.
EDLINK = _path_env("MED_EDLINK", ROOT / "references" / "mega-ed-pub" / "edlink.exe")

# Sniffer core flashed onto the Mega EverDrive Pro. Defaults to the shipped,
# hardware-verified prebuilt; point MED_CORE_RBF at a fresh Quartus build to
# test a rebuild.
CORE_RBF = _path_env("MED_CORE_RBF", ROOT / "fpga" / "prebuilt" / "mega-pro.rbf")

# Mega Drive games folder on the cart's SD card. The EverDrive OS loads
# <MD_ROM_DIR>/mapper.rbf as the mapper core for launches from that folder,
# so this is where the achievements toggle stages our core.
MD_ROM_DIR = os.environ.get("MD_ROM_DIR", "MEGA DRIVE")

# Preferred artwork release region. Used when building the LaunchBox index and
# when choosing libretro thumbnail names. Common values/aliases: Europe/PAL,
# North America/USA/NTSC-U, Japan/NTSC-J, World, or ROM (own filename first).
MEDIA_REGION = os.environ.get("MEDIA_REGION", "Europe").strip() or "Europe"
