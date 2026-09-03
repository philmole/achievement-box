"""Tests for the `edlink .stdio` response framing.

Regression coverage for the Linux bring-up failure: edlink announces
itself with a one-line version banner when the session starts, and under
mono that line is preceded by a UTF-8 BOM (mono emits one on the first
write to a redirected stream; .NET on Windows does not). EdlinkSession
read the banner where it expected the byte-count line, so the very first
memrd died with UnicodeDecodeError on the BOM's 0xEF.

The byte sequences below are the real ones captured from a Mega
EverDrive Pro over USB (mono 6.12, edlink v1.0.0.1, cart at the menu).
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

DAEMON_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DAEMON_DIR))

from achievementbox.memory import EdlinkSession, MST_MENU  # noqa: E402

BOM = b"\xef\xbb\xbf"
BANNER = b"edlink v1.0.0.1\n"
MST_RESPONSE = b"2\n" + MST_MENU  # count line, then raw payload


class _FakeProc:
    """Minimal stand-in for the edlink child process."""

    def __init__(self, stdout_bytes: bytes):
        self.stdout = io.BytesIO(stdout_bytes)
        self.stdin = io.BytesIO()
        self.killed = False

    def kill(self):
        self.killed = True

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


def _session(stdout_bytes: bytes) -> tuple[EdlinkSession, _FakeProc]:
    proc = _FakeProc(stdout_bytes)
    with patch("achievementbox.memory.subprocess.Popen", return_value=proc):
        return EdlinkSession("edlink.exe"), proc


class EdlinkStartupBannerTest(unittest.TestCase):
    def test_first_read_survives_mono_bom_and_banner(self):
        session, _ = _session(BOM + BANNER + MST_RESPONSE)
        self.assertEqual(session.read_mem(0x1800200, 2), MST_MENU)

    def test_banner_is_skipped_once_not_on_every_read(self):
        """The banner arrives once. Skipping it again would swallow a
        real byte-count line and desynchronise the stream."""
        session, _ = _session(BOM + BANNER + MST_RESPONSE + MST_RESPONSE)
        self.assertEqual(session.read_mem(0x1800200, 2), MST_MENU)
        self.assertEqual(session.read_mem(0x1800200, 2), MST_MENU)

    def test_windows_stream_without_banner_still_works(self):
        """.NET on Windows emits no BOM; the framing must not depend on
        a preamble being present."""
        session, _ = _session(MST_RESPONSE)
        self.assertEqual(session.read_mem(0x1800200, 2), MST_MENU)

    def test_garbage_after_connecting_is_an_error_not_skipped(self):
        """Once framing is established, an unparseable line means the
        session desynchronised -- fail loudly rather than hunt for the
        next number and return misaligned memory."""
        session, _ = _session(MST_RESPONSE + b"cart disconnected\n")
        self.assertEqual(session.read_mem(0x1800200, 2), MST_MENU)
        with self.assertRaises(IOError):
            session.read_mem(0x1800200, 2)


if __name__ == "__main__":
    unittest.main()
