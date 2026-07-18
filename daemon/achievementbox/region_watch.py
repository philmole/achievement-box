"""Live NTSC/PAL cadence monitoring from the frame_watch vblank counter.

The boot-time $A10001 latch only reflects what the game read at power-on;
a console whose region switch is flipped mid-game keeps running NTSC game
logic at 50Hz (~17% slow motion) with nothing on the bus to re-latch. The
FPGA's frame_watch module counts vblank vector fetches; sampling that
counter over time gives the real frame rate regardless of what the game
believes. Policy lives here, hardware polling stays in the worker.
"""

from __future__ import annotations


def classify_frame_rate(rate_hz: float) -> str | None:
    """Map a measured vblank rate to ``"NTSC"``, ``"PAL"``, or None.

    None means "no verdict — hold state": below 40Hz the game has
    v-interrupts disabled (loading screens) or the gateware predates
    frame_watch; above 75Hz something other than vblank is reading the
    vector address (ROM checksums, vector-table copies), which can only
    inflate the rate, never fake a PAL cadence.
    """
    if rate_hz < 40.0:
        return None
    if rate_hz < 55.0:
        return "PAL"
    if rate_hz < 75.0:
        return "NTSC"
    return None


class RegionMonitor:
    """Debounced mid-game region-flip detector.

    Feed it the raw 8-bit vint counter and a monotonic timestamp each poll
    (~0.5s cadence). It trips once — returning True — after ``debounce``
    consecutive off-region verdicts; no-verdict samples reset the streak
    and never trip. Once tripped it stays tripped: the session is over
    until the next game launch, so create a fresh monitor per session.
    """

    def __init__(self, expected: str = "NTSC", debounce: int = 4):
        self.expected = expected
        self.debounce = debounce
        self.tripped = False
        self._prev_count: int | None = None
        self._prev_time: float | None = None
        self._streak = 0

    def feed(self, count: int, now: float) -> bool:
        """Process one counter sample; True exactly once, when tripping."""
        if self.tripped:
            return False
        prev_count, prev_time = self._prev_count, self._prev_time
        self._prev_count, self._prev_time = count, now
        if prev_count is None or now <= prev_time:
            return False  # first sample primes; bad clock = no verdict
        rate = ((count - prev_count) % 256) / (now - prev_time)
        verdict = classify_frame_rate(rate)
        if verdict is None or verdict == self.expected:
            self._streak = 0
            return False
        self._streak += 1
        if self._streak >= self.debounce:
            self.tripped = True
            return True
        return False
