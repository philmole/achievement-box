"""Thread-safe daemon state snapshot and WebSocket fan-out."""

from __future__ import annotations

import asyncio
import json
import threading

from achievementbox.ra_status import (availability_for_connection,
                                      validate_mode)


class Hub:
    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self._subs: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        availability, reason = availability_for_connection("starting")
        self.state: dict = {
            "connection": "starting",
            "toggle": None,
            "game": None,
            "summary": None,
            "achievements": [],
            "rich_presence": None,
            "user": None,
            "cd_session": False,
            "ra_mode": None,
            "ra_availability": availability,
            "ra_unavailable_reason": reason,
        }

    def snapshot(self) -> dict:
        # Deep copy: the worker thread mutates achievements/summary while the
        # asyncio thread serializes snapshots.
        with self._lock:
            return json.loads(json.dumps(dict(self.state, type="state")))

    def update(self, **fields):
        if "ra_mode" in fields:
            validate_mode(fields["ra_mode"])
        if "connection" in fields:
            availability, reason = availability_for_connection(
                fields["connection"])
            fields["ra_availability"] = availability
            fields["ra_unavailable_reason"] = reason
        with self._lock:
            self.state.update(fields)
        self._push(self.snapshot())

    def event(self, payload: dict):
        self._push(payload)

    def _push(self, payload: dict):
        if self.loop is None:
            return

        def fan_out():
            for queue in list(self._subs):
                queue.put_nowait(payload)

        self.loop.call_soon_threadsafe(fan_out)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subs.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        self._subs.discard(queue)
