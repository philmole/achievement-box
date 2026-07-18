"""Offline RetroAchievements submission durability and replay tests."""

from __future__ import annotations

import ctypes as C
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

DAEMON_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DAEMON_DIR))

from achievementbox.offline_queue import OfflineSubmissionQueue  # noqa: E402
from achievementbox.rcbridge import (  # noqa: E402
    RETRYABLE_CLIENT_ERROR,
    RcApiRequest,
    RcClient,
    SERVER_CALLBACK,
)


class _Response:
    def __init__(self, body=b'{"Success":true}', status=200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._body


class OfflineQueueTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.queue_dir = Path(self.temp_dir.name)
        self.client = RcClient.__new__(RcClient)
        self.client._log = lambda _message: None
        self.client.response_log = None
        self.client._submission_queue = OfflineSubmissionQueue(self.queue_dir)
        self.client._startup_queue_ids = set()

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _request(operation: str) -> RcApiRequest:
        return RcApiRequest(
            url=("https://retroachievements.org/dorequest.php?r=" +
                 operation).encode(),
            post_data=b"u=test&t=secret&a=123",
            content_type=b"application/x-www-form-urlencoded",
        )

    def _call(self, operation: str) -> tuple[int, bool, int]:
        result = []

        @SERVER_CALLBACK
        def completed(response, _data):
            value = response.contents
            result.append((value.http_status_code, value.body is None,
                           value.body_length))

        request = self._request(operation)
        self.client._c_server_call(C.pointer(request), completed, None, None)
        return result[0]

    def test_offline_unlock_is_durable_deduplicated_and_retryable(self):
        with patch("achievementbox.rcbridge.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("offline")):
            first = self._call("awardachievement")
            second = self._call("awardachievement")

        self.assertEqual(first, (RETRYABLE_CLIENT_ERROR, True, 0))
        self.assertEqual(second, first)
        self.assertEqual(len(self.client._submission_queue), 1)

        reopened = OfflineSubmissionQueue(self.queue_dir)
        pending = reopened.pending()
        self.assertEqual(len(pending), 1)
        self.assertIn("awardachievement", pending[0].url)
        self.assertEqual(pending[0].post_data,
                         b"u=test&t=secret&a=123")

        db_bytes = reopened.db_path.read_bytes()
        self.assertNotIn(b"secret", db_bytes)
        self.assertNotIn(b"awardachievement", db_bytes)

        with patch("achievementbox.rcbridge.urllib.request.urlopen",
                   return_value=_Response()):
            self.assertEqual(self._call("awardachievement")[0], 200)
        self.assertEqual(len(reopened), 0)

    def test_non_submission_is_not_queued(self):
        with patch("achievementbox.rcbridge.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("offline")):
            result = self._call("login2")
        self.assertEqual(result, (RETRYABLE_CLIENT_ERROR, True, 0))
        self.assertEqual(len(self.client._submission_queue), 0)

    def test_startup_queue_replays_after_connectivity_returns(self):
        old_queue = OfflineSubmissionQueue(self.queue_dir)
        queued_id = old_queue.enqueue(
            "https://retroachievements.org/dorequest.php?r=awardachievement",
            b"u=test&t=secret&a=123",
            "application/x-www-form-urlencoded",
        )
        self.client._startup_queue_ids = {queued_id}
        seen = []

        def online(request, timeout):
            seen.append(request.full_url)
            self.assertEqual(timeout, 20)
            return _Response()

        with patch("achievementbox.rcbridge.urllib.request.urlopen",
                   side_effect=online):
            result = self._call("login2")

        self.assertEqual(result, (200, False, len(b'{"Success":true}')))
        self.assertEqual(len(seen), 2)
        self.assertIn("login2", seen[0])
        self.assertIn("awardachievement", seen[1])
        self.assertEqual(len(old_queue), 0)

    def test_tampered_ciphertext_is_discarded_not_replayed(self):
        queue = self.client._submission_queue
        item_id = queue.enqueue(
            "https://retroachievements.org/dorequest.php?r=submitlbentry",
            b"u=test&t=secret&i=4&s=10",
            "application/x-www-form-urlencoded",
        )
        db = sqlite3.connect(queue.db_path)
        try:
            db.execute("UPDATE submissions SET payload = ? WHERE id = ?",
                       (b"modified", item_id))
            db.commit()
        finally:
            db.close()

        corrupt = []
        self.assertEqual(queue.pending(on_corrupt=corrupt.append), [])
        self.assertEqual(corrupt, [item_id])
        self.assertEqual(len(queue), 0)

    def test_successful_unlock_does_not_remain_queued(self):
        with patch("achievementbox.rcbridge.urllib.request.urlopen",
                   return_value=_Response()):
            result = self._call("awardachievement")
        self.assertEqual(result[0], 200)
        self.assertEqual(len(self.client._submission_queue), 0)

    def test_retryable_http_failure_remains_queued(self):
        for status in (408, 429, 500, 503):
            with self.subTest(status=status):
                with patch("achievementbox.rcbridge.urllib.request.urlopen",
                           return_value=_Response(b"retry later", status)):
                    result = self._call("awardachievement")
                self.assertEqual(result[0], status)
                self.assertEqual(len(self.client._submission_queue), 1)

    def test_non_retryable_http_failure_is_not_replayed(self):
        with patch("achievementbox.rcbridge.urllib.request.urlopen",
                   return_value=_Response(b"bad request", 400)):
            result = self._call("awardachievement")
        self.assertEqual(result[0], 400)
        self.assertEqual(len(self.client._submission_queue), 0)


if __name__ == "__main__":
    unittest.main()
