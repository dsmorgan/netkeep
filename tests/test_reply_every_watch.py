"""A reply stops every watch of the contact (#695), and what the poll reads and keeps.

- A reply in one campaign's thread is recorded on every watched enrollment of the
  contact whose first send came before it, not only the thread's: another email
  campaign, and one that sent only on LinkedIn (#571). Only the thread's campaign label
  goes on it.
- ``replied_at`` is the earliest response on record, in whatever order they were recorded.
- A bounce notice is matched to a message ``messages.send`` sent by the Message-ID
  netkeep gave it, with no read of every watched message; one sent from a draft by its
  Gmail copy's Message-ID.
- :func:`~netkeep.services.campaign_replies.reply_work` never loads a completed
  enrollment past its watch window.

Every address and message is invented, and Gmail is :class:`FakeGmail`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import factories
import pytest
from campaign_fakes import NOW
from sqlalchemy import event
from sqlalchemy.orm import Session
from test_linkedin_only_email_replies import FIRST_SEND, QUORBELLE, World, baseline, email
from test_linkedin_only_email_replies import world as world  # the fixture

from netkeep.campaigns.compose import message_id_for
from netkeep.campaigns.gmail import MessageRef
from netkeep.models import (
    Campaign,
    Contact,
    Enrollment,
    EnrollmentStatus,
    Mailbox,
    Message,
    MessageStatus,
    ResponseVia,
    TemplateChannel,
    User,
)
from netkeep.scoping import get_scoped, scoped
from netkeep.services import campaign_replies as replies
from netkeep.services import outside_responses

EMAIL = TemplateChannel.EMAIL
LINKEDIN = TemplateChannel.LINKEDIN


def legacy_message_id(user_id: int, row: Message, domain: str) -> str:
    """The Message-ID netkeep gave a row before the rename (#661), written out here."""
    stamp = row.created_at.astimezone(UTC).isoformat(timespec="microseconds")
    seed = f"netkeeper-message-id:v1:{user_id}:{row.id}:{stamp}"
    return f"<{hashlib.sha256(seed.encode()).hexdigest()[:32]}@{domain}>"


def send_email(
    world: World,
    enrollment_id: int,
    subject: str,
    at: datetime,
    *,
    draft_id: str | None = None,
    legacy: bool = False,
) -> MessageRef:
    """Step 1 of the enrollment went out by email at ``at``, in a thread of its own.

    Sent as ``messages.send`` sends it, with the Message-ID netkeep derives for the row
    (with ``legacy``, as netkeep derived it before the rename); with ``draft_id``, as a
    draft the person sent, under a Message-ID of Gmail's making."""

    def make(session: Session, user: User) -> MessageRef:
        enrollment = get_scoped(session, user, Enrollment, enrollment_id)
        mailbox = get_scoped(session, user, Mailbox, world.mailbox_id)
        assert enrollment is not None and mailbox is not None
        contact = get_scoped(session, user, Contact, enrollment.contact_id)
        assert contact is not None
        enrollment.current_step = 1
        row = factories.make_message(
            session, enrollment, position=1, sent_at=at, gmail_draft_id=draft_id
        )
        out = EmailMessage()
        out["From"] = mailbox.email
        out["To"] = contact.emails[0].email
        out["Subject"] = subject
        out["Message-ID"] = (
            f"<gmail-own-{row.id}@mail.example>"
            if draft_id is not None
            else legacy_message_id(user.id, row, mailbox.email.rpartition("@")[2].lower())
            if legacy
            else message_id_for(
                user_id=user.id,
                message_id=row.id,
                created_at=row.created_at,
                address=mailbox.email,
            )
        )
        out.set_content("Hello.")
        ref = world.gmail.send(out, purpose="test: a campaign email")
        row.gmail_message_id, row.gmail_thread_id = ref.id, ref.thread_id
        return ref

    return world.write(make)


def enroll_again(world: World, enrollment_id: int, campaign_id: int) -> int:
    """The contact of ``enrollment_id``, enrolled in ``campaign_id`` too, nothing sent."""

    def make(session: Session, user: User) -> int:
        campaign = get_scoped(session, user, Campaign, campaign_id)
        enrollment = get_scoped(session, user, Enrollment, enrollment_id)
        assert campaign is not None and enrollment is not None
        contact = get_scoped(session, user, Contact, enrollment.contact_id)
        assert contact is not None
        return factories.make_enrollment(
            session, campaign, contact, next_action_at=NOW + timedelta(days=5)
        ).id

    return world.write(make)


def labels_of(world: World) -> dict[int, str | None]:
    """Each watch's label by enrollment id."""
    [work] = world.write(lambda s, u: replies.reply_work(s, u, now=NOW))
    return {w.enrollment_id: w.label for w in work.watches}


def poll(world: World) -> list[tuple[str, str]]:
    """One poll; the ``(label, message id)`` it labelled."""
    labelled: list[tuple[str, str]] = []
    replies.poll_replies(
        world.factory,
        world.user_id,
        open_gmail=lambda user_id, mailbox_id: world.gmail,
        now=NOW,
        label=lambda gmail, mailbox_id, name, message_id: labelled.append((name, message_id)),
    )
    return labelled


# --- a reply stops every watch ---------------------------------------------------------


@pytest.mark.parametrize("path", ["history", "scan"])
def test_a_reply_in_one_campaigns_thread_stops_the_contacts_other_campaign(
    world: World, path: str
) -> None:
    """The contact is in campaigns A and B, both emailed. A reply in A's thread stops B
    too, and carries A's label only: B's label never goes on A's thread."""
    a_campaign = world.campaign(EMAIL, own_mailbox=True)
    b_campaign = world.campaign(EMAIL, EMAIL, own_mailbox=True)
    a = world.enroll(campaign_id=a_campaign, sent_at=None)
    b = enroll_again(world, a, b_campaign)
    to_a = send_email(world, a, "About the A note", FIRST_SEND)
    send_email(world, b, "About the B note", FIRST_SEND + timedelta(hours=1))
    if path == "history":
        baseline(world)
    else:
        world.set_mailbox(history_id=None)
    labels = labels_of(world)
    assert None not in (labels[a], labels[b]) and labels[a] != labels[b]

    reply = world.gmail.reply(to_a, sender=QUORBELLE, at=NOW - timedelta(hours=3))
    labelled = poll(world)

    for enrollment_id in (a, b):
        enrollment = world.enrollment(enrollment_id)
        assert (enrollment.status, enrollment.replied_at) == (
            EnrollmentStatus.REPLIED,
            NOW - timedelta(hours=3),
        ), enrollment_id
        [message] = world.inbound(enrollment_id)
        assert (message.gmail_message_id, message.status) == (reply.id, MessageStatus.RECEIVED)
    assert labelled == [(labels[a], reply.id)]


@pytest.mark.parametrize("path", ["history", "scan"])
def test_a_reply_in_a_campaign_thread_stops_a_linkedin_only_enrollment(
    world: World, path: str
) -> None:
    """#571's LinkedIn-only enrollment of the same contact stops too, unlabelled. One
    whose first send came after the reply does not: it answers nothing of that one."""
    email_campaign = world.campaign(EMAIL, own_mailbox=True)
    later_campaign = world.campaign(LINKEDIN, LINKEDIN)
    emailed = world.enroll(campaign_id=email_campaign, sent_at=None)
    linkedin_only = enroll_again(world, emailed, world.campaign_id)
    later = enroll_again(world, emailed, later_campaign)
    to_emailed = send_email(world, emailed, "Hello", FIRST_SEND)
    world.sent(linkedin_only, 1, FIRST_SEND + timedelta(hours=1))
    world.sent(later, 1, NOW - timedelta(hours=1))
    if path == "history":
        baseline(world)
    else:
        world.set_mailbox(history_id=None)

    reply = world.gmail.reply(to_emailed, sender=QUORBELLE, at=NOW - timedelta(hours=3))
    labelled = poll(world)

    assert world.enrollment(emailed).status is EnrollmentStatus.REPLIED
    assert world.enrollment(linkedin_only).status is EnrollmentStatus.REPLIED
    [message] = world.inbound(linkedin_only)
    assert (message.channel, message.gmail_message_id) == (EMAIL, reply.id)
    assert world.enrollment(later).status is EnrollmentStatus.ACTIVE
    assert world.inbound(later) == []
    assert [message_id for _, message_id in labelled] == [reply.id]  # the email campaign's


def test_a_fresh_email_still_carries_each_watching_campaigns_label(world: World) -> None:
    """Outside every watched thread, as before #695: each campaign's label."""
    a_campaign = world.campaign(EMAIL, own_mailbox=True)
    b_campaign = world.campaign(EMAIL, own_mailbox=True)
    a = world.enroll(campaign_id=a_campaign, sent_at=None)
    b = enroll_again(world, a, b_campaign)
    send_email(world, a, "About the A note", FIRST_SEND)
    send_email(world, b, "About the B note", FIRST_SEND)
    baseline(world)
    labels = labels_of(world)

    fresh = world.gmail.deliver(email(QUORBELLE, "Coffee?"), at=NOW - timedelta(hours=3))
    labelled = poll(world)

    assert sorted(labelled) == sorted([(labels[a], fresh.id), (labels[b], fresh.id)])


# --- replied_at is the earliest --------------------------------------------------------


def test_replied_at_is_the_earliest_response_whatever_the_order_recorded(world: World) -> None:
    """A detected reply is recorded on a completed enrollment, then a response from
    outside netkeep dated before it: ``replied_at`` moves back to the earlier one, the
    step results credit."""
    enrollment_id = world.enroll(status=EnrollmentStatus.COMPLETED)
    world.gmail.deliver(email(QUORBELLE), at=NOW - timedelta(hours=3))
    world.poll()
    assert world.enrollment(enrollment_id).replied_at == NOW - timedelta(hours=3)
    earlier = NOW - timedelta(days=1)

    def record(session: Session, user: User) -> None:
        enrollment = get_scoped(session, user, Enrollment, enrollment_id)
        assert enrollment is not None
        outside_responses.record_outside_response(
            session,
            user,
            enrollment.contact_id,
            via=ResponseVia.CALL,
            at=earlier,
            now=NOW,
            enrollment_id=enrollment_id,
        )

    world.write(record)

    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.replied_at) == (EnrollmentStatus.COMPLETED, earlier)
    # A later response leaves it at the earliest.
    world.gmail.deliver(email(QUORBELLE, "One more thing"), at=NOW - timedelta(hours=1))
    world.poll(NOW + timedelta(minutes=10))
    assert len(world.inbound(enrollment_id)) == 3
    assert world.enrollment(enrollment_id).replied_at == earlier


# --- bounce matching -------------------------------------------------------------------


def notice_citing(world: World, sent: MessageRef) -> MessageRef:
    """A mailer daemon's failure notice citing ``sent``'s Message-ID, in a thread of its own."""
    original = world.gmail.get_message(sent.id, purpose="test")
    notice = email("MAILER-DAEMON@mx.fictionary.example", "Undeliverable", "No such user.")
    notice["References"] = original.header("Message-ID") or ""
    ref = world.gmail.deliver(notice, at=NOW - timedelta(hours=1))
    assert ref.thread_id != sent.thread_id
    return ref


def test_a_bounce_is_matched_by_the_derived_message_id_with_one_read(world: World) -> None:
    """Three contacts emailed, one notice: the poll reads the notice and the one copy it
    cites (for its ``To``), not every watched message's copy."""
    campaign_id = world.campaign(EMAIL, own_mailbox=True)
    sent = [
        send_email(
            world,
            world.enroll((address,), campaign_id=campaign_id, sent_at=None),
            f"Hello {n}",
            FIRST_SEND,
        )
        for n, address in enumerate(
            (QUORBELLE, "zephyrine@fictionary.example", "orvald@fictionary.example")
        )
    ]
    baseline(world)
    notice_citing(world, sent[1])
    world.gmail.calls.clear()

    poll(world)

    assert [m for m, _ in world.gmail.calls].count("messages.get") == 2
    statuses = world.write(
        lambda s, u: {
            m.gmail_message_id: m.status
            for m in s.scalars(scoped(u, Message).where(Message.gmail_message_id.is_not(None)))
        }
    )
    assert statuses == {
        sent[0].id: MessageStatus.SENT,
        sent[1].id: MessageStatus.BOUNCED,
        sent[2].id: MessageStatus.SENT,
    }


def test_a_bounce_of_a_message_sent_before_the_rename_is_matched(world: World) -> None:
    """A message sent before #661 carries the Message-ID of the old seed: a notice citing
    it from outside the thread still bounces it, with no read of every copy."""
    campaign_id = world.campaign(EMAIL, own_mailbox=True)
    enrollment_id = world.enroll(campaign_id=campaign_id, sent_at=None)
    sent = send_email(world, enrollment_id, "Hello", FIRST_SEND, legacy=True)
    baseline(world)
    notice_citing(world, sent)

    poll(world)

    assert world.enrollment(enrollment_id).status is EnrollmentStatus.BOUNCED


def test_a_bounce_of_a_sent_draft_is_matched_by_its_gmail_copy(world: World) -> None:
    """A draft the person sent may carry a Message-ID of Gmail's making: its copy is read,
    so a notice citing it outside the thread still bounces it."""
    campaign_id = world.campaign(EMAIL, own_mailbox=True)
    enrollment_id = world.enroll(campaign_id=campaign_id, sent_at=None)
    sent = send_email(world, enrollment_id, "Hello", FIRST_SEND, draft_id="r1")
    baseline(world)
    notice_citing(world, sent)

    poll(world)

    assert world.enrollment(enrollment_id).status is EnrollmentStatus.BOUNCED


# --- what reply_work loads --------------------------------------------------------------


def test_reply_work_never_loads_a_completed_enrollment_past_its_window(world: World) -> None:
    old = world.enroll(sent_at=NOW - timedelta(days=60), status=EnrollmentStatus.COMPLETED)
    recent = world.enroll(
        ("zephyrine@fictionary.example",),
        sent_at=NOW - timedelta(days=29),
        status=EnrollmentStatus.COMPLETED,
    )
    live = world.enroll(("orvald@fictionary.example",), sent_at=NOW - timedelta(days=60))

    loaded: set[int] = set()

    def on_load(row: Enrollment, context: object) -> None:
        loaded.add(row.id)

    def load(session: Session, user: User) -> set[int]:
        session.expunge_all()  # every enrollment the poll reads is loaded afresh
        event.listen(Enrollment, "load", on_load)
        try:
            [work] = replies.reply_work(session, user, now=NOW)
        finally:
            event.remove(Enrollment, "load", on_load)
        return {w.enrollment_id for w in work.watches}

    watched = world.write(load)

    assert watched == {recent, live}
    assert loaded == {recent, live}
    assert old not in loaded  # never read
