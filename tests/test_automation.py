"""One automation switch per service: Off, Manual, Automatic (#829).

The positions map onto arming: LinkedIn Automatic is armed, Manual is disarmed, Off is
disarmed and refused; Gmail Automatic is armed to send, Manual armed for drafts, Off not
armed. Every mailbox here is invented, and no test opens Gmail or a browser.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import factories
import httpx
import pytest
from campaign_fakes import make_mailbox
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeep.db import session_scope
from netkeep.models import (
    LinkedInAccount,
    Mailbox,
    MailboxArm,
    MailboxStatus,
    ScheduleDepth,
    SyncRunKind,
    SyncRunTrigger,
    User,
)
from netkeep.scoping import scoped
from netkeep.services import automation, runs, scheduler
from netkeep.services.automation import Position
from netkeep.services.linkedin_accounts import (
    arm_scheduled_runs,
    ensure_account,
    full_upkeep,
    linkedin_off,
    pause_schedule,
    schedule_paused,
    scheduled_runs_armed,
)
from netkeep.services.scheduler import JobKind

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
CSRF = {"X-Netkeep-Client": "1"}


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


def _account(session: Session, user: User) -> LinkedInAccount:
    return ensure_account(session, user)


# --- the pinned literals ---------------------------------------------------------------


def test_full_upkeep_is_the_full_sync_and_enrichment_and_never_auto_send() -> None:
    assert frozenset({"connections_full", "enrich"}) == {
        k.value for k in scheduler.FULL_UPKEEP_KINDS
    }
    assert frozenset({"connections_full", "enrich"}) == {
        k.value for k in runs.FULL_UPKEEP_RUN_KINDS
    }
    assert JobKind.AUTO_SEND not in scheduler.FULL_UPKEEP_KINDS


# --- LinkedIn: reading the position -----------------------------------------------------


def test_linkedin_without_an_account_is_manual_at_full_depth(writer: Session, user: User) -> None:
    state = automation.linkedin_state(writer, user)
    assert (state.position, state.depth, state.changed_at) == (
        Position.MANUAL,
        ScheduleDepth.FULL,
        None,
    )


def test_linkedin_armed_is_automatic_and_armed_and_paused_is_manual(
    writer: Session, user: User
) -> None:
    arm_scheduled_runs(writer, user, now=NOW)
    assert automation.linkedin_state(writer, user).position is Position.AUTOMATIC
    pause_schedule(writer, user, now=NOW)
    assert automation.linkedin_state(writer, user).position is Position.MANUAL


# --- LinkedIn: moving the switch ---------------------------------------------------------


def test_linkedin_automatic_needs_confirm_and_changes_nothing_without_it(
    writer: Session, user: User
) -> None:
    with pytest.raises(automation.ConfirmRequired, match="confirm"):
        automation.set_linkedin(writer, user, Position.AUTOMATIC, confirm=False, now=NOW)
    account = _account(writer, user)
    assert account.scheduled_runs_armed_at is None
    assert account.automation_changed_at is None


def test_linkedin_automatic_with_confirm_arms_at_the_chosen_depth(
    writer: Session, user: User
) -> None:
    state = automation.set_linkedin(
        writer, user, Position.AUTOMATIC, ScheduleDepth.QUICK, confirm=True, now=NOW
    )
    account = _account(writer, user)
    assert (state.position, state.depth, state.changed_at) == (
        Position.AUTOMATIC,
        ScheduleDepth.QUICK,
        NOW,
    )
    assert scheduled_runs_armed(writer, user, account.id)
    assert not full_upkeep(writer, user, account.id)


def test_quick_to_full_needs_confirm_whatever_the_position(writer: Session, user: User) -> None:
    automation.set_linkedin(
        writer, user, Position.MANUAL, ScheduleDepth.QUICK, confirm=False, now=NOW
    )
    with pytest.raises(automation.ConfirmRequired, match="Full upkeep"):
        automation.set_linkedin(
            writer, user, Position.MANUAL, ScheduleDepth.FULL, confirm=False, now=NOW
        )
    assert automation.linkedin_state(writer, user).depth is ScheduleDepth.QUICK
    automation.set_linkedin(
        writer, user, Position.MANUAL, ScheduleDepth.FULL, confirm=True, now=NOW
    )
    assert automation.linkedin_state(writer, user).depth is ScheduleDepth.FULL


def test_moving_left_never_needs_confirm(writer: Session, user: User) -> None:
    automation.set_linkedin(writer, user, Position.AUTOMATIC, confirm=True, now=NOW)
    later = NOW + timedelta(hours=1)
    state = automation.set_linkedin(
        writer, user, Position.AUTOMATIC, ScheduleDepth.QUICK, confirm=False, now=later
    )
    assert (state.position, state.depth) == (Position.AUTOMATIC, ScheduleDepth.QUICK)
    state = automation.set_linkedin(writer, user, Position.MANUAL, confirm=False, now=later)
    assert state.position is Position.MANUAL
    state = automation.set_linkedin(writer, user, Position.OFF, confirm=False, now=later)
    assert (state.position, state.changed_at) == (Position.OFF, later)
    account = _account(writer, user)
    assert account.scheduled_runs_armed_at is None
    assert account.automation_off_at == later
    assert linkedin_off(writer, user)


def test_manual_clears_off_and_keeps_the_schedule_disarmed(writer: Session, user: User) -> None:
    automation.set_linkedin(writer, user, Position.OFF, confirm=False, now=NOW)
    state = automation.set_linkedin(writer, user, Position.MANUAL, confirm=False, now=NOW)
    account = _account(writer, user)
    assert state.position is Position.MANUAL
    assert account.automation_off_at is None and account.scheduled_runs_armed_at is None
    assert not linkedin_off(writer, user)


def test_off_to_automatic_needs_confirm_and_leaves_off(writer: Session, user: User) -> None:
    automation.set_linkedin(writer, user, Position.OFF, confirm=False, now=NOW)
    with pytest.raises(automation.ConfirmRequired):
        automation.set_linkedin(writer, user, Position.AUTOMATIC, confirm=False, now=NOW)
    assert automation.linkedin_state(writer, user).position is Position.OFF
    state = automation.set_linkedin(writer, user, Position.AUTOMATIC, confirm=True, now=NOW)
    assert state.position is Position.AUTOMATIC
    assert _account(writer, user).automation_off_at is None


def test_arming_the_old_way_takes_linkedin_out_of_off(writer: Session, user: User) -> None:
    """Off is always disarmed: ``linkedin schedule arm`` still works this release."""
    automation.set_linkedin(writer, user, Position.OFF, confirm=False, now=NOW)
    arm_scheduled_runs(writer, user, now=NOW)
    assert automation.linkedin_state(writer, user).position is Position.AUTOMATIC
    assert not linkedin_off(writer, user)


def test_any_move_clears_a_legacy_pause(writer: Session, user: User) -> None:
    arm_scheduled_runs(writer, user, now=NOW)
    pause_schedule(writer, user, now=NOW)
    account = _account(writer, user)
    with pytest.raises(automation.ConfirmRequired):  # Manual to Automatic does more
        automation.set_linkedin(writer, user, Position.AUTOMATIC, confirm=False, now=NOW)
    assert schedule_paused(writer, user, account.id)
    automation.set_linkedin(writer, user, Position.MANUAL, confirm=False, now=NOW)
    assert not schedule_paused(writer, user, account.id)
    assert account.scheduled_runs_armed_at is None


def test_the_switch_never_turns_on_linkedin_auto_send(writer: Session, user: User) -> None:
    from netkeep.config import Settings
    from netkeep.services import ui_settings

    automation.set_linkedin(writer, user, Position.AUTOMATIC, confirm=True, now=NOW)
    assert not ui_settings.auto_send_on(writer, user, Settings())


# --- the depth gate ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "skipped"),
    [
        (JobKind.CONNECTIONS_FULL, "quick_checks"),
        (JobKind.ENRICH, "quick_checks"),
        (JobKind.CONNECTIONS_INCREMENTAL, None),
        (JobKind.INBOX, None),
    ],
)
def test_quick_checks_skip_only_the_full_upkeep_kinds(
    writer: Session, user: User, kind: JobKind, skipped: str | None
) -> None:
    automation.set_linkedin(
        writer, user, Position.AUTOMATIC, ScheduleDepth.QUICK, confirm=True, now=NOW
    )
    account = _account(writer, user)
    reason = scheduler.gate_skip_reason(
        writer, user, account.id, kind, now=NOW, heat_settings=scheduler.HEAT_SKIP_DISABLED
    )
    assert reason == skipped


@pytest.mark.parametrize("kind", [JobKind.CONNECTIONS_FULL, JobKind.ENRICH])
def test_full_upkeep_fires_every_kind(writer: Session, user: User, kind: JobKind) -> None:
    automation.set_linkedin(
        writer, user, Position.AUTOMATIC, ScheduleDepth.FULL, confirm=True, now=NOW
    )
    account = _account(writer, user)
    reason = scheduler.gate_skip_reason(
        writer, user, account.id, kind, now=NOW, heat_settings=scheduler.HEAT_SKIP_DISABLED
    )
    assert reason is None


def test_the_depth_gate_reads_quick_for_an_account_it_cannot_find(
    writer: Session, user: User
) -> None:
    assert not full_upkeep(writer, user, 999)


@pytest.mark.parametrize("kind", [SyncRunKind.CONNECTIONS_FULL, SyncRunKind.ENRICH])
def test_a_scheduled_full_upkeep_run_is_refused_at_quick_checks(
    writer: Session, user: User, kind: SyncRunKind
) -> None:
    """The second check, past the scheduler's gate: no run is recorded."""
    automation.set_linkedin(
        writer, user, Position.AUTOMATIC, ScheduleDepth.QUICK, confirm=True, now=NOW
    )
    with pytest.raises(runs.QuickChecksOnly, match="quick checks"):
        runs.create_run(writer, user, kind, trigger=SyncRunTrigger.SCHEDULED, now=NOW)
    # A run a person starts is not the schedule's: depth doesn't touch it.
    run = runs.create_run(writer, user, kind, trigger=SyncRunTrigger.MANUAL, now=NOW)
    assert run.id is not None


@pytest.mark.parametrize("kind", [SyncRunKind.CONNECTIONS_INCREMENTAL, SyncRunKind.INBOX])
def test_quick_checks_still_record_the_quick_kinds(
    writer: Session, user: User, kind: SyncRunKind
) -> None:
    automation.set_linkedin(
        writer, user, Position.AUTOMATIC, ScheduleDepth.QUICK, confirm=True, now=NOW
    )
    run = runs.create_run(writer, user, kind, trigger=SyncRunTrigger.SCHEDULED, now=NOW)
    assert run.id is not None


async def test_poll_and_fire_never_calls_the_handler_at_quick_checks(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        automation.set_linkedin(
            session, user, Position.AUTOMATIC, ScheduleDepth.QUICK, confirm=True, now=NOW
        )
        account_id = _account(session, user).id
        schedule = scheduler.DEFAULT_SCHEDULES[JobKind.ENRICH]
        scheduler.establish_schedule(
            session,
            user,
            account_id,
            JobKind.ENRICH,
            now=NOW - schedule.interval,
            schedule=schedule,
            rng=random.Random(0),
            tz="UTC",
        )
        session.expunge(user)
    called: list[Any] = []

    async def handler(ctx: scheduler.JobContext) -> None:
        called.append(ctx)

    result = await scheduler.poll_and_fire(
        session_factory,
        user,
        account_id,
        JobKind.ENRICH,
        now=NOW + timedelta(minutes=1),
        schedule=scheduler.DEFAULT_SCHEDULES[JobKind.ENRICH],
        registry={JobKind.ENRICH: handler},
        heat_settings=scheduler.HEAT_SKIP_DISABLED,
        tz="UTC",
    )
    assert called == []
    assert result is not None and result.skipped_reason == "quick_checks"


# --- Gmail --------------------------------------------------------------------------------


def _mailbox(session: Session, user: User, **overrides: Any) -> Mailbox:
    return make_mailbox(session, user, **overrides)


def _verified(session: Session, user: User, **overrides: Any) -> Mailbox:
    return _mailbox(session, user, message_id_verified_at=NOW - timedelta(days=1), **overrides)


def _arms(session: Session, user: User) -> list[MailboxArm | None]:
    return [m.arm for m in session.scalars(scoped(user, Mailbox).order_by(Mailbox.id))]


@pytest.mark.parametrize(
    ("armed_at", "send_armed_at", "position"),
    [
        (None, None, Position.OFF),
        (NOW, None, Position.MANUAL),
        (NOW, NOW, Position.AUTOMATIC),
    ],
)
def test_gmail_position_is_the_mailbox_arming(
    writer: Session,
    user: User,
    armed_at: datetime | None,
    send_armed_at: datetime | None,
    position: Position,
) -> None:
    _verified(writer, user, armed_at=armed_at, send_armed_at=send_armed_at)
    assert automation.gmail_state(writer, user, now=NOW).position is position


def test_gmail_without_a_mailbox_is_off_and_refuses_moving_right(
    writer: Session, user: User
) -> None:
    _mailbox(writer, user, status=MailboxStatus.DISABLED)  # a disconnected one doesn't count
    state = automation.gmail_state(writer, user, now=NOW)
    assert (state.position, state.automatic_ready) == (Position.OFF, False)
    assert state.blocked_reason == "Connect a Gmail mailbox first"
    for position in (Position.MANUAL, Position.AUTOMATIC):
        with pytest.raises(automation.AutomationRefused, match="Connect a Gmail mailbox"):
            automation.set_gmail(writer, user, position, confirm=True, by="test", now=NOW)
    assert _arms(writer, user) == [None]


def test_gmail_manual_arms_for_drafts_without_confirm(writer: Session, user: User) -> None:
    _mailbox(writer, user)
    state = automation.set_gmail(writer, user, Position.MANUAL, confirm=False, by="test", now=NOW)
    assert (state.position, state.changed_at) == (Position.MANUAL, NOW)
    assert _arms(writer, user) == [MailboxArm.DRAFT]


def test_gmail_automatic_is_refused_until_the_message_id_check_passed(
    writer: Session, user: User
) -> None:
    _mailbox(writer, user, armed_at=NOW)
    state = automation.gmail_state(writer, user, now=NOW)
    assert not state.automatic_ready
    assert state.blocked_reason is not None and "Message-ID" in state.blocked_reason
    with pytest.raises(automation.AutomationRefused, match="Message-ID") as refused:
        automation.set_gmail(writer, user, Position.AUTOMATIC, confirm=True, by="test", now=NOW)
    assert not isinstance(refused.value, automation.ConfirmRequired)
    assert _arms(writer, user) == [MailboxArm.DRAFT]


def test_gmail_off_says_to_move_to_manual_before_the_check(writer: Session, user: User) -> None:
    _mailbox(writer, user)
    reason = automation.gmail_state(writer, user, now=NOW).blocked_reason
    assert reason is not None and "move Gmail to Manual first" in reason


def test_gmail_automatic_needs_confirm(writer: Session, user: User) -> None:
    _verified(writer, user, armed_at=NOW)
    with pytest.raises(automation.ConfirmRequired, match="confirm"):
        automation.set_gmail(writer, user, Position.AUTOMATIC, confirm=False, by="test", now=NOW)
    assert _arms(writer, user) == [MailboxArm.DRAFT]


def test_gmail_automatic_from_off_arms_drafts_then_send(writer: Session, user: User) -> None:
    _verified(writer, user)
    state = automation.set_gmail(writer, user, Position.AUTOMATIC, confirm=True, by="test", now=NOW)
    assert (state.position, state.automatic_ready, state.blocked_reason) == (
        Position.AUTOMATIC,
        True,
        None,
    )
    assert _arms(writer, user) == [MailboxArm.SEND]


def test_gmail_moving_left_needs_no_confirm(writer: Session, user: User) -> None:
    _verified(writer, user, armed_at=NOW, send_armed_at=NOW)
    automation.set_gmail(writer, user, Position.MANUAL, confirm=False, by="test", now=NOW)
    assert _arms(writer, user) == [MailboxArm.DRAFT]
    automation.set_gmail(writer, user, Position.OFF, confirm=False, by="test", now=NOW)
    assert _arms(writer, user) == [None]


def test_gmail_switch_leaves_a_disconnected_mailbox_alone(writer: Session, user: User) -> None:
    _mailbox(writer, user, email="old@example.test", status=MailboxStatus.DISABLED)
    _verified(writer, user, email="new@example.test")
    automation.set_gmail(writer, user, Position.AUTOMATIC, confirm=True, by="test", now=NOW)
    assert _arms(writer, user) == [None, MailboxArm.SEND]


def test_gmail_a_move_that_changes_nothing_keeps_the_changed_time(
    writer: Session, user: User
) -> None:
    _mailbox(writer, user)
    automation.set_gmail(writer, user, Position.MANUAL, confirm=False, by="test", now=NOW)
    later = NOW + timedelta(hours=2)
    state = automation.set_gmail(writer, user, Position.MANUAL, confirm=False, by="test", now=later)
    assert state.changed_at == NOW


# --- the API ------------------------------------------------------------------------------


def _local(app: FastAPI) -> Iterator[tuple[Session, User]]:
    with session_scope(app.state.session_factory, write=True) as session:
        yield session, session.scalars(select(User)).one()


async def test_get_answers_both_switches(running_app: FastAPI, client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/automation")
    assert response.status_code == 200
    assert response.json() == {
        "linkedin": {"position": "manual", "depth": "full", "changed_at": None},
        "gmail": {
            "position": "off",
            "automatic_ready": False,
            "blocked_reason": "Connect a Gmail mailbox first",
            "changed_at": None,
        },
    }


async def test_put_linkedin_automatic_needs_confirm(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    refused = await client.put(
        "/api/v1/automation/linkedin", json={"position": "automatic"}, headers=CSRF
    )
    assert refused.status_code == 422 and "confirm" in refused.json()["detail"]
    ok = await client.put(
        "/api/v1/automation/linkedin",
        json={"position": "automatic", "depth": "quick", "confirm": True},
        headers=CSRF,
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["linkedin"]["position"] == "automatic" and body["linkedin"]["depth"] == "quick"
    assert body["linkedin"]["changed_at"] is not None
    assert set(body) == {"linkedin", "gmail"}
    for session, user in _local(running_app):
        assert scheduled_runs_armed(session, user, _account(session, user).id)
    left = await client.put("/api/v1/automation/linkedin", json={"position": "off"}, headers=CSRF)
    assert left.status_code == 200 and left.json()["linkedin"]["position"] == "off"


async def test_put_refuses_unknown_fields_and_positions(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    extra = await client.put(
        "/api/v1/automation/linkedin",
        json={"position": "manual", "auto_send": True},
        headers=CSRF,
    )
    unknown = await client.put(
        "/api/v1/automation/gmail", json={"position": "always"}, headers=CSRF
    )
    assert (extra.status_code, unknown.status_code) == (422, 422)


async def test_put_needs_the_csrf_header(running_app: FastAPI, client: httpx.AsyncClient) -> None:
    response = await client.put(
        "/api/v1/automation/linkedin", json={"position": "automatic", "confirm": True}
    )
    assert response.status_code == 403
    for session, user in _local(running_app):
        assert automation.linkedin_state(session, user).position is Position.MANUAL


async def test_the_old_arm_endpoint_takes_linkedin_out_of_off(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    off = await client.put("/api/v1/automation/linkedin", json={"position": "off"}, headers=CSRF)
    assert off.json()["linkedin"]["position"] == "off"
    armed = await client.post("/api/v1/linkedin/schedule/arm", json={"confirm": True}, headers=CSRF)
    assert armed.status_code == 200 and armed.json()["armed"] is True
    switch = (await client.get("/api/v1/automation")).json()["linkedin"]
    assert switch["position"] == "automatic"
    for session, user in _local(running_app):
        assert not linkedin_off(session, user)


async def test_put_gmail_automatic_is_409_with_the_reason_until_ready(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    for session, user in _local(running_app):
        _mailbox(session, user, armed_at=NOW)
    refused = await client.put(
        "/api/v1/automation/gmail", json={"position": "automatic", "confirm": True}, headers=CSRF
    )
    assert refused.status_code == 409
    gmail = (await client.get("/api/v1/automation")).json()["gmail"]
    assert gmail["position"] == "manual" and gmail["automatic_ready"] is False
    assert refused.json()["detail"] == gmail["blocked_reason"]
    for session, user in _local(running_app):
        session.scalars(scoped(user, Mailbox)).one().message_id_verified_at = NOW
    unconfirmed = await client.put(
        "/api/v1/automation/gmail", json={"position": "automatic"}, headers=CSRF
    )
    assert unconfirmed.status_code == 422
    ok = await client.put(
        "/api/v1/automation/gmail", json={"position": "automatic", "confirm": True}, headers=CSRF
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["gmail"] == {
        "position": "automatic",
        "automatic_ready": True,
        "blocked_reason": None,
        "changed_at": ok.json()["gmail"]["changed_at"],
    }
    off = await client.put("/api/v1/automation/gmail", json={"position": "off"}, headers=CSRF)
    assert off.status_code == 200 and off.json()["gmail"]["position"] == "off"


@pytest.mark.parametrize("path", ["disarm", "pause"])
@pytest.mark.parametrize(
    ("start", "end"), [("automatic", "manual"), ("manual", "manual"), ("off", "off")]
)
async def test_the_old_stop_endpoints_never_do_more(
    running_app: FastAPI, client: httpx.AsyncClient, path: str, start: str, end: str
) -> None:
    """#829 review: a stop is the left of the current position and Manual."""
    moved = await client.put(
        "/api/v1/automation/linkedin", json={"position": start, "confirm": True}, headers=CSRF
    )
    assert moved.status_code == 200, moved.text
    stopped = await client.post(f"/api/v1/linkedin/schedule/{path}", headers=CSRF)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["armed"] is False
    switch = (await client.get("/api/v1/automation")).json()["linkedin"]
    assert switch["position"] == end


def test_stop_linkedin_from_off_changes_nothing(writer: Session, user: User) -> None:
    automation.set_linkedin(writer, user, Position.OFF, confirm=False, now=NOW)
    later = NOW + timedelta(hours=1)
    state = automation.stop_linkedin(writer, user, now=later)
    assert (state.position, state.changed_at) == (Position.OFF, NOW)


@pytest.mark.parametrize("start", ["armed", "off"])
async def test_unpausing_a_legacy_pause_never_does_more_and_dates_the_switch(
    running_app: FastAPI, client: httpx.AsyncClient, start: str
) -> None:
    """A pause left from before 0049 reads as Manual. Unpausing deletes it and stays
    Manual (disarmed, not Automatic again); from Off it stays Off. Either way it's dated."""
    for session, user in _local(running_app):
        if start == "off":
            automation.set_linkedin(session, user, Position.OFF, confirm=False, now=NOW)
        else:
            arm_scheduled_runs(session, user, now=NOW)
        pause_schedule(session, user, now=NOW)
        _account(session, user).automation_changed_at = None
    response = await client.post("/api/v1/linkedin/schedule/unpause", headers=CSRF)
    assert response.status_code == 200, response.text
    assert response.json()["armed"] is False
    for session, user in _local(running_app):
        account = _account(session, user)
        assert not schedule_paused(session, user, account.id)
        assert account.scheduled_runs_armed_at is None
        assert account.automation_changed_at is not None
        expected = Position.OFF if start == "off" else Position.MANUAL
        assert automation.linkedin_state(session, user).position is expected


async def test_the_old_arm_endpoint_keeps_the_depth(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    quick = await client.put(
        "/api/v1/automation/linkedin", json={"position": "manual", "depth": "quick"}, headers=CSRF
    )
    assert quick.status_code == 200, quick.text
    armed = await client.post("/api/v1/linkedin/schedule/arm", json={"confirm": True}, headers=CSRF)
    assert armed.status_code == 200, armed.text
    switch = (await client.get("/api/v1/automation")).json()["linkedin"]
    assert (switch["position"], switch["depth"]) == ("automatic", "quick")
