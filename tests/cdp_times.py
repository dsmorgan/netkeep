"""A request's times as Chrome reports them to two CDP sessions, for the body tap fakes.

The body tap pairs a read with its own request by when it started (#713 review).
Playwright works out a read's ``request.timing["startTime"]`` from its own page
session's ``requestWillBeSent`` (``wallTime``, ``timestamp``) and the response's
``timing.requestTime``; the tap works out the same from its own session's events.
Each session reads its own clock for the first two, so on a real Chrome the two
starts differ by a few hundredths of a millisecond, never 0 (Chrome 156, #805 review).
These times do the same: realistic, not exact binary fractions (so the order of the
float operations matters), with the page session's clock a little apart from the
tap's, and identical requests about 16 ms apart, as measured.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The first request's times, in seconds, as on a real Mac: ``wallTime`` since the
#: epoch and ``timestamp`` since boot.
FIRST_WALL = 1_793_462_400.437219
FIRST_STAMP = 873_512.348765
#: How far apart consecutive requests start, in seconds (16 ms measured).
SPACING_S = 0.016381
#: From ``requestWillBeSent`` to the request being sent (``timing.requestTime``).
QUEUED_S = 0.000873
#: How much later the page session's clocks read than the tap's, in seconds. The
#: page's start comes out (0.0000247 - 0.0000031) * 1000 = 0.0216 ms below the tap's.
PAGE_WALL_SKEW_S = 0.0000031
PAGE_STAMP_SKEW_S = 0.0000247


@dataclass(frozen=True, slots=True)
class RequestTimes:
    """One request's times. ``wall`` and ``stamp`` are what the tap's session hears,
    ``page_wall`` and ``page_stamp`` what Playwright's page session hears, and ``sent``
    the response's ``timing.requestTime``, which both hear alike."""

    wall: float
    stamp: float
    sent: float
    page_wall: float
    page_stamp: float

    @property
    def page_start_ms(self) -> float:
        """Playwright's ``request.timing["startTime"]``, as its ``crNetworkManager``
        computes it from the page session's times."""
        return (self.sent - self.page_stamp + self.page_wall) * 1e3

    def request_params(self) -> dict[str, float]:
        """The times in the tap's ``Network.requestWillBeSent``."""
        return {"wallTime": self.wall, "timestamp": self.stamp}

    def response_timing(self) -> dict[str, float]:
        """``Network.responseReceived``'s ``response.timing``, as far as the tap reads it."""
        return {"requestTime": self.sent}


def request_times(n: int, *, offset_s: float = 0.0) -> RequestTimes:
    """The ``n``-th request's times, ``offset_s`` later than its place in the spacing."""
    wall = FIRST_WALL + n * SPACING_S + offset_s
    stamp = FIRST_STAMP + n * SPACING_S + offset_s
    return RequestTimes(
        wall=wall,
        stamp=stamp,
        sent=stamp + QUEUED_S,
        page_wall=wall + PAGE_WALL_SKEW_S,
        page_stamp=stamp + PAGE_STAMP_SKEW_S,
    )
