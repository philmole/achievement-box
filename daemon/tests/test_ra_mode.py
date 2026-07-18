"""Tests for native RA mode configuration and daemon session state."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

DAEMON_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DAEMON_DIR))

from achievementbox.ra_status import (  # noqa: E402
    availability_for_connection,
    mode_from_native,
)
from achievementbox.rcbridge import RcClient  # noqa: E402
from achievementbox.statehub import Hub  # noqa: E402


class _FakeFunction:
    def __init__(self, implementation=lambda *_args: None):
        self.implementation = implementation
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.implementation(*args)


class _FakeRcheevos:
    def __init__(self, getter_value=0, honor_set=True):
        self.native_mode = getter_value
        self.honor_set = honor_set
        self.rc_client_create = _FakeFunction(lambda *_args: 0x1234)
        self.rc_client_set_event_handler = _FakeFunction()
        self.rc_client_enable_logging = _FakeFunction()
        self.rc_client_set_hardcore_enabled = _FakeFunction(
            self._set_hardcore)
        self.rc_client_get_hardcore_enabled = _FakeFunction(
            lambda _client: self.native_mode)
        self.rc_client_destroy = _FakeFunction()

    def _set_hardcore(self, _client, enabled):
        if self.honor_set:
            self.native_mode = int(enabled)


class NativeModeBridgeTest(unittest.TestCase):
    def test_real_constructor_forces_and_reports_casual(self):
        fake = _FakeRcheevos()
        with tempfile.TemporaryDirectory() as temp_dir, patch(
                "achievementbox.rcbridge.C.CDLL", return_value=fake):
            client = RcClient(
                lambda _address, length: bytes(length),
                lambda _kind, _info: None,
                log=lambda _message: None,
                queue_dir=Path(temp_dir),
            )
            try:
                self.assertEqual(client.mode, "casual")
                self.assertEqual(
                    len(fake.rc_client_set_hardcore_enabled.calls), 1)
                _client, enabled = (
                    fake.rc_client_set_hardcore_enabled.calls[0])
                self.assertEqual(enabled, 0)
            finally:
                client.close()

        self.assertEqual(len(fake.rc_client_destroy.calls), 1)

    def test_native_mode_change_outside_mode_flow_is_rejected(self):
        fake = _FakeRcheevos()
        with tempfile.TemporaryDirectory() as temp_dir, patch(
                "achievementbox.rcbridge.C.CDLL", return_value=fake):
            client = RcClient(
                lambda _address, length: bytes(length),
                lambda _kind, _info: None,
                queue_dir=Path(temp_dir),
            )
            try:
                fake.native_mode = 1
                with self.assertRaisesRegex(RuntimeError, "changed"):
                    _ = client.mode
            finally:
                client.close()

    def test_invalid_native_boolean_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "invalid native"):
            mode_from_native(2)

    def test_constructor_fails_closed_and_destroys_bad_native_client(self):
        fake = _FakeRcheevos(getter_value=2, honor_set=False)
        with tempfile.TemporaryDirectory() as temp_dir, patch(
                "achievementbox.rcbridge.C.CDLL", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "invalid native"):
                RcClient(
                    lambda _address, length: bytes(length),
                    lambda _kind, _info: None,
                    queue_dir=Path(temp_dir),
                )
        self.assertEqual(len(fake.rc_client_destroy.calls), 1)


class DaemonRaStateTest(unittest.TestCase):
    def test_hub_exposes_mode_and_generic_availability(self):
        hub = Hub()
        hub.update(connection="logging-in", ra_mode="casual")
        hub.update(connection="playing", game={"title": "Homebrew"})

        state = hub.snapshot()
        self.assertEqual(state["type"], "state")
        self.assertEqual(state["ra_mode"], "casual")
        self.assertEqual(state["ra_availability"], "available")
        self.assertIsNone(state["ra_unavailable_reason"])

        # The library mapper preference is not the Casual/Hardcore mode.
        hub.update(toggle=False)
        self.assertEqual(hub.snapshot()["ra_mode"], "casual")

    def test_unavailable_reasons_are_specific_but_state_is_generic(self):
        expected = {
            "cd-session": "mega_cd",
            "no-set": "no_set",
            "unsupported-region": "unsupported_region",
            "core-inactive": "core_inactive",
            "capture-invalid": "capture_invalid",
            "offline": "offline",
            "ra-disabled": "ra_disabled",
        }
        hub = Hub()
        hub.update(ra_mode="casual")
        for connection, reason in expected.items():
            with self.subTest(connection=connection):
                hub.update(connection=connection)
                state = hub.snapshot()
                self.assertEqual(state["ra_mode"], "casual")
                self.assertEqual(state["ra_availability"], "unavailable")
                self.assertEqual(state["ra_unavailable_reason"], reason)

    def test_unknown_mode_cannot_enter_public_state(self):
        hub = Hub()
        with self.assertRaisesRegex(ValueError, "invalid RA mode"):
            hub.update(ra_mode="hardocre")
        self.assertIsNone(hub.snapshot()["ra_mode"])

    def test_ra_disabled_maps_to_specific_reason(self):
        availability, reason = availability_for_connection("ra-disabled")
        self.assertEqual(availability, "unavailable")
        self.assertEqual(reason, "ra_disabled")

    def test_unknown_connection_fails_to_generic_unavailable(self):
        availability, reason = availability_for_connection("future-system")
        self.assertEqual(availability, "unavailable")
        self.assertEqual(reason, "not_in_game")


if __name__ == "__main__":
    unittest.main()
