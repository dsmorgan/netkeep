"""The per-mailbox reply hold record and the ``held_reason`` derived from it (#716).

The record is display only: the tick keeps it after its send, and the campaign page and
the dashboard read it. These tests pin when it is written and lifted, which follow-ups
show a reason, that it never reaches another user, and that a failure keeping it changes
nothing the tick sent.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import factories
import httpx
import pytest
from campaign_fakes import ARMED_FOR_SEND, NOW, make_mailbox
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from test_campaign_replies import ADA, HELD_STALE, poller, set_mailbox
from test_campaign_sender import WEEK, Mail, make_mail

from netkeep.campaigns.gmail import GmailTransient
from netkeep.db import session_scope
from netkeep.models import (
    Campaign,
    Enrollment,
    EnrollmentStatus,
    MailboxReplyHold,
    MessageStatus,
    StepMode,
    TemplateChannel,
    User,
    UserKind,
)
from netkeep.models.base import utcnow
from netkeep.scoping import get_scoped, scoped
from netkeep.services import campaign_engine as engine_module
from netkeep.services import reply_holds
from netkeep.services.campaign_engine import SendOutcome, Skip

# --- through the tick ------------------------------------------------------------------


@pytest.fixture(name="mail")
def _mail(session_factory: sessionmaker[Session]) -> Mail:
    """Two ``send`` steps a week apart, the second in the first's thread."""
    return make_mail(session_factory, modes=(StepMode.SEND,) * 2, same_thread=(False, True))


def holds(mail: Mail) -> list[tuple[int, str, datetime]]:
    return mail.read(
        lambda s: [
            (h.mailbox_id, h.reason, h.held_since)
            for h in s.scalars(scoped(mail.user, MailboxReplyHold))
        ]
    )


def reasons(mail: Mail, *enrollment_ids: int) -> dict[int, str]:
    return mail.read(
        lambda s: engine_module.held_reasons(s, mail.user, enrollment_ids, now=mail.clock.now)
    )


def held_follow_up(mail: Mail) -> tuple[int, datetime]:
    """Step 1 goes out, then step 2 comes due while the reply poll fails once: the tick
    holds it. Returns the enrollment and the tick's time."""
    mail.any_time()
    mail.sender = poller(mail, every=timedelta(minutes=10))
    enrollment_id = mail.enroll(ADA)
    mail.tick(NOW)
    mail.tick(NOW + timedelta(minutes=10))  # the baseline scan
    due = NOW + WEEK
    mail.gmail.fail_next("history.list", GmailTransient("down", code="unavailable"))
    result = mail.tick(due)
    assert result.skipped()[enrollment_id] == (Skip.REPLIES_STALE.value,)
    assert result.fired == []
    return enrollment_id, due


def test_a_held_follow_up_shows_why_until_the_hold_lifts(mail: Mail) -> None:
    enrollment_id, due = held_follow_up(mail)

    assert holds(mail) == [(mail.mailbox.id, HELD_STALE, due)]
    assert reasons(mail, enrollment_id) == {enrollment_id: HELD_STALE}
    # Nothing on the enrollment changed (#694).
    enrollment = mail.enrollment(enrollment_id)
    assert (enrollment.not_sent_count, enrollment.not_sent_error) == (0, None)

    # Still held a minute later, inside the poll's backoff: the record is not rewritten.
    mail.gmail.fail_next("history.list", GmailTransient("down", code="unavailable"))
    mail.tick(due + timedelta(minutes=1))
    assert holds(mail) == [(mail.mailbox.id, HELD_STALE, due)]

    # The poll catches up: the follow-up goes out, and the record goes.
    later = due + timedelta(minutes=10)
    [(firing, outcome)] = mail.tick(later).fired
    assert firing.enrollment_id == enrollment_id and outcome.outcome is SendOutcome.SENT
    assert holds(mail) == []
    assert reasons(mail, enrollment_id) == {}


def test_a_poll_that_caught_up_lifts_the_record_before_the_next_tick(mail: Mail) -> None:
    """A poll that read everything after the hold started lifts it, so the record reads
    as lifted even while the row is still there."""
    enrollment_id, due = held_follow_up(mail)
    mail.write(
        lambda s: setattr(s.scalars(scoped(mail.user, MailboxReplyHold)).one(), "held_since", due)
    )
    set_mailbox(mail, replies_polled_at=due + timedelta(seconds=1))
    assert reasons(mail, enrollment_id) == {}
    set_mailbox(mail, replies_polled_at=due)
    assert reasons(mail, enrollment_id) == {enrollment_id: HELD_STALE}
    set_mailbox(mail, replies_polled_at=None)  # signed in again: still held
    assert reasons(mail, enrollment_id) == {enrollment_id: HELD_STALE}


def test_a_failure_keeping_the_record_changes_nothing_the_tick_did(
    mail: Mail, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record is kept after the send is recorded, and its failure is only logged: a
    step 1 on the held mailbox still goes out in that tick, and the next tick still holds
    the follow-up."""

    def broken(*args: object, **kwargs: object) -> dict[int, str | None]:
        raise RuntimeError("the hold record could not be read")

    monkeypatch.setattr(reply_holds, "changes", broken)
    mail.any_time()
    mail.sender = poller(mail, every=timedelta(minutes=10))
    held_id = mail.enroll(ADA)
    mail.tick(NOW)
    mail.tick(NOW + timedelta(minutes=10))
    due = NOW + WEEK
    mail.gmail.fail_next("history.list", GmailTransient("down", code="unavailable"))
    mail.clock.now = due
    new_id = mail.enroll("new@example.test")

    result = mail.tick(due)

    assert result.skipped()[held_id] == (Skip.REPLIES_STALE.value,)
    [(firing, outcome)] = result.fired
    assert firing.enrollment_id == new_id and outcome.outcome is SendOutcome.SENT
    assert holds(mail) == []
    assert [m.status for m in mail.messages(held_id)] == [MessageStatus.SENT]


def test_the_tick_decides_the_same_with_or_without_the_record(mail: Mail) -> None:
    """A record that says held, on a mailbox whose replies are read, holds nothing: the
    follow-up goes out, and the tick deletes the record it disagrees with."""
    mail.any_time()
    mail.sender = poller(mail, every=timedelta(minutes=10))
    enrollment_id = mail.enroll(ADA)
    mail.tick(NOW)
    mail.tick(NOW + timedelta(minutes=10))
    due = NOW + WEEK
    mail.write(
        lambda s: s.add(
            MailboxReplyHold(
                user_id=mail.user.id,
                mailbox_id=mail.mailbox.id,
                reason=HELD_STALE,
                held_since=due + timedelta(hours=1),
            )
        )
    )

    [(firing, outcome)] = mail.tick(due).fired

    assert firing.enrollment_id == enrollment_id and outcome.outcome is SendOutcome.SENT
    assert holds(mail) == []


# --- which follow-ups show a reason ----------------------------------------------------------


def _world(session: Session, user: User, *, at: datetime) -> dict[str, int]:
    """A mailbox with a hold record, and enrollments of every kind on its campaigns."""
    mailbox = make_mailbox(session, user, **ARMED_FOR_SEND)
    session.add(
        MailboxReplyHold(user_id=user.id, mailbox_id=mailbox.id, reason=HELD_STALE, held_since=at)
    )
    campaign = factories.make_campaign(
        session, user, channels=(TemplateChannel.EMAIL,) * 2, mailbox_id=mailbox.id
    )
    linkedin_next = factories.make_campaign(
        session,
        user,
        channels=(TemplateChannel.EMAIL, TemplateChannel.LINKEDIN),
        mailbox_id=mailbox.id,
    )

    def enroll(on: Campaign = campaign, **fields: object) -> int:
        contact = factories.make_contact(session, user)
        return factories.make_enrollment(session, on, contact, **fields).id

    return {
        "due_follow_up": enroll(current_step=1, next_action_at=at),
        "due_step_1": enroll(current_step=None, next_action_at=at),
        "not_yet_due": enroll(current_step=1, next_action_at=at + timedelta(days=1)),
        "ended": enroll(current_step=1, next_action_at=at, status=EnrollmentStatus.REPLIED),
        "linkedin_next": enroll(linkedin_next, current_step=1, next_action_at=at),
    }


def test_only_due_gmail_follow_ups_on_a_held_mailbox_show_a_reason(
    session_factory: sessionmaker[Session],
) -> None:
    at = utcnow()
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        ids = _world(session, user, at=at)
    with session_scope(session_factory) as session:
        got = engine_module.held_reasons(session, user, list(ids.values()), now=at)
    assert got == {ids["due_follow_up"]: HELD_STALE}


def test_another_users_hold_record_never_shows(session_factory: sessionmaker[Session]) -> None:
    """ADR 0005: a record is read only through its own user, even one whose mailbox id
    names another user's mailbox."""
    at = utcnow()
    with session_scope(session_factory, write=True) as session:
        ours = factories.make_user(session)
        theirs = factories.make_user(session)
        ids = _world(session, ours, at=at)
        hold = session.scalars(scoped(ours, MailboxReplyHold)).one()
        session.delete(hold)
        session.flush()
        session.add(
            MailboxReplyHold(
                user_id=theirs.id, mailbox_id=hold.mailbox_id, reason=HELD_STALE, held_since=at
            )
        )
    with session_scope(session_factory) as session:
        assert engine_module.held_reasons(session, ours, list(ids.values()), now=at) == {}
        assert engine_module.held_reasons(session, theirs, list(ids.values()), now=at) == {}


# --- the API ------------------------------------------------------------------------------


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


def _seed(app: FastAPI) -> tuple[dict[str, int], int]:
    with session_scope(app.state.session_factory, write=True) as session:
        user = _local(session)
        ids = _world(session, user, at=utcnow() - timedelta(minutes=1))
        enrollment = get_scoped(session, user, Enrollment, ids["due_follow_up"])
        assert enrollment is not None
        return ids, enrollment.campaign_id


async def test_the_campaign_page_says_why_a_due_follow_up_waits(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    ids, campaign_id = _seed(running_app)

    body = (await client.get(f"/api/v1/campaigns/{campaign_id}/enrollments")).json()

    shown = {item["id"]: item["held_reason"] for item in body["items"]}
    expected = dict.fromkeys(
        (ids[k] for k in ("due_follow_up", "due_step_1", "not_yet_due", "ended")), None
    )
    expected[ids["due_follow_up"]] = HELD_STALE
    assert shown == expected
    assert all(item["not_sent_error"] is None for item in body["items"])


async def test_the_dashboard_says_why_a_due_follow_up_waits(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    ids, _ = _seed(running_app)

    body = (await client.get("/api/v1/dashboard/next-fires?limit=50")).json()

    shown: Callable[[str], object] = lambda key: next(  # noqa: E731
        item["held_reason"] for item in body["items"] if item["enrollment_id"] == ids[key]
    )
    assert shown("due_follow_up") == HELD_STALE
    assert shown("due_step_1") is None
    assert shown("not_yet_due") is None
