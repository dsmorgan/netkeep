"""``GET /gmail/leftover-drafts``: drafts netkeep left in Gmail, for cleanup by hand (#543).

One test per kind of row (discarded, ended, waiting, test), the flags and the order, the
rows that are never listed, another user's rows, and no message text in the logs. The
two-user isolation test of the endpoint is in ``tests/isolation``. Nothing here reaches
Gmail: it is a read of what the campaign engine stored.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session

from netkeep.campaigns.compose import message_id_for
from netkeep.db import session_scope
from netkeep.models import (
    Campaign,
    DoNotSendAddress,
    DoNotSendReason,
    Enrollment,
    EnrollmentStatus,
    Mailbox,
    Message,
    MessageStatus,
    TemplateChannel,
    TestSend,
    User,
    UserKind,
)
from netkeep.scoping import get_scoped
from netkeep.services import leftover_drafts as service
from netkeep.services import mailboxes as mailbox_service

AT = datetime(2026, 9, 16, 15, 0, tzinfo=UTC)
SUBJECT = "Subject-marker-543"
BODY = "Body-marker-543 never logged"
K = service.LeftoverKind
W = service.DraftWarning


def _box(session: Session, user: User) -> Mailbox:
    return mailbox_service.connect(
        session, user, f"me{user.id}@example.test", f"rt-543-{user.id}", daily_cap=80
    )


def _campaign(session: Session, user: User, box: Mailbox) -> Campaign:
    return factories.make_campaign(session, user, name="Autumn hello", mailbox_id=box.id)


def _draft(
    session: Session,
    campaign: Campaign,
    *,
    enrollment: EnrollmentStatus = EnrollmentStatus.ACTIVE,
    status: MessageStatus = MessageStatus.DRAFTED,
    draft_id: str | None = "draft-1",
    created_at: datetime = AT,
    **contact: Any,
) -> Message:
    person = factories.make_contact(
        session, campaign.user, first_name="Fictional", last_name="Person", **contact
    )
    row = factories.make_enrollment(session, campaign, person, status=enrollment)
    return factories.make_message(
        session,
        row,
        status=status,
        sent_at=AT if status is MessageStatus.SENT else None,
        gmail_draft_id=draft_id,
        subject=SUBJECT,
        body_rendered=BODY,
        created_at=created_at,
    )


def _test_send(
    session: Session, campaign: Campaign, *, draft_id: str | None = "test-draft", at: datetime = AT
) -> TestSend:
    row = TestSend(
        user_id=campaign.user_id,
        campaign_id=campaign.id,
        step_id=campaign.steps[0].id,
        fingerprint="f" * 64,
        to_address="me@example.test",
        gmail_draft_id=draft_id,
        rfc822_message_id="<test-543@example.test>",
        sent_at=at,
    )
    session.add(row)
    session.flush()
    return row


def _listed(session: Session, user: User, email: str) -> None:
    """``email`` on ``user``'s do-not-send list, as a person adds one (normalized)."""
    session.add(DoNotSendAddress(user_id=user.id, email=email, reason=DoNotSendReason.MANUAL))
    session.flush()


def _list(session: Session, user: User) -> list[service.LeftoverDraft]:
    return list(service.leftover_drafts(session, user).items)


def test_nothing_is_left_over_for_a_new_user(session: Session) -> None:
    user = factories.make_user(session)
    found = service.leftover_drafts(session, user)
    assert found.items == ()
    assert found.more is False


def test_a_discarded_message_with_a_draft_is_listed(session: Session) -> None:
    user = factories.make_user(session)
    box = _box(session, user)
    campaign = _campaign(session, user, box)
    message = _draft(session, campaign, status=MessageStatus.DISCARDED)
    # A discarded message without a draft id has nothing left in Gmail (#544 clears it
    # when the person deletes the draft).
    _draft(session, campaign, status=MessageStatus.DISCARDED, draft_id=None)

    [row] = _list(session, user)

    assert row.kind is K.DISCARDED
    assert row.message_id == message.id
    assert row.warning is None
    assert row.low_priority is False
    assert (row.campaign_id, row.campaign_name, row.step_position) == (
        campaign.id,
        "Autumn hello",
        1,
    )
    assert row.contact_name == "Fictional Person"
    assert row.mailbox_email == box.email
    assert row.rfc822_message_id == message_id_for(
        user_id=user.id, message_id=message.id, created_at=message.created_at, address=box.email
    )


def test_a_discarded_draft_the_person_sent_drops_off(session: Session) -> None:
    """#544 records it ``sent``: it is no longer a leftover."""
    user = factories.make_user(session)
    campaign = _campaign(session, user, _box(session, user))
    message = _draft(session, campaign, status=MessageStatus.DISCARDED)
    assert len(_list(session, user)) == 1

    message.status = MessageStatus.SENT
    message.sent_at = AT
    session.flush()

    assert _list(session, user) == []


@pytest.mark.parametrize(
    "status",
    [EnrollmentStatus.COMPLETED, EnrollmentStatus.REMOVED, EnrollmentStatus.REPLIED],
)
def test_a_draft_on_an_ended_enrollment_is_listed(
    session: Session, status: EnrollmentStatus
) -> None:
    user = factories.make_user(session)
    campaign = _campaign(session, user, _box(session, user))
    _draft(session, campaign, enrollment=status)

    [row] = _list(session, user)

    assert row.kind is K.ENDED
    assert row.enrollment_status is status
    assert row.warning is None
    assert row.low_priority is (status is EnrollmentStatus.REPLIED)


@pytest.mark.parametrize(
    ("status", "warning"),
    [(EnrollmentStatus.OPTED_OUT, W.OPTED_OUT), (EnrollmentStatus.BOUNCED, W.BOUNCED)],
)
def test_a_draft_left_after_an_opt_out_or_a_bounce_is_flagged(
    session: Session, status: EnrollmentStatus, warning: service.DraftWarning
) -> None:
    user = factories.make_user(session)
    campaign = _campaign(session, user, _box(session, user))
    _draft(session, campaign, enrollment=status)

    [row] = _list(session, user)

    assert (row.kind, row.warning, row.low_priority) == (K.ENDED, warning, False)
    assert service.flagged_waiting(session, user)


def test_a_waiting_draft_is_listed_only_when_sending_it_would_be_wrong(session: Session) -> None:
    """Case 7: no step fires while a draft waits, so the guards never see a do not contact
    mark or a do-not-send entry made after it."""
    user = factories.make_user(session)
    campaign = _campaign(session, user, _box(session, user))
    _draft(session, campaign)  # waiting, nothing wrong: not a leftover
    for status in (EnrollmentStatus.PENDING, EnrollmentStatus.PAUSED):
        _draft(session, campaign, enrollment=status)
    assert _list(session, user) == []
    assert not service.flagged_waiting(session, user)

    marked = _draft(session, campaign, do_not_contact=True)
    listed = _draft(session, campaign, emails=["listed-543@example.test"])
    _listed(session, user, "Listed-543@example.test")

    rows = {row.message_id: row for row in _list(session, user)}

    assert set(rows) == {marked.id, listed.id}
    assert (rows[marked.id].kind, rows[marked.id].warning) == (K.WAITING, W.DO_NOT_CONTACT)
    assert (rows[listed.id].kind, rows[listed.id].warning) == (K.WAITING, W.DO_NOT_SEND)
    assert service.flagged_waiting(session, user)


def test_a_test_draft_is_listed_and_kept_until_the_mailbox_is_verified(session: Session) -> None:
    user = factories.make_user(session)
    box = _box(session, user)
    campaign = _campaign(session, user, box)
    test = _test_send(session, campaign)
    _test_send(session, campaign, draft_id=None)  # a test that was sent, not drafted

    [row] = _list(session, user)

    assert row.kind is K.TEST
    assert (row.test_send_id, row.message_id, row.contact_id) == (test.id, None, None)
    assert row.rfc822_message_id == "<test-543@example.test>"
    assert row.mailbox_email == box.email
    assert row.keep_for_check is True

    box.message_id_verified_at = AT
    session.flush()
    [row] = _list(session, user)
    assert row.keep_for_check is False
    assert not service.flagged_waiting(session, user), "a test draft is never flagged"


def test_rows_that_are_never_leftovers(session: Session) -> None:
    user = factories.make_user(session)
    campaign = _campaign(session, user, _box(session, user))
    ended = EnrollmentStatus.REMOVED
    _draft(session, campaign, enrollment=ended, status=MessageStatus.SENT)
    _draft(session, campaign, enrollment=ended, status=MessageStatus.SCHEDULED, draft_id=None)
    _draft(session, campaign, enrollment=ended, draft_id=None)  # drafted, but no Gmail draft
    linkedin = factories.make_campaign(session, user, channels=(TemplateChannel.LINKEDIN,))
    person = factories.make_contact(session, user)
    factories.make_message(
        session,
        factories.make_enrollment(session, linkedin, person, status=ended),
        status=MessageStatus.DISCARDED,
        gmail_draft_id="not-email",
    )

    assert _list(session, user) == []


def test_flagged_first_then_discarded_and_ended_then_tests_then_replies(session: Session) -> None:
    user = factories.make_user(session)
    campaign = _campaign(session, user, _box(session, user))
    reply = _draft(session, campaign, enrollment=EnrollmentStatus.REPLIED, created_at=AT)
    test = _test_send(session, campaign, at=AT - timedelta(days=1))
    old = _draft(session, campaign, status=MessageStatus.DISCARDED, created_at=AT - timedelta(2))
    new = _draft(session, campaign, enrollment=EnrollmentStatus.REMOVED, created_at=AT)
    opted = _draft(
        session, campaign, enrollment=EnrollmentStatus.OPTED_OUT, created_at=AT - timedelta(5)
    )

    rows = _list(session, user)

    assert [(row.message_id, row.test_send_id) for row in rows] == [
        (opted.id, None),
        (new.id, None),
        (old.id, None),
        (None, test.id),
        (reply.id, None),
    ]


def test_a_reply_draft_for_a_do_not_contact_contact_is_flagged_not_low_priority(
    session: Session,
) -> None:
    user = factories.make_user(session)
    campaign = _campaign(session, user, _box(session, user))
    _draft(session, campaign, enrollment=EnrollmentStatus.REPLIED, do_not_contact=True)

    [row] = _list(session, user)

    assert (row.kind, row.warning, row.low_priority) == (K.ENDED, W.DO_NOT_CONTACT, False)


def test_a_long_list_says_there_is_more(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "MESSAGES_MAX", 2)
    user = factories.make_user(session)
    campaign = _campaign(session, user, _box(session, user))
    # The flagged draft is the oldest, so only the query's order puts it in the page.
    flagged = _draft(session, campaign, enrollment=EnrollmentStatus.BOUNCED)
    _draft(session, campaign, enrollment=EnrollmentStatus.REMOVED)
    assert service.leftover_drafts(session, user).more is False

    _draft(session, campaign, enrollment=EnrollmentStatus.REMOVED)
    found = service.leftover_drafts(session, user)

    assert found.more is True
    assert len(found.items) == 2
    assert found.items[0].message_id == flagged.id, "a flagged draft is never cut off first"


def test_another_users_leftovers_never_show(session: Session) -> None:
    mine, theirs = factories.make_user(session), factories.make_user(session)
    other = _campaign(session, theirs, _box(session, theirs))
    _draft(session, other, enrollment=EnrollmentStatus.OPTED_OUT)
    _draft(session, other, status=MessageStatus.DISCARDED)
    _test_send(session, other)
    # Their do-not-send entry is about their address book, not mine.
    campaign = _campaign(session, mine, _box(session, mine))
    _draft(session, campaign, emails=["shared-543@example.test"])
    _listed(session, theirs, "shared-543@example.test")

    assert _list(session, mine) == []
    assert not service.flagged_waiting(session, mine)
    assert len(_list(session, theirs)) == 3


def test_reading_the_list_logs_no_message_text(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    user = factories.make_user(session)
    campaign = _campaign(session, user, _box(session, user))
    _draft(session, campaign, enrollment=EnrollmentStatus.OPTED_OUT)
    _draft(session, campaign, status=MessageStatus.DISCARDED)
    # A campaign with no mailbox has no Message-ID to search for, which logs a warning.
    bare = factories.make_campaign(session, user, mailbox_id=None)
    _draft(session, bare, status=MessageStatus.DISCARDED)

    with caplog.at_level(logging.DEBUG):
        rows = _list(session, user)
        service.flagged_waiting(session, user)

    assert len(rows) == 3
    assert SUBJECT not in caplog.text
    assert "Body-marker-543" not in caplog.text


async def test_get_leftover_drafts(
    client: httpx.AsyncClient, running_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        campaign = _campaign(session, user, _box(session, user))
        opted = _draft(session, campaign, enrollment=EnrollmentStatus.OPTED_OUT).id
        test = _test_send(session, campaign).id

    with caplog.at_level(logging.DEBUG):
        response = await client.get("/api/v1/gmail/leftover-drafts")

    assert response.status_code == 200
    body = response.json()
    assert body["more"] is False
    assert [(row["kind"], row["message_id"], row["test_send_id"]) for row in body["items"]] == [
        ("ended", opted, None),
        ("test", None, test),
    ]
    first = body["items"][0]
    assert first["warning"] == "opted_out"
    assert first["enrollment_status"] == "opted_out"
    assert first["rfc822_message_id"].startswith("<")
    assert SUBJECT not in response.text and "Body-marker-543" not in response.text
    assert SUBJECT not in caplog.text and "Body-marker-543" not in caplog.text


async def test_the_endpoint_only_reads(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        campaign = _campaign(session, user, _box(session, user))
        message = _draft(session, campaign, status=MessageStatus.DISCARDED).id

    assert (await client.get("/api/v1/gmail/leftover-drafts")).status_code == 200

    with session_scope(running_app.state.session_factory) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        row = get_scoped(session, user, Message, message)
        assert row is not None
        assert (row.status, row.gmail_draft_id) == (MessageStatus.DISCARDED, "draft-1")
        enrollment = get_scoped(session, user, Enrollment, row.enrollment_id)
        assert enrollment is not None
        assert enrollment.status is EnrollmentStatus.ACTIVE
