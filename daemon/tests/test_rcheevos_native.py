"""Smoke test for the actual native rcheevos library of this platform.

Windows uses the shipped MSVC rcheevos.dll; Linux uses librcheevos.so
built locally by daemon/lib/build_rcheevos.sh. rcbridge.DLL_PATH already
resolves to whichever applies, so the test follows it rather than
naming a platform.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

DAEMON_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DAEMON_DIR))

from achievementbox.rcbridge import DLL_PATH, RcClient  # noqa: E402


@unittest.skipUnless(DLL_PATH.is_file(),
                     f"requires the native rcheevos library at {DLL_PATH}")
class NativeRcheevosLibraryTest(unittest.TestCase):
    def test_native_library_creates_client_in_casual_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = RcClient(
                lambda _address, length: bytes(length),
                lambda _kind, _info: None,
                log=lambda _message: None,
                queue_dir=Path(temp_dir),
            )
            try:
                self.assertEqual(client.mode, "casual")
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
