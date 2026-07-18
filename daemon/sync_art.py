"""One-time art/metadata sync for the game library.

Downloads the two datasets the library uses and builds compact local
indexes (then deletes the big source files):

  OpenVGDB   ~30MB   title / year / description (matched by No-Intro name)
  LaunchBox  ~200MB  FRONT / BACK / SPINE box scans, region-tagged, so the
                     picker can follow MEDIA_REGION -- images are pulled and
                     cached on demand from images.launchbox-app.com

Run once (again to refresh). Safe while a game is running -- no hardware
access, just HTTP. After this, rescan the SD from the web UI.

  .venv\\Scripts\\python daemon\\sync_art.py [--openvgdb-only] [--art-only]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from achievementbox import gamelib


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--openvgdb-only", action="store_true")
    ap.add_argument("--art-only", action="store_true",
                    help="LaunchBox box art only")
    ap.add_argument("--fresh", action="store_true",
                    help="re-download Metadata.zip instead of the cache")
    args = ap.parse_args()

    if not args.art_only:
        if gamelib.openvgdb_ready():
            print("OpenVGDB already present -- skipping")
        else:
            gamelib.sync_openvgdb()

    if not args.openvgdb_only:
        gamelib.sync_launchbox(fresh=args.fresh)

    print("done -- rescan the SD from the web UI to apply")


if __name__ == "__main__":
    main()
