"""Smoke test for the actual shipped Windows rcheevos DLL."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

DAEMON_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DAEMON_DIR))

from achievementbox.rcbridge import DLL_PATH, RcClient  # noqa: E402


@unittest.skipUnless(sys.platform == "win32" and DLL_PATH.is_file(),
                     "requires the shipped Windows rcheevos.dll")
class ShippedRcheevosNativeTest(unittest.TestCase):
    def test_actual_dll_is_created_in_casual_mode(self):
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
