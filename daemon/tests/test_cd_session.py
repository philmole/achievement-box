"""Tests for Mega CD session tracking across console resets.

Regression coverage for the wedge where resetting out of a Mega CD game
left cd_session armed forever (the MCU keeps reporting the CD title as
selected, so the old reconnect logic re-armed the flag on every poll and
/api/launch refused every cartridge launch).
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pathlib import Path

DAEMON_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DAEMON_DIR))

import webapp  # noqa: E402
from achievementbox.statehub import Hub  # noqa: E402


class _FakeBackend:
    def __init__(self, in_game):
        self._in_game = in_game

    def in_game(self):
        return self._in_game

    def close(self):
        pass


class _FakeDev:
    def __init__(self, rom_path, cue_path=""):
        self._rom_path = rom_path
        self._cue_path = cue_path

    def rom_path(self, path_type=0):
        return self._cue_path if path_type else self._rom_path


CD_PATH = "MEGA CD/Sonic CD (U).cue"
SCAN = [("MEGA CD", "mcd"), ("MEGA DRIVE", "md")]


class _SuperviseTestBase(unittest.TestCase):
    def setUp(self):
        self.hub = Hub()
        patches = [
            patch.object(webapp, "hub", self.hub),
            patch.object(webapp, "_toggle_preference", return_value=True),
            patch.object(webapp.gamelib, "cached_library", return_value=[]),
            patch.object(webapp.time, "sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        webapp.SCAN_DIRS[:] = SCAN
        self.addCleanup(lambda: webapp.SCAN_DIRS.clear())

        self.worker = webapp.HwWorker("COM9", "user", "pass")

    def _wire(self, in_game=None, rom_path="/" + CD_PATH, cue_path=""):
        self.worker._open_backend = lambda: _FakeBackend(in_game)
        self.worker._with_serial = (
            lambda fn, deadline=None: fn(_FakeDev(rom_path, cue_path)))


class ResetOutOfCdTest(_SuperviseTestBase):
    def test_reset_to_menu_clears_session(self):
        """USB back + CD title still selected + no game running => menu."""
        self.hub.update(cd_session=True, connection="cd-session")
        self.worker._cd_cart_gone = True
        self._wire(in_game=None)  # non-sniffer core: register unreadable

        self.worker._supervise([])

        self.assertFalse(self.hub.state["cd_session"])
        self.assertEqual(self.hub.state["connection"], "menu")
        # remembered so a later USB drop restores the session display
        self.assertEqual(self.worker._last_cd_path, CD_PATH)

    def test_running_game_keeps_session(self):
        """Mapper positively reports a game (MD+ keeps USB alive)."""
        self.hub.update(cd_session=True, connection="cd-session")
        self.worker._cd_cart_gone = True
        self._wire(in_game=True)

        self.worker._supervise([])

        self.assertTrue(self.hub.state["cd_session"])
        self.assertEqual(self.hub.state["connection"], "cd-session")

    def test_non_cd_selection_clears_session_and_memory(self):
        self.hub.update(cd_session=True, connection="cd-session")
        self.worker._cd_cart_gone = True
        self._wire(in_game=None, rom_path="/MEGA DRIVE/Sonic (W).md")

        self.worker._supervise([])

        self.assertFalse(self.hub.state["cd_session"])
        self.assertIsNone(self.worker._last_cd_path)

    def test_usb_drop_with_cd_selected_restores_session(self):
        """Relaunching the CD title from the console menu drops USB for
        longer than the grace window -- then the session display returns."""
        self.hub.update(cd_session=False, connection="menu")
        self.worker.com_port = None
        self.worker._last_cd_path = CD_PATH

        now = [100.0]
        with patch.object(webapp.time, "monotonic",
                          side_effect=lambda: now[0]), \
                patch("achievementbox.edpro.find_cart_port",
                      return_value=None):
            self.worker._supervise([])  # first failed poll: still in grace
            self.assertFalse(self.hub.state["cd_session"])
            self.assertEqual(self.hub.state["connection"], "offline")
            now[0] += webapp.CD_RESUME_GRACE
            self.worker._supervise([])  # drop outlived the grace window

        self.assertTrue(self.hub.state["cd_session"])
        self.assertEqual(self.hub.state["connection"], "cd-session")
        self.assertTrue(self.worker._cd_cart_gone)

    def test_transient_usb_drop_does_not_resurrect_cd_session(self):
        """A cartridge ROM switch drops USB for a few seconds. With a CD
        title still remembered, that brief drop must read as offline --
        not as the old CD title running (Sonic 2 booting showed as WWF)."""
        self.hub.update(cd_session=False, connection="menu")
        self.worker.com_port = None
        self.worker._last_cd_path = CD_PATH

        now = [100.0]
        with patch.object(webapp.time, "monotonic",
                          side_effect=lambda: now[0]), \
                patch("achievementbox.edpro.find_cart_port",
                      return_value=None):
            for _ in range(3):  # a few 2 s polls, all inside the grace window
                self.worker._supervise([])
                self.assertFalse(self.hub.state["cd_session"])
                self.assertEqual(self.hub.state["connection"], "offline")
                now[0] += 2.0

        # USB returns (the cartridge game finished loading): the grace timer
        # must reset so the next drop starts a fresh window.
        self._wire(in_game=None)
        with patch("achievementbox.edpro.find_cart_port",
                   return_value="COM9"):
            self.worker._supervise([])

        self.assertIsNone(self.worker._usb_gone_since)
        self.assertFalse(self.hub.state["cd_session"])
        self.assertEqual(self.hub.state["connection"], "menu")

    def test_usb_drop_without_cd_memory_reads_offline(self):
        self.hub.update(cd_session=False, connection="menu")
        self.worker.com_port = None
        self.worker._last_cd_path = None

        with patch("achievementbox.edpro.find_cart_port", return_value=None):
            self.worker._supervise([])

        self.assertFalse(self.hub.state["cd_session"])
        self.assertEqual(self.hub.state["connection"], "offline")


class ConsoleLaunchedCdTest(_SuperviseTestBase):
    """A CD title picked on the console menu keeps the cart on USB: the
    mapper reports "game" but the MCU rom path is empty and the .cue sits
    in the cue slot (hardware-confirmed 2026-07-17, Batman Returns). This
    must classify as a cd-session, never enter cartridge identification."""

    def test_empty_rom_path_with_cue_becomes_cd_session(self):
        self._wire(in_game=True, rom_path="", cue_path="/" + CD_PATH)
        with patch.object(webapp, "identify_running_game",
                          side_effect=AssertionError("must not identify")):
            self.worker._supervise([])

        self.assertTrue(self.hub.state["cd_session"])
        self.assertEqual(self.hub.state["connection"], "cd-session")
        self.assertEqual(self.hub.state["game"]["system"], "mcd")
        self.assertEqual(self.worker._last_cd_path, CD_PATH)

    def test_no_rom_and_no_cue_idles_quietly(self):
        """Console powered off with the cart still USB-powered: mapper says
        game, MCU has nothing. Settle at menu instead of hammering
        identification (2026-07-17: retried every 6 s for 20 minutes)."""
        self._wire(in_game=True, rom_path="", cue_path="")
        with patch.object(webapp, "identify_running_game",
                          side_effect=AssertionError("must not identify")):
            self.worker._supervise([])
            self.assertTrue(self.worker._unidentified_noted)
            self.worker._supervise([])  # stays quiet on repeat passes

        self.assertFalse(self.hub.state["cd_session"])
        self.assertEqual(self.hub.state["connection"], "menu")

    def test_non_mcd_cue_idles_instead_of_cd_session(self):
        self._wire(in_game=True, rom_path="",
                   cue_path="/OTHER/whatever.cue")
        with patch.object(webapp, "identify_running_game",
                          side_effect=AssertionError("must not identify")):
            self.worker._supervise([])

        self.assertFalse(self.hub.state["cd_session"])
        self.assertEqual(self.hub.state["connection"], "menu")

    def test_cartridge_rom_path_still_identifies(self):
        class _Stop(Exception):
            pass

        self._wire(in_game=True, rom_path="/MEGA DRIVE/Sonic (W).md")
        with patch.object(webapp, "identify_running_game",
                          side_effect=_Stop) as identify:
            with self.assertRaises(_Stop):
                self.worker._supervise([])
        identify.assert_called_once()
        self.assertIsNone(self.worker._last_cd_path)


class CdSessionTeardownDebounceTest(_SuperviseTestBase):
    """One flickered "menu" read during CD boot must not end the session
    (2026-07-17: a single transient False killed a fresh session)."""

    def _wire_sequence(self, readings):
        it = iter(readings)
        self.worker._open_backend = lambda: _FakeBackend(next(it))

    def test_single_menu_flicker_keeps_session(self):
        self.hub.update(cd_session=True, connection="cd-session")
        self._wire_sequence([False, True])

        self.worker._supervise([])  # flicker: debounced, session kept
        self.assertTrue(self.hub.state["cd_session"])
        self.worker._supervise([])  # game again: counter resets
        self.assertTrue(self.hub.state["cd_session"])
        self.assertEqual(self.worker._cd_menu_reads, 0)

    def test_two_consecutive_menu_reads_end_session(self):
        self.hub.update(cd_session=True, connection="cd-session")
        # third read feeds the post-teardown fallthrough into menu detection
        self._wire_sequence([False, False, False])

        self.worker._supervise([])
        self.assertTrue(self.hub.state["cd_session"])
        self.worker._supervise([])
        self.assertFalse(self.hub.state["cd_session"])
        self.assertEqual(self.hub.state["connection"], "menu")


class LaunchGuardTest(unittest.TestCase):
    def setUp(self):
        self.hub = Hub()
        patch.object(webapp, "hub", self.hub).start()
        self.addCleanup(patch.stopall)
        webapp.SCAN_DIRS[:] = SCAN
        self.addCleanup(lambda: webapp.SCAN_DIRS.clear())

    def _fake_worker(self, com_port):
        req = SimpleNamespace(error=None, result="launched")
        return SimpleNamespace(toggle_in_flight=False, com_port=com_port,
                               submit=lambda *a, **k: req)

    def test_cd_session_with_cart_offline_is_refused(self):
        self.hub.update(cd_session=True)
        with patch.object(webapp, "worker", self._fake_worker(None)):
            resp = webapp.api_launch({"path": "MEGA DRIVE/Sonic (W).md"})
        self.assertEqual(resp.status_code, 409)

    def test_cd_session_with_cart_online_launches(self):
        """Cart answering USB means the console is back at the menu."""
        self.hub.update(cd_session=True)
        with patch.object(webapp, "worker", self._fake_worker("COM9")):
            resp = webapp.api_launch({"path": "MEGA DRIVE/Sonic (W).md"})
        self.assertEqual(resp.get("message"), "launched")


if __name__ == "__main__":
    unittest.main()
