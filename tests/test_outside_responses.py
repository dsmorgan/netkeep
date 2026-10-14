"""A response that came outside netkeeper, recorded by a person (#512).

The first half pins what recording one does: which enrollments end, what is written, the
refusals, the inbox and the results. The second half pins that nothing sends after it on
any path, and that it ends an enrollment exactly as a detected reply does:

- an email follow-up: the tick, and the Gmail sender's read just before it sends;
- the LinkedIn queue: the ready list and a person's prefill claim;
- auto-send: the scheduler's claim, and its read just before it clicks Send;
- an open prefill: left as a detected reply leaves it, and discarding or sending it
  never moves the ended enrollment on.

Every contact, address, URN and note is invented. Nothing here touches Gmail, LinkedIn
or a browser.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import factories
import httpx
import pytest
from campaign_fakes import NOW, make_mailbox
from fastapi import FastAPI
from inbox_fakes import record_poll
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from test_auto_send import AUTO, CONTACT, Auto, _auto_steps, _last_key_does, _no_send
from test_campaign_sender import make_mail
from test_linkedin_replies import ADA, World, make_world, said, you_sent
from test_linkedin_steps import Lane, make_lane
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.campaigns.gmail import Message as GmailMessage
from netkeeper.cli import app as cli
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.models import (
    Contact,
    DoNotSendAddress,
    Enrollment,
    EnrollmentStatus,
    Interaction,
    InteractionKind,
    Message,
    MessageDirection,
    MessageStatus,
    ResponseOutcome,
    ResponseVia,
    StepMode,
    SyncRunKind,
    TemplateChannel,
    User,
    UserKind,
)
from netkeeper.scoping import get_scoped, install_scope_guard, scoped, unscoped
from netkeeper.services import campaign_replies, campaign_results, inbox, linkedin_steps, runs
from netkeeper.services import outside_responses as outside
from netkeeper.services.campaign_engine import SendOutcome
from netkeeper.services.linkedin_accounts import arm_scheduled_runs
from netkeeper.services.linkedin_steps import claim_auto_send, ready_to_prefill, waiting_for_you
from netkeeper.services.outside_responses import (
    ContactNotFound,
    EnrollmentNotFound,
    NothingToEnd,
    record_outside_response,
)
from netkeeper.services.users import ensure_local_user

EMAIL = TemplateChannel.EMAIL
LINKEDIN = TemplateChannel.LINKEDIN
CSRF = {"X-Netkeeper-Client": "1"}
NOTE = "Called back from an invented number; keen to talk."
_gmail_ids = itertools.count(1)


@pytest.fixture(autouse=True)
def _message_send_has_a_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """``message_send`` claims record their run through the real ``create_run``, as the
    LinkedIn step tests do."""
    monkeypatch.setattr(runs, "RUNNABLE_KINDS", runs.RUNNABLE_KINDS | {SyncRunKind.MESSAGE_SEND})


def _contact_of(session: Session, user: User, enrollment_id: int) -> int:
    enrollment = get_scoped(session, user, Enrollment, enrollment_id)
    assert enrollment is not None
    return enrollment.contact_id


def respond(
    enrollment_id: int,
    *,
    at: datetime,
    via: ResponseVia = ResponseVia.CALL,
    pick: bool = False,
    now: datetime | None = None,
    **fields: Any,
) -> Callable[[Session, User], outside.OutsideResponse]:
    """Record a response from the contact of ``enrollment_id``; ``pick`` names that
    enrollment, otherwise every one of theirs that could still send."""

    def act(session: Session, user: User) -> outside.OutsideResponse:
        return record_outside_response(
            session,
            user,
            _contact_of(session, user, enrollment_id),
            via=via,
            at=at,
            now=max(at, NOW) if now is None else now,
            enrollment_id=enrollment_id if pick else None,
            **fields,
        )

    return act


def _also_enrolled(world: World, enrollment_id: int, *statuses: EnrollmentStatus) -> list[int]:
    """The contact of ``enrollment_id`` enrolled in one more campaign per status."""

    def make(session: Session, user: User) -> list[int]:
        contact = get_scoped(session, user, Contact, _contact_of(session, user, enrollment_id))
        assert contact is not None
        return [
            factories.make_enrollment(
                session, factories.make_campaign(session, user), contact, status=status
            ).id
            for status in statuses
        ]

    return world.write(make)


def _interactions(world: World, enrollment_id: int) -> list[Interaction]:
    return world.read(
        lambda s, u: list(
            s.scalars(
                scoped(u, Interaction)
                .where(Interaction.contact_id == _contact_of(s, u, enrollment_id))
                .order_by(Interaction.id)
            )
        )
    )


def _contact(world: World, enrollment_id: int) -> Contact:
    def load(session: Session, user: User) -> Contact:
        contact = get_scoped(session, user, Contact, _contact_of(session, user, enrollment_id))
        assert contact is not None
        return contact

    return world.read(load)


@pytest.fixture
def world(session_factory: sessionmaker[Session]) -> World:
    """Three email steps a week apart, step 1 sent to each enrollment eight days ago."""
    return make_world(session_factory, channels=(EMAIL, EMAIL, EMAIL))


def _sent_enrollment(world: World, **overrides: Any) -> int:
    enrollment_id = world.enroll(**overrides)
    world.sent(enrollment_id, NOW - timedelta(days=8))
    return enrollment_id


# --- what it ends and writes ----------------------------------------------------------


def test_a_response_ends_every_enrollment_of_the_contact_that_could_still_send(
    world: World,
) -> None:
    active = _sent_enrollment(world)
    paused, pending, completed, removed, replied = _also_enrolled(
        world,
        active,
        EnrollmentStatus.PAUSED,
        EnrollmentStatus.PENDING,
        EnrollmentStatus.COMPLETED,
        EnrollmentStatus.REMOVED,
        EnrollmentStatus.REPLIED,
    )
    someone_else = _sent_enrollment(world, urn="urn:li:fsd_profile:INVENTEDOTHER")
    at = NOW - timedelta(hours=1)

    result = world.write(respond(active, at=at, note=NOTE, outcome=ResponseOutcome.POSITIVE))

    assert [e.id for e in result.enrollments] == [active, paused, pending]
    for enrollment_id in (active, paused, pending):
        enrollment = world.enrollment(enrollment_id)
        assert (enrollment.status, enrollment.exit_reason) == (EnrollmentStatus.REPLIED, "replied")
        assert enrollment.replied_at == at
        assert enrollment.next_action_at is None
        [row] = world.inbound(enrollment_id)
        assert (row.status, row.sent_at, row.snippet) == (MessageStatus.RECEIVED, at, NOTE)
        assert (row.outside_via, row.outside_outcome) == (
            ResponseVia.CALL,
            ResponseOutcome.POSITIVE,
        )
        assert row.handled_at is not None and not row.asks_unsubscribe
        assert row.gmail_message_id is None and row.li_message_urn is None
    [answered] = world.inbound(active)
    assert answered.step_id == world.messages(active)[0].step_id  # step 1 is what it answers
    assert answered.channel is EMAIL
    assert world.inbound(pending)[0].step_id is None  # nothing was sent to answer
    for enrollment_id, status in (
        (completed, EnrollmentStatus.COMPLETED),
        (removed, EnrollmentStatus.REMOVED),
        (replied, EnrollmentStatus.REPLIED),
    ):
        assert world.enrollment(enrollment_id).status is status
        assert world.inbound(enrollment_id) == []
    assert world.enrollment(someone_else).status is EnrollmentStatus.ACTIVE
    assert world.inbound(someone_else) == []

    [interaction] = _interactions(world, active)
    assert interaction.kind is InteractionKind.CALL_IN
    assert interaction.at == at and interaction.message_id == answered.id
    assert interaction.summary == (
        f"Responded to a campaign by phone, outside netkeeper (positive): {NOTE}"
    )
    # Inbound: they reached you. Nothing moves the recent-contact clock.
    assert _contact(world, active).last_contacted_at is None


def test_picking_one_enrollment_ends_that_one_only(world: World) -> None:
    first = _sent_enrollment(world)
    [second] = _also_enrolled(world, first, EnrollmentStatus.ACTIVE)
    world.write(respond(second, at=NOW, pick=True))
    assert world.enrollment(second).status is EnrollmentStatus.REPLIED
    assert world.enrollment(first).status is EnrollmentStatus.ACTIVE
    assert world.inbound(first) == []


def test_a_completed_enrollment_picked_records_the_reply_and_stays_completed(
    world: World,
) -> None:
    """As a detected reply to a completed enrollment does: it counts, nothing changes."""
    enrollment_id = _sent_enrollment(world)
    world.write(
        lambda s, u: setattr(
            get_scoped(s, u, Enrollment, enrollment_id), "status", EnrollmentStatus.COMPLETED
        )
    )
    world.write(respond(enrollment_id, at=NOW - timedelta(hours=1), pick=True))
    enrollment = world.enrollment(enrollment_id)
    assert enrollment.status is EnrollmentStatus.COMPLETED
    assert enrollment.replied_at == NOW - timedelta(hours=1)
    assert len(world.inbound(enrollment_id)) == 1


@pytest.mark.parametrize(
    ("via", "kind"),
    [
        (ResponseVia.CALL, InteractionKind.CALL_IN),
        (ResponseVia.IN_PERSON, InteractionKind.MEETING_IN),
        (ResponseVia.OTHER_EMAIL, InteractionKind.EMAIL_IN),
        (ResponseVia.TEXT, InteractionKind.OTHER_IN),
        (ResponseVia.OTHER_APP, InteractionKind.OTHER_IN),
        (ResponseVia.OTHER, InteractionKind.OTHER_IN),
    ],
)
def test_each_way_to_respond_is_an_inbound_timeline_kind(
    world: World, via: ResponseVia, kind: InteractionKind
) -> None:
    enrollment_id = _sent_enrollment(world)
    world.write(respond(enrollment_id, at=NOW, via=via))
    [interaction] = _interactions(world, enrollment_id)
    assert interaction.kind is kind
    assert world.inbound(enrollment_id)[0].outside_via is via
    assert _contact(world, enrollment_id).last_contacted_at is None


def test_the_kinds_and_statuses_are_pinned() -> None:
    assert {s.value for s in outside.STOPPABLE} == {"pending", "active", "paused"}
    assert {s.value for s in outside.RECORDABLE} == {"pending", "active", "paused", "completed"}
    assert timedelta(minutes=5) == outside.FUTURE_SKEW
    assert outside.NOTE_MAX_LENGTH == 500
    assert {v.value for v in ResponseVia} == {
        "call",
        "text",
        "other_email",
        "other_app",
        "in_person",
        "other",
    }
    assert {o.value for o in ResponseOutcome} == {
        "positive",
        "neutral",
        "not_interested",
        "opt_out",
    }


# --- opting out -------------------------------------------------------------------------


def test_an_opt_out_told_not_to_ends_opted_out_and_leaves_them_contactable(
    world: World,
) -> None:
    enrollment_id = _sent_enrollment(world)
    world.write(
        respond(enrollment_id, at=NOW, outcome=ResponseOutcome.OPT_OUT, do_not_contact=False)
    )
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.exit_reason) == (
        EnrollmentStatus.OPTED_OUT,
        campaign_replies.UNSUBSCRIBE_REASON,
    )
    assert world.inbound(enrollment_id)[0].asks_unsubscribe
    assert not _contact(world, enrollment_id).do_not_contact
    assert (
        world.read(
            lambda s, u: s.scalar(
                scoped(u, DoNotSendAddress).with_only_columns(func.count(DoNotSendAddress.id))
            )
        )
        == 0
    )


@pytest.mark.parametrize("flag", [None, True], ids=["by default", "when told"])
def test_an_opt_out_does_what_an_unsubscribe_reply_does(world: World, flag: bool | None) -> None:
    enrollment_id = _sent_enrollment(world)
    [pending] = _also_enrolled(world, enrollment_id, EnrollmentStatus.PENDING)
    fields: dict[str, Any] = {} if flag is None else {"do_not_contact": flag}
    world.write(respond(enrollment_id, at=NOW, outcome=ResponseOutcome.OPT_OUT, **fields))
    for ended in (enrollment_id, pending):
        assert world.enrollment(ended).status is EnrollmentStatus.OPTED_OUT
    contact = _contact(world, enrollment_id)
    assert contact.do_not_contact
    assert contact.do_not_contact_reason is not None
    assert "outside netkeeper" in contact.do_not_contact_reason
    [address] = world.read(lambda s, u: list(s.scalars(scoped(u, DoNotSendAddress))))
    assert address.contact_id == contact.id


@pytest.mark.parametrize("outcome", [ResponseOutcome.NOT_INTERESTED, ResponseOutcome.NEUTRAL])
def test_not_interested_or_neutral_ends_replied(world: World, outcome: ResponseOutcome) -> None:
    enrollment_id = _sent_enrollment(world)
    world.write(respond(enrollment_id, at=NOW, outcome=outcome))
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED
    assert not _contact(world, enrollment_id).do_not_contact


# --- refusals ---------------------------------------------------------------------------


def test_refusals(world: World) -> None:
    enrollment_id = _sent_enrollment(world)
    with pytest.raises(ValueError, match="future"):
        world.write(respond(enrollment_id, at=NOW + timedelta(hours=1), now=NOW))
    with pytest.raises(ValueError, match="timezone"):
        world.write(respond(enrollment_id, at=NOW.replace(tzinfo=None), now=NOW))
    with pytest.raises(ValueError, match="opt-out"):
        world.write(respond(enrollment_id, at=NOW, do_not_contact=True))
    with pytest.raises(ValueError, match="longer"):
        world.write(respond(enrollment_id, at=NOW, note="x" * 501))
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE

    world.write(respond(enrollment_id, at=NOW))
    with pytest.raises(NothingToEnd):
        world.write(respond(enrollment_id, at=NOW))  # nothing left that could send
    with pytest.raises(NothingToEnd):
        world.write(respond(enrollment_id, at=NOW, pick=True))  # already replied
    assert len(world.inbound(enrollment_id)) == 1

    other = _sent_enrollment(world, urn="urn:li:fsd_profile:INVENTEDOTHER")

    def wrong_contact(session: Session, user: User) -> None:
        record_outside_response(
            session,
            user,
            _contact_of(session, user, enrollment_id),
            via=ResponseVia.CALL,
            at=NOW,
            now=NOW,
            enrollment_id=other,
        )

    with pytest.raises(EnrollmentNotFound):
        world.write(wrong_contact)
    with pytest.raises(ContactNotFound):
        world.write(
            lambda s, u: record_outside_response(
                s, u, 999_999, via=ResponseVia.CALL, at=NOW, now=NOW
            )
        )
    with session_scope(world.factory) as session:
        user = session.get(User, world.user_id)
        assert user is not None
        with pytest.raises(RuntimeError, match="writer"):
            record_outside_response(
                session,
                user,
                _contact_of(session, user, other),
                via=ResponseVia.CALL,
                at=NOW,
                now=NOW,
            )


def test_a_response_before_the_first_send_is_refused(world: World) -> None:
    enrollment_id = _sent_enrollment(world)  # step 1 went out eight days ago
    [pending] = _also_enrolled(world, enrollment_id, EnrollmentStatus.PENDING)
    too_early = NOW - timedelta(days=9)
    with pytest.raises(outside.TooEarly, match="before the first message"):
        world.write(respond(enrollment_id, at=too_early))
    with pytest.raises(outside.TooEarly):
        world.write(respond(enrollment_id, at=too_early, pick=True))
    # Nothing changed: not the sent one, and not the pending one beside it.
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE
    assert world.enrollment(pending).status is EnrollmentStatus.PENDING
    assert world.inbound(enrollment_id) == world.inbound(pending) == []
    # At the very time of the send, it answers it.
    world.write(respond(enrollment_id, at=NOW - timedelta(days=8)))
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED


def test_a_pending_enrollment_takes_any_past_time(world: World) -> None:
    enrollment_id = world.enroll(status=EnrollmentStatus.PENDING)
    world.write(respond(enrollment_id, at=NOW - timedelta(days=400)))
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED
    with pytest.raises(NothingToEnd):
        world.write(respond(enrollment_id, at=NOW))


def test_another_users_contact_is_not_found(world: World) -> None:
    enrollment_id = _sent_enrollment(world)
    theirs = make_world(world.factory)

    def act(session: Session, user: User) -> None:
        record_outside_response(
            session,
            user,
            world.read(lambda s, u: _contact_of(s, u, enrollment_id)),
            via=ResponseVia.CALL,
            at=NOW,
            now=NOW,
        )

    with pytest.raises(ContactNotFound):
        theirs.write(act)
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE


# --- the inbox and the results -----------------------------------------------------------


def test_it_is_in_the_inbox_already_handled(world: World) -> None:
    enrollment_id = _sent_enrollment(world)
    [other] = _also_enrolled(world, enrollment_id, EnrollmentStatus.ACTIVE)
    world.write(respond(enrollment_id, at=NOW, via=ResponseVia.TEXT, note=NOTE))
    page = world.read(lambda s, u: inbox.list_inbox(s, u))
    assert page.unhandled == 0
    assert {item.message.enrollment_id for item in page.items} == {enrollment_id, other}
    for item in page.items:
        assert item.kind is inbox.InboxKind.REPLY
        assert item.message.outside_via is ResponseVia.TEXT
        assert item.received_at == NOW
        assert item.enrollment_status is EnrollmentStatus.REPLIED
    unhandled = world.read(lambda s, u: inbox.list_inbox(s, u, handled=False))
    assert unhandled.total == 0


def test_an_opt_out_is_an_unsubscribe_item(world: World) -> None:
    enrollment_id = _sent_enrollment(world)
    world.write(respond(enrollment_id, at=NOW, outcome=ResponseOutcome.OPT_OUT))
    [item] = world.read(lambda s, u: inbox.list_inbox(s, u)).items
    assert item.kind is inbox.InboxKind.UNSUBSCRIBE


def test_it_counts_in_the_results_as_a_reply_outside_netkeeper(world: World) -> None:
    outside_id = _sent_enrollment(world)
    detected = _sent_enrollment(world, urn="urn:li:fsd_profile:INVENTEDOTHER")
    _sent_enrollment(world, urn="urn:li:fsd_profile:INVENTEDTHIRD")
    world.write(respond(outside_id, at=NOW - timedelta(days=1)))
    world.write(lambda s, u: _email_reply(s, u, detected, NOW - timedelta(days=2)))

    results = world.read(
        lambda s, u: campaign_results.campaign_results(s, u, world.campaign_id, now=NOW)
    )
    assert (results.totals.replied, results.totals.replied_outside) == (2, 1)
    assert [(s.replied, s.replied_outside) for s in results.steps] == [(2, 1), (0, 0), (0, 0)]
    assert results.totals.reply_rate == pytest.approx(2 / 3)


# --- the same end as a detected reply ------------------------------------------------------


def _email_reply(
    session: Session, user: User, enrollment_id: int, at: datetime, *, unsubscribe: bool = False
) -> None:
    n = next(_gmail_ids)
    message = GmailMessage(
        id=f"gm-invented-{n}",
        thread_id=f"thread-invented-{n}",
        label_ids=frozenset({"INBOX"}),
        history_id=n,
        internal_date=at,
        snippet="An invented reply.",
        headers=(("Subject", "Re: Hello"),),
    )
    reply = campaign_replies.Reply(enrollment_id, message, unsubscribe, "netkeeper/x")
    assert campaign_replies.record_reply(session, user, reply)


Ender = Callable[[World, int, datetime], None]


def _detected_email(world: World, enrollment_id: int, at: datetime) -> None:
    world.write(lambda s, u: _email_reply(s, u, enrollment_id, at))


def _detected_linkedin(world: World, enrollment_id: int, at: datetime) -> None:
    world.apply(said(ADA, at), polled_at=at + timedelta(minutes=1))


def _outside(world: World, enrollment_id: int, at: datetime) -> None:
    world.write(respond(enrollment_id, at=at))


ENDERS: list[Any] = [
    pytest.param(_detected_email, id="detected email reply"),
    pytest.param(_detected_linkedin, id="detected LinkedIn reply"),
    pytest.param(_outside, id="outside netkeeper"),
]


def _end_state(world: World, enrollment_id: int) -> tuple[Any, ...]:
    enrollment = world.enrollment(enrollment_id)
    [row] = world.inbound(enrollment_id)
    return (
        enrollment.status,
        enrollment.exit_reason,
        enrollment.replied_at,
        enrollment.next_action_at,
        enrollment.current_step,
        row.direction,
        row.status,
        row.sent_at,
        row.step_id,
    )


def test_it_ends_an_enrollment_exactly_as_a_detected_reply_does(
    session_factory: sessionmaker[Session],
) -> None:
    states = []
    for ender in (_detected_email, _detected_linkedin, _outside):
        world = make_world(session_factory, channels=(EMAIL, EMAIL, EMAIL))
        enrollment_id = _sent_enrollment(world)
        ender(world, enrollment_id, NOW - timedelta(hours=2))
        state = _end_state(world, enrollment_id)
        # Step ids differ between worlds; that it is step 1 is the same.
        assert state[-1] == world.messages(enrollment_id)[0].step_id
        states.append(state[:-1])
    assert states[0] == states[1] == states[2]
    assert states[0][:2] == (EnrollmentStatus.REPLIED, "replied")


# --- no path sends after it: an email follow-up -----------------------------------------


@pytest.mark.parametrize("ender", ENDERS)
def test_no_email_follow_up_fires_after_it(
    session_factory: sessionmaker[Session], ender: Ender
) -> None:
    world = make_world(session_factory, channels=(EMAIL, EMAIL, EMAIL))
    enrollment_id = world.enroll()
    [(firing, _)] = world.tick(NOW).fired
    assert firing.enrollment_id == enrollment_id
    due = world.enrollment(enrollment_id).next_action_at
    assert due is not None

    ender(world, enrollment_id, NOW + timedelta(days=1))

    assert world.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED
    for later in (due, due + timedelta(days=7), due + timedelta(days=30)):
        assert not world.tick(later).fired
    assert [m.direction for m in world.messages(enrollment_id)] == [
        MessageDirection.OUT,
        MessageDirection.IN,
    ]


def test_the_tick_ends_a_revived_enrollment_on_the_response_and_sends_nothing(
    world: World,
) -> None:
    """The inbound row stops the claim on its own, with the status put back by hand."""
    enrollment_id = world.enroll()
    world.tick(NOW)
    due = world.enrollment(enrollment_id).next_action_at
    assert due is not None
    world.write(respond(enrollment_id, at=NOW + timedelta(hours=1)))

    def revive(session: Session, user: User) -> None:
        enrollment = get_scoped(session, user, Enrollment, enrollment_id)
        assert enrollment is not None
        enrollment.status = EnrollmentStatus.ACTIVE
        enrollment.exit_reason = None
        enrollment.next_action_at = due

    world.write(revive)
    assert not world.tick(due).fired
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED


def test_a_response_between_the_claim_and_the_gmail_send_stops_the_send(
    session_factory: sessionmaker[Session],
) -> None:
    """The Gmail sender reads for a reply just before it sends: a response recorded after
    the claim, before Gmail is asked, stops it, as a detected reply does (#416)."""
    answer: list[Callable[[], object]] = []

    def opener(user_id: int, mailbox_id: int) -> Any:
        while answer:
            answer.pop()()
        return box.gmail

    box = make_mail(
        session_factory,
        modes=(StepMode.SEND, StepMode.SEND),
        same_thread=(False, False),
        opener=opener,
    )
    enrollment_id = box.enroll()
    [(_, first)] = box.tick().fired
    assert first.at is not None
    due = box.enrollment(enrollment_id).next_action_at
    assert due is not None

    def mark_polled(session: Session) -> None:
        from netkeeper.models import Mailbox

        mailbox = get_scoped(session, box.user, Mailbox, box.mailbox.id)
        assert mailbox is not None
        mailbox.replies_polled_at = due  # replies are not stale: step 2 may go

    box.write(mark_polled)
    answer.append(
        lambda: box.write(
            lambda s: record_outside_response(
                s,
                box.user,
                box.enrollment(enrollment_id).contact_id,
                via=ResponseVia.CALL,
                at=due - timedelta(hours=1),
                now=due,
            )
        )
    )
    result = box.tick(due)

    [(_, outcome)] = result.fired
    assert outcome.outcome is SendOutcome.NOT_SENT
    assert len(box.gmail.sent()) == 1  # step 1 only
    assert box.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED
    assert not box.tick(due + timedelta(hours=3)).fired
    assert len(box.gmail.sent()) == 1


# --- no path sends after it: the LinkedIn queue -------------------------------------------


@pytest.fixture
def lane(session_factory: sessionmaker[Session]) -> Lane:
    return make_lane(session_factory)


def _lane_respond(lane: Lane, enrollment_id: int, at: datetime = NOW) -> None:
    lane.write(respond(enrollment_id, at=at))


def test_it_leaves_the_linkedin_queue_and_the_claim_refuses(lane: Lane) -> None:
    enrollment_id = lane.enroll()
    ready, _ = lane.read(
        lambda s, u: ready_to_prefill(s, u, now=NOW, settings=lane.settings, limit=50)
    )
    assert enrollment_id in [row.enrollment.id for row in ready]

    _lane_respond(lane, enrollment_id)

    ready, _ = lane.read(
        lambda s, u: ready_to_prefill(s, u, now=NOW, settings=lane.settings, limit=50)
    )
    assert enrollment_id not in [row.enrollment.id for row in ready]
    claim = lane.claim(enrollment_id)
    assert not claim.claimed
    assert [m.direction for m in lane.messages(enrollment_id)] == [MessageDirection.IN]
    assert lane.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED
    assert lane.runs() == []


def test_a_linkedin_claim_refuses_a_revived_enrollment_on_the_response(lane: Lane) -> None:
    enrollment_id = lane.enroll()
    _lane_respond(lane, enrollment_id)

    def revive(session: Session, user: User) -> None:
        enrollment = get_scoped(session, user, Enrollment, enrollment_id)
        assert enrollment is not None
        enrollment.status = EnrollmentStatus.ACTIVE
        enrollment.exit_reason = None
        enrollment.next_action_at = NOW

    lane.write(revive)
    claim = lane.claim(enrollment_id)
    assert not claim.claimed
    assert lane.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED
    assert [m.direction for m in lane.messages(enrollment_id)] == [MessageDirection.IN]


# --- no path sends after it: auto-send ----------------------------------------------------


@pytest.fixture
def auto_lane(session_factory: sessionmaker[Session]) -> Lane:
    made = make_lane(session_factory, channels=(LINKEDIN, LINKEDIN), settings=AUTO)
    made.write(_auto_steps)
    made.write(lambda s, u: arm_scheduled_runs(s, u, now=NOW - timedelta(days=1)))
    return made


def test_auto_send_claims_nothing_after_it(auto_lane: Lane) -> None:
    enrollment_id = auto_lane.enroll(contact=dict(CONTACT))
    _lane_respond(auto_lane, enrollment_id)
    auto_lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(minutes=1)))
    assert auto_lane.write(lambda s, u: claim_auto_send(s, u, now=NOW, settings=AUTO)) is None
    assert [m.direction for m in auto_lane.messages(enrollment_id)] == [MessageDirection.IN]


def _respond_to_the_claim(a: Auto) -> Callable[[Session, User], Any]:
    return respond(a.enrollment_id, at=NOW)


async def test_a_response_after_the_auto_send_claim_stops_it_before_it_opens_anything(
    auto_lane: Lane,
) -> None:
    a = Auto(auto_lane)
    auto_lane.write(_respond_to_the_claim(a))
    await a.execute()
    assert a.site.navigations == []
    _no_send(a)
    assert a.run().notes == "not started: a reply arrived"
    assert auto_lane.enrollment(a.enrollment_id).status is EnrollmentStatus.REPLIED


async def test_a_response_after_the_last_key_stops_the_send_click(auto_lane: Lane) -> None:
    a = Auto(auto_lane)
    _last_key_does(a, _respond_to_the_claim(a))
    await a.execute()
    _no_send(a)
    assert a.run().notes == "not sent: a reply arrived"
    message = a.message()
    assert message is not None and message.send_clicked_at is None
    enrollment = auto_lane.enrollment(a.enrollment_id)
    assert (enrollment.status, enrollment.next_action_at) == (EnrollmentStatus.REPLIED, None)


# --- an open prefill: as a detected reply leaves it ----------------------------------------


@pytest.fixture
def prefill_world(session_factory: sessionmaker[Session]) -> World:
    """Step 1 by email, sent; step 2 on LinkedIn, prefilled and open; step 3 by email."""
    return make_world(session_factory, channels=(EMAIL, LINKEDIN, EMAIL))


def _open_prefill(world: World) -> tuple[int, int]:
    enrollment_id = _sent_enrollment(world)
    message_id = world.prefill(enrollment_id)
    assert world.message(message_id).status is MessageStatus.PREFILLED
    return enrollment_id, message_id


def _waiting_ids(world: World) -> list[int]:
    rows, _ = world.read(lambda s, u: waiting_for_you(s, u, limit=10))
    return [row.message.id for row in rows]


@pytest.mark.parametrize("ender", ENDERS)
def test_an_open_prefill_is_left_for_the_person_and_discarding_it_moves_nothing(
    prefill_world: World, ender: Ender
) -> None:
    world = prefill_world
    enrollment_id, message_id = _open_prefill(world)

    ender(world, enrollment_id, NOW + timedelta(hours=1))

    # The composer is the person's: the prefill stays, listed, until they act on it.
    assert world.message(message_id).status is MessageStatus.PREFILLED
    assert _waiting_ids(world) == [message_id]
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.next_action_at) == (EnrollmentStatus.REPLIED, None)

    world.write(
        lambda s, u: linkedin_steps.discard(
            s, u, message_id, settings=world.settings, now=NOW + timedelta(hours=2)
        )
    )
    assert world.message(message_id).status is MessageStatus.DISCARDED
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.next_action_at) == (EnrollmentStatus.REPLIED, None)
    assert _waiting_ids(world) == []
    assert not world.tick(NOW + timedelta(days=30)).fired


@pytest.mark.parametrize("ender", ENDERS)
def test_an_open_prefill_sent_anyway_is_recorded_and_moves_nothing(
    prefill_world: World, ender: Ender
) -> None:
    world = prefill_world
    enrollment_id, message_id = _open_prefill(world)
    ender(world, enrollment_id, NOW + timedelta(hours=1))

    thread, _ = you_sent(ADA, NOW + timedelta(hours=2))
    world.apply(thread, polled_at=NOW + timedelta(hours=3))

    assert world.message(message_id).status is MessageStatus.SENT
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.next_action_at) == (EnrollmentStatus.REPLIED, None)
    assert not world.tick(NOW + timedelta(days=30)).fired


def test_a_claimed_prefill_recorded_after_it_moves_nothing(prefill_world: World) -> None:
    """A prefill claimed before the response, its outcome recorded after: it is recorded
    and the ended enrollment gets no next step."""
    world = prefill_world
    enrollment_id = _sent_enrollment(world)

    def claim(session: Session, user: User) -> int:
        record_poll(session, user, NOW - timedelta(minutes=1))
        result = linkedin_steps.claim_prefill(
            session, user, enrollment_id, now=NOW, settings=world.settings
        )
        assert result.claimed and result.message_id is not None
        return result.message_id

    message_id = world.write(claim)
    world.write(respond(enrollment_id, at=NOW))
    from netkeeper.linkedin.messaging import MessageOutcome, MessageOutcomeKind

    outcome = MessageOutcome(MessageOutcomeKind.PREFILLED, "fixed words", None, 12)
    assert world.write(
        lambda s, u: linkedin_steps.record_prefill_outcome(
            s, u, message_id, outcome, settings=world.settings, now=NOW + timedelta(minutes=1)
        )
    )
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.next_action_at) == (EnrollmentStatus.REPLIED, None)
    assert not world.tick(NOW + timedelta(days=30)).fired


# --- the API ---------------------------------------------------------------------------------


def _local_seed(app: FastAPI, *statuses: EnrollmentStatus) -> tuple[int, list[int]]:
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        contact = factories.make_contact(session, user, first_name="Invented")
        ids = [
            factories.make_enrollment(
                session, factories.make_campaign(session, user), contact, status=status
            ).id
            for status in statuses
        ]
        return contact.id, ids


async def test_the_api_records_a_response_and_lists_the_enrollments(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    contact_id, (active, completed) = _local_seed(
        running_app, EnrollmentStatus.ACTIVE, EnrollmentStatus.COMPLETED
    )
    listed = await client.get(f"/api/v1/contacts/{contact_id}/enrollments")
    assert listed.status_code == 200
    assert [(e["id"], e["can_still_send"]) for e in listed.json()] == [
        (completed, False),
        (active, True),
    ]

    at = datetime.now(UTC) - timedelta(hours=1)
    response = await client.post(
        f"/api/v1/contacts/{contact_id}/outside-response",
        json={"via": "text", "at": at.isoformat(), "note": NOTE, "outcome": "positive"},
        headers=CSRF,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    [ended] = body["enrollments"]
    assert (ended["id"], ended["status"]) == (active, "replied")

    page = (await client.get("/api/v1/inbox")).json()
    [item] = page["items"]
    assert (item["id"], item["outside_via"], item["outside_outcome"]) == (
        ended["message_id"],
        "text",
        "positive",
    )
    assert item["handled_at"] is not None and page["unhandled"] == 0
    timeline = (await client.get(f"/api/v1/contacts/{contact_id}/timeline")).json()
    assert timeline["items"][0]["interaction"]["kind"] == "other_in"

    again = await client.post(
        f"/api/v1/contacts/{contact_id}/outside-response",
        json={"via": "call", "at": at.isoformat()},
        headers=CSRF,
    )
    assert again.status_code == 409


async def test_the_api_refuses_what_it_cannot_record(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    contact_id, (active,) = _local_seed(running_app, EnrollmentStatus.ACTIVE)
    url = f"/api/v1/contacts/{contact_id}/outside-response"
    now = datetime.now(UTC)
    cases: list[tuple[dict[str, Any], int]] = [
        ({"via": "call", "at": (now + timedelta(days=1)).isoformat()}, 422),
        ({"via": "call", "at": now.isoformat(), "do_not_contact": True}, 422),
        ({"via": "call", "at": now.replace(tzinfo=None).isoformat()}, 422),
        ({"via": "pigeon", "at": now.isoformat()}, 422),
        ({"via": "call", "at": now.isoformat(), "note": "x" * 501}, 422),
        ({"via": "call", "at": now.isoformat(), "enrollment_id": 999_999}, 404),
    ]
    for body, status in cases:
        response = await client.post(url, json=body, headers=CSRF)
        assert response.status_code == status, (body, response.text)
    missing = await client.post(
        "/api/v1/contacts/999999/outside-response",
        json={"via": "call", "at": now.isoformat()},
        headers=CSRF,
    )
    assert missing.status_code == 404
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        enrollment = get_scoped(session, user, Enrollment, active)
        assert enrollment is not None and enrollment.status is EnrollmentStatus.ACTIVE


def _sent_at(app: FastAPI, enrollment_id: int, at: datetime) -> None:
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        enrollment = get_scoped(session, user, Enrollment, enrollment_id)
        assert enrollment is not None
        factories.make_message(session, enrollment, sent_at=at)


async def test_the_api_refuses_a_time_before_the_first_send_in_plain_words(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    contact_id, (active,) = _local_seed(running_app, EnrollmentStatus.ACTIVE)
    sent = datetime.now(UTC) - timedelta(days=3)
    _sent_at(running_app, active, sent)
    response = await client.post(
        f"/api/v1/contacts/{contact_id}/outside-response",
        json={"via": "call", "at": (sent - timedelta(hours=1)).isoformat()},
        headers=CSRF,
    )
    assert response.status_code == 422
    assert "before the first message" in response.json()["detail"]


async def test_another_users_enrollment_is_404(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    contact_id, (active,) = _local_seed(running_app, EnrollmentStatus.ACTIVE)
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        other = factories.make_user(session, UserKind.HOSTED)
        theirs = factories.make_enrollment(
            session, factories.make_campaign(session, other), factories.make_contact(session, other)
        ).id
    response = await client.post(
        f"/api/v1/contacts/{contact_id}/outside-response",
        json={"via": "call", "at": datetime.now(UTC).isoformat(), "enrollment_id": theirs},
        headers=CSRF,
    )
    assert response.status_code == 404
    with session_scope(factory) as session:
        enrollment = session.scalars(
            unscoped(select(Enrollment).where(Enrollment.id.in_([theirs, active])))
        ).all()
        assert {e.status for e in enrollment} == {EnrollmentStatus.ACTIVE}
        assert session.scalar(unscoped(select(func.count(Message.id)))) == 0


@pytest.mark.parametrize(
    ("body", "marked"),
    [({}, True), ({"do_not_contact": None}, True), ({"do_not_contact": False}, False)],
    ids=["omitted", "null", "false"],
)
async def test_the_api_opt_out_marks_do_not_contact_unless_told_not_to(
    running_app: FastAPI, client: httpx.AsyncClient, body: dict[str, Any], marked: bool
) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        contact = factories.make_contact(session, user, emails=["invented-optout@example.test"])
        factories.make_enrollment(session, factories.make_campaign(session, user), contact)
        contact_id = contact.id
    response = await client.post(
        f"/api/v1/contacts/{contact_id}/outside-response",
        json={"via": "text", "at": datetime.now(UTC).isoformat(), "outcome": "opt_out", **body},
        headers=CSRF,
    )
    assert response.status_code == 201, response.text
    assert response.json()["enrollments"][0]["status"] == "opted_out"
    with session_scope(factory) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        found = get_scoped(session, user, Contact, contact_id)
        assert found is not None and found.do_not_contact is marked
        listed = session.scalar(
            scoped(user, DoNotSendAddress).with_only_columns(func.count(DoNotSendAddress.id))
        )
        assert listed == (1 if marked else 0)


async def test_a_logged_call_asks_whether_it_was_the_response_and_ends_nothing(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    contact_id, (active,) = _local_seed(running_app, EnrollmentStatus.ACTIVE)
    idle_contact, _ = _local_seed(running_app)
    at = datetime.now(UTC).isoformat()

    async def logged(contact: int, kind: str) -> bool:
        response = await client.post(
            f"/api/v1/contacts/{contact}/interactions",
            json={"kind": kind, "at": at},
            headers=CSRF,
        )
        assert response.status_code == 201, response.text
        asked: bool = response.json()["ask_if_response"]
        return asked

    assert await logged(contact_id, "call")
    assert await logged(contact_id, "meeting")
    assert not await logged(contact_id, "note")
    assert not await logged(idle_contact, "call")
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        enrollment = get_scoped(session, user, Enrollment, active)
        assert enrollment is not None and enrollment.status is EnrollmentStatus.ACTIVE


# --- the CLI ---------------------------------------------------------------------------------


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        ensure_local_user(session)
    yield factory
    engine.dispose()


def test_the_cli_records_a_response(cli_db: sessionmaker[Session]) -> None:
    with session_scope(cli_db, write=True) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        mailbox = make_mailbox(session, user)
        contact = factories.make_contact(session, user)
        first, second = (
            factories.make_enrollment(
                session, factories.make_campaign(session, user, mailbox_id=mailbox.id), contact
            ).id
            for _ in range(2)
        )
        contact_id = contact.id
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "campaigns",
            "record-response",
            str(contact_id),
            "--via",
            "in_person",
            "--at",
            "2026-10-01T10:00:00+00:00",
            "--enrollment",
            str(second),
            "--note",
            NOTE,
        ],
    )
    assert result.exit_code == 0, result.output
    assert f"enrollment {second}" in result.output and "replied" in result.output
    with session_scope(cli_db) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        statuses = {
            e.id: e.status
            for e in session.scalars(
                scoped(user, Enrollment).where(Enrollment.id.in_([first, second]))
            )
        }
        assert statuses == {first: EnrollmentStatus.ACTIVE, second: EnrollmentStatus.REPLIED}
        [row] = session.scalars(
            scoped(user, Message).where(Message.direction == MessageDirection.IN)
        )
        assert row.sent_at == datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
        assert row.outside_via is ResponseVia.IN_PERSON

    again = runner.invoke(
        cli,
        [
            "campaigns",
            "record-response",
            str(contact_id),
            "--via",
            "call",
            "--enrollment",
            str(second),
        ],
    )
    assert again.exit_code == 1
    assert "already ended" in again.output


@pytest.mark.parametrize(
    ("flag", "marked"),
    [([], True), (["--do-not-contact"], True), (["--no-do-not-contact"], False)],
    ids=["omitted", "--do-not-contact", "--no-do-not-contact"],
)
def test_the_cli_opt_out_marks_do_not_contact_unless_told_not_to(
    cli_db: sessionmaker[Session], flag: list[str], marked: bool
) -> None:
    with session_scope(cli_db, write=True) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        contact = factories.make_contact(session, user, emails=["invented-cli@example.test"])
        factories.make_enrollment(session, factories.make_campaign(session, user), contact)
        contact_id = contact.id
    result = CliRunner().invoke(
        cli,
        [
            "campaigns",
            "record-response",
            str(contact_id),
            "--via",
            "call",
            "--outcome",
            "opt_out",
            *flag,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "opted_out" in result.output
    with session_scope(cli_db) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        found = get_scoped(session, user, Contact, contact_id)
        assert found is not None and found.do_not_contact is marked
        listed = session.scalar(
            scoped(user, DoNotSendAddress).with_only_columns(func.count(DoNotSendAddress.id))
        )
        assert listed == (1 if marked else 0)
