"""The body tap (#200): the streamed copy of an answer whose own body Chrome could not keep.

:class:`netkeep.linkedin.body_tap.BodyTap` is fed CDP event parameters by hand here,
the way the one read-only session ``BrowserRun._open_body_tap`` opens feeds it; the
stream call is a fake that records what it was asked. Nothing opens a socket.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Mapping
from typing import Any

import pytest
from cdp_times import RequestTimes, request_times

from netkeep.linkedin import body_tap
from netkeep.linkedin.body_tap import BodyTap
from netkeep.linkedin.observe import ResponseMatch, ResponseRule

ORIGIN = "https://www.linkedin.com"
PAGINATION = f"{ORIGIN}/flagship-web/rsc-action/actions/pagination?sduiid=fake"
MATCH = ResponseMatch(
    origin=ORIGIN, rules=(ResponseRule("POST", "/flagship-web/rsc-action/actions/pagination"),)
)
ASK = '{"startIndex":20}'


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class Stream:
    """The fake ``Network.streamResourceContent``: what was buffered, or a refusal."""

    def __init__(self, buffered: bytes = b"", refuse: bool = False) -> None:
        self.buffered = buffered
        self.refuse = refuse
        self.asked: list[str] = []

    async def __call__(self, request_id: str) -> Mapping[str, Any]:
        self.asked.append(request_id)
        if self.refuse:
            raise RuntimeError("Request with the provided ID has already finished loading")
        return {"bufferedData": _b64(self.buffered)}


def _tap(stream: Stream, *, limit: int = 1024) -> BodyTap:
    return BodyTap(MATCH, stream=stream, max_body_bytes=limit)


def _answer(
    tap: BodyTap,
    rid: str,
    *,
    url: str = PAGINATION,
    method: str = "POST",
    post: str | None = ASK,
    chunks: tuple[bytes, ...] = (),
    end: str | None = "failed",
    at: RequestTimes | None = None,
    cached: bool = False,
) -> None:
    """Feed the tap one answer's events. ``at``: Chrome's times for it (none without);
    ``cached``: served from the cache, so its response carries no timing."""
    request: dict[str, Any] = {"url": url, "method": method}
    if post is not None:
        request["postData"] = post
    sending: dict[str, Any] = {"requestId": rid, "request": request}
    response: dict[str, Any] = {"url": url, "status": 200}
    if at is not None:
        sending |= at.request_params()
        if not cached:
            response["timing"] = at.response_timing()
    tap.on_request(sending)
    tap.on_response({"requestId": rid, "response": response})
    for chunk in chunks:
        tap.on_data({"requestId": rid, "data": _b64(chunk), "dataLength": len(chunk)})
    if end == "failed":
        tap.on_failed({"requestId": rid, "errorText": "net::ERR_ABORTED", "canceled": True})
    elif end == "finished":
        tap.on_finished({"requestId": rid})


async def test_an_aborted_answer_is_handed_over_whole_once() -> None:
    stream = Stream(buffered=b"0:first-")
    tap = _tap(stream)
    _answer(tap, "7.1", chunks=(b"second-", b"third"))
    got = await tap.take("post", PAGINATION, ASK, wait_s=1.0)
    assert got == b"0:first-second-third" and stream.asked == ["7.1"] and tap.handed == 1
    assert await tap.take("POST", PAGINATION, ASK, wait_s=0.01) is None


async def test_only_matching_answers_are_streamed() -> None:
    stream = Stream()
    tap = _tap(stream)
    _answer(tap, "1", url=f"{ORIGIN}/voyager/api/graphql")
    _answer(tap, "2", method="GET")
    _answer(tap, "3", url="https://www.linkedin.com.evil.example.test/flagship-web/x")
    await asyncio.sleep(0)
    assert stream.asked == []


async def test_a_refused_stream_is_no_copy() -> None:
    """Chrome refuses an answer that already finished: its own copy is whole."""
    tap = _tap(Stream(refuse=True))
    _answer(tap, "1", chunks=(b"ignored",), end="finished")
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) is None


async def test_an_answer_that_has_not_ended_is_no_copy() -> None:
    tap = _tap(Stream(buffered=b"part"))
    _answer(tap, "1", end=None)
    assert await tap.take("POST", PAGINATION, ASK, wait_s=0.05) is None


async def test_an_answer_past_the_body_limit_is_no_copy() -> None:
    tap = _tap(Stream(buffered=b"x" * 10), limit=16)
    _answer(tap, "1", chunks=(b"y" * 10,))
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) is None


async def test_the_copy_is_matched_by_request_and_the_oldest_goes_first() -> None:
    tap = _tap(Stream())
    _answer(tap, "1", chunks=(b"first ask",))
    _answer(tap, "2", post='{"startIndex":30}', chunks=(b"another start",))
    _answer(tap, "3", chunks=(b"second ask",))
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) == b"first ask"
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) == b"second ask"
    assert await tap.take("POST", PAGINATION, '{"startIndex":30}', wait_s=1.0) == b"another start"


async def test_a_read_that_succeeded_discards_its_copy() -> None:
    tap = _tap(Stream())
    _answer(tap, "1", chunks=(b"read fine",), end="finished")
    _answer(tap, "2", chunks=(b"lost",))
    tap.discard("POST", PAGINATION, ASK)
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) == b"lost"


async def test_a_redirect_to_an_unmatched_url_drops_the_held_entry() -> None:
    """#697: a redirect is a second request event under the same id. What streams on
    under it is the new url's answer, which the match does not name."""
    stream = Stream()
    tap = _tap(stream)
    tap.on_request(
        {"requestId": "9", "request": {"url": PAGINATION, "method": "POST", "postData": ASK}}
    )
    tap.on_request(
        {
            "requestId": "9",
            "request": {"url": f"{ORIGIN}/voyager/api/graphql", "method": "POST"},
            "redirectResponse": {"url": PAGINATION, "status": 307},
        }
    )
    tap.on_response({"requestId": "9", "response": {"status": 200}})
    tap.on_data({"requestId": "9", "data": _b64(b"not ours"), "dataLength": 8})
    tap.on_failed({"requestId": "9", "errorText": "net::ERR_ABORTED", "canceled": True})
    await asyncio.sleep(0)
    assert stream.asked == [] and tap._streams == {}
    assert await tap.take("POST", PAGINATION, ASK, wait_s=0.05) is None
    assert tap.last_miss == body_tap.MISS_NOT_MATCHED


async def test_a_redirect_to_a_matched_url_is_held_for_the_new_url() -> None:
    """#697, #713 review: the redirect drops the old url's entry and holds the new url's
    as the newest, so it outlives the answers held before it. The oldest held answer
    redirects here; with the entry overwritten in place instead of dropped, it would
    keep its old place and be the one the next answer pushes out."""
    tap = _tap(Stream())
    other = f"{PAGINATION}&page=2"
    tap.on_request(
        {"requestId": "0", "request": {"url": PAGINATION, "method": "POST", "postData": ASK}}
    )
    for index in range(1, body_tap.MAX_STREAMS):
        _answer(tap, str(index), post=f'{{"startIndex":{index}}}', end=None)
    _answer(tap, "0", url=other, chunks=(b"the new answer",))
    _answer(tap, "next", post='{"startIndex":99}', end=None)  # one past the bound
    assert "1" not in tap._streams and "0" in tap._streams
    assert await tap.take("POST", PAGINATION, ASK, wait_s=0.05) is None
    assert await tap.take("POST", other, ASK, wait_s=1.0) == b"the new answer"
    await tap.close()


async def test_held_answers_are_bounded() -> None:
    tap = _tap(Stream())
    for index in range(body_tap.MAX_STREAMS + 5):
        _answer(tap, str(index), post=f'{{"startIndex":{index}}}', end=None)
    assert len(tap._streams) == body_tap.MAX_STREAMS
    assert "0" not in tap._streams and str(body_tap.MAX_STREAMS + 4) in tap._streams
    await tap.close()


async def test_malformed_events_are_ignored() -> None:
    tap = _tap(Stream())
    tap.on_request({})
    tap.on_request({"requestId": "1", "request": None})
    tap.on_response({"no": "id"})
    tap.on_data({"requestId": "unknown", "data": "!!"})
    tap.on_failed({})
    _answer(tap, "2", chunks=())
    tap.on_data({"requestId": "2", "data": 5})
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) == b""


async def test_close_detaches_once_and_stops_listening() -> None:
    detached: list[bool] = []

    async def detach() -> None:
        detached.append(True)

    tap = BodyTap(MATCH, stream=Stream(), detach=detach, max_body_bytes=1024)
    _answer(tap, "1", end=None)
    await tap.close()
    await tap.close()
    _answer(tap, "2", chunks=(b"after close",))
    assert detached == [True]
    assert await tap.take("POST", PAGINATION, ASK, wait_s=0.01) is None


async def test_a_detach_that_fails_is_quiet() -> None:
    async def detach() -> None:
        raise RuntimeError("Target closed")

    await BodyTap(MATCH, stream=Stream(), detach=detach, max_body_bytes=1024).close()


def test_the_tap_listens_to_the_network_events_only() -> None:
    tap = _tap(Stream())
    assert [event for event, _ in tap.handlers()] == [
        "Network.requestWillBeSent",
        "Network.responseReceived",
        "Network.dataReceived",
        "Network.loadingFinished",
        "Network.loadingFailed",
    ]
    assert body_tap.MAX_STREAMS == 32


@pytest.mark.parametrize("bad", ["!!not base64!!", None, 12])
async def test_undecodable_buffered_data_is_empty(bad: object) -> None:
    class Odd(Stream):
        async def __call__(self, request_id: str) -> Mapping[str, Any]:
            return {"bufferedData": bad}

    tap = _tap(Odd())
    _answer(tap, "1", chunks=(b"rest",))
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) == b"rest"


def test_the_tap_session_buffers_are_pinned() -> None:
    from netkeep.linkedin import browser

    assert browser.TAP_RESOURCE_BUFFER_BYTES == 8 * 1024 * 1024
    assert browser.TAP_TOTAL_BUFFER_BYTES == 32 * 1024 * 1024


@pytest.mark.parametrize(
    ("failed", "whole"),
    [
        ({"canceled": True, "errorText": "net::ERR_ABORTED"}, True),
        ({"canceled": False, "errorText": "net::ERR_ABORTED"}, False),
        ({"canceled": True, "errorText": "net::ERR_CONNECTION_RESET"}, False),
        ({"errorText": "net::ERR_CONTENT_LENGTH_MISMATCH"}, False),
        ({}, False),
    ],
    ids=["page-cancel", "not-canceled", "reset", "length-mismatch", "no-reason"],
)
async def test_a_failed_stream_is_a_copy_only_when_the_page_cancelled_it(
    failed: dict[str, Any], whole: bool
) -> None:
    """#202 review: only a cancel by the page's own client (``canceled`` and
    ``net::ERR_ABORTED``) leaves a failed stream whole; any other failure may have
    cut it short."""
    tap = _tap(Stream(buffered=b"all of it"))
    _answer(tap, "1", end=None)
    tap.on_failed({"requestId": "1", **failed})
    got = await tap.take("POST", PAGINATION, ASK, wait_s=1.0)
    assert (got == b"all of it") is whole and (got is None) is not whole


async def test_a_finished_stream_is_a_copy() -> None:
    tap = _tap(Stream(buffered=b"whole"))
    _answer(tap, "1", end="finished")
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) == b"whole"


def test_the_one_whole_failure_is_pinned() -> None:
    assert body_tap.ABORTED_BY_PAGE == "net::ERR_ABORTED"


# --- #405: why a take had no copy, in fixed words -------------------------------------------


class _Stalled(Stream):
    """A ``Network.streamResourceContent`` that never answers within the wait."""

    async def __call__(self, request_id: str) -> Mapping[str, Any]:
        self.asked.append(request_id)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.parametrize(
    ("case", "miss"),
    [
        ("unmatched", body_tap.MISS_NOT_MATCHED),
        ("refused", body_tap.MISS_NOT_STREAMED),
        ("stalled", body_tap.MISS_NOT_STARTED),
        ("open", body_tap.MISS_NOT_ENDED),
        ("reset", body_tap.MISS_FAILED),
        ("large", body_tap.MISS_TOO_LARGE),
        ("whole", None),
    ],
)
async def test_each_take_without_a_copy_says_why(case: str, miss: str | None) -> None:
    stream: Stream = _Stalled() if case == "stalled" else Stream(buffered=b"x" * 10)
    if case == "refused":
        stream = Stream(refuse=True)
    tap = _tap(stream, limit=16 if case == "large" else 1024)
    if case == "large":
        _answer(tap, "1", chunks=(b"y" * 10,))
    elif case == "refused":
        _answer(tap, "1", end="finished")
    elif case in ("stalled", "open", "reset"):
        _answer(tap, "1", end=None)
        if case == "reset":
            tap.on_failed({"requestId": "1", "errorText": "net::ERR_CONNECTION_RESET"})
    elif case == "whole":
        _answer(tap, "1")
    got = await tap.take("POST", PAGINATION, ASK, wait_s=0.05)
    assert tap.last_miss == miss and (got is None) is (miss is not None)
    await tap.close()


# --- #713 review: a read is paired with its own request by when it started ----------------

FIRST, SECOND = request_times(1), request_times(2)


async def test_identical_requests_finishing_out_of_order_each_get_their_own_copy() -> None:
    """Two answers to the same request, the later one's read failing first. Each read
    is paired with its own request by its start, within the tolerance, though the
    page's start is never exactly the tap's (each session reads its own clock)."""
    tap = _tap(Stream())
    _answer(tap, "1", chunks=(b"first answer",), at=FIRST)
    _answer(tap, "2", chunks=(b"second answer",), at=SECOND)
    assert SECOND.page_start_ms not in tap._streams["2"].starts  # skewed, as on Chrome
    second = await tap.take("POST", PAGINATION, ASK, wait_s=1.0, started_ms=SECOND.page_start_ms)
    first = await tap.take("POST", PAGINATION, ASK, wait_s=1.0, started_ms=FIRST.page_start_ms)
    assert (first, second) == (b"first answer", b"second answer")
    assert (tap.paired_by_start, tap.paired_oldest) == (2, 0)


async def test_an_answer_served_from_the_cache_pairs_by_its_wall_time() -> None:
    """Playwright's start for an answer whose timing it ignores is ``wallTime`` alone."""
    tap = _tap(Stream())
    _answer(tap, "1", chunks=(b"first answer",), at=FIRST, cached=True)
    _answer(tap, "2", chunks=(b"second answer",), at=SECOND)
    cached_start = FIRST.page_wall * 1e3
    got = await tap.take("POST", PAGINATION, ASK, wait_s=1.0, started_ms=cached_start)
    assert got == b"first answer" and tap.paired_by_start == 1


async def test_a_read_that_succeeded_discards_its_own_copy() -> None:
    tap = _tap(Stream())
    _answer(tap, "1", chunks=(b"lost",), at=FIRST)
    _answer(tap, "2", chunks=(b"read fine",), end="finished", at=SECOND)
    tap.discard("POST", PAGINATION, ASK, started_ms=SECOND.page_start_ms)
    assert list(tap._streams) == ["1"]
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) == b"lost"


@pytest.mark.parametrize(
    "newer",
    [request_times(1), request_times(1, offset_s=0.0005)],
    ids=["same-start", "half-a-millisecond-later"],
)
async def test_two_held_answers_at_one_start_go_oldest_first(newer: RequestTimes) -> None:
    """Two held answers that started within the margin of each other can't be told
    apart by start, even when the newer is the nearer: the oldest goes first, as before
    starts."""
    tap = _tap(Stream())
    _answer(tap, "1", chunks=(b"older",), at=FIRST)
    _answer(tap, "2", chunks=(b"newer",), at=newer)
    got = await tap.take("POST", PAGINATION, ASK, wait_s=1.0, started_ms=newer.page_start_ms)
    assert got == b"older"
    assert (tap.paired_by_start, tap.paired_oldest) == (0, 1)


async def test_a_runner_up_past_the_margin_still_pairs_by_start() -> None:
    tap = _tap(Stream())
    later = request_times(1, offset_s=0.006)  # 6 ms after the first: past the 5 ms margin
    _answer(tap, "1", chunks=(b"older",), at=FIRST)
    _answer(tap, "2", chunks=(b"newer",), at=later)
    got = await tap.take("POST", PAGINATION, ASK, wait_s=1.0, started_ms=later.page_start_ms)
    assert got == b"newer" and tap.paired_by_start == 1


@pytest.mark.parametrize(
    ("off_ms", "paired"), [(0.9, b"second answer"), (1.2, b"first answer")], ids=["in", "out"]
)
async def test_a_start_pairs_only_within_the_tolerance(off_ms: float, paired: bytes) -> None:
    """Past the tolerance no held answer is the read's by start, and the oldest goes."""
    tap = _tap(Stream())
    _answer(tap, "1", chunks=(b"first answer",), at=FIRST)
    _answer(tap, "2", chunks=(b"second answer",), at=SECOND)
    start = SECOND.page_start_ms + off_ms
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0, started_ms=start) == paired


@pytest.mark.parametrize("call", ["take", "discard"])
async def test_an_answer_not_yet_heard_pairs_nothing_by_start(call: str) -> None:
    """#811 review, as reproduced on Chrome 156: the read's own answer A queued 9.4 ms,
    and the tap hasn't heard its responseReceived yet, so A's only start is its
    ``wallTime``, 9.4 ms from the read's. B, an identical request that started 9.1 ms
    after A and was heard whole, is within the tolerance of the read's start. Pairing
    by start would pick B; until A is heard, the oldest (A, the read's own) goes, as
    before starts."""
    tap = _tap(Stream())
    a = request_times(1)
    queued = RequestTimes(
        wall=a.wall,
        stamp=a.stamp,
        sent=a.stamp + 0.0094,
        page_wall=a.page_wall,
        page_stamp=a.page_stamp,
    )
    b = request_times(1, offset_s=0.0091)
    request = {"url": PAGINATION, "method": "POST", "postData": ASK}
    tap.on_request({"requestId": "A", "request": request, **queued.request_params()})
    _answer(tap, "B", chunks=(b"B's answer",), at=b)
    assert abs(tap._streams["B"].distance(queued.page_start_ms)) <= body_tap.PAIR_TOLERANCE_MS
    if call == "discard":
        tap.discard("POST", PAGINATION, ASK, started_ms=queued.page_start_ms)
    else:  # A's own copy, not streaming yet, so none; B's is never handed over
        got = await tap.take("POST", PAGINATION, ASK, wait_s=0.01, started_ms=queued.page_start_ms)
        assert got is None and tap.last_miss == body_tap.MISS_NOT_STARTED
    assert list(tap._streams) == ["B"]  # B's copy stays for B's own read
    assert tap.paired_by_start == 0
    await tap.close()


async def test_a_newer_answer_not_yet_heard_pairs_nothing_by_start() -> None:
    """#811 re-review: every held answer must be heard, not only the oldest. A, the
    oldest, is heard whole; B, newer and identical, isn't heard yet, so its only start
    is its ``wallTime``, and the read's start falls within the tolerance of it with A
    past the margin. Until B is heard its real start is unknown, so the oldest goes."""
    tap = _tap(Stream())
    _answer(tap, "A", chunks=(b"A's answer",), at=FIRST)
    b = request_times(1, offset_s=0.009)
    request = {"url": PAGINATION, "method": "POST", "postData": ASK}
    tap.on_request({"requestId": "B", "request": request, **b.request_params()})
    read_start = b.wall * 1e3 + 0.4
    assert tap._streams["B"].distance(read_start) <= body_tap.PAIR_TOLERANCE_MS
    assert tap._streams["A"].distance(read_start) >= body_tap.PAIR_MARGIN_MS
    assert tap._own(("POST", PAGINATION, ASK), read_start) == "A"
    assert tap.paired_by_start == 0
    await tap.close()


async def test_a_heard_answer_without_times_pairs_nothing_by_start() -> None:
    """#811 re-review: A, the oldest, was heard, but its requestWillBeSent carried no
    times, so it has no start at all. B, an identical answer, starts within the
    tolerance of the read's start. A's own start is unknown, so the oldest goes."""
    tap = _tap(Stream())
    _answer(tap, "A", chunks=(b"A's answer",))
    assert tap._streams["A"].answered and tap._streams["A"].starts == set()
    _answer(tap, "B", chunks=(b"B's answer",), at=SECOND)
    assert tap._own(("POST", PAGINATION, ASK), SECOND.page_start_ms) == "A"
    assert tap.paired_by_start == 0
    await tap.close()


async def test_without_a_start_the_oldest_goes_first() -> None:
    tap = _tap(Stream())
    _answer(tap, "1", chunks=(b"first answer",), at=FIRST)
    _answer(tap, "2", chunks=(b"second answer",), at=SECOND)
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) == b"first answer"
    assert (tap.paired_by_start, tap.paired_oldest) == (0, 1)


async def test_the_tap_works_out_playwrights_start_in_playwrights_order() -> None:
    """``(requestTime - timestamp + wallTime) * 1e3``, as Playwright's crNetworkManager
    computes it, and ``wallTime * 1e3`` for an answer from the cache. Written out: for
    these times another order of the same operations gives another float."""
    tap = _tap(Stream())
    _answer(tap, "7", end=None, at=request_times(7))
    assert tap._streams["7"].starts == {1793462400552.759, 1793462400551.8857}
    await tap.close()


def test_the_pairing_bounds_are_pinned() -> None:
    assert body_tap.PAIR_TOLERANCE_MS == 1.0
    assert body_tap.PAIR_MARGIN_MS == 5.0


@pytest.mark.parametrize("bad", [None, "1793462400.4", True, [1.0]])
async def test_times_that_are_not_numbers_are_no_start(bad: object) -> None:
    tap = _tap(Stream())
    tap.on_request(
        {
            "requestId": "1",
            "request": {"url": PAGINATION, "method": "POST", "postData": ASK},
            "wallTime": bad,
            "timestamp": bad,
        }
    )
    tap.on_response({"requestId": "1", "response": {"timing": {"requestTime": bad}}})
    assert tap._streams["1"].starts == set()
    await tap.close()


async def test_close_says_how_reads_were_paired(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="netkeep")
    tap = _tap(Stream())
    _answer(tap, "1", chunks=(b"first answer",), at=FIRST)
    _answer(tap, "2", chunks=(b"second answer",), at=SECOND)
    await tap.take("POST", PAGINATION, ASK, wait_s=1.0, started_ms=SECOND.page_start_ms)
    await tap.take("POST", PAGINATION, ASK, wait_s=1.0)
    await tap.close()
    assert "body tap: reads paired by their start 1, by the oldest held answer 1" in caplog.text
