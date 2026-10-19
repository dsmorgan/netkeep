"""The browser worker: a recorded run in, browser work done, the run's ending recorded (P2-10).

This is the one place a LinkedIn run meets the browser. For a ``sync_runs``
row (:mod:`netkeep.services.runs`) it takes the account's activity lock,
attaches to the Chrome the user started (ADR 0002: attach only, never
launch), builds the source that reads through that tab, and calls the runner:
:func:`netkeep.services.connections_sync.sync_connections`,
:func:`netkeep.services.enrichment.enrich_contacts`, or
:func:`netkeep.services.inbox_poll.poll_inbox`. The runners own budgets,
heat, the session flag, pacing, cancel, and how the run ended; this module
owns only what needs the browser.

**Who calls it.** ``netkeep serve``'s scheduler (through
:mod:`netkeep.services.scheduled_runs`), the runs API (which submits it to
the task runner and answers ``202``, never awaiting it inside the request,
spec 9.9), and ``netkeep linkedin sync``/``enrich`` on their own loops.
``netkeep linkedin message-check`` calls :func:`run_message_check` (#473), a
prefill's steps up to its click, on its own loop, and with ``--bubble``,
:func:`run_bubble_check` (#495), a read of a bubble opened by hand. The
app and the API reach it only as a :class:`~netkeep.services.runs.RunExecutor`,
so no request handler imports the browser (``tests/test_browser_safety.py``
lists this module among the browser's few callers on purpose).

**Nothing is attached before it has to be.** Before the lock and the attach
the worker re-checks, from the database, the things that would make the run
refuse anyway: a scheduled run on a disarmed account (the third check, after
the scheduler's arm gate and ``runs.create_run``), a scheduled connections run
whose route-changed breaker or answer-lost limit is tripped (the second
independent check for those gates, matching the arming design; #191 review F6,
#199), the session flag, and heat
over its skip threshold. A refused run is recorded ``failed`` and the browser
is never touched. And the connections page a sync reads is loaded by its
source's first ``fetch_page``, after the runner's own checks, not before them.

**When the browser is not there.** ``BrowserUnavailable`` (Chrome is not
running, or went away twice in one run) and ``BrowserBusy`` (another run, or
another netkeep process, holds the account) end the run ``failed`` and
answer :attr:`~netkeep.services.runs.RunOutcome.RETRY_LATER`; the scheduler
parks a retry 20 to 50 minutes out (spec 9.9). Nothing here retries, and
nothing answers a missing browser by starting one.

**Events.** ``run.started``, ``run.progress`` (counts only), and
``run.finished`` go out on the event bus for the SSE stream, each for the run's
own user.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from netkeep.chrome_profile import profile_lock_pid
from netkeep.chrome_spawn import ChromeLauncher, configured_port
from netkeep.config import LinkedInSettings, Settings, load_settings
from netkeep.crm.inbox_apply import owner_hold
from netkeep.db import CancelledWhileFailing, off_loop, session_scope
from netkeep.linkedin import activity_lock
from netkeep.linkedin.activity_lock import account_key
from netkeep.linkedin.browser import (
    CHROME_PROFILE_DIRNAME,
    ActivityLocks,
    AttachBrowserProvider,
    BrowserBusy,
    BrowserProvider,
    BrowserRun,
    BrowserUnavailable,
    BubbleCheck,
    BubbleCheckFailed,
    CdpIdentityReader,
    PidReader,
    ProfileRefused,
    ReadFailure,
    classify_read_failure,
)
from netkeep.linkedin.connections import ConnectionsSource, SyncMode
from netkeep.linkedin.enrich import LINKEDIN_ORIGIN, ProfileSource
from netkeep.linkedin.inbox import InboxSource
from netkeep.linkedin.messaging import MessageOutcome, MessageOutcomeKind, PrefillSource
from netkeep.linkedin.page_connections import PageConnections
from netkeep.linkedin.page_inbox import PageInbox

# Re-exported for the CLI's report, so the CLI reaches the page source only through here.
from netkeep.linkedin.page_messaging import MessageCheckResult as MessageCheckResult
from netkeep.linkedin.page_messaging import PageBubbleCheck, PageMessageCheck, PagePrefill
from netkeep.linkedin.page_profiles import PageProfiles
from netkeep.linkedin.preflight import LoginState, PreflightReport, read_session
from netkeep.logging_setup import personal_exc, setup_logging
from netkeep.models import SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeep.paths import data_dir
from netkeep.services import (
    local_api_key,
    message_check,
    message_send,
    route_breaker,
    runs,
    session_check,
    ui_settings,
)
from netkeep.services.budgets import (
    ActionClass,
    li_message_risk_warning,
    profile_visit_risk_warning,
)
from netkeep.services.chrome_launch import (
    LaunchResult,
    LaunchStatus,
    LaunchUnderWay,
    PortProbe,
    submit_launch,
)
from netkeep.services.connections_sync import sync_connections
from netkeep.services.enrichment import enrich_contacts
from netkeep.services.events import Event, EventBus
from netkeep.services.inbox_poll import poll_inbox
from netkeep.services.linkedin_accounts import local_account_id, scheduled_runs_armed
from netkeep.services.posture import SessionProbe
from netkeep.services.scheduled_runs import ServeExtractor
from netkeep.services.users import local_owner
from netkeep.web.app import create_app

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]

#: How often, at most, a Message check asks the database whether it was cancelled.
CANCEL_POLL_S: Final = 1.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


def connections_source(
    run: BrowserRun, *, mode: SyncMode, sleep: Sleep = asyncio.sleep
) -> ConnectionsSource:
    """The source a live connections sync reads through (spec 9.3, ADR 0006).

    :class:`~netkeep.linkedin.page_connections.PageConnections`: the connections
    page, loaded and scrolled like a person, read from the answers the page itself
    fetches. It loads the page inside its first ``fetch_page``, which the job calls
    only after the runner's refusals and the gate's budget, so a refused run never
    loads a page. An incremental sync requires the list sorted newest first, since it
    stops at the first page of connections it knows.

    There is no second source behind it (#187). The in-page Voyager fetch has no
    connections endpoint to call any more (the #149 capture), and P2-08's DOM scroll
    reads selectors nobody has seen on the live page, which found nothing on the first
    supervised run; a run whose page answers are unreadable stops as ``RouteChanged``
    and ages nobody. One of these per run. ``sleep`` waits out the scroll's pauses and
    dwell.
    """
    return PageConnections(run, require_newest_first=mode is SyncMode.INCREMENTAL, sleep=sleep)


def profile_source(run: BrowserRun, *, sleep: Sleep = asyncio.sleep) -> PageProfiles:
    """The source a live enrichment reads through (spec 9.4, ADR 0006, #190).

    :class:`~netkeep.linkedin.page_profiles.PageProfiles`: each profile opened and
    scrolled like a person, read from the answers the page itself loads, with ADR
    0006's one click on **Contact info** per visit. The in-page Voyager fetch it
    replaced is gone. ``sleep`` waits out the scroll's pauses and the pause before the
    click, the same sleeper the run's other waits use.
    """
    return PageProfiles(run, sleep=sleep)


def inbox_source(run: BrowserRun, *, sleep: Sleep = asyncio.sleep) -> InboxSource:
    """The source a live inbox poll reads through (P4-01, #380, ADR 0006).

    :class:`~netkeep.linkedin.page_inbox.PageInbox`: ``/messaging/`` opened and its list
    scrolled like a person, threads opened by navigation only, and everything read from
    the answers the page itself loads. It loads the page inside its first ``read``,
    which the runner calls only after its refusals and the budget, so a refused run
    never loads a page. ``sleep`` waits out the scroll's pauses and the pause before
    each thread.
    """
    return PageInbox(run, sleep=sleep)


def prefill_source(
    run: BrowserRun, *, sleep: Sleep = asyncio.sleep, clock: Clock = _utcnow
) -> PrefillSource:
    """The source a LinkedIn prefill types through (P4-03, ADR 0007):
    :class:`~netkeep.linkedin.page_messaging.PagePrefill` on the run's tab."""
    return PagePrefill(run, sleep=sleep, clock=clock)


class PrefillSourceFactory(Protocol):
    """Builds a prefill's :class:`PrefillSource`: :func:`prefill_source`'s shape."""

    def __call__(self, run: BrowserRun, *, sleep: Sleep, clock: Clock) -> PrefillSource: ...


class InboxSourceFactory(Protocol):
    """Builds an inbox poll's :class:`InboxSource`: :func:`inbox_source`'s shape."""

    def __call__(self, run: BrowserRun, *, sleep: Sleep) -> InboxSource: ...


class ProfileSourceFactory(Protocol):
    """Builds an enrichment run's :class:`ProfileSource`: :func:`profile_source`'s shape."""

    def __call__(self, run: BrowserRun, *, sleep: Sleep) -> ProfileSource: ...


_MODE: Final[dict[SyncRunKind, SyncMode]] = {
    SyncRunKind.CONNECTIONS_FULL: SyncMode.FULL,
    SyncRunKind.CONNECTIONS_INCREMENTAL: SyncMode.INCREMENTAL,
}

#: The kinds the route-changed breaker governs (#189 item 1, #191 review F6): not
#: ``enrich``, which has its own separate unreadable-profile cap. Matches
#: ``services.scheduler``'s own ``_ROUTE_BREAKER_KINDS``.
_ROUTE_BREAKER_KINDS: Final = frozenset(
    {SyncRunKind.CONNECTIONS_FULL, SyncRunKind.CONNECTIONS_INCREMENTAL}
)


@dataclass(frozen=True, slots=True)
class _RunFacts:
    kind: SyncRunKind
    status: SyncRunStatus
    trigger: SyncRunTrigger
    account_id: int


class BrowserWorker:
    """Executes recorded runs against the attached browser. A ``RunExecutor``."""

    def __init__(
        self,
        provider: BrowserProvider,
        factory: sessionmaker[Session],
        settings: LinkedInSettings,
        *,
        bus: EventBus | None = None,
        clock: Clock = _utcnow,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
        profiles: ProfileSourceFactory = profile_source,
        inbox_sources: InboxSourceFactory = inbox_source,
        campaign_settings: Settings | None = None,
        prefill_sources: PrefillSourceFactory = prefill_source,
    ) -> None:
        self.provider = provider
        self._factory = factory
        self._settings = settings
        self._bus = bus
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        # A test seam (#210): the offline suite passes PageProfiles with a short
        # landing wait, since its fake Chrome serves no profile screen and would
        # otherwise sit out the live 20 s wait on every visit. Always the default live.
        self._profiles = profiles
        # The inbox poll's source (P4-08): a test passes a fake; live, P4-01's page
        # source once it exists, and until then a factory that refuses.
        self._inbox_sources = inbox_sources
        # The whole config, for the inbox poll's reply hook (P4-02): a prefilled message
        # seen sent schedules the next step by the campaign settings. None: the defaults.
        self._campaign_settings = campaign_settings
        # The prefill's page source (P4-03): a test passes a fake.
        self._prefill_sources = prefill_sources

    async def execute(self, run_id: int, user_id: int) -> runs.RunOutcome:
        """Run ``run_id`` to its end and record how it ended. See the module docstring.

        Every database read and write here runs off the event loop
        (:func:`netkeep.db.off_loop`, #259), each in its own session as before;
        the browser work stays on the loop.
        """
        facts = await off_loop(self._facts, run_id, user_id)
        if facts is None:
            return runs.RunOutcome.DONE
        # #467: the run's heartbeat stays fresh for as long as this process works on it,
        # so another data directory sharing the database never takes it for left behind.
        async with runs.heartbeat(self._factory, user_id, run_id, clock=self._clock):
            return await self._execute(run_id, user_id, facts)

    async def _execute(self, run_id: int, user_id: int, facts: _RunFacts) -> runs.RunOutcome:
        """:meth:`execute` for a run that is still ``running``, inside its heartbeat."""
        if facts.kind is SyncRunKind.SESSION_CHECK:
            return await self._session_check(run_id, user_id, facts)
        # The Settings page's values (#343), read once as the run starts: a change applies
        # from the next run, and this one keeps the limits it started with.
        settings = await off_loop(self._resolved, user_id)
        refusal = await off_loop(self._refusal, run_id, user_id, facts, settings)
        if refusal is not None:
            if facts.kind is SyncRunKind.MESSAGE_SEND:
                # Fixed words, never the exception's text: the UI maps each phrase.
                await self._prefill_not_typed(
                    run_id,
                    user_id,
                    "the LinkedIn session is flagged"
                    if refusal[0] == "session_flagged"
                    else "heat is too high"
                    if refusal[0] == "heat_skip"
                    else "the run was refused",
                    settings=settings,
                )
            await off_loop(
                self._finish, run_id, user_id, SyncRunStatus.FAILED, refusal[0], refusal[1]
            )
            self._publish("run.finished", run_id, user_id, {"status": "failed"})
            return runs.RunOutcome.DONE
        prepared: message_send.PreparedPrefill | None = None
        if facts.kind is SyncRunKind.MESSAGE_SEND:
            # ADR 0007: the claim's lapse and the whole typing plan, before the lock, the
            # attach, any budget, or any navigation. A refusal is recorded there.
            ready = await off_loop(
                message_send.prepare,
                self._factory,
                user_id,
                run_id,
                settings=settings,
                clock=self._clock,
            )
            if isinstance(ready, message_send.PrefillReport):
                status = await off_loop(self._status, run_id, user_id)
                self._publish("run.finished", run_id, user_id, {"status": status})
                return runs.RunOutcome.DONE
            prepared = ready
        self._publish("run.started", run_id, user_id, {"kind": facts.kind.value})
        outcome = runs.RunOutcome.DONE
        # Whether run_prefill was reached: before it, nothing was opened (#458 review).
        runner_started = False
        # #195: the run, for its note when its tab had to open in front.
        opened: BrowserRun | None = None
        try:
            # wait=False, the default: a prefill never waits for the lock (ADR 0007).
            async with self.provider.run(activity_lock.account_key(facts.account_id)) as browser:
                opened = browser
                if prepared is not None:
                    runner_started = True
                    await message_send.run_prefill(
                        self._factory,
                        user_id,
                        prepared,
                        self._prefill_sources(browser, sleep=self._sleep, clock=self._clock),
                        settings=settings,
                        clock=self._clock,
                    )
                else:
                    await self._run_job(run_id, user_id, facts, browser, settings)
        except (BrowserBusy, BrowserUnavailable) as exc:
            reason = runs.browser_stop_reason(exc)
            log.warning("run %d could not use the browser: %s", run_id, personal_exc(exc))
            if prepared is not None:
                # The runner never ran, so no key was sent: the claim goes back.
                await self._prefill_not_typed(
                    run_id,
                    user_id,
                    "the browser was busy"
                    if isinstance(exc, BrowserBusy)
                    else "the browser was unavailable",
                    settings=settings,
                    opened=None if runner_started else False,
                )
            await off_loop(
                self._finish, run_id, user_id, SyncRunStatus.FAILED, reason, runs.describe(exc)
            )
            outcome = runs.RunOutcome.RETRY_LATER
        except asyncio.CancelledError as exc:
            # The process is shutting down. A cancel that landed inside the runner
            # was recorded there; one that landed while attaching was not. A cancel
            # that carries a failed database write is a failure (#266).
            # Quietly: a failed write here must not replace the cancel (#294), or the
            # task would end as a plain failure, not a cancelled one.
            failed = exc.error if isinstance(exc, CancelledWhileFailing) else None
            await off_loop(
                self._finish_quietly,
                run_id,
                user_id,
                SyncRunStatus.ABORTED if failed is None else SyncRunStatus.FAILED,
                "interrupted" if failed is None else "error",
                runs.INTERRUPTED if failed is None else runs.describe(failed),
            )
            raise
        except (runs.HeatSkipped, runs.SessionFlagged) as exc:
            log.info("run %d refused: %s", run_id, personal_exc(exc))  # recorded by the runner
        except Exception as exc:
            # The runner usually recorded it already; this records only what it did
            # not (a failure before the runner started), and stops it from taking the
            # caller down.
            log.exception("run %d failed", run_id)
            await off_loop(
                self._finish, run_id, user_id, SyncRunStatus.FAILED, "error", runs.describe(exc)
            )
        if opened is not None and opened.opened_in_front:
            await off_loop(self._note_quietly, run_id, user_id, runs.OPENED_IN_FRONT_NOTE)
        status = await off_loop(self._status, run_id, user_id)
        self._publish("run.finished", run_id, user_id, {"status": status})
        return outcome

    async def _session_check(self, run_id: int, user_id: int, facts: _RunFacts) -> runs.RunOutcome:
        """Check connection (#652): attach as every run does, read the session the way
        ``netkeep preflight`` does (:func:`~netkeep.linkedin.preflight.read_session`),
        detach, and record it (:mod:`netkeep.services.session_check`).

        No navigation, so no budget, and no flag, heat, or active-hours refusal: nothing
        reaches linkedin.com. A busy, unreachable, or refused Chrome ends the run
        ``failed`` with the usual stop reason, and is never retried: the outcome is
        ``DONE`` whatever happened, since a person asked for this one check.
        """
        self._publish("run.started", run_id, user_id, {"kind": facts.kind.value})
        try:
            async with self.provider.run(activity_lock.account_key(facts.account_id)) as browser:
                report = await read_session(browser, cdp_url=self.provider.cdp_url)
            result = session_check.CheckResult(
                probe=session_probe(report), warnings=report.warnings
            )
            await off_loop(
                session_check.finish, self._factory, user_id, run_id, result, now=self._clock()
            )
        except (BrowserBusy, BrowserUnavailable) as exc:
            log.warning("check run %d could not use the browser: %s", run_id, personal_exc(exc))
            await off_loop(
                self._finish,
                run_id,
                user_id,
                SyncRunStatus.FAILED,
                runs.browser_stop_reason(exc),
                runs.describe(exc),
            )
        except asyncio.CancelledError:
            await off_loop(
                self._finish_quietly,
                run_id,
                user_id,
                SyncRunStatus.ABORTED,
                "interrupted",
                runs.INTERRUPTED,
            )
            raise
        except Exception as exc:
            log.exception("check run %d failed", run_id)
            await off_loop(
                self._finish, run_id, user_id, SyncRunStatus.FAILED, "error", runs.describe(exc)
            )
        status = await off_loop(self._status, run_id, user_id)
        self._publish("run.finished", run_id, user_id, {"status": status})
        return runs.RunOutcome.DONE

    def _resolved(self, user_id: int) -> Settings:
        """The whole config a run uses: this worker's, with ``user_id``'s Settings-page
        values laid over it (#343). The defaults' when the user is gone."""
        base = self._campaign_settings if self._campaign_settings is not None else Settings()
        base = dataclasses.replace(base, linkedin=self._settings)
        with session_scope(self._factory) as session:
            user = session.get(User, user_id)
            return base if user is None else ui_settings.resolve(session, user, base)

    async def _prefill_not_typed(
        self,
        run_id: int,
        user_id: int,
        reason: str,
        *,
        settings: Settings,
        opened: bool | None = False,
    ) -> None:
        """Give a prefill's claim back, ``not_typed``, when its run ended before the runner
        started, so no budget was spent (#445). The click keys stay out, as for any path
        that ran no source. A run the runner recorded is not recorded again."""
        await off_loop(
            message_send.record_quietly,
            self._factory,
            user_id,
            run_id,
            MessageOutcome(MessageOutcomeKind.NOT_TYPED, reason, None, 0),
            settings=settings,
            now=self._clock(),
            budget_spent=False,
            # Before the runner, nothing was opened: an auto-send's step stays due (#458).
            opened=opened,
        )

    async def _run_job(
        self, run_id: int, user_id: int, facts: _RunFacts, browser: BrowserRun, settings: Settings
    ) -> None:
        async def progress(event: Any) -> None:
            self._publish("run.progress", run_id, user_id, _plain(event))

        if facts.kind is SyncRunKind.INBOX:
            await poll_inbox(
                self._factory,
                user_id,
                self._inbox_sources(browser, sleep=self._sleep),
                settings=settings.linkedin,
                run_id=run_id,
                clock=self._clock,
                campaign_settings=settings,
            )
            return
        if facts.kind is SyncRunKind.ENRICH:
            await enrich_contacts(
                self._factory,
                user_id,
                self._profiles(browser, sleep=self._sleep),
                settings=settings.linkedin,
                run_id=run_id,
                on_progress=progress,
                clock=self._clock,
                sleep=self._sleep,
                rng=self._rng,
            )
            return
        await sync_connections(
            self._factory,
            user_id,
            _MODE[facts.kind],
            connections_source(browser, mode=_MODE[facts.kind], sleep=self._sleep),
            settings=settings.linkedin,
            run_id=run_id,
            on_progress=progress,
            clock=self._clock,
            sleep=self._sleep,
            rng=self._rng,
        )

    def _facts(self, run_id: int, user_id: int) -> _RunFacts | None:
        with session_scope(self._factory) as session:
            user = session.get(User, user_id)
            if user is None:
                log.error("run %d: no user %d", run_id, user_id)
                return None
            try:
                run = runs.get_run(session, user, run_id)
            except runs.RunNotFound:
                log.error("run %d does not exist for user %d", run_id, user_id)
                return None
            facts = _RunFacts(
                kind=run.kind,
                status=run.status,
                trigger=run.trigger,
                account_id=run.linkedin_account_id,
            )
        if facts.status is not SyncRunStatus.RUNNING:
            log.warning("run %d already ended %s; not running it", run_id, facts.status.value)
            return None
        if facts.kind not in runs.RUNNABLE_KINDS:
            self._finish(
                run_id, user_id, SyncRunStatus.FAILED, "no_runner", f"no runner for {facts.kind}"
            )
            return None
        return facts

    def _refusal(
        self, run_id: int, user_id: int, facts: _RunFacts, settings: Settings
    ) -> tuple[str, str] | None:
        """``(stop_reason, error)`` when the run must not touch the browser at all."""
        with session_scope(self._factory) as session:
            user = session.get(User, user_id)
            assert user is not None  # _facts found it
            if facts.trigger is SyncRunTrigger.SCHEDULED and not scheduled_runs_armed(
                session, user, facts.account_id
            ):
                log.error("scheduled run %d reached the worker on a disarmed account", run_id)
                return "disarmed", "scheduled runs are disarmed for this account"
            if (
                facts.trigger is SyncRunTrigger.SCHEDULED
                and facts.kind in _ROUTE_BREAKER_KINDS
                and route_breaker.tripped(session, user, facts.account_id)
            ):
                # #191 review F6: a second, independent check, matching the arming
                # design above -- the scheduler's own gate (services.scheduler's
                # poll_and_fire) already skips a tripped account's due fire, so
                # reaching this refusal means something bypassed that gate.
                log.error(
                    "scheduled run %d reached the worker with the route-changed"
                    " breaker tripped for account %d",
                    run_id,
                    facts.account_id,
                )
                return (
                    "route_changed_breaker",
                    "the route-changed breaker is tripped for this account",
                )
            if (
                facts.trigger is SyncRunTrigger.SCHEDULED
                and facts.kind in _ROUTE_BREAKER_KINDS
                and route_breaker.answer_lost_tripped(session, user, facts.account_id)
            ):
                # #199: the same second, independent check for the answer-lost limit.
                log.error(
                    "scheduled run %d reached the worker with the answer-lost limit"
                    " tripped for account %d",
                    run_id,
                    facts.account_id,
                )
                return (
                    "answer_lost_breaker",
                    "the answer-lost limit is tripped for this account",
                )
            if (
                facts.trigger is SyncRunTrigger.SCHEDULED
                and facts.kind is SyncRunKind.ENRICH
                and route_breaker.contact_info_tripped(session, user, facts.account_id)
            ):
                # #424: the same second, independent check for the Contact info breaker.
                log.error(
                    "scheduled run %d reached the worker with the Contact info breaker"
                    " tripped for account %d",
                    run_id,
                    facts.account_id,
                )
                return (
                    "contact_info_breaker",
                    "the Contact info breaker is tripped for this account",
                )
            if (
                facts.trigger is SyncRunTrigger.SCHEDULED
                and facts.kind is SyncRunKind.INBOX
                and route_breaker.inbox_tripped(session, user, facts.account_id)
            ):
                # #437: the same second, independent check for the inbox breaker.
                log.error(
                    "scheduled run %d reached the worker with the inbox breaker"
                    " tripped for account %d",
                    run_id,
                    facts.account_id,
                )
                return (
                    "inbox_route_changed_breaker",
                    "the inbox breaker is tripped for this account",
                )
            if (
                facts.trigger is SyncRunTrigger.SCHEDULED
                and facts.kind is SyncRunKind.INBOX
                and route_breaker.inbox_owner_tripped(session, user, facts.account_id)
            ):
                # #443: the same second, independent check for the owner breaker.
                log.error(
                    "scheduled run %d reached the worker with the inbox owner breaker"
                    " tripped for account %d",
                    run_id,
                    facts.account_id,
                )
                return (
                    "inbox_owner_breaker",
                    "the inbox owner breaker is tripped for this account",
                )
            if (
                facts.trigger is SyncRunTrigger.SCHEDULED
                and facts.kind is SyncRunKind.INBOX
                and not runs.has_completed_inbox_poll(session, user)
            ):
                # P4-01: a second, independent check; the scheduled handler already
                # skips the fire. The first inbox poll is a person's, by hand.
                return (
                    "first_inbox_poll",
                    "no inbox poll has completed yet: run `netkeep linkedin inbox` by hand first",
                )
            if (
                facts.trigger is SyncRunTrigger.SCHEDULED
                and facts.kind is SyncRunKind.INBOX
                and owner_hold(session, user)
            ):
                # #592: the same second check for a forgotten inbox owner. The poll
                # that records the owner again is a person's, by hand.
                return (
                    "inbox_owner_by_hand",
                    "the inbox owner was forgotten: run `netkeep linkedin inbox` by hand"
                    " to record it again",
                )
            if (
                facts.trigger is SyncRunTrigger.SCHEDULED
                and facts.kind is not SyncRunKind.MESSAGE_SEND
            ):
                # #343, #464: the scheduler's timetable was planned from the user's active
                # hours when it last planned a due time; a window narrowed on the Settings
                # page since applies here at once. Runs a person starts are checked where
                # they start, and an auto-send (ADR 0008) is checked by run_prefill's
                # gates() (message_send), with these same settings and its own words.
                try:
                    runs.refuse_if_outside_active_hours(settings.linkedin, now=self._clock())
                except (runs.OutsideActiveHours, runs.RunError) as exc:
                    return "inactive", str(exc)
            try:
                runs.refuse_if_flagged_or_hot(
                    session, user, facts.account_id, now=self._clock(), settings=settings.linkedin
                )
            except runs.SessionFlagged as exc:
                return "session_flagged", str(exc)
            except runs.HeatSkipped as exc:
                return "heat_skip", str(exc)
        return None

    def _finish(
        self, run_id: int, user_id: int, status: SyncRunStatus, reason: str, error: str
    ) -> None:
        """Record how the run ended, unless the runner already did (#197).

        The runners record their own endings, a failure included
        (``runs.recording``), then re-raise; the worker's handlers see the same
        exception afterwards. Finishing an ended run again was a second, refused
        write that logged "already ended", so an ended run is left alone here.
        """
        with session_scope(self._factory, write=True) as session:
            user = session.get(User, user_id)
            if user is None:
                return
            if runs.get_run(session, user, run_id).status is not SyncRunStatus.RUNNING:
                log.debug("run %d was already recorded as ended by its runner", run_id)
                return
            runs.finish_run(
                session,
                user,
                run_id,
                status=status,
                now=self._clock(),
                stop_reason=reason,
                error=error,
            )

    def _finish_quietly(
        self, run_id: int, user_id: int, status: SyncRunStatus, reason: str, error: str
    ) -> None:
        """:meth:`_finish` on the way out of a cancel: a failed write is logged, never
        raised, so the cancel itself always propagates (#294)."""
        try:
            self._finish(run_id, user_id, status, reason, error)
        except Exception:
            log.exception("could not record how run %d ended", run_id)

    def _note_quietly(self, run_id: int, user_id: int, note: str) -> None:
        """Add a note to the run's line; a failed write is logged, never raised (#195)."""
        try:
            with session_scope(self._factory, write=True) as session:
                user = session.get(User, user_id)
                if user is not None:
                    runs.add_note(session, user, run_id, note)
        except Exception:
            log.exception("could not add a note to run %d", run_id)

    def _status(self, run_id: int, user_id: int) -> str:
        with session_scope(self._factory) as session:
            user = session.get(User, user_id)
            if user is None:
                return "unknown"
            return runs.get_run(session, user, run_id).status.value

    def _publish(self, kind: str, run_id: int, user_id: int, data: dict[str, Any]) -> None:
        if self._bus is None:
            return
        self._bus.publish(Event(type=kind, data={"run_id": run_id, **data}, user_id=user_id))


def session_probe(report: PreflightReport) -> SessionProbe:
    """A preflight report reduced to what a posture report, or a Check connection run's
    record (#652), may say about it.

    The adapter lives on the browser side of the line because ``services.posture`` may
    not import ``linkedin.preflight``: a module under ``services/`` that reaches the
    browser is how browser work ends up inside a request handler, and
    ``tests/test_browser_safety.py`` fails the build for it.

    Cookie *names* cross this line and cookie values do not -- they cannot,
    because preflight never read one and :class:`SessionProbe` has no field to
    put one in (spec 9.1, CLAUDE.md).
    """
    logged_in: dict[LoginState, bool | None] = {
        LoginState.LOGGED_IN: True,
        LoginState.NO_SESSION: False,
        LoginState.UNKNOWN: None,
    }
    return SessionProbe(
        attached=report.attached,
        logged_in=logged_in[report.login],
        browser_version=report.browser_version,
        cookie_names=report.session_cookies,
        problems=report.problems,
        profile=report.profile,
    )


def _plain(event: object) -> dict[str, Any]:
    """A progress event as JSON-ready counts: the dataclass's fields, enums as values."""
    if not dataclasses.is_dataclass(event) or isinstance(event, type):
        return {}
    return {
        key: value.value if isinstance(value, enum.Enum) else value
        for key, value in dataclasses.asdict(event).items()
    }


# --- what `netkeep serve` runs --------------------------------------------------


def serve_extractor(
    settings: Settings, *, provider: BrowserProvider | None = None
) -> ServeExtractor:
    """The extractor ``netkeep serve`` hands the app: a worker on the one attach provider.

    ``provider`` defaults to :class:`~netkeep.linkedin.browser.AttachBrowserProvider`
    on ``linkedin.cdp_url``, built once the app has its database, so the
    legacy-lock co-claim (#169 F) goes with the local user's account whatever its
    id (#175 review, F10), and the address is the one in force for the local user:
    the file's, else the Settings page's (#597). It is read here only, so a change
    applies at serve's next start. Building it attaches to nothing; the provider only
    connects inside a run, and a run on a disarmed account is never scheduled.
    """

    def executor(factory: sessionmaker[Session], bus: EventBus) -> runs.RunExecutor:
        chosen = provider
        if chosen is None:
            with session_scope(factory) as session:
                local = local_account_id(session)
                cdp_url = _local_cdp_url(session, settings)
            partner = activity_lock.SINGLE_ACCOUNT_KEY if local is None else account_key(local)
            chosen = AttachBrowserProvider(cdp_url, locks=ActivityLocks(legacy_partner=partner))
        return BrowserWorker(
            chosen, factory, settings.linkedin, bus=bus, campaign_settings=settings
        )

    return ServeExtractor(executor=executor)


def _local_cdp_url(session: Session, settings: Settings) -> str:
    """``linkedin.cdp_url`` in force for the first local user, or the file's without one."""
    user = local_owner(session)
    if user is None:
        return settings.linkedin.cdp_url
    return ui_settings.resolve(session, user, settings).linkedin.cdp_url


def serve_app(settings: Settings, *, ui_base: str | None = None) -> FastAPI:
    """The app ``netkeep serve`` runs: ``create_app`` with the extractor and the launcher.

    ``ui_base`` is the origin a browser here reaches the server at (``serve --host`` and
    ``--port``), where the Chrome launcher points the UI's window; it defaults to
    ``web.host`` and ``web.port``.

    Logs the profile-visit risk warning (#318) once here, at startup, when the
    daily limit is above the level netkeep was designed around, and the same for
    a LinkedIn message budget above 20 a day (#447).
    """
    risk = profile_visit_risk_warning(settings.linkedin.budget)
    if risk is not None:
        log.warning("%s", risk)
    for action in (ActionClass.LI_PREFILLS, ActionClass.LI_MESSAGES_AUTO):
        message_risk = li_message_risk_warning(action, settings.linkedin.budget)
        if message_risk is not None:
            log.warning("%s", message_risk)
    app = create_app(settings, extractor=serve_extractor(settings))
    base = ui_base or local_api_key.base_url(settings.web.host, settings.web.port)
    app.state.chrome_launcher = serve_chrome_launcher(app, settings, base)
    app.state.after_listening = lambda: _after_listening(app)
    return app


# --- the Chrome launcher (#636) ---------------------------------------------------


#: How long the launcher's probe waits for the Chrome on the port to name its process,
#: while it holds the activity lock: a port that accepts but never speaks CDP would
#: otherwise hold it and refuse runs as busy meanwhile.
PROBE_CONNECT_TIMEOUT_S: Final = 5.0


async def launcher_probe(
    cdp_url: str,
    *,
    locks: ActivityLocks,
    account: str,
    reader: PidReader | None = None,
    timeout_s: float = PROBE_CONNECT_TIMEOUT_S,
) -> PortProbe:
    """What the Chrome on ``cdp_url`` is, for the launcher's checks (#636).

    Under ``account``'s activity lock, never waiting: one read of the browser's pid
    (#624) over the browser target's own websocket (:class:`CdpIdentityReader`, #675),
    which then closes. It needs no Playwright context, so a Chrome with no windows open
    still names its process. A Chrome that can't be reached or doesn't answer within
    ``timeout_s`` is no pid, which the launcher refuses. A held lock is ``busy``, and
    nothing connects: a run is attached, and a second CDP client would drop its
    connection (spec 9.9). Then ``verified_pid`` is the Chrome pid that run's attach
    verified, from the holder note of ``account``'s own lock, read only while that lock
    is held.
    """
    chosen = CdpIdentityReader() if reader is None else reader
    try:
        async with locks.hold(account):
            try:
                pid = await asyncio.wait_for(chosen.read_pid(cdp_url), timeout_s)
            except TimeoutError:
                log.info("the launcher's probe got no answer from %s in time", cdp_url)
                return PortProbe(busy=False)
            return PortProbe(busy=False, browser_pid=pid)
    except BrowserBusy:
        # Only the account's own lock's note, and only while it's held: a busy legacy
        # lock, or a note a crash left on a free lock, verifies nothing.
        if not activity_lock.inspect(account, locks.directory).held:
            return PortProbe(busy=True)
        holder = activity_lock.read_holder(account, locks.directory)
        return PortProbe(busy=True, verified_pid=None if holder is None else holder.browser_pid)


def launcher_lock_key(session: Session) -> str:
    """The activity-lock key the launcher's probe holds: the local user's account's."""
    local = local_account_id(session)
    return activity_lock.SINGLE_ACCOUNT_KEY if local is None else account_key(local)


def serve_chrome_launcher(app: FastAPI, settings: Settings, ui_base: str) -> ChromeLauncher:
    """The launcher ``serve`` puts on ``app.state.chrome_launcher`` (#636, ADR 0012).

    The app holds it only as a :class:`~netkeep.services.chrome_launch.ChromeSpawner`,
    and ``POST /linkedin/browser/start`` submits its launch to the task runner. At each
    launch it reads the Chrome address in force (the file, then the Settings page,
    #597), mints a connect link for ``ui_base`` from the key ``serve`` holds (ADR 0013),
    and probes under the local account's activity lock. Building it starts nothing.
    """

    def factory() -> sessionmaker[Session]:
        made: sessionmaker[Session] = app.state.session_factory
        return made

    def address() -> tuple[str, str]:
        with session_scope(factory()) as session:
            return _local_cdp_url(session, settings), launcher_lock_key(session)

    async def cdp_url() -> str:
        return (await off_loop(address))[0]

    async def probe(url: str) -> PortProbe:
        key = (await off_loop(address))[1]
        return await launcher_probe(url, locks=ActivityLocks(legacy_partner=key), account=key)

    def app_url() -> str:
        held: local_api_key.LocalApiKey = app.state.local_api_key
        return local_api_key.connect_url(ui_base, held.key)

    port = configured_port(ui_base)
    return ChromeLauncher(
        cdp_url=cdp_url,
        profile=data_dir() / CHROME_PROFILE_DIRNAME,
        app_url=app_url,
        ui_port=0 if port is None else port,
        probe=probe,
        run_holds_browser=runs.any_browser_lock_held,
        pid_alive=activity_lock.pid_alive,
    )


async def _after_listening(app: FastAPI) -> None:
    """:func:`start_chrome_at_startup`, with anything it raises logged: nobody awaits it."""
    try:
        await start_chrome_at_startup(app)
    except Exception:
        log.exception("could not start netkeep's Chrome at startup")


#: How long ``serve``'s start at startup waits for the server to listen before it gives up.
LISTEN_WAIT_S: Final = 60.0
_LISTEN_POLL_S: Final = 0.1

#: The file in the data directory that records the start at startup's last hand-off.
STARTUP_HANDOFF_FILENAME: Final = "chrome-startup-handoff.json"
#: How long after a hand-off at startup to the same live Chrome a second one is skipped.
STARTUP_HANDOFF_COOLDOWN_S: Final = 300.0


def _startup_handoff_path() -> Path:
    return data_dir() / STARTUP_HANDOFF_FILENAME


def recent_startup_handoff(pid: int, now: float) -> bool:
    """Whether the start at startup handed off to ``pid`` within the cooldown (#679).

    Defense in depth against a ``serve`` that crashes after listening and is restarted
    by launchd: each restart would otherwise add a window to the same Chrome. A record
    that is missing, unreadable, for another pid, or from the future counts as none.
    """
    try:
        record = json.loads(_startup_handoff_path().read_text(encoding="utf-8"))
        last_pid, at = record["pid"], record["at"]
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return False
    if type(last_pid) is not int or not isinstance(at, int | float):
        return False
    return last_pid == pid and 0 <= now - at < STARTUP_HANDOFF_COOLDOWN_S


def record_startup_handoff(pid: int, now: float) -> None:
    """Record a confirmed hand-off at startup to ``pid`` at ``now``.

    Called only for a ``handed_off`` outcome. A failed write is logged only.
    """
    path = _startup_handoff_path()
    partial = path.with_name(f"{path.name}.tmp")
    try:
        partial.write_text(json.dumps({"pid": pid, "at": now}), encoding="utf-8")
        partial.replace(path)
    except OSError as exc:
        log.warning("could not record the startup hand-off: %s", personal_exc(exc))


async def start_chrome_at_startup(
    app: FastAPI,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    wait_s: float = LISTEN_WAIT_S,
    run_holds_browser: Callable[[], bool] = runs.any_browser_lock_held,
    pid_alive: Callable[[int], bool] = activity_lock.pid_alive,
    clock: Callable[[], float] = time.time,
) -> str | None:
    """Start netkeep's Chrome once ``serve`` is listening, when you turned that on (#599).

    ``serve`` runs this as a task from its lifespan (``app.state.after_listening``), so it
    never holds up startup. It waits for ``app.state.listening()`` (``serve`` sets it to
    uvicorn's ``started``), then reads ``linkedin.start_chrome_at_startup`` for the local
    owner, the file's value over the Settings page's. When that is on, it submits one
    :meth:`~netkeep.services.chrome_launch.ChromeSpawner.start` through
    :func:`~netkeep.services.chrome_launch.submit_launch`, the same task the **Start
    Chrome** button's route runs, and logs the outcome. When a live process already
    holds netkeep's profile, that launch hands the UI's window to the running Chrome
    once the launcher has shown it's netkeep's, so the UI is visible after every start
    (#679), except within :data:`STARTUP_HANDOFF_COOLDOWN_S` of a hand-off at startup to
    the same live Chrome. It starts nothing, and says why in the log, when a run holds
    the browser as it checks, or while a launch is under way. A run that starts after
    that check can still get the hand-off, as a **Start Chrome** press can (#636): an
    ``--app`` window, never an attach. Returns the launch's task id, or ``None`` when it
    started none.
    """
    listening: Callable[[], bool] | None = getattr(app.state, "listening", None)
    if listening is None:
        # `serve --reload` and test apps have no server to watch.
        log.debug("no listening signal: netkeep's Chrome isn't started at startup")
        return None
    waited = 0.0
    while not listening():
        if waited >= wait_s:
            log.warning("serve didn't start listening, so netkeep's Chrome wasn't started")
            return None
        await sleep(_LISTEN_POLL_S)
        waited += _LISTEN_POLL_S

    def read() -> tuple[int, bool] | None:
        with session_scope(app.state.session_factory) as session:
            owner = local_owner(session)
            if owner is None:
                return None
            resolved = ui_settings.resolve(session, owner, app.state.settings)
            return owner.id, resolved.linkedin.start_chrome_at_startup

    found = await off_loop(read)
    if found is None or not found[1]:
        log.debug("start netkeep's Chrome at startup is off")
        return None
    owner_id = found[0]
    launcher = getattr(app.state, "chrome_launcher", None)
    if launcher is None:
        log.warning("start at startup is on, but this process has no Chrome launcher")
        return None
    if await asyncio.to_thread(run_holds_browser):
        log.info("not starting netkeep's Chrome at startup: a run holds the browser")
        return None
    holder = profile_lock_pid(data_dir() / CHROME_PROFILE_DIRNAME)
    running = holder if holder is not None and pid_alive(holder) else None
    if running is not None:
        # A restart while netkeep's Chrome runs (#679): the launcher checks that the
        # Chrome on the port is this one (#675's identity read), then hands the UI's
        # window to it. Each hand-off opens a window (#678), so a second one to the same
        # Chrome within the cooldown is skipped, in case serve is crash-looping.
        if await asyncio.to_thread(recent_startup_handoff, running, clock()):
            log.info(
                "not opening the UI at startup: netkeep's Chrome (pid %d) was handed it"
                " less than %d minutes ago",
                running,
                int(STARTUP_HANDOFF_COOLDOWN_S // 60),
            )
            return None
        log.info("netkeep's Chrome is already running (pid %d): opening the UI in it", running)

    def logged(result: LaunchResult) -> None:
        refusal = "" if result.refusal is None else f" ({result.refusal.value})"
        log.info(
            "start netkeep's Chrome at startup: %s%s: %s",
            result.status.value,
            refusal,
            result.message,
        )
        if running is not None and result.status is LaunchStatus.HANDED_OFF:
            # Only a confirmed hand-off starts the cooldown: a refused, unconfirmed or
            # failed one leaves the next restart free to try again.
            record_startup_handoff(running, clock())

    try:
        return submit_launch(
            app.state.tasks, app.state.bus, launcher, user_id=owner_id, on_result=logged
        )
    except LaunchUnderWay:
        log.info("not starting netkeep's Chrome at startup: a launch is already under way")
        return None


def dev_app() -> FastAPI:
    """The factory ``netkeep serve --reload`` hands uvicorn as an import string.

    The reloader imports it in a worker process that never runs the CLI callback,
    so logging is set up here first; then it is :func:`serve_app` with the settings
    resolved from the environment and the search path.
    """
    setup_logging()
    return serve_app(load_settings())


async def run_message_check(
    provider: BrowserProvider,
    factory: sessionmaker[Session],
    user_id: int,
    target: message_check.CheckTarget,
    *,
    settings: Settings,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = _utcnow,
    rng: random.Random | None = None,
    origin: str = LINKEDIN_ORIGIN,
) -> MessageCheckResult:
    """Run a recorded Message check (#473) and record how its run ended.

    Under the account's activity lock, never waiting for it (as a prefill): attach,
    re-check the gates and spend one profile visit (:func:`message_check.spend`), then
    :class:`~netkeep.linkedin.page_messaging.PageMessageCheck` on the run's own tab,
    which the provider closes at the end. A busy or missing browser, a refusal, or a
    failure ends the run ``failed`` with fixed words or an exception's type name, never
    a page's text. A cancel from outside records ``aborted`` and propagates."""
    asked_at = -CANCEL_POLL_S

    async def cancelled() -> bool:
        nonlocal asked_at
        loop = asyncio.get_running_loop()
        if loop.time() - asked_at < CANCEL_POLL_S:
            return False
        asked_at = loop.time()
        return await off_loop(message_check.cancel_requested, factory, user_id, target.run_id)

    async def end(status: SyncRunStatus, reason: str, error: str | None = None) -> None:
        await off_loop(
            message_check.finish_quietly,
            factory,
            user_id,
            target,
            status=status,
            stop_reason=reason,
            now=clock(),
            settings=settings,
            error=error,
        )

    try:
        async with (
            runs.heartbeat(factory, user_id, target.run_id, clock=clock),
            provider.run(account_key(target.account_id)) as browser,
        ):
            refused = await off_loop(
                message_check.spend, factory, user_id, target, settings=settings, now=clock()
            )
            if refused is not None:
                await end(SyncRunStatus.FAILED, *refused)
                return MessageCheckResult(stopped=refused[1])
            check = PageMessageCheck(browser, origin=origin, sleep=sleep, rng=rng)
            result = await check.run(target.public_id, target.profile_id, cancelled=cancelled)
    except (BrowserBusy, BrowserUnavailable) as exc:
        reason = runs.browser_stop_reason(exc)
        log.warning(
            "message check run %d could not use the browser: %s", target.run_id, personal_exc(exc)
        )
        # A refused profile keeps its own words (#624): which profile, and how to fix it.
        stopped = str(exc) if isinstance(exc, ProfileRefused) else runs.describe_stop_reason(reason)
        await end(SyncRunStatus.FAILED, reason, stopped)
        return MessageCheckResult(stopped=stopped)
    except asyncio.CancelledError:
        await end(SyncRunStatus.ABORTED, "interrupted", runs.INTERRUPTED)
        raise
    except Exception as exc:
        # The type only: a Playwright error's text can quote a selector, and a selector
        # here holds the contact's compose href.
        log.error("message check run %d failed (%s)", target.run_id, type(exc).__name__)
        await end(SyncRunStatus.FAILED, "error", f"the check failed ({type(exc).__name__})")
        return MessageCheckResult(stopped=f"the check failed ({type(exc).__name__})")
    if result.stopped is None:
        status, reason = SyncRunStatus.COMPLETED, message_check.MESSAGE_CHECK_STOP
    elif result.stopped == "cancelled":
        status, reason = SyncRunStatus.ABORTED, runs.CANCELLED
    elif result.wall is not None:
        status, reason = SyncRunStatus.FAILED, result.wall.value
    else:
        status, reason = SyncRunStatus.FAILED, "error"
    await off_loop(
        message_check.finish,
        factory,
        user_id,
        target,
        status=status,
        stop_reason=reason,
        now=clock(),
        settings=settings,
        error=result.stopped if status is SyncRunStatus.FAILED else None,
        wall=result.wall,
        wall_url=result.wall_url,
    )
    return result


@dataclass(frozen=True, slots=True)
class BubbleCheckResult:
    """What :func:`run_bubble_check` read (#495), or why it read nothing: fixed words."""

    bubble: BubbleCheck | None = None
    stopped: str | None = None


def _bubble_check_failure(exc: Exception) -> str:
    """How a bubble check failed (#497), in fixed words: the step, when the read itself
    failed, then the exception's class and its kind. Never the exception's text."""
    if isinstance(exc, BubbleCheckFailed):
        return f"{exc.step.value} ({exc.error}: {exc.kind.value})"
    kind = classify_read_failure(exc)
    if kind is ReadFailure.OTHER:
        return f"({type(exc).__name__})"
    return f"({type(exc).__name__}: {kind.value})"


async def run_bubble_check(
    provider: BrowserProvider,
    factory: sessionmaker[Session],
    user_id: int,
    target: message_check.CheckTarget,
    *,
    settings: Settings,
    clock: Clock = _utcnow,
    origin: str = LINKEDIN_ORIGIN,
) -> BubbleCheckResult:
    """Run a recorded bubble check (#495) and record how its run ended.

    Under the account's activity lock, never waiting for it, as :func:`run_message_check`:
    attach, re-check the session flag, heat, and a cancel (:func:`message_check.recheck`,
    no visit spent, since no profile opens), then
    :class:`~netkeep.linkedin.page_messaging.PageBubbleCheck`'s one read, then a cancel
    once more. A cancel ends the run ``aborted``. A busy or
    missing browser, a refusal, or a failure ends the run ``failed`` with fixed words or
    an exception's type name. A cancel from outside records ``aborted`` and propagates."""

    async def end(status: SyncRunStatus, reason: str, error: str | None = None) -> None:
        await off_loop(
            message_check.finish_quietly,
            factory,
            user_id,
            target,
            status=status,
            stop_reason=reason,
            now=clock(),
            settings=settings,
            error=error,
        )

    try:
        async with (
            runs.heartbeat(factory, user_id, target.run_id, clock=clock),
            provider.run(account_key(target.account_id)) as browser,
        ):
            refused = await off_loop(
                message_check.recheck, factory, user_id, target, settings=settings, now=clock()
            )
            if refused is not None:
                # A cancel ends the run aborted, as a cancelled Message check's does.
                cancel = refused[0] == runs.CANCELLED
                await end(SyncRunStatus.ABORTED if cancel else SyncRunStatus.FAILED, *refused)
                return BubbleCheckResult(stopped=refused[1])
            check = await PageBubbleCheck(browser, origin=origin).run(target.profile_id)
            if await off_loop(message_check.cancel_requested, factory, user_id, target.run_id):
                await end(SyncRunStatus.ABORTED, runs.CANCELLED)
                return BubbleCheckResult(check, stopped="cancelled")
    except (BrowserBusy, BrowserUnavailable) as exc:
        reason = runs.browser_stop_reason(exc)
        log.warning(
            "bubble check run %d could not use the browser: %s", target.run_id, personal_exc(exc)
        )
        # A refused profile keeps its own words (#624): which profile, and how to fix it.
        stopped = str(exc) if isinstance(exc, ProfileRefused) else runs.describe_stop_reason(reason)
        await end(SyncRunStatus.FAILED, reason, stopped)
        return BubbleCheckResult(stopped=stopped)
    except asyncio.CancelledError:
        await end(SyncRunStatus.ABORTED, "interrupted", runs.INTERRUPTED)
        raise
    except Exception as exc:
        # Fixed words only: a Playwright error's text can quote a selector, and a selector
        # here can hold the contact's name.
        failed = _bubble_check_failure(exc)
        log.error("bubble check run %d failed %s", target.run_id, failed)
        await end(SyncRunStatus.FAILED, "error", f"the check failed {failed}")
        return BubbleCheckResult(stopped=f"the check failed {failed}")
    await end(SyncRunStatus.COMPLETED, message_check.BUBBLE_CHECK_STOP)
    return BubbleCheckResult(check)
