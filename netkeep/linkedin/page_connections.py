"""The connections list, read from what the page loads (ADR 0006, spec 9.3, 9.4).

:class:`PageConnections` is the :class:`~netkeep.linkedin.connections.ConnectionsSource`
a live sync reads through. It does what a person does -- opens the connections page and
scrolls it -- and reads the answers the page itself fetched on netkeep's tab: the first
screen (carried in the page's own HTML, or in the screen request an in-app navigation
sends) and each ``pagination`` answer the scroll made the page ask for. It sends no
request of its own, intercepts none, and changes none: the only input it gives the page
is :meth:`~netkeep.linkedin.browser.BrowserRun.scroll`'s mouse-wheel replay, and the
only thing it reads is :meth:`~netkeep.linkedin.browser.BrowserRun.observe`'s record of
responses the browser already received.

**One budgeted unit, as before.** :func:`~netkeep.linkedin.connections.run_connections_sync`
calls :meth:`PageConnections.fetch_page` once per unit, after the gate has spent one
``connection_pages`` unit (spec 9.6). One call returns ``count`` connections (40 by
default, the spec's page) from the list read so far, scrolling for more only while it
has fewer: about four of the page's own ten-card answers. So the budget still counts
roughly forty connections per unit, as spec 9.6's table assumes, and one unit never
triggers more than the answers those forty need plus :data:`MAX_IDLE_SCROLLS` scrolls
that brought nothing.

**What ends the list.** Only the page's own answers do
(:attr:`~netkeep.linkedin.flagship.ConnectionsChunk.ends_list`): a connections
answer with no cards, or a short answer that asks for no next page -- and a full
answer that asks for none once the run has seen as many distinct people as the first
screen's total, less :func:`~netkeep.linkedin.connections.completion_slack` (a list
whose visible length is a multiple of ten; #208). A page that simply stops
loading more is not an end: after :data:`MAX_IDLE_SCROLLS` scrolls that
brought nothing, the call answers ``RouteChanged``, so the run stops and ages nobody.
A short slice is returned only once the end is proven, which is what lets the job's
short-page rule stand; that slice carries :attr:`~netkeep.linkedin.connections.SourcePage.ended`,
so the job ends there rather than read one more, empty, unit to find out (#515).

**Hidden members** (#515). LinkedIn's total counts *places*, and an answer can hold
fewer cards than the places it covers: nine cards, still asking for the start ten
places on. The member in that place is hidden from the list. The run counts those
places over the answers it read whole
(:attr:`~netkeep.linkedin.connections.SourcePage.hidden`) and reports them; the
completion slack still decides whether the run is complete.

**What makes a run complete.** The first screen states the total connection count, and
every slice reports it as the page's ``total``.
:attr:`~netkeep.linkedin.connections.SyncResult.complete` then holds a full sync to
the bar it always has: the end of the list proven, and distinct URNs seen no more than
:func:`~netkeep.linkedin.connections.completion_slack` short of that total (#204) --
the larger of 5 and one percent of it, rounded up, since LinkedIn's own total can count
a member the list never shows. A first screen without a total reports
0, which completes nothing. The answers must also arrive in order: each pagination
answer's ``startIndex`` has to be the one the previous answer asked for. A retried
request for a page already read is skipped; a start past it means a page went unseen,
and the call answers ``RouteChanged``.

**Classified before it is read** (spec 9.7). Where a navigation or a scroll left the tab
is classified first: a checkpoint or a login wall stops the run as that, and a tab that
has left the connections page answers ``RouteChanged``. A pagination answer that is not
``200`` is classified from its status and url, a redirect from where it pointed. No
HTML is ever searched for wall paths (#188 review, M1): every logged-in page links to
``/uas/logout`` and ``/login``, so a document without a first screen is judged by the
tab's url alone, and a wall served in place, which that cannot see, stops the run as
``RouteChanged`` -- no session flag, no heat. The first non-``Ok`` answer is sticky:
every later call returns it, except that a unit already read whole from answers that
arrived before it is still handed over. A unit is never handed over part-read.
:class:`~netkeep.linkedin.observe.ObservationFailed` -- the mechanism failing, not
LinkedIn answering -- is handled the same way, except that the run ends by that
exception rather than an outcome: recorded failed, never complete.

**An answer whose body cannot be read** (#197, #200). Chrome can receive a
pagination answer and still have no body to hand over: the page's own client aborted
the stream after reading it, or the body was evicted. The answer's start is compared
with the expected one first, so a stale or repeated answer without a body is skipped
like any other. One for the expected start is logged at INFO, with the exception's
class, a fixed category, and the observation's fixed diagnostics
(:class:`~netkeep.linkedin.observe.ReadDiagnostics`), never its message, and the
scroll goes on:

* If the page asks for that start again and the answer reads, the run goes on as if
  nothing was lost.
* If the page moves on to the next answer (at most :data:`~netkeep.linkedin.flagship.FULL_PAGE`
  past the lost start), the loss is recorded and the run **reads on** (#200): the
  page itself read that answer, only Chrome's copy is gone. The loss travels with
  the next slice handed over (:attr:`~netkeep.linkedin.connections.SourcePage.lost`),
  and a run with any loss is never complete, so it ages nobody.
* The call raises :class:`~netkeep.linkedin.connections.AnswerLost`, a safe
  incomplete stop (:attr:`~netkeep.linkedin.connections.StopReason.ANSWER_LOST`),
  when more than :data:`MAX_LOST_PER_RUN` answers are lost in one run, when the page
  jumps further past a lost start than one answer, or when a lost answer is not read
  again or moved past within :data:`MAX_IDLE_SCROLLS` scrolls (a stall after a loss).
  None of these is ``RouteChanged``, so none counts toward the route-changed breaker.

**The streamed copy** (#200, ADR 0006's amendment). This source observes with the
body tap (:mod:`netkeep.linkedin.body_tap`), so a lost answer may come with the copy
Chrome streamed to netkeep's read-only CDP session as the answer arrived. Before a
loss is recorded, that copy stands in for the answer only if it parses whole as the
page for the lost start and agrees with what the page did next: when the page moved
on, it must ask for the start the page asked for and hold at least one card and no
more cards than the places in between (fewer when the answer hides a member, #515);
when the page went quiet, it must prove the end of the list. Otherwise the answer is
lost, as above.

The list's end is still proven only by the page's own answers; the cards of a lost
answer (the gap between its start and the next one) count toward the first screen's
total when a full last answer that asks for nothing is weighed as the end. A body
that could not be kept for any other reason (too large, too slow) is still
:class:`~netkeep.linkedin.observe.ObservationFailed`.

**Cancel** lands between units, at the gate's next check (spec 9.9): one call spends a
few scrolls at most, and stopping partway through one would hand the job a short slice
it could mistake for the end of the list.

Nothing here imports the ORM or opens a session (spec 9.10). This module holds a real tab
across real time, so it is registered in ``tests/test_browser_safety.py`` as a browser
module, never imported from a request handler (spec 9.9).
"""

from __future__ import annotations

import dataclasses
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, cast
from urllib.parse import urlsplit

from netkeep.linkedin.browser import BrowserRun, BrowserUnavailable, PageLike
from netkeep.linkedin.classify import Outcome, classify
from netkeep.linkedin.connections import AnswerLost, LostAnswer, SourcePage, completion_slack
from netkeep.linkedin.flagship import (
    CONNECTIONS_PAGE_PATH,
    CONNECTIONS_SCREEN_PATH,
    FULL_PAGE,
    PAGINATION_PATH,
    SORT_NEWEST_FIRST,
    ConnectionsChunk,
    parse_connections_chunk,
    parse_pagination_request,
    rehydration_payload,
)
from netkeep.linkedin.flight import is_whole, parse_flight, why_not_whole
from netkeep.linkedin.observe import (
    FAILURE_REDIRECT,
    FAILURE_UNREADABLE,
    Observation,
    ObservationFailed,
    ObservationLimits,
    ObservedResponse,
    ReadDiagnostics,
    ResponseMatch,
    ResponseRule,
)
from netkeep.linkedin.pacing import (
    DEFAULT_SCROLL_PROFILE,
    ScrollProfile,
    scroll_like_a_person,
)
from netkeep.linkedin.strict_origin import (
    LINKEDIN_ORIGIN,
    on_origin,
    require_page_origin,
    url_path,
)
from netkeep.linkedin.voyager import ConnectionsPageResult, ConnectionSummary, RouteChanged

log = logging.getLogger(__name__)

#: What :class:`PageConnections` calls itself in logs and ``RouteChanged``.
PAGE_CONNECTIONS_ENDPOINT: Final = "flagship-web/connections-list"

#: How long, after a scroll's dwell, to wait for the answer it made the page ask for.
#: The captured answers took a quarter to half a second.
RESPONSE_WAIT_S: Final = 5.0

#: How long to wait for the first screen after the navigation returns.
LANDING_WAIT_S: Final = 20.0

#: Scrolls in one call that bring no new answer before the call gives up with
#: ``RouteChanged``: the page stopped loading without proving the list's end.
MAX_IDLE_SCROLLS: Final = 6

#: Answers the landing reads looking for the first screen before it gives up: a page
#: that keeps answering with documents that carry none is not the connections page.
MAX_LANDING_ANSWERS: Final = 8

#: Answers one run may lose and read on past (#200). One more stops the run as
#: ``answer_lost``: a run losing that many is not reading the list.
MAX_LOST_PER_RUN: Final = 5


@dataclass(frozen=True, slots=True)
class _Pending:
    """An answer for the expected start that arrived unreadable and is not settled yet."""

    start: int
    cause: str
    diagnostics: ReadDiagnostics | None
    #: The body tap's streamed copy of the answer (#200), unchecked until the page
    #: says what it asked for next.
    streamed: bytes | None
    #: What became of that copy (#735), one of the ``COPY_`` words: why there is none,
    #: not checked yet, or the rescue rule it broke. The run record keeps it.
    copy: str


#: How a lost answer the page moved past ends (#200): the run read on without it.
MOVED_PAST: Final = "the page moved past it"

#: How a lost answer ends when the run stops for another reason first (#201 review, L2).
STOPPED_PENDING: Final = "the run stopped before it was read again"

#: What became of a lost answer's streamed copy (#735), in fixed words, for the log and
#: the run record. A missing copy is :data:`COPY_NONE`, a colon, and why the tap had
#: none: the tap's own words (:data:`~netkeep.linkedin.body_tap.MISS_NOT_MATCHED` and
#: the rest), :data:`~netkeep.linkedin.observe.NO_TAP`, or :data:`MISS_UNKNOWN`. Never
#: a url, a body, or anything from the copy itself.
COPY_NONE: Final = "no streamed copy"
#: The read kept no diagnostics, or they name no reason, so why the copy is missing is
#: not known.
MISS_UNKNOWN: Final = "no reason recorded"
#: A copy the run stopped before checking: the page never said what it asked next.
COPY_NOT_CHECKED: Final = "streamed copy not checked"
#: A copy that does not parse as the page for the lost start.
COPY_UNPARSED: Final = "streamed copy did not parse"
#: A copy of the last answer that asks for a next page, so it does not prove the end.
COPY_ASKS_NEXT: Final = "streamed copy asks for a next page"
#: A copy of the last answer that leaves the run short of the first screen's total.
COPY_SHORT: Final = "streamed copy short of the total"
#: A copy of the last answer with no total to check it against: the first screen
#: gave none, so the copy cannot prove the end.
COPY_NO_TOTAL: Final = "streamed copy has no total to check"
#: A copy that does not agree with the start the page asked for next.
COPY_DISAGREES: Final = "streamed copy disagrees with the next request"


def _copy_note(streamed: bytes | None, diagnostics: ReadDiagnostics | None) -> str:
    """What a lost answer's streamed copy is before it is checked, in fixed words (#735)."""
    return COPY_NOT_CHECKED if streamed is not None else f"{COPY_NONE}: {_miss(diagnostics)}"


def _miss(diagnostics: ReadDiagnostics | None) -> str:
    """Why the body tap had no copy of a lost answer, in fixed words (#405, #735)."""
    miss = None if diagnostics is None else diagnostics.streamed_miss
    return MISS_UNKNOWN if miss is None else miss


class PageConnections:
    """The connections list from the page's own answers. One instance per run.

    ``origin`` is LinkedIn's, and a loopback origin only for the smoke suite's
    replica: anything else is refused. ``require_newest_first`` refuses a list the
    page sorts any other way than by recently added -- an incremental sync, which
    stops at the first page of connections it knows, is only right newest first.
    ``rng``, ``scroll_profile``, and ``sleep`` shape the scroll exactly as they do for
    :meth:`~netkeep.linkedin.browser.BrowserRun.scroll`; the tests pass fast ones.
    """

    def __init__(
        self,
        run: BrowserRun,
        *,
        origin: str = LINKEDIN_ORIGIN,
        require_newest_first: bool = False,
        rng: random.Random | None = None,
        scroll_profile: ScrollProfile = DEFAULT_SCROLL_PROFILE,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        response_wait_s: float = RESPONSE_WAIT_S,
        landing_wait_s: float = LANDING_WAIT_S,
        max_idle_scrolls: int = MAX_IDLE_SCROLLS,
        max_lost: int = MAX_LOST_PER_RUN,
        limits: ObservationLimits | None = None,
    ) -> None:
        self._run = run
        self._origin = require_page_origin(origin, reader="the connections reader")
        self._require_newest_first = require_newest_first
        self._rng = rng if rng is not None else random.Random()  # noqa: S311 -- pacing, not crypto
        self._scroll_profile = scroll_profile
        self._sleep = sleep
        self._response_wait_s = response_wait_s
        self._landing_wait_s = landing_wait_s
        self._max_idle = max_idle_scrolls
        self._max_lost = max_lost
        self._limits = limits
        self._observation: Observation | None = None
        self._cards: list[ConnectionSummary] = []
        self._next_start: int | None = None
        self._total = 0
        self._landed = False
        self._ended = False
        self._stopped: SourcePage | None = None
        self._failed: ObservationFailed | None = None
        #: The lost answer that stopped this source (#197, #200); sticky like ``_failed``.
        self._stop_lost: LostAnswer | None = None
        #: The pagination answer for the expected start that arrived unreadable and is
        #: not settled yet (#197): read again, moved past, or given up on.
        self._lost: _Pending | None = None
        #: Answers lost and read on past in this run (#200), and those not yet
        #: handed to the job with a slice.
        self._lost_count = 0
        self._unreported: list[LostAnswer] = []
        #: The places the lost answers covered: the gap each left in the list's indexes.
        self._lost_places = 0
        #: Places the answers read whole held no card for (#515): a report only.
        self._hidden = 0
        #: Whether any answer read in this run asked for a next page (#208 review, M1).
        #: The completion slack at a full last answer is allowed only once one has:
        #: a parser that stopped reading the next request (a renamed ``$type``) would
        #: otherwise make every full answer look like a possible end.
        self._asked_next = False
        #: The body size of the last pagination answer that read, for the diagnostics.
        self._last_bytes: int | None = None
        #: Answers read from the body tap's streamed copy instead (#200).
        self._rescued = 0
        #: Of those, the ones :func:`~netkeep.linkedin.flight.is_whole` did not find
        #: whole (#525 review): logged and counted, not yet enforced.
        self._rescued_not_whole = 0
        self._url = ""

    @property
    def endpoint(self) -> str:
        return PAGE_CONNECTIONS_ENDPOINT

    @property
    def page_url(self) -> str:
        """The connections page this source opens."""
        return f"{self._origin}{CONNECTIONS_PAGE_PATH}"

    async def fetch_page(self, *, start: int, count: int) -> SourcePage:
        ended = self._failed is not None or self._stop_lost is not None
        if ended and len(self._cards) < start + count:
            raise self._failure()
        if self._stopped is None and not ended:
            try:
                blocked = await self._read_until(start + count)
            except RouteChanged:
                blocked = SourcePage(Outcome.ROUTE_CHANGED, self._url)
            except (ObservationFailed, AnswerLost) as exc:
                # The mechanism failed, or an answer was lost (#197), not LinkedIn
                # answering: there is no outcome to report, so the run ends by this
                # exception (never complete, ageing nobody). A unit already read whole
                # before it is still handed over, the same as before a RouteChanged;
                # the next call raises it.
                if isinstance(exc, AnswerLost):
                    self._stop_lost = exc.lost
                    if self._lost is not None and self._lost.start == exc.lost.start:
                        self._lost = None  # the stop is that very answer
                else:
                    self._failed = exc
                self._settle_pending()
                blocked = None
                if len(self._cards) < start + count:
                    raise self._failure() from None
            if blocked is not None:
                self._stopped = blocked
                self._settle_pending()
        if self._stopped is not None and len(self._cards) < start + count:
            # The first non-Ok answer is sticky, and only a unit this source had already
            # read whole, before it, is still handed over: a stop never becomes a short
            # slice the job could mistake for the end of the list.
            unreported = self._report()
            return (
                dataclasses.replace(self._stopped, lost=unreported) if unreported else self._stopped
            )
        selected = tuple(self._cards[start : start + count])
        return SourcePage(
            outcome=Outcome.OK,
            final_url=self._url,
            page=ConnectionsPageResult(
                connections=selected, start=start, count=count, total=self._total
            ),
            lost=self._report(),
            # #515: the list's end is proven and this slice holds its last card. A
            # short Ok slice already implies ``_ended``: _read_until stops short of
            # ``start + count`` cards only once the end is proven (any other stop is
            # sticky and never handed over as a short slice), so this flag adds no
            # new way to end -- it only spares the job one more, empty unit.
            ended=self._ended and len(self._cards) <= start + count,
            hidden=self._hidden,
            rescued_not_whole=self._rescued_not_whole,
        )

    def _failure(self) -> ObservationFailed | AnswerLost:
        """The sticky exception this source ended by, carrying any loss not yet reported."""
        if self._failed is not None:
            unreported = self._report()
            if unreported:
                # An exception has nowhere to carry them, and a failed run is never
                # complete anyway: say which answers were lost before it (#201 L2).
                log.warning(
                    "connections: the run failed with %d of the page's answers lost (starts %s)",
                    len(unreported),
                    ", ".join(str(lost.start) for lost in unreported),
                )
            return self._failed
        assert self._stop_lost is not None
        return AnswerLost(self._stop_lost, earlier=self._report())

    def _settle_pending(self) -> None:
        """A stop while an answer is still lost: it will never be read now (#201 L2)."""
        pending, self._lost = self._lost, None
        if pending is not None:
            self._unreported.append(
                LostAnswer(
                    start=pending.start,
                    cause=pending.cause,
                    ending=STOPPED_PENDING,
                    copy=pending.copy,
                )
            )

    def _report(self) -> tuple[LostAnswer, ...]:
        """The answers lost and read on past since the last report, once each (#200)."""
        unreported, self._unreported = tuple(self._unreported), []
        return unreported

    async def _read_until(self, needed: int) -> SourcePage | None:
        """Land, then scroll until ``needed`` cards are read or the list's end is proven.

        Returns the first answer that is not ``Ok``, or ``None``. Cards read from whole
        answers before it are kept: an answer that arrived after the unit was already
        full does not take the unit back, it stops the next one.
        """
        if not self._landed:
            blocked = await self._land()
            if blocked is not None:
                return blocked
        blocked = await self._absorb(wait_s=0.0)
        if blocked is not None:
            return blocked
        idle = 0
        while len(self._cards) < needed and not self._ended:
            if idle >= self._max_idle and self._lost is not None:
                if self._rescue(next_start=None):
                    continue
                raise self._give_up(f"it was not read again within {idle} scrolls")
            if idle >= self._max_idle:
                log.warning(
                    "connections: %d scrolls brought no new answer before the list"
                    " proved its end; stopping without trusting it",
                    idle,
                )
                return SourcePage(Outcome.ROUTE_CHANGED, self._url)
            before = (len(self._cards), self._ended)
            lost_before = None if self._lost is None else self._lost.start
            blocked = await self._scroll()
            if blocked is None:
                blocked = await self._absorb(wait_s=self._response_wait_s)
            if blocked is not None:
                return blocked
            if self._lost is not None and self._lost.start != lost_before:
                # An answer was just lost (#197): the page gets MAX_IDLE_SCROLLS
                # scrolls, from here, to ask for it again or move past it. The same
                # start lost again on a re-ask is not a new loss, and does not
                # restart the count (#198 review, L4).
                idle = 0
            elif (len(self._cards), self._ended) == before:
                idle += 1
        return None

    # --- landing: the navigation and the first screen -----------------------------

    async def _land(self) -> SourcePage | None:
        self._landed = True
        match = ResponseMatch(
            origin=self._origin,
            rules=(
                ResponseRule("GET", CONNECTIONS_PAGE_PATH),
                ResponseRule("POST", CONNECTIONS_SCREEN_PATH),
                ResponseRule("POST", PAGINATION_PATH),
            ),
        )
        self._observation = await self._run.observe(match, limits=self._limits, tap=True)
        page = await self._run.goto(self.page_url)
        self._require_observed(page)
        blocked = self._where(page.url)
        if blocked is not None:
            return blocked
        observation = self._observation
        for _answer in range(MAX_LANDING_ANSWERS):
            response = await observation.next(self._landing_wait_s)
            if response is None:
                log.warning("connections: the page loaded, but no first screen arrived")
                return SourcePage(Outcome.ROUTE_CHANGED, self._url)
            if url_path(response.url) == url_path(PAGINATION_PATH):
                log.warning("connections: a page of the list arrived before its first screen")
                return SourcePage(Outcome.ROUTE_CHANGED, self._url)
            outcome = _outcome(response)
            if outcome is not Outcome.OK:
                return SourcePage(outcome, response.location or response.url)
            if response.body is None:
                raise _not_kept(response)
            payload: bytes | None
            if response.method == "GET":
                payload = rehydration_payload(response.text() or "")
                if payload is None:
                    # No first screen in the document: a page that fetches its screen
                    # next, or a wall. Where the tab is decides which, by its url alone
                    # (#188 review, M1): the HTML itself is never searched for wall
                    # paths, because every logged-in page links to /uas/logout, and
                    # reading that as a login wall would flag the session and end the
                    # run with nothing. A wall served in place at the connections url
                    # stays unrecognized: no first screen ever comes, and the run stops
                    # as RouteChanged, ageing nobody and flagging nothing.
                    blocked = self._where(page.url)
                    if blocked is not None:
                        return blocked
                    continue
            else:
                payload = response.body
            assert payload is not None  # a response without a body was refused above
            chunk = parse_connections_chunk(payload, endpoint=self.endpoint, expected_start=0)
            if not chunk.cards and chunk.total != 0:
                # A first screen with no cards is an empty list only when it says the
                # list is empty; otherwise the cards come some other way now.
                log.warning("connections: the first screen has no cards and no zero total")
                return SourcePage(Outcome.ROUTE_CHANGED, self._url)
            self._total = chunk.total or 0
            self._take(chunk)
            if self._total == 0:
                log.warning(
                    "connections: the first screen states no total; this run cannot complete"
                )
            return None
        log.warning("connections: %d answers arrived, none the first screen", MAX_LANDING_ANSWERS)
        return SourcePage(Outcome.ROUTE_CHANGED, self._url)

    # --- the scroll, and the answers it brought ------------------------------------

    async def _scroll(self) -> SourcePage | None:
        plan = scroll_like_a_person(
            self._rng,
            steps_range=self._scroll_profile.steps_range,
            delta_range_px=self._scroll_profile.delta_range_px,
            pause_range_s=self._scroll_profile.pause_range_s,
            back_up_p=self._scroll_profile.back_up_p,
            back_up_delta_range_px=self._scroll_profile.back_up_delta_range_px,
            dwell_median_s=self._scroll_profile.dwell_median_s,
            dwell_sigma=self._scroll_profile.dwell_sigma,
        )
        # `rng` shares this source's own stream with the scroll plan above, on
        # purpose: the pointer-rest walk (#192) is spent from the same seed a
        # caller gives this source, so the whole read -- not just the wheel
        # deltas and dwells -- reproduces identically for a fixed seed (the same
        # promise :mod:`netkeep.linkedin.pacing` makes for everything else it
        # draws). Sharing costs nothing correctness-wise: it only decides which
        # otherwise-equally-valid `ScrollPlan` each later scroll draws, since the
        # rest walk's few extra draws land before them once, on the first call.
        # `tests/test_page_connections.py`'s incremental-stop test pins the bound
        # this shifted rather than the exact one a particular seed happened to
        # land on before, which is what a test asserting the real invariant
        # (stopped well short of the list's actual end) should have done from
        # the start.
        if self._sleep is None:
            outcome = await self._run.scroll(plan, rng=self._rng)
        else:
            outcome = await self._run.scroll(plan, sleep=self._sleep, rng=self._rng)
        self._require_observed(outcome.page)
        return self._where(outcome.page.url)

    async def _absorb(self, *, wait_s: float) -> SourcePage | None:
        """Read every answer that has arrived, waiting up to ``wait_s`` for the first."""
        observation = self._observation
        assert observation is not None
        response = await observation.next(wait_s)
        while response is not None:
            try:
                blocked = self._take_answer(response)
            except RouteChanged:
                blocked = SourcePage(Outcome.ROUTE_CHANGED, self._url)
            if blocked is not None:
                return blocked
            response = await observation.next(0.0)
        return None

    def _take_answer(self, response: ObservedResponse) -> SourcePage | None:
        if url_path(response.url) != url_path(PAGINATION_PATH):
            # A second first screen (the client refetching it) says nothing new.
            return None
        request = parse_pagination_request(response.request_body)
        if request is None:
            return None  # another pager on the same path: not the connections list
        # A status or a redirect is classified whatever start it answered: a
        # checkpoint on a duplicate is still a checkpoint. Only a 200 whose body
        # could not be kept waits for the start comparison below (#197).
        outcome = _outcome(response)
        if outcome is not Outcome.OK:
            return SourcePage(outcome, response.location or response.url)
        if self._require_newest_first and request.sort != SORT_NEWEST_FIRST:
            log.warning("connections: the list is not sorted by recently added; stopping")
            return SourcePage(Outcome.ROUTE_CHANGED, self._url)
        if self._ended:
            return None
        expected = self._next_start
        assert expected is not None  # set by every chunk taken before this one
        if request.start_index < expected:
            # Stale or retried, with or without a body: already read, never fatal.
            log.info("connections: the page asked for a page it already had; skipped")
            return None
        if request.start_index > expected:
            if self._lost is None:
                log.warning(
                    "connections: a page of the list went unseen (asked from %d, expected %d)",
                    request.start_index,
                    expected,
                )
                return SourcePage(Outcome.ROUTE_CHANGED, self._url)
            # The page moved on past the answer it lost (#197): it read its own copy,
            # only the browser's is gone. The streamed copy stands in for it if it
            # agrees with what the page asked for next (#200); otherwise the loss is
            # recorded and the run reads on, unless that is one loss too many.
            if not self._rescue(next_start=request.start_index):
                self._move_past(request.start_index, response)
        if response.body is None:
            if response.failure != FAILURE_UNREADABLE:
                raise _not_kept(response)
            # #197: the browser received the answer but could not hand its body
            # over. Not LinkedIn answering: the scroll goes on, and what the page
            # does next settles it (asks again, moves past, or goes quiet).
            cause = response.cause or "unknown"
            if self._lost is not None and self._lost.start == request.start_index:
                # #735 review: the retry's own diagnostics, so the log says whether the
                # tap had a copy of it. Logged only; the rescue still uses the first.
                log.info(
                    "connections: the page asked again for start %d, and it could not be"
                    " read again (%s); %s",
                    request.start_index,
                    cause,
                    "no diagnostics"
                    if response.diagnostics is None
                    else response.diagnostics.describe(),
                )
                return None
            self._lost = _Pending(
                request.start_index,
                cause,
                response.diagnostics,
                response.streamed,
                _copy_note(response.streamed, response.diagnostics),
            )
            log.info(
                "connections: the page's answer for start %d could not be read (%s); %s;"
                " previous_answer_bytes=%s",
                request.start_index,
                cause,
                "no diagnostics"
                if response.diagnostics is None
                else response.diagnostics.describe(),
                "none" if self._last_bytes is None else self._last_bytes,
            )
            return None
        if self._lost is not None:
            log.info(
                "connections: the page asked again for start %d, and it read",
                request.start_index,
            )
            self._lost = None
        self._last_bytes = len(response.body)
        self._take(
            parse_connections_chunk(
                response.body, endpoint=self.endpoint, expected_start=request.start_index
            )
        )
        return None

    def _take(self, chunk: ConnectionsChunk) -> None:
        self._cards.extend(chunk.cards)
        # The slack is judged against the answers *before* this one: this answer's own
        # missing next request is what is being weighed as the end.
        slack = completion_slack(self._total) if self._asked_next else 0
        if chunk.next_start is not None:
            self._asked_next = True
            # #515: the places this answer covers that it holds no card for. Only an
            # answer that asks for a next page says how many places it covers.
            self._hidden += max(0, chunk.next_start - chunk.start - len(chunk.cards))
        if chunk.ends_list:
            self._ended = True
        elif (
            chunk.next_start is None
            and self._total > 0
            and self._distinct() + self._lost_places >= self._total - slack
        ):
            # A full answer that asks for no next page is the end of a list whose length
            # is a multiple of ten -- but only once the run has seen as many distinct
            # people as the first screen's total, counting the places of the answers it
            # lost (#200), less the completion slack a full sync is allowed anyway
            # (#204): the total can count members the list never shows, so a visible
            # list of 620 under a total of 624 ends here (#208). The slack applies only
            # once an earlier answer in this run asked for a next page (#208 review,
            # M1): if the parser never sees a next request (LinkedIn renamed its
            # ``$type``), every full answer would look like the end, and the slack would
            # end the run early, as complete, missing the same oldest tail every week.
            # Short of that it proves nothing, and the page stopping there still ends
            # the run as RouteChanged.
            self._ended = True
        self._next_start = (
            chunk.next_start if chunk.next_start is not None else chunk.start + len(chunk.cards)
        )
        log.info(
            "connections: read %d at %d%s",
            len(chunk.cards),
            chunk.start,
            ", the end of the list" if self._ended else "",
        )

    def _rescue(self, *, next_start: int | None) -> bool:
        """Read the pending lost answer from the body tap's streamed copy, if it checks out.

        The copy is what Chrome streamed as the answer arrived (#200). It stands in
        for the answer only when it parses whole as the page for the lost start and
        agrees with what the page itself did next: when the page asked for
        ``next_start``, the copy must ask for that start too and hold at least one
        card and no more than the places in between. An answer can hold fewer cards
        than its places, one per hidden member (#515), so fewer is accepted; the
        parser's strictness is what keeps a copy cut short from passing for one, since
        a copy cut short does not parse. When the page went quiet (``None``), the copy
        must prove the end of the list. Anything else -- no copy, a copy cut short, a
        copy that does not parse -- leaves the answer lost.
        """
        pending = self._lost
        assert pending is not None
        if pending.streamed is None:
            # #735: say why the tap had no copy, so the next loss says which case it was.
            log.info(
                "connections: the page's answer for start %d has no streamed copy (%s);"
                " not rescued",
                pending.start,
                _miss(pending.diagnostics),
            )
            return False
        try:
            chunk = parse_connections_chunk(
                pending.streamed, endpoint=self.endpoint, expected_start=pending.start
            )
        except RouteChanged:
            log.info(
                "connections: the streamed copy of the answer for start %d did not parse; not used",
                pending.start,
            )
            return self._refuse(pending, COPY_UNPARSED)
        if next_start is None:
            # The page went quiet: the copy must prove the end of the list on its own
            # -- an answer that ends it, or a full last answer that asks for nothing --
            # and the run must then account for every place the first screen's total
            # names (#202 review). This rests on the parser's strictness: a copy cut
            # short does not parse, so it cannot pass for a short last answer. Hidden
            # places count (#554): each was proven by an answer read whole that asked
            # for a start further on than its cards, so the count stays exact, with no
            # slack (#208 review, L1). The copy itself asks for nothing and adds none.
            # Hidden places are exact only while the list holds still: a member added
            # mid-run can shift a hidden member into the next answer and count them
            # twice, just as it can for a last answer read normally.
            places = (
                len({card.urn for card in self._cards} | {card.urn for card in chunk.cards})
                + self._lost_places
                + self._hidden
            )
            if not (chunk.ends_list or chunk.next_start is None):
                log.info(
                    "connections: the streamed copy of the last answer for start %d asks"
                    " for a next page, so it does not prove the end; not used",
                    pending.start,
                )
                return self._refuse(pending, COPY_ASKS_NEXT)
            if not 0 < self._total <= places:
                log.info(
                    "connections: the streamed copy of the last answer for start %d accounts"
                    " for %d of the %d places the total names (%d hidden, %d lost); not used",
                    pending.start,
                    places,
                    self._total,
                    self._hidden,
                    self._lost_places,
                )
                return self._refuse(pending, COPY_SHORT if self._total > 0 else COPY_NO_TOTAL)
        elif not (
            chunk.next_start == next_start and 0 < len(chunk.cards) <= next_start - pending.start
        ):
            log.info(
                "connections: the streamed copy of the answer for start %d does not agree"
                " with what the page asked for next; not used",
                pending.start,
            )
            return self._refuse(pending, COPY_DISAGREES)
        self._lost = None
        self._rescued += 1
        # #525 review: the checks above rest on the captured layout (cards inline in
        # row 0). The flight structure check enrichment enforces (page_profiles) is
        # only reported here until live runs show a real rescued copy passes it.
        # A check that is only reported must never abort a rescue: any exception
        # from it reads as "not whole", logged by its type name only, never its value.
        try:
            whole = is_whole(
                parse_flight(pending.streamed, endpoint=self.endpoint), endpoint=self.endpoint
            )
            check = "yes" if whole else "no"
        except Exception as exc:
            whole = False
            check = f"no ({type(exc).__name__})"
        # #530: which rule the copy broke, as counts in fixed words, never its content.
        why = ""
        if not whole:
            self._rescued_not_whole += 1
            why = f"; why: {why_not_whole(pending.streamed, endpoint=self.endpoint)}"
        log.info(
            "connections: the page's answer for start %d was read from the copy streamed"
            " as it arrived (%d bytes); nothing was lost; whole by flight check: %s%s",
            pending.start,
            len(pending.streamed),
            check,
            why,
        )
        self._last_bytes = len(pending.streamed)
        self._take(chunk)
        return True

    def _refuse(self, pending: _Pending, copy: str) -> bool:
        """Keep the answer lost, noting which rule its copy broke (#735). Always ``False``."""
        self._lost = dataclasses.replace(pending, copy=copy)
        return False

    def _move_past(self, next_start: int, response: ObservedResponse) -> None:
        """The page asked past the lost answer: record the loss and read on (#200).

        Raises :class:`AnswerLost` instead when the page jumped further than one answer
        past the lost start (a gap on top of the loss), or when this loss is one more
        than :data:`MAX_LOST_PER_RUN`.
        """
        pending = self._lost
        assert pending is not None
        if next_start > pending.start + FULL_PAGE:
            raise self._give_up("the page moved more than one answer past it")
        self._lost = None
        self._lost_count += 1
        self._lost_places += next_start - pending.start
        self._next_start = next_start
        lost = LostAnswer(
            start=pending.start, cause=pending.cause, ending=MOVED_PAST, copy=pending.copy
        )
        next_bytes = "unreadable" if response.body is None else str(len(response.body))
        if self._lost_count > self._max_lost:
            raise AnswerLost(
                dataclasses.replace(
                    lost,
                    ending=(
                        f"{MOVED_PAST}, and that is {self._lost_count} answers lost in this"
                        f" run, more than the {self._max_lost} a run may lose"
                    ),
                )
            )
        self._unreported.append(lost)
        log.info(
            "connections: the page moved past its answer for start %d (next_answer_bytes=%s);"
            " reading on, %d lost in this run",
            pending.start,
            next_bytes,
            self._lost_count,
        )

    def _give_up(self, ending: str) -> AnswerLost:
        """The pending lost answer as the exception that ends the run incomplete (#197)."""
        pending = self._lost
        assert pending is not None
        return AnswerLost(
            LostAnswer(start=pending.start, cause=pending.cause, ending=ending, copy=pending.copy)
        )

    def _distinct(self) -> int:
        return len({card.urn for card in self._cards})

    # --- where the tab is -----------------------------------------------------------

    def _require_observed(self, page: PageLike) -> None:
        """The tab a navigation or scroll used must be the one being listened to."""
        observation = self._observation
        assert observation is not None
        if page is not cast(object, observation.page):
            raise BrowserUnavailable(
                "the run's tab was replaced mid-read, so its answers are no longer"
                " observed; aborting the run rather than read part of the list"
            )

    def _where(self, url: str) -> SourcePage | None:
        """Classify where the tab is (spec 9.7): a wall, somewhere else, or the list."""
        self._url = url
        outcome = classify(200, url, "")
        if outcome in (Outcome.CHECKPOINT, Outcome.LOGGED_OUT):
            return SourcePage(outcome, url)
        # #796: the origin read strictly; the path only once it reads one way.
        on_page = (
            "://" in url
            and on_origin(url, base=self._origin)
            and url_path(urlsplit(url).path) == url_path(CONNECTIONS_PAGE_PATH)
        )
        if not on_page:
            log.warning("connections: the tab left the connections page; stopping")
            return SourcePage(Outcome.ROUTE_CHANGED, url)
        return None


def _outcome(response: ObservedResponse) -> Outcome:
    """What one observed answer means (spec 9.7), before anything parses it.

    A redirect is classified by where it pointed. A ``200`` is ``Ok``, whether or not
    its body could be kept: which answer it was decides what a missing body means
    (a duplicate's is skipped, a lost one's is waited out, #197), so the caller
    checks the body after that, never here. Any other status is
    classified from its status and url, never its body (#188 review, M1): an error
    page is HTML with the site's own navigation in it, and its Sign-out link to
    ``/uas/logout`` is not a login wall.
    """
    if response.failure == FAILURE_REDIRECT:
        target = response.location or ""
        outcome = classify(response.status, target, "")
        return (
            outcome
            if outcome in (Outcome.CHECKPOINT, Outcome.LOGGED_OUT)
            else (Outcome.ROUTE_CHANGED)
        )
    if response.status == 200:
        return Outcome.OK
    return classify(response.status, response.url, "")


def _not_kept(response: ObservedResponse) -> ObservationFailed:
    """A ``200`` answer the run needed whose body the observation could not keep."""
    return ObservationFailed(f"an answer of the page could not be kept: {response.failure}")
