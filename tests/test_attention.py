"""``GET /attention``: the nav counts, the warning flags and the activity state (#508, #347).

Each count equals what its page lists, each clears the way its page clears it, another
user's rows never count, the activity state takes the first match in its order, and one
call costs a fixed number of statements. The endpoint answers a record, not a list, so
its two-user test is here rather than in ``tests/isolation``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import Settings
from netkeeper.crm import import_runs, triage
from netkeeper.db import session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.models import (
    Campaign,
    ContactMet,
    Enrollment,
    EnrollmentStatus,
    ImportStatus,
    Mailbox,
    MailboxStatus,
    Message,
    MessageDirection,
    MessageStatus,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    TemplateChannel,
    User,
    UserKind,
)
from netkeeper.scoping import get_scoped
from netkeeper.services import attention as service
from netkeeper.services import heat as heat_service
from netkeeper.services import inbox, linkedin_steps, posture, route_breaker, runs
from netkeeper.services.linkedin_accounts import (
    arm_scheduled_runs,
    ensure_account,
    pause_schedule,
)
from netkeeper.services.linkedin_session import clear_session_flag, flag_session

#: 11:00 in New York, inside the default LinkedIn active hours.
NOW = datetime(2026, 9, 16, 15, 0, tzinfo=UTC)
A = service.ActivityState

#: The most statements one call may cost: the service's own reads, and the endpoint's
#: with the request's user and settings. Measured at 19 and 21 on the dearest path; the
#: ceiling leaves headroom, and the data-independence test keeps the number flat.
SERVICE_STATEMENTS_MAX = 30
ENDPOINT_STATEMENTS_MAX = 32


@dataclass
class World:
    factory: sessionmaker[Session]
    user_id: int

    @contextmanager
    def write(self) -> Iterator[tuple[Session, User]]:
        with session_scope(self.factory, write=True) as session:
            user = session.get(User, self.user_id)
            assert user is not None
            yield session, user

    @contextmanager
    def read_session(self) -> Iterator[tuple[Session, User]]:
        with session_scope(self.factory) as session:
            user = session.get(User, self.user_id)
            assert user is not None
            yield session, user

    def read(self) -> service.Attention:
        with self.read_session() as (session, user):
            return service.attention(session, user, now=NOW, settings=Settings())


@pytest.fixture
def world(session_factory: sessionmaker[Session]) -> World:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session, timezone="America/New_York")
        user_id = user.id
    return World(session_factory, user_id)


def _linkedin_campaign(session: Session, user: User) -> Campaign:
    return factories.make_campaign(session, user, channels=(TemplateChannel.LINKEDIN,))


def _enroll(session: Session, campaign: Campaign, **overrides: Any) -> Enrollment:
    contact = factories.make_contact(session, campaign.user)
    return factories.make_enrollment(session, campaign, contact, **overrides)


def _seed_inbox(session: Session, user: User) -> dict[str, int]:
    """A reply, an unsubscribe and a bounce, one already handled reply, and a sent message."""
    enrollment = _enroll(session, factories.make_campaign(session, user))
    received: dict[str, Any] = {"direction": MessageDirection.IN, "status": MessageStatus.RECEIVED}
    reply = factories.make_message(session, enrollment, sent_at=NOW, **received)
    factories.make_message(session, enrollment, sent_at=NOW, asks_unsubscribe=True, **received)
    factories.make_message(session, enrollment, status=MessageStatus.BOUNCED, bounced_at=NOW)
    factories.make_message(session, enrollment, sent_at=NOW, handled_at=NOW, **received)
    factories.make_message(session, enrollment)
    return {"reply": reply.id}


def _seed_waiting(session: Session, user: User) -> dict[str, int]:
    """Prefilled, stale and partly typed LinkedIn messages; a sent one and an auto-sent one
    still on the inbox poll, which wait for nobody. (One open prefill per user, so the
    interrupted kind has its own test.)"""
    campaign = _linkedin_campaign(session, user)
    ids: dict[str, int] = {}
    for status in (MessageStatus.PREFILLED, MessageStatus.STALE):
        ids[status.value] = factories.make_message(
            session, _enroll(session, campaign), status=status, prefilled_at=NOW
        ).id
    ids["partly_typed"] = factories.make_message(
        session,
        _enroll(session, campaign),
        status=MessageStatus.FAILED,
        error="partially_typed: the composer closed",
    ).id
    factories.make_message(session, _enroll(session, campaign), status=MessageStatus.SENT)
    factories.make_message(
        session,
        _enroll(session, campaign),
        status=MessageStatus.STALE,
        prefilled_at=NOW,
        send_clicked_at=NOW,
    )
    return ids


def _seed_try_again(session: Session, user: User) -> dict[str, int]:
    """Two steps whose prefill typed nothing, and a due one that is only ready."""
    campaign = _linkedin_campaign(session, user)
    retry = {"not_sent_count": 1, "not_sent_error": "not_typed: the browser was busy"}
    first = _enroll(session, campaign, next_action_at=None, **retry)
    second = _enroll(session, campaign, next_action_at=None, **retry)
    _enroll(session, campaign, next_action_at=NOW - timedelta(hours=1))
    return {"first": first.id, "second": second.id}


def _seed_import(session: Session, user: User) -> int:
    return import_runs.create_run(
        session,
        user,
        filename="people.csv",
        content="First Name,Last Name\nHortensia,Blennerhassett\n",
        mapping={"First Name": "first_name", "Last Name": "last_name"},
    ).id


def _seed_everything(session: Session, user: User) -> None:
    _seed_inbox(session, user)
    _seed_waiting(session, user)
    _seed_try_again(session, user)
    _seed_import(session, user)
    _mailbox(session, user, MailboxStatus.REAUTH_REQUIRED)


def _mailbox(session: Session, user: User, status: MailboxStatus) -> Mailbox:
    mailbox = Mailbox(
        user_id=user.id,
        email=f"m{user.id}-{status.value}@example.test",
        keychain_ref=f"gmail-{user.id}-{status.value}",
        daily_cap=50,
        status=status,
    )
    session.add(mailbox)
    session.flush()
    return mailbox


# --- each count is its page's list --------------------------------------------------


def test_each_count_equals_its_pages_list(world: World) -> None:
    with world.write() as (session, user):
        _seed_everything(session, user)

    found = world.read()

    with world.read_session() as (session, user):
        page = inbox.list_inbox(session, user)
        _, waiting = linkedin_steps.waiting_for_you(session, user, limit=50)
        retries = linkedin_steps.try_again(session, user, now=NOW, settings=Settings())
        queue = triage.progress(session, user)
        _, drafts = import_runs.list_runs(session, user, status=ImportStatus.DRAFT)
    assert found.counts.inbox == page.unhandled == 3
    assert found.counts.waiting == waiting == 4
    assert found.counts.try_again == len(retries) == 2
    assert found.counts.dashboard == 6
    assert found.counts.triage == queue.remaining > 0
    assert found.counts.imports == drafts == 1
    assert found.title_total == 3 + 6 + 1, "Triage stays out of the tab total"
    assert found.flags.gmail == service.REAUTH_REQUIRED


def test_the_try_again_count_is_not_capped_at_the_page(world: World) -> None:
    with world.write() as (session, user):
        campaign = _linkedin_campaign(session, user)
        for _ in range(linkedin_steps.READY_PAGE_MAX + 1):
            _enroll(
                session,
                campaign,
                next_action_at=None,
                not_sent_count=1,
                not_sent_error="not_typed: nothing",
            )

    assert world.read().counts.try_again == linkedin_steps.READY_PAGE_MAX + 1


def test_nothing_to_do_reads_zero_and_idle(world: World) -> None:
    found = world.read()

    assert found.counts == service.Counts(0, 0, 0, 0, 0, 0)
    assert found.flags == service.Flags(None, None, None, None)
    assert found.activity == service.Activity(A.IDLE, service.IDLE, None, None)
    assert found.title_total == 0


# --- each clearing rule ---------------------------------------------------------------


def test_handling_an_inbox_item_clears_it(world: World) -> None:
    with world.write() as (session, user):
        reply = _seed_inbox(session, user)["reply"]
    with world.write() as (session, user):
        inbox.set_handled(session, user, reply, True)

    assert world.read().counts.inbox == 2


def test_sending_or_discarding_clears_a_waiting_message(world: World) -> None:
    with world.write() as (session, user):
        ids = _seed_waiting(session, user)
    assert world.read().counts.waiting == 4, "an auto-sent stale one waits for you too"

    with world.write() as (session, user):
        sent = get_scoped(session, user, Message, ids["prefilled"])
        assert sent is not None
        sent.status = MessageStatus.SENT
    with world.write() as (session, user):
        linkedin_steps.discard(session, user, ids["stale"], settings=Settings(), now=NOW)
        linkedin_steps.discard(session, user, ids["partly_typed"], settings=Settings(), now=NOW)

    with world.read_session() as (session, user):
        _, listed = linkedin_steps.waiting_for_you(session, user, limit=50)
    assert world.read().counts.waiting == listed == 1


def test_an_interrupted_message_counts_until_its_run_runs(world: World) -> None:
    """A claimed message whose run is not running waits for you; one whose run is still
    running does not."""
    with world.write() as (session, user):
        campaign = _linkedin_campaign(session, user)
        message = factories.make_message(
            session, _enroll(session, campaign), status=MessageStatus.SCHEDULED
        )
    assert world.read().counts.waiting == 1

    with world.write() as (session, user):
        run = linkedin_steps.start_message_send_run(session, user, NOW)
        row = get_scoped(session, user, Message, message.id)
        assert row is not None
        row.sync_run_id = run.id
    found = world.read()
    assert found.counts.waiting == 0
    assert found.activity.state is A.RUNNING
    assert found.activity.label == "A LinkedIn message run is running."


def test_a_retry_or_a_stopped_enrollment_clears_try_again(world: World) -> None:
    with world.write() as (session, user):
        ids = _seed_try_again(session, user)
    with world.write() as (session, user):
        retried = get_scoped(session, user, Enrollment, ids["first"])
        stopped = get_scoped(session, user, Enrollment, ids["second"])
        assert retried is not None and stopped is not None
        retried.not_sent_count = 0
        stopped.status = EnrollmentStatus.REMOVED

    found = world.read()
    assert found.counts.try_again == 0
    assert found.counts.dashboard == 0


def test_a_decision_clears_a_triage_contact(world: World) -> None:
    with world.write() as (session, user):
        first = factories.make_contact(session, user)
        factories.make_contact(session, user)
    assert world.read().counts.triage == 2

    with world.write() as (session, user):
        triage.decide(session, user, first.id, ContactMet.MET)
    assert world.read().counts.triage == 1


def test_deleting_a_draft_import_clears_it(world: World) -> None:
    with world.write() as (session, user):
        run_id = _seed_import(session, user)
    assert world.read().counts.imports == 1

    with world.write() as (session, user):
        import_runs.delete_run(session, user, run_id)
    assert world.read().counts.imports == 0


def test_only_a_mailbox_that_needs_signing_in_raises_the_gmail_flag(world: World) -> None:
    with world.write() as (session, user):
        _mailbox(session, user, MailboxStatus.OK)
        _mailbox(session, user, MailboxStatus.DISABLED)
    assert world.read().flags.gmail is None

    with world.write() as (session, user):
        mailbox = _mailbox(session, user, MailboxStatus.REAUTH_REQUIRED)
    assert world.read().flags.gmail == service.REAUTH_REQUIRED

    with world.write() as (session, user):
        row = get_scoped(session, user, Mailbox, mailbox.id)
        assert row is not None
        row.status = MailboxStatus.OK
    assert world.read().flags.gmail is None


def test_resuming_auto_send_clears_the_dashboard_flag(world: World) -> None:
    with world.write() as (session, user):
        account = ensure_account(session, user).id
        linkedin_steps.hold_auto_send(
            session, user, account, reason=linkedin_steps.AUTO_SEND_HOLD_BUBBLE, now=NOW, run_id=1
        )
    found = world.read()
    assert found.flags.dashboard == service.AUTO_SEND_HELD
    assert found.activity == service.Activity(
        A.STOPPED, service.AUTO_SEND_HELD_ACTIVITY, None, None
    ), "the hold blocks auto-send only, and says so"
    assert found.title_total == 0, "a flag is a dot, not a number"

    with world.write() as (session, user):
        linkedin_steps.resume_auto_send(session, user, account)
    assert world.read().flags.dashboard is None


def test_clearing_the_session_flag_or_heat_clears_the_linkedin_flag(world: World) -> None:
    with world.write() as (session, user):
        flag_session(session, user, Outcome.CHECKPOINT, url="/checkpoint/challenge")
    assert world.read().flags.linkedin == service.SESSION_FLAGGED

    heat = Settings().linkedin.heat
    with world.write() as (session, user):
        clear_session_flag(session, user)
        account = ensure_account(session, user).id
        while not heat_service.should_skip(session, user, account, now=NOW, settings=heat):
            heat_service.raise_heat(session, user, account, now=NOW, settings=heat)
    assert world.read().flags.linkedin == service.HEAT_TRIPPED

    with world.write() as (session, user):
        heat_service.clear(session, user, account, now=NOW)
    assert world.read().flags.linkedin is None


def _trip_route(session: Session, user: User, account: int) -> None:
    for _ in range(3):
        route_breaker.record(session, user, account, route_changed=True, succeeded=False, now=NOW)


def _trip_answer_lost(session: Session, user: User, account: int) -> None:
    for _ in range(3):
        route_breaker.record_answer_lost(
            session,
            user,
            account,
            kind=SyncRunKind.CONNECTIONS_FULL,
            answer_lost=True,
            clean_end=False,
            now=NOW,
        )


def _trip_contact_info(session: Session, user: User, account: int) -> None:
    for _ in range(3):
        route_breaker.record_contact_info(
            session, user, account, answer_lost=True, clean_end=False, now=NOW
        )


def _trip_inbox(session: Session, user: User, account: int) -> None:
    for _ in range(2):
        route_breaker.record_inbox(
            session, user, account, route_changed=True, completed=False, now=NOW
        )


def _trip_inbox_owner(session: Session, user: User, account: int) -> None:
    for _ in range(2):
        route_breaker.record_inbox_owner(
            session, user, account, owner_mismatch=True, completed=False, now=NOW
        )


@pytest.mark.parametrize(
    "trip", [_trip_route, _trip_answer_lost, _trip_contact_info, _trip_inbox, _trip_inbox_owner]
)
def test_each_tripped_breaker_raises_the_settings_flag_until_reset(world: World, trip: Any) -> None:
    with world.write() as (session, user):
        account = ensure_account(session, user).id
        trip(session, user, account)
    found = world.read()
    assert found.flags.settings == service.BREAKER_TRIPPED
    assert found.activity == service.Activity(A.STOPPED, service.BREAKER_TRIPPED, None, None)

    with world.write() as (session, user):
        route_breaker.reset(session, user, account)
    assert world.read().flags.settings is None


# --- another user's rows -----------------------------------------------------------------


def test_another_users_rows_never_count(world: World) -> None:
    with world.write() as (session, _):
        other = factories.make_user(session, UserKind.HOSTED)
        _seed_everything(session, other)
        account = ensure_account(session, other).id
        flag_session(session, other, Outcome.CHECKPOINT, url="/checkpoint/challenge")
        linkedin_steps.hold_auto_send(
            session, other, account, reason=linkedin_steps.AUTO_SEND_HOLD_BUBBLE, now=NOW, run_id=1
        )
        _trip_route(session, other, account)
        arm_scheduled_runs(session, other, now=NOW - timedelta(days=1))
        runs.create_run(session, other, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW)

    found = world.read()

    assert found.counts == service.Counts(0, 0, 0, 0, 0, 0)
    assert found.flags == service.Flags(None, None, None, None)
    assert found.activity.state is A.IDLE


# --- the activity state --------------------------------------------------------------------


def test_the_activity_state_takes_the_first_match(world: World) -> None:
    with world.write() as (session, user):
        account = ensure_account(session, user).id
        arm_scheduled_runs(session, user, now=NOW - timedelta(days=1))
    assert world.read().activity == service.Activity(A.ARMED, service.ARMED, None, None)

    with world.write() as (session, user):
        pause_schedule(session, user, now=NOW)
    assert world.read().activity.state is A.PAUSED

    with world.write() as (session, user):
        run = runs.create_run(
            session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
        )
        flag_session(session, user, Outcome.CHECKPOINT, url="/checkpoint/challenge")
    running = world.read().activity
    assert running.state is A.RUNNING, "a run in progress outranks the flag that will stop it"
    assert (running.run_id, running.run_kind) == (run.id, SyncRunKind.ENRICH)
    assert running.label == "A LinkedIn enrichment run is running."

    with world.write() as (session, user):
        runs.finish_run(session, user, run.id, status=SyncRunStatus.ABORTED, now=NOW)
    assert world.read().activity == service.Activity(A.STOPPED, service.SESSION_FLAGGED, None, None)

    with world.write() as (session, user):
        clear_session_flag(session, user)
    assert world.read().activity.state is A.PAUSED, "stopped outranks paused"
    assert account


def test_a_disarmed_account_is_idle(world: World) -> None:
    with world.write() as (session, user):
        ensure_account(session, user)

    assert world.read().activity.state is A.IDLE


def test_every_run_kind_has_a_name() -> None:
    assert set(service._RUN_NAMES) == set(SyncRunKind)


def test_flag_labels_are_plain_sentences() -> None:
    labels = (
        service.AUTO_SEND_HELD,
        service.AUTO_SEND_HELD_ACTIVITY,
        service.REAUTH_REQUIRED,
        service.SESSION_FLAGGED,
        service.HEAT_TRIPPED,
        service.BREAKER_TRIPPED,
        service.PAUSED,
        service.ARMED,
        service.IDLE,
    )
    for label in labels:
        assert label[0].isupper() and label.endswith("."), label
        assert "{" not in label, label


# --- the cost of one call ------------------------------------------------------------------


class _Statements:
    def __init__(self, engine: Engine) -> None:
        self.count = 0
        self.engine = engine
        event.listen(engine, "before_cursor_execute", self._record)

    def _record(self, _conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        if not statement.lstrip().upper().startswith(("PRAGMA", "BEGIN", "SAVEPOINT")):
            self.count += 1

    def close(self) -> None:
        event.remove(self.engine, "before_cursor_execute", self._record)


def _count_statements(world: World, engine: Engine) -> tuple[int, service.Attention]:
    with world.read_session() as (session, user):
        statements = _Statements(engine)
        try:
            found = service.attention(session, user, now=NOW, settings=Settings())
        finally:
            statements.close()
    return statements.count, found


def test_one_call_costs_a_bounded_number_of_statements(world: World, engine: Engine) -> None:
    """Measured with every count non-zero and no breaker or flag up, so no ``or`` stops
    early and every activity branch is read (the dearest path)."""
    with world.write() as (session, user):
        _seed_everything(session, user)
        ensure_account(session, user)

    count, found = _count_statements(world, engine)

    assert found.activity.state is A.IDLE
    assert count <= SERVICE_STATEMENTS_MAX, count


def _seed_rows(session: Session, user: User, n: int) -> None:
    """``n`` rows of each kind a count reads: an unhandled reply, a message waiting for
    you, a step waiting for Try again, a draft import, and their contacts."""
    campaign = _linkedin_campaign(session, user)
    for _ in range(n):
        factories.make_message(
            session,
            _enroll(session, campaign),
            direction=MessageDirection.IN,
            status=MessageStatus.RECEIVED,
            sent_at=NOW,
        )
        factories.make_message(
            session, _enroll(session, campaign), status=MessageStatus.STALE, prefilled_at=NOW
        )
        _enroll(
            session,
            campaign,
            next_action_at=None,
            not_sent_count=1,
            not_sent_error="not_typed: nothing",
        )
        _seed_import(session, user)


def test_the_statement_count_does_not_grow_with_the_rows(world: World, engine: Engine) -> None:
    with world.write() as (session, user):
        ensure_account(session, user)
        _seed_rows(session, user, 1)
    one, found_one = _count_statements(world, engine)

    with world.write() as (session, user):
        _seed_rows(session, user, 9)
    ten, found_ten = _count_statements(world, engine)

    assert (found_one.counts.inbox, found_ten.counts.inbox) == (1, 10)
    assert (found_one.counts.waiting, found_ten.counts.waiting) == (1, 10)
    assert (found_one.counts.try_again, found_ten.counts.try_again) == (1, 10)
    assert (found_one.counts.imports, found_ten.counts.imports) == (1, 10)
    assert one == ten


@pytest.mark.parametrize("raises", [0, 1, 50])
@pytest.mark.parametrize("half_life_hours", [6, 0])
def test_the_heat_flag_reads_as_the_posture_report_does(
    world: World, raises: int, half_life_hours: int
) -> None:
    """The flag skips the score and multiplier reads, and still says what
    ``posture.heat_status(...).tripped`` says: cold, warm, hot, and a zero half-life."""
    base = Settings()
    settings = replace(
        base,
        linkedin=replace(
            base.linkedin, heat=replace(base.linkedin.heat, half_life_hours=half_life_hours)
        ),
    )
    with world.write() as (session, user):
        account = ensure_account(session, user).id
        for _ in range(raises):
            heat_service.raise_heat(session, user, account, now=NOW, settings=base.linkedin.heat)

    with world.read_session() as (session, user):
        report = posture.heat_status(session, user, account, now=NOW, settings=settings.linkedin)
        flag = service._heat_tripped(session, user, account, now=NOW, settings=settings)
    assert flag is report.tripped
    if half_life_hours and raises == 50:
        assert flag, "fifty raises trip it"


# --- the endpoint ----------------------------------------------------------------------------


async def test_get_attention(app: FastAPI, client: httpx.AsyncClient, bare_engine: Engine) -> None:
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        _seed_everything(session, user)
        ensure_account(session, user)

    statements = _Statements(bare_engine)
    try:
        response = await client.get("/api/v1/attention")
    finally:
        statements.close()

    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {
        "inbox": 3,
        "dashboard": 6,
        "waiting": 4,
        "try_again": 2,
        "triage": body["counts"]["triage"],
        "imports": 1,
    }
    assert body["counts"]["triage"] > 0
    assert body["flags"] == {
        "dashboard": None,
        "gmail": service.REAUTH_REQUIRED,
        "linkedin": None,
        "settings": None,
    }
    assert body["activity"] == {
        "state": "idle",
        "label": service.IDLE,
        "run_id": None,
        "run_kind": None,
    }
    assert body["title_total"] == 10
    assert statements.count <= ENDPOINT_STATEMENTS_MAX, statements.count


async def test_get_attention_never_counts_another_users_rows(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """The endpoint's two-user test. ``GET /attention`` is a record, not a list, so the
    isolation registry (lists only) does not take it."""
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        other = factories.make_user(session, UserKind.HOSTED)
        _seed_everything(session, other)
        account = ensure_account(session, other).id
        flag_session(session, other, Outcome.CHECKPOINT, url="/checkpoint/challenge")
        _trip_route(session, other, account)
        runs.create_run(session, other, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW)

    body = (await client.get("/api/v1/attention")).json()

    assert body["counts"] == dict.fromkeys(
        ("inbox", "dashboard", "waiting", "try_again", "triage", "imports"), 0
    )
    assert body["flags"] == dict.fromkeys(("dashboard", "gmail", "linkedin", "settings"))
    assert body["activity"]["state"] == "idle"
    assert body["title_total"] == 0
