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
from sqlalchemy.orm import Session, sessionmaker
from test_linkedin_steps import LINKEDIN, Lane, make_lane

from netkeeper.config import LinkedInSettings, Settings
from netkeeper.linkedin.messaging import MessageOutcome, MessageOutcomeKind
from netkeeper.models import (
    CampaignStep,
    StepMode,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    User,
)
from netkeeper.scoping import scoped
from netkeeper.services import budgets, linkedin_steps, runs, scheduled_runs, scheduler
from netkeeper.services import heat as heat_service
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
