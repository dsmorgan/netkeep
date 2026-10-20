"""The scheduler's liveness at the edges (#696): a ``message_send`` row left running by a
process that went away, and fires that come due outside active hours.

A stale row read as still running deferred every due kind at every heartbeat, for
good. A fire claimed outside the window was refused by the worker yet counted as run,
which cost a day (or a week) of cadence. Each test here fails without its fix.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from random import Random

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeep.db import session_scope
from netkeep.models import SyncRun, SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeep.scoping import scoped
from netkeep.services import runs, scheduler
from netkeep.services.events import EventBus
from netkeep.services.linkedin_accounts import arm_scheduled_runs, ensure_account
from netkeep.services.scheduled_runs import (
    REFUSED_OUTSIDE_HOURS,
    auto_send_spacing_left,
    serve_registry,
)
from netkeep.services.scheduler import JobContext, JobKind, JobOutcome, JobSchedule
from netkeep.services.tasks import TaskRunner

ACCOUNT = 1
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
ALL_DAY = (time(0, 0), time(0, 0))
WINDOW = (time(8, 0), time(21, 0))
DAILY = JobSchedule(JobKind.CONNECTIONS_INCREMENTAL, timedelta(days=1))
FIRST_SETUP = JobSchedule(JobKind.CONNECTIONS_FULL, timedelta(days=7), run_on_first_setup=True)


def _user(factory: sessionmaker[Session]) -> User:
    with session_scope(factory, write=True) as session:
        return factories.make_user(session, timezone="UTC")


def _set_due(
    factory: sessionmaker[Session],
    user: User,
    schedule: JobSchedule,
    due: datetime,
    *,
    fired_once: bool = True,
) -> None:
    """Establish ``schedule`` and store ``due`` as its due time."""
    with session_scope(factory, write=True) as session:
        scheduler.establish_schedule(
            session,
            user,
            ACCOUNT,
            schedule.kind,
            now=due - timedelta(days=30),
            schedule=schedule,
            rng=Random(0),
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )
        state = scheduler._load_state(session, user, ACCOUNT, schedule.kind)
        assert state is not None
        scheduler._store_state(
            session,
            user,
            ACCOUNT,
            schedule.kind,
            replace(state, due=due, fired_once=fired_once),
        )


def _state(factory: sessionmaker[Session], user: User, kind: JobKind) -> scheduler._JobState:
    with session_scope(factory) as session:
        state = scheduler._load_state(session, user, ACCOUNT, kind)
    assert state is not None
    return state


def _recording(
    calls: list[JobKind], outcome: JobOutcome | None = None
) -> Callable[[JobContext], object]:
    async def handler(ctx: JobContext) -> JobOutcome | None:
        calls.append(ctx.kind)
        return outcome

    return handler


def _registry(handler: Callable[[JobContext], object]) -> scheduler.JobRegistry:
    return {kind: handler for kind in JobKind}  # type: ignore[misc]


# --- B4: a stale message_send row ---------------------------------------------------


def _message_send(
    factory: sessionmaker[Session],
    user: User,
    *,
    started: datetime,
    beat: datetime | None,
    beat_by: str | None = None,
) -> int:
    with session_scope(factory, write=True) as session:
        run = runs.create_run(
            session,
            user,
            SyncRunKind.MESSAGE_SEND,
            trigger=SyncRunTrigger.MANUAL,
            now=started,
            gate=runs.MESSAGE_SEND_GATE,
            browser_held=lambda _account: False,
        )
        run.heartbeat_at = beat
        if beat_by is not None:
            run.heartbeat_by = beat_by
        return run.id


def test_a_stale_message_send_row_ends_at_its_last_sign_of_life(
    session_factory: sessionmaker[Session],
) -> None:
    user = _user(session_factory)
    # Killed ten minutes ago, before its first heartbeat: no lock held, so it is stale.
    _message_send(session_factory, user, started=NOW - timedelta(minutes=10), beat=None)
    assert scheduler._last_message_send(session_factory, user, NOW) is None


def test_a_stale_row_that_beat_recently_still_holds_the_gap_from_that_beat(
    session_factory: sessionmaker[Session],
) -> None:
    user = _user(session_factory)
    beat = NOW - timedelta(seconds=30)
    _message_send(session_factory, user, started=NOW - timedelta(minutes=10), beat=beat)
    assert scheduler._last_message_send(session_factory, user, NOW) == beat


def test_a_message_send_row_still_in_its_start_grace_counts_as_running(
    session_factory: sessionmaker[Session],
) -> None:
    user = _user(session_factory)
    _message_send(session_factory, user, started=NOW - timedelta(seconds=30), beat=None)
    assert scheduler._last_message_send(session_factory, user, NOW) == NOW


def test_a_message_send_row_whose_browser_lock_is_held_counts_as_running(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past its start grace, but a process here still holds the account's lock: the run
    is alive, however old, and keeps holding the gap (#696 review)."""
    user = _user(session_factory)
    _message_send(session_factory, user, started=NOW - timedelta(minutes=10), beat=None)
    monkeypatch.setattr(runs, "browser_held_for", lambda session: lambda account_id: True)
    assert scheduler._last_message_send(session_factory, user, NOW) == NOW


def test_a_message_send_row_another_runner_beats_for_counts_as_running(
    session_factory: sessionmaker[Session],
) -> None:
    """Started ten minutes ago in another data directory, which beat 30 seconds ago: its
    heartbeat is the sign of life, so it is still running (#696 review, #467)."""
    user = _user(session_factory)
    other = "b" * runs.RUNNER_ID_LENGTH
    assert other != runs.runner_id()
    _message_send(
        session_factory,
        user,
        started=NOW - timedelta(minutes=10),
        beat=NOW - timedelta(seconds=30),
        beat_by=other,
    )
    assert scheduler._last_message_send(session_factory, user, NOW) == NOW


async def test_a_stale_message_send_row_no_longer_defers_every_due_kind(
    session_factory: sessionmaker[Session],
) -> None:
    user = _user(session_factory)
    _set_due(session_factory, user, DAILY, NOW - timedelta(minutes=1))
    _message_send(session_factory, user, started=NOW - timedelta(hours=3), beat=None)
    calls: list[JobKind] = []
    fired = await scheduler.poll_once(
        session_factory,
        [(user, ACCOUNT)],
        now=NOW,
        registry=_registry(_recording(calls)),
        schedules={DAILY.kind: DAILY},
        armed=scheduler.ARMING_NOT_REQUIRED,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )
    assert calls == [DAILY.kind]
    assert [r.fired for r in fired] == [True]


def test_auto_send_spacing_counts_from_the_start_so_a_stale_row_cannot_hold_it(
    session_factory: sessionmaker[Session],
) -> None:
    """The audit's second check: the spacing runs from ``started_at``, never "now while
    running", so it ends on its own after a stale row. Pinned, not changed."""
    user = _user(session_factory)
    _message_send(session_factory, user, started=NOW - timedelta(hours=3), beat=None)
    later = NOW + timedelta(minutes=scheduler.AUTO_SEND_SPACING_MAX)
    with session_scope(session_factory) as session:
        assert auto_send_spacing_left(session, user, later) == timedelta(0)


# --- B5: a fire due outside active hours -----------------------------------------------


async def _poll(
    factory: sessionmaker[Session],
    user: User,
    schedule: JobSchedule,
    handler: Callable[[JobContext], object],
    *,
    now: datetime,
    window: tuple[time, time] = WINDOW,
    clock: Callable[[], datetime] | None = None,
) -> scheduler.FireResult | None:
    return await scheduler.poll_and_fire(
        factory,
        user,
        ACCOUNT,
        schedule.kind,
        now=now,
        schedule=schedule,
        registry=_registry(handler),
        armed=scheduler.ARMING_NOT_REQUIRED,
        tz="UTC",
        active_start=window[0],
        active_end=window[1],
        clock=clock,
    )


async def test_a_fire_due_past_the_windows_end_is_parked_for_its_next_start(
    session_factory: sessionmaker[Session],
) -> None:
    """A long run held the heartbeat past 21:00: the 20:55 fire is claimed at 21:20."""
    user = _user(session_factory)
    due = datetime(2026, 9, 21, 20, 55, tzinfo=UTC)
    _set_due(session_factory, user, DAILY, due)
    calls: list[JobKind] = []
    late = datetime(2026, 9, 21, 21, 20, tzinfo=UTC)
    assert await _poll(session_factory, user, DAILY, _recording(calls), now=late) is None
    assert calls == []
    tomorrow = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
    assert _state(session_factory, user, DAILY.kind).due == tomorrow
    # At the window's start it fires, and the cadence runs on from there.
    result = await _poll(session_factory, user, DAILY, _recording(calls), now=tomorrow)
    assert result is not None and result.fired
    assert calls == [DAILY.kind]
    assert result.next_due == tomorrow + DAILY.interval


async def test_a_window_narrowed_since_keeps_a_first_setup_kinds_standing(
    session_factory: sessionmaker[Session],
) -> None:
    user = _user(session_factory)
    _set_due(session_factory, user, FIRST_SETUP, NOW - timedelta(minutes=1), fired_once=False)
    narrowed = (time(13, 0), time(17, 0))
    calls: list[JobKind] = []
    assert (
        await _poll(session_factory, user, FIRST_SETUP, _recording(calls), now=NOW, window=narrowed)
        is None
    )
    assert calls == []
    state = _state(session_factory, user, FIRST_SETUP.kind)
    assert state.due == NOW.replace(hour=13)
    assert not state.fired_once


async def test_a_schedule_that_ignores_active_hours_still_fires_outside_them(
    session_factory: sessionmaker[Session],
) -> None:
    user = _user(session_factory)
    anytime = JobSchedule(DAILY.kind, DAILY.interval, respect_active_hours=False)
    late = datetime(2026, 9, 21, 22, 0, tzinfo=UTC)
    _set_due(session_factory, user, anytime, late)
    calls: list[JobKind] = []
    result = await _poll(session_factory, user, anytime, _recording(calls), now=late)
    assert result is not None and result.fired and calls == [anytime.kind]


async def test_a_worker_refusal_outside_hours_undoes_the_claim(
    session_factory: sessionmaker[Session],
) -> None:
    """The window closed between the claim (20:59:30) and the worker (21:00:30)."""
    user = _user(session_factory)
    due = datetime(2026, 9, 21, 20, 59, tzinfo=UTC)
    _set_due(session_factory, user, FIRST_SETUP, due, fired_once=False)
    with session_scope(session_factory, write=True) as session:
        state = scheduler._load_state(session, user, ACCOUNT, FIRST_SETUP.kind)
        assert state is not None
        scheduler._store_state(
            session, user, ACCOUNT, FIRST_SETUP.kind, replace(state, is_catchup=True)
        )
    claimed = due + timedelta(seconds=30)
    refused = datetime(2026, 9, 21, 21, 0, 30, tzinfo=UTC)
    calls: list[JobKind] = []
    result = await _poll(
        session_factory,
        user,
        FIRST_SETUP,
        _recording(calls, JobOutcome.OUTSIDE_HOURS_AFTER_GATE),
        now=claimed,
        clock=lambda: refused,
    )
    assert calls == [FIRST_SETUP.kind]
    assert result is not None
    assert (result.fired, result.skipped_reason) == (False, "outside_hours_after_gate")
    tomorrow = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
    assert result.next_due == tomorrow
    state = _state(session_factory, user, FIRST_SETUP.kind)
    # The state from before the claim, at the window's next start: not a week out, and
    # the full sync still has its first-setup standing and its catch-up flag.
    assert (state.due, state.fired_once, state.is_catchup) == (tomorrow, False, True)


async def test_a_refused_scheduled_run_answers_outside_hours(
    session_factory: sessionmaker[Session],
) -> None:
    """The handler reads the worker's refusal back from the run's row."""

    class Refuses:
        def __init__(self, reason: str) -> None:
            self.reason = reason

        async def execute(self, run_id: int, user_id: int) -> runs.RunOutcome:
            with session_scope(session_factory, write=True) as session:
                owner = session.get(User, user_id)
                assert owner is not None
                runs.finish_run(
                    session,
                    owner,
                    run_id,
                    status=SyncRunStatus.FAILED,
                    now=NOW,
                    stop_reason=self.reason,
                    error="outside active hours",
                )
            return runs.RunOutcome.DONE

    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session)
        account = ensure_account(session, owner).id
        arm_scheduled_runs(session, owner, now=NOW)
        owner_id = owner.id
    ctx = JobContext(user_id=owner_id, account_id=account, kind=DAILY.kind, due=NOW, catch_up=False)
    outcomes = []
    for reason in (REFUSED_OUTSIDE_HOURS, "heat_skip"):
        registry = serve_registry(
            session_factory, Refuses(reason), TaskRunner(EventBus()), clock=lambda: NOW
        )
        outcomes.append(await registry[DAILY.kind](ctx))
    # Only the window's refusal is a skip; any other refusal still counts as run.
    assert outcomes == [JobOutcome.OUTSIDE_HOURS_AFTER_GATE, None]
    with session_scope(session_factory) as session:
        reader = session.get(User, owner_id)
        assert reader is not None
        assert len(session.scalars(scoped(reader, SyncRun)).all()) == 2


# --- B5: the gap and stagger pushes stay inside the window ------------------------------


def test_the_interleave_gap_push_past_the_windows_end_goes_to_its_next_start(
    session_factory: sessionmaker[Session],
) -> None:
    user = _user(session_factory)
    fire = datetime(2026, 9, 21, 20, 59, tzinfo=UTC)
    _set_due(session_factory, user, DAILY, fire)
    scheduler._defer_past_the_gap(
        session_factory,
        user,
        ACCOUNT,
        DAILY.kind,
        after=fire,
        schedule=DAILY,
        tz="UTC",
        active_start=WINDOW[0],
        active_end=WINDOW[1],
    )
    assert _state(session_factory, user, DAILY.kind).due == datetime(2026, 9, 22, 8, 0, tzinfo=UTC)


def test_a_stagger_push_past_the_windows_end_goes_to_its_next_start(
    session_factory: sessionmaker[Session],
) -> None:
    """Two kinds first scheduled at 20:59 are both due at 20:59 the next day; the second
    is pushed by the gap, past 21:00, so it goes to 08:00 the day after."""
    user = _user(session_factory)
    schedules = {
        JobKind.CONNECTIONS_INCREMENTAL: DAILY,
        JobKind.ENRICH: JobSchedule(JobKind.ENRICH, timedelta(days=1)),
    }
    at = datetime(2026, 9, 21, 20, 59, tzinfo=UTC)
    with session_scope(session_factory, write=True) as session:
        results = scheduler.sync_account_schedule(
            session,
            user,
            ACCOUNT,
            now=at,
            schedules=schedules,
            rng=Random(0),
            tz="UTC",
            active_start=WINDOW[0],
            active_end=WINDOW[1],
        )
    dues = sorted(r.due for r in results.values())
    assert dues == [at + timedelta(days=1), datetime(2026, 9, 23, 8, 0, tzinfo=UTC)]


def test_a_stagger_push_snapped_onto_another_kinds_due_time_is_staggered_again(
    session_factory: sessionmaker[Session],
) -> None:
    """Two kinds due at 20:59 and a third due at 08:00 the day after next: the gap pushes
    the second past 21:00, the snap puts it on the third's 08:00, and the stagger after
    the snap pushes one of those two a gap later (#696 review)."""
    user = _user(session_factory)
    at = datetime(2026, 9, 21, 20, 59, tzinfo=UTC)
    morning = datetime(2026, 9, 23, 8, 0, tzinfo=UTC)
    schedules = {
        JobKind.CONNECTIONS_INCREMENTAL: DAILY,
        JobKind.ENRICH: JobSchedule(JobKind.ENRICH, timedelta(days=1)),
        JobKind.INBOX: JobSchedule(JobKind.INBOX, morning - at),
    }
    with session_scope(session_factory, write=True) as session:
        results = scheduler.sync_account_schedule(
            session,
            user,
            ACCOUNT,
            now=at,
            schedules=schedules,
            rng=Random(0),
            tz="UTC",
            active_start=WINDOW[0],
            active_end=WINDOW[1],
        )
    dues = sorted(r.due for r in results.values())
    assert dues == [at + timedelta(days=1), morning, morning + scheduler.MIN_JOB_KIND_GAP]


def test_a_seeded_kind_pushed_past_the_windows_end_goes_to_its_next_start(
    session_factory: sessionmaker[Session],
) -> None:
    user = _user(session_factory)
    at = datetime(2026, 9, 21, 20, 59, tzinfo=UTC)
    _set_due(session_factory, user, DAILY, at + timedelta(days=1))
    enrich = JobSchedule(JobKind.ENRICH, timedelta(days=1))
    with session_scope(session_factory, write=True) as session:
        seeded = scheduler.seed_missing_kinds(
            session,
            user,
            ACCOUNT,
            now=at,
            schedules={JobKind.ENRICH: enrich},
            rng=Random(0),
            tz="UTC",
            active_start=WINDOW[0],
            active_end=WINDOW[1],
        )
    assert seeded[JobKind.ENRICH].due == datetime(2026, 9, 23, 8, 0, tzinfo=UTC)
