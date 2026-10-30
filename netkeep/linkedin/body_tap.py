"""A second, read-only copy of the page's answers, streamed as they arrive (#200, ADR 0006).

Chrome keeps no body for an answer the page's own client aborted after reading it:
a streamed ``pagination`` answer the page has everything it needs from, cancelled
while the stream is still open. The page moves on with the data; ``response.body()``
(``Network.getResponseBody``) then fails with "No data found", and the run loses that
page of the list (#197, #200). Part B of #200 reproduced exactly that on an isolated
Chrome, and found one passive way to keep the bytes: ``Network.streamResourceContent``,
which has Chrome forward an answer's data to a DevTools client as it arrives, before
anything aborts it.

A :class:`BodyTap` is that client's bookkeeping. It holds no browser: it receives
CDP event parameters (plain mappings) from the one read-only CDP session
:meth:`netkeep.linkedin.browser.BrowserRun.observe` opens for it, and calls back
through the one function it is given to ask Chrome to stream an answer it matched.
Nothing here sends, holds, changes, answers, or cancels a request: the session it
listens on enables the Network domain (events only) and asks for a matched answer's
data. What it keeps is used only when the page's own copy could not be read, and
only after the caller checks it: against what the page asked for next
(:mod:`netkeep.linkedin.page_connections`), or, for enrichment's lazy cards and
overlay, for being whole (:mod:`netkeep.linkedin.page_profiles`, #203).

**Bounded.** At most :data:`MAX_STREAMS` answers are held at once (the oldest is
dropped), each at most the observation's body limit; an answer that grows past it is
kept as too large, which is no copy at all. Bodies never reach a log.

Nothing here imports the ORM or opens a session (spec 9.10).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import collections
import contextlib
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from netkeep.linkedin.observe import ResponseMatch

log = logging.getLogger(__name__)

#: The CDP events a tap listens to, in the order Chrome sends them for one answer.
REQUEST_EVENT: Final = "Network.requestWillBeSent"
RESPONSE_EVENT: Final = "Network.responseReceived"
DATA_EVENT: Final = "Network.dataReceived"
FINISHED_EVENT: Final = "Network.loadingFinished"
FAILED_EVENT: Final = "Network.loadingFailed"

#: The one way a failed answer may still have a whole copy (#202 review): the page's
#: own client cancelled it after reading it. Any other failure (a connection reset, a
#: length mismatch, a failure Chrome did not mark as a cancel) may have cut it short.
ABORTED_BY_PAGE: Final = "net::ERR_ABORTED"

#: The most answers a tap holds at once; the oldest is dropped past it.
MAX_STREAMS: Final = 32

#: Why :meth:`BodyTap.take` had no copy to hand over, in fixed words (#405), for the
#: read's diagnostics: the next lost answer then says which of these it was.
MISS_NOT_MATCHED: Final = "never matched"
MISS_NOT_STREAMED: Final = "Chrome did not stream it"
MISS_NOT_STARTED: Final = "the stream did not start in time"
MISS_NOT_ENDED: Final = "the stream did not end in time"
MISS_FAILED: Final = "the stream ended by a failure"
MISS_TOO_LARGE: Final = "the copy grew past the body limit"

#: How near a read's start must be to a held answer's for the two to be one request, in
#: milliseconds (#713 review). Playwright's ``request.timing["startTime"]`` and the
#: start the tap works out come from two CDP sessions, each reading its own clock: on
#: Chrome 156 they differed by 0.015 to 0.03 ms, never 0 (#805 review).
PAIR_TOLERANCE_MS: Final = 1.0

#: How far every other held answer to the same request must start from the read's,
#: in milliseconds, for the nearest to be the read's own. Identical requests on Chrome
#: 156 started 16 ms apart; nearer than this, the tap can't tell them apart and pairs
#: as it did before starts (the oldest held answer).
PAIR_MARGIN_MS: Final = 5.0

#: Asks Chrome to stream one answer (``Network.streamResourceContent``) and returns
#: its parameters: ``bufferedData``, what had already arrived, base64-encoded.
StreamStart = Callable[[str], Awaitable[Mapping[str, Any]]]

_Key = tuple[str, str, str | None]


@dataclass(slots=True)
class _Stream:
    """One matched answer as the tap sees it."""

    key: _Key
    data: bytearray = field(default_factory=bytearray)
    too_large: bool = False
    #: Whether Chrome agreed to stream it; ``False`` when it refused (an answer that
    #: had already finished), ``None`` until it answered.
    streaming: bool | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    how: str | None = None
    #: ``loadingFailed``'s ``canceled`` and ``errorText``; a fixed Chrome error code,
    #: never logged.
    canceled: bool = False
    error: str | None = None
    #: ``requestWillBeSent``'s ``wallTime`` and ``timestamp``, when Chrome sent them.
    wall: float | None = None
    stamp: float | None = None
    #: When this request started, in milliseconds since the epoch, each way Playwright
    #: computes a response's ``request.timing["startTime"]``: what a read's start is
    #: compared with, within :data:`PAIR_TOLERANCE_MS` (#713 review). Empty when Chrome
    #: sent no times.
    starts: set[float] = field(default_factory=set)
    #: Whether the tap has heard this answer's ``responseReceived``. Until then its only
    #: start is ``wallTime``'s, while Playwright's is the timing-based one: they differ
    #: by however long the request queued, which has no bound (#811 review).
    answered: bool = False

    def distance(self, started_ms: float) -> float:
        """How far ``started_ms`` is from this request's nearest start, in milliseconds;
        infinite when it has none."""
        return min((abs(start - started_ms) for start in self.starts), default=math.inf)

    @property
    def whole(self) -> bool:
        """Whether the stream ended in a way that leaves the copy whole: it finished,
        or the page's own client cancelled it (``net::ERR_ABORTED``)."""
        if self.how == "finished":
            return True
        return self.how == "failed" and self.canceled and self.error == ABORTED_BY_PAGE


class BodyTap:
    """The streamed copies of the answers ``match`` names, by request (see the module).

    Feed it the CDP events in :meth:`handlers`; a caller whose own read of an answer
    failed asks :meth:`take` for the streamed copy of the same request.
    """

    def __init__(
        self,
        match: ResponseMatch,
        *,
        stream: StreamStart,
        detach: Callable[[], Awaitable[None]] | None = None,
        max_body_bytes: int,
    ) -> None:
        self.match = match
        self._stream = stream
        self._detach = detach
        self._max_body_bytes = max_body_bytes
        self._streams: collections.OrderedDict[str, _Stream] = collections.OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        #: How many streamed copies were handed to a caller whose read had failed.
        self.handed = 0
        #: Why the last :meth:`take` handed nothing over (one of the ``MISS_`` words),
        #: or ``None`` when it handed a copy (#405).
        self.last_miss: str | None = None
        #: How many takes and discards found their held answer by the read's start,
        #: and how many by the oldest held answer to the same request (#713 review).
        self.paired_by_start = 0
        self.paired_oldest = 0

    def handlers(self) -> tuple[tuple[str, Callable[[Mapping[str, Any]], None]], ...]:
        """Each CDP event a tap listens to, with its handler."""
        return (
            (REQUEST_EVENT, self.on_request),
            (RESPONSE_EVENT, self.on_response),
            (DATA_EVENT, self.on_data),
            (FINISHED_EVENT, self.on_finished),
            (FAILED_EVENT, self.on_failed),
        )

    # --- the listeners: read the event, never touch the request ----------------------

    def on_request(self, params: Mapping[str, Any]) -> None:
        if self._closed:
            return
        try:
            request_id = str(params["requestId"])
            request = params["request"]
            method = str(request["method"]).upper()
            url = str(request["url"])
            post_data = request.get("postData")
        except (KeyError, TypeError, AttributeError):
            return
        # A redirect is a second request event under the same id, for the new url
        # (#697). The entry held for the old url is dropped either way: what streams on
        # under this id is the new url's answer, which only a new match may keep, as
        # the newest entry, since the pop puts it back at the end.
        self._streams.pop(request_id, None)
        if not self.match.matches(method, url):
            return
        body = post_data if isinstance(post_data, str) else None
        entry = _Stream(key=(method, url, body))
        entry.wall, entry.stamp = _number(params.get("wallTime")), _number(params.get("timestamp"))
        if entry.wall is not None:
            # Playwright's start for an answer without timing (served from the cache).
            entry.starts.add(entry.wall * 1e3)
        self._streams[request_id] = entry
        while len(self._streams) > MAX_STREAMS:
            self._streams.popitem(last=False)

    def on_response(self, params: Mapping[str, Any]) -> None:
        entry, request_id = self._entry(params)
        if entry is None or request_id is None or entry.streaming is not None:
            return
        entry.answered = True
        response = params.get("response")
        timing = response.get("timing") if isinstance(response, Mapping) else None
        sent = _number(timing.get("requestTime")) if isinstance(timing, Mapping) else None
        if sent is not None and entry.stamp is not None and entry.wall is not None:
            # Playwright's start for an answer with timing, in its order of operations.
            entry.starts.add((sent - entry.stamp + entry.wall) * 1e3)
        task = asyncio.get_running_loop().create_task(self._start(request_id, entry))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _start(self, request_id: str, entry: _Stream) -> None:
        try:
            answer = await self._stream(request_id)
            buffered = _decode(answer.get("bufferedData"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Chrome refuses an answer that has already finished loading; its own
            # copy is then whole, and nothing here is needed.
            log.debug("body tap: an answer was not streamed (%s)", type(exc).__name__)
            entry.streaming = False
        else:
            entry.data[:0] = buffered
            entry.streaming = True
            self._check_size(entry)
        finally:
            entry.started.set()

    def on_data(self, params: Mapping[str, Any]) -> None:
        entry, _ = self._entry(params)
        if entry is None or entry.too_large or entry.streaming is False:
            return
        data = params.get("data")
        if isinstance(data, str) and data:
            entry.data += _decode(data)
            self._check_size(entry)

    def on_finished(self, params: Mapping[str, Any]) -> None:
        self._end(params, "finished")

    def on_failed(self, params: Mapping[str, Any]) -> None:
        entry, _ = self._entry(params)
        if entry is not None:
            entry.canceled = params.get("canceled") is True
            error = params.get("errorText")
            entry.error = error if isinstance(error, str) else None
        self._end(params, "failed")

    def _end(self, params: Mapping[str, Any], how: str) -> None:
        entry, _ = self._entry(params)
        if entry is None:
            return
        entry.how = how
        entry.ended.set()

    def _entry(self, params: Mapping[str, Any]) -> tuple[_Stream | None, str | None]:
        if self._closed:
            return None, None
        try:
            request_id = str(params["requestId"])
        except (KeyError, TypeError):
            return None, None
        return self._streams.get(request_id), request_id

    def _check_size(self, entry: _Stream) -> None:
        if len(entry.data) > self._max_body_bytes:
            entry.too_large = True
            entry.data = bytearray()

    # --- the caller's side --------------------------------------------------------------

    async def take(
        self,
        method: str,
        url: str,
        post_data: str | None,
        *,
        wait_s: float,
        started_ms: float | None = None,
    ) -> bytes | None:
        """The streamed copy of this read's held answer (:meth:`_own`), or ``None``.

        Waits up to ``wait_s`` for Chrome to say whether it streamed the answer and
        for the answer to end. The copy is handed over once and forgotten. ``None``
        when the tap never matched the request, Chrome did not stream it, the answer
        had not ended in time, it ended by any failure but the page's own cancel
        (``canceled`` with ``net::ERR_ABORTED``), or it grew past the body limit.
        """
        request_id = self._own((method.upper(), url, post_data), started_ms)
        if request_id is None:
            self.last_miss = MISS_NOT_MATCHED
            return None
        entry = self._streams.pop(request_id)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(wait_s):
                await entry.started.wait()
                await entry.ended.wait()
        self.last_miss = _miss(entry)
        if self.last_miss is not None:
            return None
        self.handed += 1
        return bytes(entry.data)

    def discard(
        self, method: str, url: str, post_data: str | None, *, started_ms: float | None = None
    ) -> None:
        """Forget this read's held answer (:meth:`_own`): the caller read its own copy,
        or takes none."""
        request_id = self._own((method.upper(), url, post_data), started_ms)
        if request_id is not None:
            del self._streams[request_id]

    def _own(self, key: _Key, started_ms: float | None) -> str | None:
        """The request id of a read's held answer to ``key``, or ``None`` when none is held.

        Playwright doesn't hand a read Chrome's request id, so a read is paired with its
        own request by when it started (#713 review): the held answer whose start is
        within :data:`PAIR_TOLERANCE_MS` of ``started_ms``, when every other held answer
        to the same request starts at least :data:`PAIR_MARGIN_MS` away. Starts count only
        once the tap has heard every such answer's ``responseReceived`` and each has a
        start, so each has the start Playwright computes: one not heard yet has just its
        ``wallTime`` start, off from Playwright's by its queueing time, and one without
        times has none; either could make another request's answer look nearest (#811
        review). Otherwise, with no start, an answer not yet heard or without times, none
        near enough, or two too near to tell apart, it is the oldest held answer to the
        same request, as before starts.
        """
        same = [(rid, entry) for rid, entry in self._streams.items() if entry.key == key]
        if not same:
            return None
        if started_ms is not None and all(entry.answered and entry.starts for _, entry in same):
            near = sorted((entry.distance(started_ms), rid) for rid, entry in same)
            nearest, rid = near[0]
            runner_up = near[1][0] if len(near) > 1 else math.inf
            if nearest <= PAIR_TOLERANCE_MS and runner_up >= PAIR_MARGIN_MS:
                self.paired_by_start += 1
                return rid
        self.paired_oldest += 1
        return same[0][0]

    async def close(self) -> None:
        """Stop, drop every held copy, and detach the session. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self.paired_by_start or self.paired_oldest:
            log.info(
                "body tap: reads paired by their start %d, by the oldest held answer %d",
                self.paired_by_start,
                self.paired_oldest,
            )
        self._streams = collections.OrderedDict()
        tasks, self._tasks = list(self._tasks), set()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        if self._detach is not None:
            try:
                await self._detach()
            except Exception as exc:
                log.debug("body tap: detaching failed (%s)", type(exc).__name__)


def _miss(entry: _Stream) -> str | None:
    """Why ``entry`` has no copy to hand over, in fixed words, or ``None`` when it has one."""
    if entry.too_large:
        return MISS_TOO_LARGE
    if not entry.started.is_set():
        return MISS_NOT_STARTED
    if not entry.streaming:
        return MISS_NOT_STREAMED
    if not entry.ended.is_set():
        return MISS_NOT_ENDED
    if not entry.whole:
        return MISS_FAILED
    return None


def _number(value: object) -> float | None:
    """A CDP time as a float, or ``None`` when it is missing or not a number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _decode(data: object) -> bytes:
    if not isinstance(data, str):
        return b""
    try:
        return base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return b""
