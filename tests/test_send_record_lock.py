"""A send's record when another writer holds SQLite's write lock past the busy timeout (#627).

Each test holds the real write lock (``BEGIN IMMEDIATE`` on a second connection) from
the moment the fake send returns, so the record finds the database locked. The claim was
committed before the send, so the step is never sent twice, whether the record lands on
a retry or never lands at all.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from campaign_fakes import NOW, SETTINGS, FakeSender
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from test_auto_send import AUTO, Auto, fast_source
from test_auto_send import lane as lane  # the fixture
from test_campaign_engine import World, make_world
from test_linkedin_steps import Lane

from netkeep import db
from netkeep.linkedin.messaging import PrefillResult
from netkeep.models import Message, MessageStatus, SyncRunStatus
from netkeep.scoping import get_scoped
from netkeep.services import campaign_engine as engine_module
from netkeep.services import message_send
from netkeep.services import posture as posture_module
from netkeep.services.campaign_engine import (
    RECORD_PENDING_AFTER,
    SCHEDULED_IN_GMAIL,
    STOP_WAIT_S,
    Firing,
    SendResult,
    run_tick,
    unrecorded_sends,
)
from netkeep.services.linkedin_accounts import ensure_account
from netkeep.services.linkedin_steps import (
    AUTO_SEND_HOLD_INTERRUPTED,
    auto_send_hold,
    claim_auto_send,
    unrecorded_prefills,
)

BUSY_MS = 50


class WriteLock:
    """SQLite's write lock, held by a connection of its own, as a long request would."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._held: sqlite3.Connection | None = None

    def take(self) -> None:
        connection = sqlite3.connect(self._path, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        self._held = connection

    def release(self) -> None:
        if self._held is not None:
            self._held.rollback()
            self._held.close()
            self._held = None

    @property
    def held(self) -> bool:
        return self._held is not None


@pytest.fixture
def write_lock(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[WriteLock]:
    """A short busy timeout, so a locked record fails in milliseconds, not 5 s."""
    monkeypatch.setattr(db, "SQLITE_BUSY_TIMEOUT_MS", BUSY_MS)
    engine.dispose()  # the next connections take the short timeout
    assert engine.url.database is not None
    lock = WriteLock(engine.url.database)
    yield lock
    lock.release()


class LockingSender(FakeSender):
    """Sends, then takes the write lock before the tick can record the send."""

    lock: WriteLock

    def send(self, firing: Firing) -> SendResult:
        result = super().send(firing)
        self.lock.take()
        return result


def _locking_world(factory: Any, lock: WriteLock) -> World:
    world = make_world(factory)
    sender = LockingSender()
    sender.lock = lock
    world.sender = sender
    return world


def _waits(
    monkeypatch: pytest.MonkeyPatch, module: Any, on_wait: Callable[[], None]
) -> list[float]:
    waited: list[float] = []

    def sleep(seconds: float) -> None:
        waited.append(seconds)
        on_wait()

    monkeypatch.setattr(module, "_sleep", sleep)
    return waited


def test_the_retry_waits_are_pinned_and_fit_the_stop_wait() -> None:
    """Every try waits up to the busy timeout, plus the waits between: all of it within
    the 45 s the engine's stop waits for a tick in flight."""
    assert db.RECORD_RETRY_WAITS_S == (1.0, 2.0, 4.0, 8.0)
    assert db.SQLITE_BUSY_TIMEOUT_MS == 5000
    tries = len(db.RECORD_RETRY_WAITS_S) + 1
    worst = sum(db.RECORD_RETRY_WAITS_S) + tries * db.SQLITE_BUSY_TIMEOUT_MS / 1000
    assert worst < STOP_WAIT_S
    assert timedelta(minutes=5) == RECORD_PENDING_AFTER
    # A send's several Gmail requests (30 s timeout each), then the record's retries.
    assert RECORD_PENDING_AFTER.total_seconds() > 4 * 30 + worst


def test_a_lock_timeout_is_told_from_other_operational_errors(
    engine: Engine, write_lock: WriteLock
) -> None:
    write_lock.take()
    with pytest.raises(OperationalError) as locked, engine.connect() as connection:
        connection.execution_options(**{db.IMMEDIATE_OPTION: True}).execute(text("SELECT 1"))
    assert db.is_lock_timeout(locked.value)
    write_lock.release()
    with pytest.raises(OperationalError) as other, engine.connect() as connection:
        connection.execute(text("SELECT * FROM no_such_table"))
    assert not db.is_lock_timeout(other.value)
    assert not db.is_lock_timeout(RuntimeError("database is locked"))


# --- email: the campaign engine's tick ------------------------------------------------


def test_a_record_that_finds_the_lock_held_lands_on_a_retry_and_nothing_is_resent(
    session_factory: Any, write_lock: WriteLock, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _locking_world(session_factory, write_lock)
    enrollment_id = world.enroll_new()
    waited = _waits(monkeypatch, engine_module, write_lock.release)

    result = world.tick()

    assert waited == [1.0]  # one try found the lock held; the retry recorded it
    assert len(result.fired) == 1
    [message] = world.messages(enrollment_id)
    assert message.status is MessageStatus.SENT and message.gmail_message_id is not None
    enrollment = world.enrollment(enrollment_id)
    assert enrollment.current_step == 1 and enrollment.next_action_at is not None
    assert len(world.sender.firings) == 1


def test_a_record_that_never_lands_leaves_the_claim_and_never_sends_again(
    session_factory: Any,
    write_lock: WriteLock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The actual outcome when the lock outlasts every retry (#627's question): the tick
    fails after the send, the message stays ``scheduled`` with its enrollment parked,
    posture warns, and no later tick sends the step again."""
    world = _locking_world(session_factory, write_lock)
    enrollment_id = world.enroll_new()
    waited = _waits(monkeypatch, engine_module, lambda: None)

    results = run_tick(
        world.factory,
        settings=SETTINGS,
        sender=world.sender,
        clock=lambda: NOW,
        rng=random.Random(1),
    )

    assert waited == list(db.RECORD_RETRY_WAITS_S)
    assert results == []  # the user's tick failed after its send; the next user's goes on
    assert "its send could not be recorded" in caplog.text
    assert "campaign tick failed for user" in caplog.text
    write_lock.release()
    [claimed] = world.messages(enrollment_id)
    assert claimed.status is MessageStatus.SCHEDULED
    assert world.enrollment(enrollment_id).next_action_at is None

    def pending(at: Any) -> int:
        def read(session: Any) -> int:
            user = session.get(type(world.user), world.user.id)
            return unrecorded_sends(session, user, now=at)

        return world.read(read)

    assert pending(NOW + RECORD_PENDING_AFTER - timedelta(seconds=1)) == 0
    assert pending(NOW + RECORD_PENDING_AFTER) == 1

    def row(at: Any) -> posture_module.Protection:
        def read(session: Any) -> posture_module.Protection:
            user = session.get(type(world.user), world.user.id)
            return posture_module._send_records(session, user, now=at)

        return world.read(read)

    later = row(NOW + timedelta(minutes=5))
    assert later.key == posture_module.KEY_SEND_RECORDS
    [warning] = later.warnings
    assert warning.startswith("1 email send(s)") and "None is sent again" in warning

    # One the person put in Gmail's Schedule send waits for them, not for a record.
    world.write(
        lambda s: setattr(
            get_scoped(s, world.user, Message, claimed.id), "error", SCHEDULED_IN_GMAIL
        )
    )
    assert pending(NOW + timedelta(hours=1)) == 0
    assert row(NOW + timedelta(hours=1)).warnings == ()

    world.sender.__class__ = FakeSender  # the lock is gone; sends would go through
    for hours in (1, 2, 24):
        assert world.tick(NOW + timedelta(hours=hours)).fired == []
    assert len(world.sender.firings) == 1


def test_a_record_that_fails_for_another_reason_is_not_retried(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = make_world(session_factory)
    enrollment_id = world.enroll_new()
    waited = _waits(monkeypatch, engine_module, lambda: None)

    def broken(*_: Any, **__: Any) -> None:
        raise OperationalError("UPDATE messages", {}, sqlite3.OperationalError("disk I/O error"))

    monkeypatch.setattr(engine_module, "_record", broken)
    assert run_tick(world.factory, settings=SETTINGS, sender=world.sender, clock=lambda: NOW) == []
    assert waited == []
    [claimed] = world.messages(enrollment_id)
    assert claimed.status is MessageStatus.SCHEDULED
    assert len(world.sender.firings) == 1


# --- LinkedIn: an auto-send's record -------------------------------------------------


def _lock_after_page_work(a: Auto, lock: WriteLock) -> None:
    """The source does its page work (the Send click included), then the lock is taken
    before the runner records the outcome."""

    def source(run: Any, *, sleep: Any, clock: Any) -> Any:
        made = fast_source(run, sleep=sleep, clock=clock)
        page_work = made.prefill

        async def prefill(*args: Any, **kwargs: Any) -> PrefillResult:
            result = await page_work(*args, **kwargs)
            lock.take()
            return result

        made.prefill = prefill  # type: ignore[method-assign]
        return made

    a.worker._prefill_sources = source


def _async_waits(monkeypatch: pytest.MonkeyPatch, on_wait: Callable[[], None]) -> list[float]:
    waited: list[float] = []

    async def sleep(seconds: float) -> None:
        waited.append(seconds)
        on_wait()

    monkeypatch.setattr(message_send, "_sleep", sleep)
    return waited


async def test_an_auto_sends_record_lands_on_a_retry_and_send_is_clicked_once(
    lane: Lane, write_lock: WriteLock, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = Auto(lane)
    _lock_after_page_work(a, write_lock)
    waited = _async_waits(monkeypatch, write_lock.release)

    await a.execute()

    assert waited == [1.0]
    message = a.message()
    assert message is not None and message.status is MessageStatus.PREFILLED
    assert message.send_clicked_at is not None
    assert (a.run().status, a.run().stop_reason) == (SyncRunStatus.COMPLETED, "send_clicked")
    assert len(a.site.sent) == 1


async def test_an_auto_send_whose_record_never_lands_is_held_and_never_sent_again(
    lane: Lane, write_lock: WriteLock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual outcome for LinkedIn: Send was clicked, the record and the run's
    failure both find the lock held. The message stays claimed, which holds the one open
    slot; once the run reads as left behind, posture counts it and the next auto-send
    claim holds auto-send instead of claiming anything."""
    a = Auto(lane)
    _lock_after_page_work(a, write_lock)
    waited = _async_waits(monkeypatch, lambda: None)

    with pytest.raises(OperationalError):
        await a.execute()

    assert waited == list(db.RECORD_RETRY_WAITS_S)
    write_lock.release()
    assert len(a.site.sent) == 1
    message = a.message()
    assert message is not None and message.status is MessageStatus.SCHEDULED
    assert a.run().status is SyncRunStatus.RUNNING  # nothing could end it

    later = NOW + timedelta(hours=3)
    assert lane.read(lambda s, u: unrecorded_prefills(s, u, now=NOW)) == 0  # still live
    assert lane.read(lambda s, u: unrecorded_prefills(s, u, now=later)) == 1
    [warning] = lane.read(lambda s, u: posture_module._send_records(s, u, now=later)).warnings
    assert warning.startswith("1 LinkedIn message(s)")

    other = lane.enroll(contact={"li_urn": "urn:li:fsd_profile:ACoAAOther9", "li_public_id": "q"})
    claim = lane.write(lambda s, u: claim_auto_send(s, u, now=later, settings=AUTO))
    assert claim is None
    hold = lane.read(lambda s, u: auto_send_hold(s, u, ensure_account(s, u).id))
    assert hold is not None and hold.reason == AUTO_SEND_HOLD_INTERRUPTED
    assert lane.enrollment(other).next_action_at is not None  # not claimed
    assert len(a.site.sent) == 1


async def test_a_linkedin_record_that_fails_for_another_reason_is_not_retried(
    lane: Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = Auto(lane)
    waited = _async_waits(monkeypatch, lambda: None)

    def broken(*_: Any, **__: Any) -> None:
        raise OperationalError("UPDATE messages", {}, sqlite3.OperationalError("disk I/O error"))

    monkeypatch.setattr(message_send, "record", broken)
    await a.execute()  # the worker fails the run, which the database still allows
    assert waited == []
    assert a.run().status is SyncRunStatus.FAILED
    assert len(a.site.sent) == 1


async def test_each_linkedin_record_try_takes_its_own_time(monkeypatch: pytest.MonkeyPatch) -> None:
    _async_waits(monkeypatch, lambda: None)
    report = message_send.PrefillReport(1, None)
    times = iter([NOW, NOW + timedelta(seconds=7)])
    seen: list[Any] = []

    def call(*, now: Any) -> Any:
        seen.append(now)
        if len(seen) == 1:
            raise OperationalError(
                "BEGIN IMMEDIATE", {}, sqlite3.OperationalError("database is locked")
            )
        return report

    assert await message_send._record_retrying(1, lambda: next(times), call) is report
    assert seen == [NOW, NOW + timedelta(seconds=7)]
