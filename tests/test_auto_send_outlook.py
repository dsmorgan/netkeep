"""When an auto-send step goes out, or why it waits.

Two halves, on the same fixtures:

- **The handler's decisions are pinned.** Each scenario runs the scheduler's auto-send
  handler (``scheduled_runs._auto_send_handler``) and checks its outcome, the words it
  logs, the runs it records, and the hold it writes. These expectations were written
  before the handler's checks became shared functions and pass unchanged after.
- **The outlook agrees.** :func:`netkeeper.services.auto_send_outlook.outlook` reads the
  same checks, in the same order, without writing anything, and names the same reason.

No test here touches a browser, LinkedIn, or the network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from campaign_fakes import NOW
from inbox_fakes import record_poll
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from test_linkedin_steps import LINKEDIN, Lane, make_lane

from netkeeper.config import LinkedInSettings, Settings
from netkeeper.db import session_scope
from netkeeper.linkedin.messaging import MessageOutcome, MessageOutcomeKind
from netkeeper.models import (
    CampaignStep,
    StepMode,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    User,
    UserKind,
)
from netkeeper.scoping import scoped
from netkeeper.services import (
    auto_send_outlook,
    budgets,
    linkedin_steps,
    runs,
    scheduled_runs,
    scheduler,
)
from netkeeper.services import heat as heat_service
from netkeeper.services.auto_send_outlook import Wait
from netkeeper.services.budgets import ActionClass
from netkeeper.services.linkedin_accounts import (
    arm_scheduled_runs,
    disarm_scheduled_runs,
    ensure_account,
    pause_schedule,
)
from netkeeper.services.settings_kv import get_setting, set_setting

AUTO = Settings(
    campaigns=replace(Settings().campaigns, linkedin_auto_send=True),
    linkedin=LinkedInSettings(timezone="UTC"),
)
OFF = replace(AUTO, campaigns=replace(AUTO.campaigns, linkedin_auto_send=False))
#: NOW is 14:00 UTC: these hours have closed for the day.
MORNING = replace(AUTO, linkedin=replace(AUTO.linkedin, active_hours=("08:00", "10:00")))

CONTACT = {"li_urn": "urn:li:fsd_profile:ACoAAOutlook1", "li_public_id": "outlook-one"}
OTHER = {"li_urn": "urn:li:fsd_profile:ACoAAOutlook2", "li_public_id": "outlook-two"}
NOTHING = "no auto-send step is ready"


def _auto_steps(session: Session, user: User) -> None:
    for step in session.scalars(scoped(user, CampaignStep)):
        step.mode = StepMode.AUTO_SEND


def _lane(factory: sessionmaker[Session], *, polled: bool = True) -> Lane:
    made = make_lane(factory, channels=(LINKEDIN, LINKEDIN), settings=AUTO, polled=False)
    made.write(_auto_steps)
    made.write(lambda s, u: arm_scheduled_runs(s, u, now=NOW - timedelta(days=1)))
    if polled:
        made.write(lambda s, u: record_poll(s, u, NOW - timedelta(minutes=30)))
    return made


@pytest.fixture
def lane(session_factory: sessionmaker[Session]) -> Lane:
    return _lane(session_factory)


class _Tasks:
    """Runs a submitted run's work when it is waited for."""

    def __init__(self) -> None:
        self.submitted: list[str] = []
        self._work: Any = None

    def submit(self, name: str, work: Any, *, user_id: int) -> Any:
        self.submitted.append(name)
        self._work = work
        return SimpleNamespace(id=f"task-{len(self.submitted)}")

    async def wait(self, task_id: str) -> None:
        await self._work()


class _Executor:
    def __init__(self) -> None:
        self.executed: list[int] = []

    async def execute(self, run_id: int, user_id: int) -> runs.RunOutcome:
        self.executed.append(run_id)
        return runs.RunOutcome.DONE


@dataclass(frozen=True)
class Fired:
    """What one handler call decided, and everything it left behind."""

    outcome: scheduler.JobOutcome | None
    words: str | None
    runs: list[tuple[str, str, str]]
    executed: int
    hold: str | None


def _account(lane: Lane) -> int:
    return lane.read(lambda s, u: ensure_account(s, u).id)


def _hold(lane: Lane) -> str | None:
    account = _account(lane)
    raw = lane.read(lambda s, u: get_setting(s, u, f"linkedin.auto_send.hold.{account}"))
    return None if raw is None else str(raw["reason"])


async def fire(
    lane: Lane,
    caplog: pytest.LogCaptureFixture,
    *,
    settings: Settings = AUTO,
    at: datetime = NOW,
) -> Fired:
    """Run the auto-send handler once at ``at``, as the scheduler would."""
    executor, tasks = _Executor(), _Tasks()
    handler = scheduled_runs._auto_send_handler(
        lane.factory, executor, tasks, settings, clock=lambda: at
    )
    ctx = scheduler.JobContext(lane.user_id, _account(lane), scheduler.JobKind.AUTO_SEND, at, False)
    before = {run.id for run in lane.runs()}
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="netkeeper.services.scheduled_runs"):
        outcome = await handler(ctx)
    prefix = "scheduled auto-send claimed nothing: "
    words = [
        record.getMessage()[len(prefix) :]
        for record in caplog.records
        if record.getMessage().startswith(prefix)
    ]
    made = [
        (run.kind.value, run.trigger.value, run.status.value)
        for run in lane.runs()
        if run.id not in before
    ]
    return Fired(
        outcome=outcome,
        words=words[0] if words else None,
        runs=made,
        executed=len(executor.executed),
        hold=_hold(lane),
    )


# --- the scenarios ---------------------------------------------------------------------


def _held(lane: Lane) -> None:
    account = _account(lane)
    lane.write(
        lambda s, u: linkedin_steps.hold_auto_send(
            s, u, account, reason=linkedin_steps.AUTO_SEND_HOLD_BUBBLE, now=NOW, run_id=1
        )
    )


def _message_send_run(lane: Lane, started: datetime, *, status: SyncRunStatus) -> int:
    def add(session: Session, user: User) -> int:
        run = SyncRun(
            user_id=user.id,
            linkedin_account_id=ensure_account(session, user).id,
            kind=SyncRunKind.MESSAGE_SEND,
            status=status,
            trigger=SyncRunTrigger.MANUAL,
            started_at=started,
            completed_at=None if status is SyncRunStatus.RUNNING else started,
        )
        session.add(run)
        session.flush()
        return run.id

    return lane.write(add)


def _hot(lane: Lane) -> None:
    def heat(session: Session, user: User) -> None:
        account = ensure_account(session, user).id
        for _ in range(10):
            heat_service.raise_heat(session, user, account, now=NOW, settings=AUTO.linkedin.heat)

    lane.write(heat)


def _spent(lane: Lane) -> None:
    def spend(session: Session, user: User) -> None:
        account = ensure_account(session, user)
        key = budgets._day_key(account.id, ActionClass.LI_MESSAGES_AUTO, NOW.date())
        limit = budgets._limits_for(ActionClass.LI_MESSAGES_AUTO, AUTO.linkedin.budget).day
        set_setting(session, user, key, limit)

    lane.write(spend)


def _interrupted(lane: Lane) -> None:
    """An auto-send claimed two hours ago whose run failed with no outcome recorded."""
    first = lane.enroll(contact=dict(OTHER), next_action_at=NOW - timedelta(hours=2))
    earlier = NOW - timedelta(hours=2)
    lane.write(lambda s, u: record_poll(s, u, earlier - timedelta(minutes=1)))
    claim = lane.write(
        lambda s, u: linkedin_steps.claim_auto_send(s, u, now=earlier, settings=AUTO)
    )
    assert claim is not None and claim.claimed and claim.enrollment_id == first, claim
    run_id = claim.run_id
    assert run_id is not None
    lane.write(lambda s, u: runs.finish_run(s, u, run_id, status=SyncRunStatus.FAILED, now=earlier))


def _prefill_open(lane: Lane) -> None:
    """A person prefilled another step two hours ago and hasn't sent it yet."""
    other = lane.enroll(contact=dict(OTHER), next_action_at=NOW - timedelta(hours=2))
    earlier = NOW - timedelta(hours=2)
    lane.write(lambda s, u: record_poll(s, u, earlier - timedelta(minutes=1)))
    claim = lane.write(
        lambda s, u: linkedin_steps.claim_prefill(s, u, other, now=earlier, settings=AUTO)
    )
    assert claim.claimed and claim.message_id is not None, claim.reasons
    message_id = claim.message_id
    outcome = MessageOutcome(
        MessageOutcomeKind.PREFILLED, "fixed words", "urn:li:msg_conversation:OUT1", 12
    )
    lane.write(
        lambda s, u: linkedin_steps.record_prefill_outcome(
            s, u, message_id, outcome, settings=AUTO, now=earlier
        )
    )
    lane.finish_runs()
    lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(minutes=30)))


def _sending_hours_closed(lane: Lane) -> None:
    from netkeeper.services import sending_hours

    lane.write(
        lambda s, u: sending_hours.write(
            s, u, enabled=True, days=["Mon", "Tue", "Wed", "Thu", "Fri"], start="08:00", end="09:00"
        )
    )


# --- the handler, pinned ------------------------------------------------------------------


async def test_pinned_off(lane: Lane, caplog: pytest.LogCaptureFixture) -> None:
    lane.enroll(contact=dict(CONTACT))
    fired = await fire(lane, caplog, settings=OFF)
    assert fired == Fired(scheduler.JobOutcome.NOTHING_TO_SEND, "auto-send is off", [], 0, None)


async def test_pinned_disarmed(lane: Lane, caplog: pytest.LogCaptureFixture) -> None:
    lane.enroll(contact=dict(CONTACT))
    lane.write(lambda s, u: disarm_scheduled_runs(s, u))
    fired = await fire(lane, caplog)
    assert fired == Fired(scheduler.JobOutcome.DISARMED_AFTER_GATE, None, [], 0, None)


async def test_pinned_paused(lane: Lane, caplog: pytest.LogCaptureFixture) -> None:
    lane.enroll(contact=dict(CONTACT))
    lane.write(lambda s, u: pause_schedule(s, u, now=NOW))
    fired = await fire(lane, caplog)
    assert fired == Fired(scheduler.JobOutcome.PAUSED_AFTER_GATE, None, [], 0, None)


async def test_pinned_paused_and_disarmed_says_paused(
    lane: Lane, caplog: pytest.LogCaptureFixture
) -> None:
    lane.enroll(contact=dict(CONTACT))
    lane.write(lambda s, u: pause_schedule(s, u, now=NOW))
    lane.write(lambda s, u: disarm_scheduled_runs(s, u))
    fired = await fire(lane, caplog)
    assert fired.outcome is scheduler.JobOutcome.PAUSED_AFTER_GATE


async def test_pinned_held(lane: Lane, caplog: pytest.LogCaptureFixture) -> None:
    lane.enroll(contact=dict(CONTACT))
    _held(lane)
    fired = await fire(lane, caplog)
    assert fired == Fired(
        scheduler.JobOutcome.NOTHING_TO_SEND,
        "auto-send is held until the open message bubbles are closed",
        [],
        0,
        linkedin_steps.AUTO_SEND_HOLD_BUBBLE,
    )


async def test_pinned_spacing(lane: Lane, caplog: pytest.LogCaptureFixture) -> None:
    lane.enroll(contact=dict(CONTACT))
    _message_send_run(lane, NOW - timedelta(minutes=5), status=SyncRunStatus.COMPLETED)
    fired = await fire(lane, caplog)
    assert fired == Fired(
        scheduler.JobOutcome.NOTHING_TO_SEND,
        "the spacing since the last auto-send has not passed",
        [],
        0,
        None,
    )


async def test_pinned_interrupted_writes_the_hold(
    lane: Lane, caplog: pytest.LogCaptureFixture
) -> None:
    _interrupted(lane)
    lane.enroll(contact=dict(CONTACT))
    fired = await fire(lane, caplog)
    assert fired == Fired(
        scheduler.JobOutcome.NOTHING_TO_SEND,
        NOTHING,
        [],
        0,
        linkedin_steps.AUTO_SEND_HOLD_INTERRUPTED,
    )


async def test_pinned_heat(lane: Lane, caplog: pytest.LogCaptureFixture) -> None:
    lane.enroll(contact=dict(CONTACT))
    _hot(lane)
    fired = await fire(lane, caplog)
    assert fired == Fired(scheduler.JobOutcome.NOTHING_TO_SEND, NOTHING, [], 0, None)


async def test_pinned_budget_spent(lane: Lane, caplog: pytest.LogCaptureFixture) -> None:
    lane.enroll(contact=dict(CONTACT))
    _spent(lane)
    fired = await fire(lane, caplog)
    assert fired == Fired(scheduler.JobOutcome.NOTHING_TO_SEND, NOTHING, [], 0, None)


async def test_pinned_no_step_due(lane: Lane, caplog: pytest.LogCaptureFixture) -> None:
    lane.enroll(contact=dict(CONTACT), next_action_at=NOW + timedelta(hours=1))
    fired = await fire(lane, caplog)
    assert fired == Fired(scheduler.JobOutcome.NOTHING_TO_SEND, NOTHING, [], 0, None)


async def test_pinned_inbox_stale(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    lane = _lane(session_factory, polled=False)
    lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(hours=8)))
    lane.enroll(contact=dict(CONTACT))
    fired = await fire(lane, caplog)
    assert fired == Fired(scheduler.JobOutcome.NOTHING_TO_SEND, "linkedin_inbox_stale", [], 0, None)


async def test_pinned_outside_active_hours(lane: Lane, caplog: pytest.LogCaptureFixture) -> None:
    lane.enroll(contact=dict(CONTACT))
    fired = await fire(lane, caplog, settings=MORNING)
    assert fired == Fired(scheduler.JobOutcome.NOTHING_TO_SEND, "outside_active_hours", [], 0, None)


async def test_pinned_prefill_open(lane: Lane, caplog: pytest.LogCaptureFixture) -> None:
    _prefill_open(lane)
    lane.enroll(contact=dict(CONTACT))
    fired = await fire(lane, caplog)
    assert fired == Fired(scheduler.JobOutcome.NOTHING_TO_SEND, "prefill_open", [], 0, None)


async def test_pinned_sending_hours(lane: Lane, caplog: pytest.LogCaptureFixture) -> None:
    lane.enroll(contact=dict(CONTACT))
    _sending_hours_closed(lane)
    fired = await fire(lane, caplog)
    assert fired == Fired(
        scheduler.JobOutcome.NOTHING_TO_SEND, "outside_sending_hours", [], 0, None
    )


async def test_pinned_would_send(lane: Lane, caplog: pytest.LogCaptureFixture) -> None:
    lane.enroll(contact=dict(CONTACT))
    fired = await fire(lane, caplog)
    assert fired == Fired(None, None, [("message_send", "scheduled", "running")], 1, None)


# --- the outlook agrees -------------------------------------------------------------------


def _scheduled(lane: Lane, due: datetime = NOW) -> None:
    """The auto-send job's stored due time, as serve's first start would write it."""
    import random

    def establish(session: Session, user: User) -> None:
        scheduler.establish_schedule(
            session,
            user,
            ensure_account(session, user).id,
            scheduler.JobKind.AUTO_SEND,
            now=due - scheduler.AUTO_SEND_INTERVAL,
            schedule=scheduler.AUTO_SEND_SCHEDULE,
            rng=random.Random(1),
            tz="UTC",
        )

    lane.write(establish)


def look(
    lane: Lane, *, settings: Settings = AUTO, now: datetime = NOW - timedelta(minutes=3)
) -> Any:
    return lane.read(lambda s, u: auto_send_outlook.outlook(s, u, now=now, settings=settings))


def test_the_outlook_says_off_first(lane: Lane) -> None:
    lane.enroll(contact=dict(CONTACT))
    _scheduled(lane)
    seen = look(lane, settings=OFF)
    assert seen.wait is Wait.AUTO_SEND_OFF and not seen.would_send


def test_the_outlook_says_when_the_job_has_no_due_time(lane: Lane) -> None:
    lane.enroll(contact=dict(CONTACT))
    seen = look(lane)
    assert seen.wait is Wait.NOT_SCHEDULED and seen.next_fire is None


@pytest.mark.parametrize("state", ["disarmed", "paused"])
def test_the_outlook_reads_the_gate(lane: Lane, state: str) -> None:
    lane.enroll(contact=dict(CONTACT))
    _scheduled(lane)
    if state == "disarmed":
        lane.write(lambda s, u: disarm_scheduled_runs(s, u))
    else:
        lane.write(lambda s, u: pause_schedule(s, u, now=NOW))
    assert look(lane).wait is Wait(state)


def test_the_outlook_reads_the_session_flag(lane: Lane) -> None:
    from netkeeper.linkedin.classify import Outcome
    from netkeeper.services.linkedin_session import flag_session

    lane.enroll(contact=dict(CONTACT))
    _scheduled(lane)
    lane.write(
        lambda s, u: flag_session(s, u, Outcome.LOGGED_OUT, url="https://www.linkedin.com/login")
    )
    assert look(lane).wait is Wait.SESSION_FLAGGED


def test_the_outlook_reads_the_hold_and_its_reason(lane: Lane) -> None:
    lane.enroll(contact=dict(CONTACT))
    _scheduled(lane)
    _held(lane)
    seen = look(lane)
    assert seen.wait is Wait.HELD
    assert seen.hold_reason == linkedin_steps.AUTO_SEND_HOLD_BUBBLE


def test_the_outlook_gives_the_spacing_s_exact_end_and_its_bounds(lane: Lane) -> None:
    lane.enroll(contact=dict(CONTACT))
    _scheduled(lane)
    started = NOW - timedelta(minutes=5)
    _message_send_run(lane, started, status=SyncRunStatus.COMPLETED)
    seen = look(lane)
    assert seen.wait is Wait.SPACING
    assert seen.spacing_earliest == started + timedelta(minutes=20)
    assert seen.spacing_latest == started + timedelta(minutes=45)
    assert seen.until is not None and seen.spacing_earliest <= seen.until <= seen.spacing_latest
    # The handler's own countdown ends at the same instant.
    left = lane.read(lambda s, u: scheduled_runs.auto_send_spacing_left(s, u, NOW))
    assert seen.until == NOW + left


def test_the_outlook_reads_an_interrupted_auto_send_as_held_and_writes_nothing(
    lane: Lane,
) -> None:
    _interrupted(lane)
    lane.enroll(contact=dict(CONTACT))
    _scheduled(lane)
    seen = look(lane)
    assert seen.wait is Wait.HELD
    assert seen.hold_reason == linkedin_steps.AUTO_SEND_HOLD_INTERRUPTED
    assert _hold(lane) is None  # the claim writes it; the outlook only reads


def test_the_outlook_reads_heat_and_when_it_cools(lane: Lane) -> None:
    lane.enroll(contact=dict(CONTACT))
    _scheduled(lane)
    _hot(lane)
    seen = look(lane)
    assert seen.wait is Wait.HEAT
    assert seen.until is not None and seen.until > NOW


def test_the_outlook_reads_the_budget_and_when_it_resets(lane: Lane) -> None:
    lane.enroll(contact=dict(CONTACT))
    _scheduled(lane)
    _spent(lane)
    seen = look(lane)
    assert seen.wait is Wait.BUDGET_SPENT
    assert seen.until == datetime(2026, 9, 30, 0, 0, tzinfo=NOW.tzinfo)


def test_the_outlook_says_no_step_is_due(lane: Lane) -> None:
    lane.enroll(contact=dict(CONTACT), next_action_at=NOW + timedelta(hours=1))
    _scheduled(lane)
    assert look(lane).wait is Wait.NO_STEP_DUE


def test_the_outlook_reads_the_sending_hours(lane: Lane) -> None:
    lane.enroll(contact=dict(CONTACT))
    _scheduled(lane)
    _sending_hours_closed(lane)
    seen = look(lane)
    assert seen.wait is Wait.SENDING_HOURS
    assert seen.until == datetime(2026, 9, 30, 8, 0, tzinfo=NOW.tzinfo)


def test_the_outlook_reads_an_open_prefill(lane: Lane) -> None:
    _prefill_open(lane)
    lane.enroll(contact=dict(CONTACT))
    _scheduled(lane)
    seen = look(lane)
    assert seen.wait is Wait.PREFILL_OPEN and seen.detail is not None


def test_the_outlook_reads_active_hours_and_when_they_open(lane: Lane) -> None:
    lane.enroll(contact=dict(CONTACT))
    _scheduled(lane)
    seen = look(lane, settings=MORNING)
    assert seen.wait is Wait.OUTSIDE_ACTIVE_HOURS
    assert seen.until == datetime(2026, 9, 30, 8, 0, tzinfo=NOW.tzinfo)


def test_the_outlook_reads_the_inbox_poll_s_age_and_limit(
    session_factory: sessionmaker[Session],
) -> None:
    lane = _lane(session_factory, polled=False)
    lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(hours=8)))
    lane.enroll(contact=dict(CONTACT))
    _scheduled(lane)
    seen = look(lane)
    assert seen.wait is Wait.INBOX_STALE
    assert seen.inbox_age_hours == pytest.approx(8.0)
    assert seen.inbox_limit_hours == pytest.approx(6 + 50 / 60)


def test_the_outlook_would_send_the_step_the_handler_then_claims(
    lane: Lane, caplog: pytest.LogCaptureFixture
) -> None:
    first = lane.enroll(contact=dict(CONTACT), next_action_at=NOW - timedelta(minutes=30))
    lane.enroll(contact=dict(OTHER))
    _scheduled(lane)
    seen = look(lane)
    assert seen.would_send and seen.wait is None
    assert seen.next_fire == NOW and seen.checked_at == NOW
    assert seen.enrollment_id == first


def test_a_passed_due_time_is_read_as_now(lane: Lane) -> None:
    lane.enroll(contact=dict(CONTACT))
    _scheduled(lane, due=NOW - timedelta(hours=1))
    seen = look(lane, now=NOW)
    assert seen.next_fire == NOW - timedelta(hours=1) and seen.checked_at == NOW


# --- the outlook and the handler, on the same fixtures -------------------------------------

#: Each scenario: its setup, the settings both read, the outlook's wait, and the handler's
#: outcome and logged words.
AGREE: dict[str, tuple[Any, Settings, Wait | None, scheduler.JobOutcome | None, str | None]] = {
    "held": (
        _held,
        AUTO,
        Wait.HELD,
        scheduler.JobOutcome.NOTHING_TO_SEND,
        "auto-send is held until the open message bubbles are closed",
    ),
    "heat": (_hot, AUTO, Wait.HEAT, scheduler.JobOutcome.NOTHING_TO_SEND, NOTHING),
    "budget": (_spent, AUTO, Wait.BUDGET_SPENT, scheduler.JobOutcome.NOTHING_TO_SEND, NOTHING),
    "sending_hours": (
        _sending_hours_closed,
        AUTO,
        Wait.SENDING_HOURS,
        scheduler.JobOutcome.NOTHING_TO_SEND,
        "outside_sending_hours",
    ),
    "active_hours": (
        lambda lane: None,
        MORNING,
        Wait.OUTSIDE_ACTIVE_HOURS,
        scheduler.JobOutcome.NOTHING_TO_SEND,
        "outside_active_hours",
    ),
    "would_send": (lambda lane: None, AUTO, None, None, None),
}


@pytest.mark.parametrize("name", list(AGREE))
async def test_the_outlook_and_the_handler_agree(
    lane: Lane, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    setup, settings, wait, outcome, words = AGREE[name]
    lane.enroll(contact=dict(CONTACT))
    _scheduled(lane)
    setup(lane)
    assert look(lane, settings=settings).wait is wait
    fired = await fire(lane, caplog, settings=settings)
    assert (fired.outcome, fired.words) == (outcome, words)


# --- the last fire ------------------------------------------------------------------------


def _last(lane: Lane) -> scheduler.LastFire | None:
    account = _account(lane)
    return lane.read(lambda s, u: scheduler.last_fire(s, u, account, scheduler.JobKind.AUTO_SEND))


async def test_the_handler_notes_a_claim_and_a_reason(
    lane: Lane, caplog: pytest.LogCaptureFixture
) -> None:
    lane.enroll(contact=dict(CONTACT), next_action_at=NOW + timedelta(hours=1))
    await fire(lane, caplog)
    assert _last(lane) == scheduler.LastFire(at=NOW, reason=NOTHING)
    lane.enroll(contact=dict(OTHER))
    fired = await fire(lane, caplog)
    [run] = [run for run in lane.runs() if run.kind is SyncRunKind.MESSAGE_SEND]
    assert fired.outcome is None
    assert _last(lane) == scheduler.LastFire(at=NOW, run_id=run.id)
    assert look(lane).last_fire == scheduler.LastFire(at=NOW, run_id=run.id)


async def test_the_handler_notes_nothing_with_the_flag_off(
    lane: Lane, caplog: pytest.LogCaptureFixture
) -> None:
    await fire(lane, caplog, settings=OFF)
    assert _last(lane) is None


async def test_the_scheduler_notes_a_fire_its_gate_skipped(lane: Lane) -> None:
    lane.enroll(contact=dict(CONTACT))
    _scheduled(lane)
    _hot(lane)
    user = lane.read(lambda s, u: u)
    result = await scheduler.poll_and_fire(
        lane.factory,
        user,
        _account(lane),
        scheduler.JobKind.AUTO_SEND,
        now=NOW,
        schedule=scheduler.AUTO_SEND_SCHEDULE,
        registry=scheduler.default_registry(),
        heat_settings=AUTO.linkedin.heat,
        tz="UTC",
    )
    assert result is not None and result.skipped_reason == "heat"
    assert _last(lane) == scheduler.LastFire(at=NOW, reason="heat")


def test_a_last_fire_that_cannot_be_read_is_none(lane: Lane) -> None:
    account = _account(lane)
    lane.write(
        lambda s, u: set_setting(s, u, f"scheduler.last_fire.{account}.auto_send", {"at": "x"})
    )
    assert _last(lane) is None


async def test_a_note_that_cannot_be_written_changes_nothing(
    lane: Lane, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(scheduler, "store_last_fire", broken)
    lane.enroll(contact=dict(CONTACT))
    fired = await fire(lane, caplog)
    assert fired == Fired(None, None, [("message_send", "scheduled", "running")], 1, None)


def test_the_day_resets_at_local_midnight() -> None:
    user = User(timezone="America/New_York")
    assert budgets.day_resets_at(user, NOW) == datetime(2026, 9, 30, 4, 0, tzinfo=NOW.tzinfo)


# --- the API ------------------------------------------------------------------------------


async def test_the_schedule_says_auto_send_is_off(client: Any) -> None:
    body = (await client.get("/api/v1/linkedin/schedule")).json()
    seen = body["auto_send"]
    assert seen["on"] is False and seen["would_send"] is False
    assert seen["wait"] == "auto_send_off" and seen["last_fire"] is None


async def test_the_schedule_gives_the_outlook_with_auto_send_on(
    bare_engine: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, _migrated_template: Any
) -> None:
    import httpx
    from conftest import _copy_template

    from netkeeper.web.app import create_app

    _copy_template(_migrated_template, tmp_path / "bare")
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", str(tmp_path / "no-dist"))
    app = create_app(AUTO, engine=bare_engine)
    async with app.router.lifespan_context(app):
        factory = app.state.session_factory
        with session_scope(factory, write=True) as session:
            user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
            account = ensure_account(session, user).id
            scheduler.store_last_fire(
                session,
                user,
                account,
                scheduler.JobKind.AUTO_SEND,
                scheduler.LastFire(at=NOW, reason="heat"),
            )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            seen = (await client.get("/api/v1/linkedin/schedule")).json()["auto_send"]
    assert seen["on"] is True
    # Nothing has scheduled the job in this database yet.
    assert seen["wait"] == "not_scheduled" and seen["next_fire"] is None
    assert seen["last_fire"] == {"at": seen["last_fire"]["at"], "run_id": None, "reason": "heat"}
    assert datetime.fromisoformat(seen["last_fire"]["at"]) == NOW


# --- the CLI ------------------------------------------------------------------------------


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    from netkeeper import migrations
    from netkeeper.db import database_url, make_engine, make_session_factory
    from netkeeper.scoping import install_scope_guard
    from netkeeper.services.users import ensure_local_user

    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        ensure_local_user(session, settings=Settings())
    yield factory
    engine.dispose()


def _status(*args: str) -> str:
    from typer.testing import CliRunner

    from netkeeper.cli import app as cli

    result = CliRunner().invoke(cli, [*args, "linkedin", "schedule", "status"])
    assert result.exit_code == 0, result.output
    return result.output


def test_schedule_status_says_auto_send_is_off(cli_db: Any) -> None:
    output = _status()
    assert "auto-send: off; auto-send is off ([campaigns] linkedin_auto_send" in output


def test_schedule_status_gives_the_next_check_and_the_last(cli_db: Any, tmp_path: Any) -> None:
    import random

    from netkeeper.models.base import utcnow

    config = tmp_path / "auto.toml"
    config.write_text('[campaigns]\nlinkedin_auto_send = true\n[linkedin]\ntimezone = "UTC"\n')
    with session_scope(cli_db, write=True) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        account = ensure_account(session, user).id
        arm_scheduled_runs(session, user, now=utcnow())
        due = scheduler.establish_schedule(
            session,
            user,
            account,
            scheduler.JobKind.AUTO_SEND,
            now=utcnow(),
            schedule=replace(scheduler.AUTO_SEND_SCHEDULE, respect_active_hours=False),
            rng=random.Random(1),
            tz="UTC",
        ).due
        linkedin_steps.hold_auto_send(
            session,
            user,
            account,
            reason=linkedin_steps.AUTO_SEND_HOLD_BUBBLE,
            now=utcnow(),
            run_id=1,
        )
    output = _status("--config", str(config))
    assert f"auto-send: on; next check {due:%Y-%m-%d %H:%M} UTC" in output
    assert (
        "  next check: auto-send is held: a message bubble is open in Chrome. Fix it in Chrome,"
        " then run `netkeeper linkedin auto-send-resume`"
    ) in output
    assert "  last check: none recorded yet" in output

    with session_scope(cli_db, write=True) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        scheduler.store_last_fire(
            session,
            user,
            account,
            scheduler.JobKind.AUTO_SEND,
            scheduler.LastFire(at=NOW, reason="heat"),
        )
    output = _status("--config", str(config))
    assert (
        "  last check: 2026-09-29 14:00 UTC: sent nothing, heat is at its skip threshold"
    ) in output


# --- in words -----------------------------------------------------------------------------


def _utc(value: datetime) -> str:
    return f"{value:%H:%M}"


@pytest.mark.parametrize("wait", list(Wait))
def test_every_wait_has_words(wait: Wait) -> None:
    seen = auto_send_outlook.AutoSendOutlook(next_fire=NOW, checked_at=NOW, wait=wait)
    words = auto_send_outlook.describe(seen, _utc)
    assert words and "#" not in words and "ADR" not in words


def test_the_words_carry_the_times() -> None:
    def seen(wait: Wait | None, **fields: Any) -> auto_send_outlook.AutoSendOutlook:
        return auto_send_outlook.AutoSendOutlook(next_fire=NOW, checked_at=NOW, wait=wait, **fields)

    describe = auto_send_outlook.describe
    assert describe(seen(None), _utc) == "it would send the oldest due auto-send step"
    assert describe(
        seen(
            Wait.SPACING,
            until=NOW + timedelta(minutes=30),
            spacing_earliest=NOW + timedelta(minutes=15),
            spacing_latest=NOW + timedelta(minutes=40),
        ),
        _utc,
    ) == (
        "it waits out the spacing after the last LinkedIn message run; it ends at 14:30"
        " (20 to 45 minutes after that run: between 14:15 and 14:40)"
    )
    assert describe(seen(Wait.BUDGET_SPENT, until=NOW + timedelta(hours=10)), _utc) == (
        "today's auto-send budget (li_messages_auto) is spent; it resets at 00:00"
    )
    assert describe(seen(Wait.INBOX_STALE, inbox_age_hours=8.0, inbox_limit_hours=6.8), _utc) == (
        "the LinkedIn inbox hasn't been read recently: the last complete poll was 8.0 hours"
        " ago (limit 6.8)"
    )
    last = auto_send_outlook.describe_last
    assert last(scheduler.LastFire(at=NOW, run_id=7), _utc) == "14:00: started run 7"
    assert last(scheduler.LastFire(at=NOW, reason="linkedin_inbox_stale"), _utc) == (
        "14:00: sent nothing, linkedin inbox stale"
    )
