"""An email reply stops an enrollment that has sent only on LinkedIn (#571).

The Gmail reply poll (:func:`netkeeper.services.campaign_replies.poll_replies`) watches
such an enrollment through an armed mailbox: mail from one of the contact's known
addresses, dated after the enrollment's first send, is its reply. Nothing else is, and
nothing in Gmail changes for it. Every address and message is invented, and Gmail is
:class:`FakeGmail`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any

import factories
import pytest
from campaign_fakes import ARMED_FOR_SEND, NOW, make_mailbox
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns.gmail_fake import FakeGmail
from netkeeper.db import session_scope
from netkeeper.models import (
    Campaign,
    Contact,
    Enrollment,
    EnrollmentStatus,
    Interaction,
    InteractionKind,
    Mailbox,
    Message,
    MessageDirection,
    MessageStatus,
    TemplateChannel,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import campaign_replies as replies

QUORBELLE = "quorbelle@fictionary.example"
FIRST_SEND = NOW - timedelta(days=2)
READ_ONLY = frozenset(
    {"users.getProfile", "history.list", "messages.get", "messages.list", "threads.get"}
)
"""The Gmail calls a LinkedIn-only watch may make: reads, never a label or a change."""


@dataclass
class World:
    factory: sessionmaker[Session]
    user_id: int
    mailbox_id: int
    campaign_id: int
    gmail: FakeGmail

    def write[T](self, fn: Callable[[Session, User], T]) -> T:
        with session_scope(self.factory, write=True) as session:
            user = session.get(User, self.user_id)
            assert user is not None
            return fn(session, user)

    def enroll(
        self,
        emails: tuple[str, ...] = (QUORBELLE,),
        sent_at: datetime | None = FIRST_SEND,
        *,
        campaign_id: int | None = None,
        **overrides: Any,
    ) -> int:
        """A contact with ``emails``, enrolled; its LinkedIn step 1 went out at ``sent_at``."""

        def make(session: Session, user: User) -> int:
            campaign = get_scoped(session, user, Campaign, campaign_id or self.campaign_id)
            assert campaign is not None
            contact = factories.make_contact(session, user, emails=list(emails))
            fields: dict[str, Any] = {"current_step": 1, "next_action_at": NOW + timedelta(days=5)}
            fields.update(overrides)
            enrollment = factories.make_enrollment(session, campaign, contact, **fields)
            if sent_at is not None:
                factories.make_message(session, enrollment, position=1, sent_at=sent_at)
            return enrollment.id

        return self.write(make)

    def sent(self, enrollment_id: int, position: int, at: datetime, **overrides: Any) -> None:
        """Step ``position`` of the enrollment went out at ``at``."""

        def make(session: Session, user: User) -> None:
            enrollment = get_scoped(session, user, Enrollment, enrollment_id)
            assert enrollment is not None
            enrollment.current_step = position
            factories.make_message(session, enrollment, position=position, sent_at=at, **overrides)

        self.write(make)

    def campaign(self, *channels: TemplateChannel, own_mailbox: bool = False) -> int:
        """Another campaign of the user, on the armed mailbox when ``own_mailbox``."""

        def make(session: Session, user: User) -> int:
            mailbox_id = self.mailbox_id if own_mailbox else None
            return factories.make_campaign(
                session, user, channels=channels, mailbox_id=mailbox_id
            ).id

        return self.write(make)

    def poll(self, now: datetime = NOW, *, labels: bool = False) -> replies.RepliesPolled:
        """One poll; ``labels``: a reply may be labelled (a watch with a sent email)."""
        labelled: list[str] = []
        polled = replies.poll_replies(
            self.factory,
            self.user_id,
            open_gmail=lambda user_id, mailbox_id: self.gmail,
            now=now,
            label=lambda gmail, mailbox_id, name, message_id: labelled.append(message_id),
        )
        if not labels:
            assert labelled == []  # a LinkedIn-only watch's reply is never labelled
        return polled

    def enrollment(self, enrollment_id: int) -> Enrollment:
        row = self.write(lambda s, u: get_scoped(s, u, Enrollment, enrollment_id))
        assert row is not None
        return row

    def inbound(self, enrollment_id: int) -> list[Message]:
        return self.write(
            lambda s, u: list(
                s.scalars(
                    scoped(u, Message).where(
                        Message.enrollment_id == enrollment_id,
                        Message.direction == MessageDirection.IN,
                    )
                )
            )
        )

    def set_mailbox(self, **changes: object) -> None:
        def change(session: Session, user: User) -> None:
            mailbox = get_scoped(session, user, Mailbox, self.mailbox_id)
            assert mailbox is not None
            for name, value in changes.items():
                setattr(mailbox, name, value)

        self.write(change)


@pytest.fixture
def world(session_factory: sessionmaker[Session]) -> World:
    """A LinkedIn-only campaign of two steps, with no mailbox of its own, and the user's
    one mailbox armed."""
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        mailbox = make_mailbox(session, user, **ARMED_FOR_SEND)
        campaign = factories.make_campaign(
            session, user, channels=(TemplateChannel.LINKEDIN, TemplateChannel.LINKEDIN)
        )
        assert campaign.mailbox_id is None
        ids = (user.id, mailbox.id, campaign.id)
    gmail = FakeGmail(f"me{ids[0]}@example.test", mailbox_id=ids[1], clock=lambda: NOW)
    return World(session_factory, *ids, gmail=gmail)


def email(
    sender: str, subject: str = "Re: your note", body: str = "Happy to talk."
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "me@example.test"
    message["Subject"] = subject
    message.set_content(body)
    return message


def baseline(world: World) -> None:
    """The first poll scans and stores a history baseline, so the next reads history."""
    world.poll(NOW - timedelta(days=1))
    mailbox = world.write(lambda s, u: get_scoped(s, u, Mailbox, world.mailbox_id))
    assert mailbox is not None and mailbox.history_id is not None


# --- what is watched ------------------------------------------------------------------


def test_a_linkedin_only_enrollment_is_watched_from_its_first_linkedin_send(
    world: World,
) -> None:
    enrollment_id = world.enroll()

    [work] = world.write(lambda s, u: replies.reply_work(s, u, now=NOW))

    [watch] = work.watches
    assert (watch.enrollment_id, watch.addresses) == (enrollment_id, frozenset({QUORBELLE}))
    assert watch.first_sent_at == FIRST_SEND
    assert (watch.sent, watch.label) == ((), None)


def test_one_with_nothing_sent_or_no_address_is_not_watched(world: World) -> None:
    world.enroll(sent_at=None)
    world.enroll(emails=())

    [work] = world.write(lambda s, u: replies.reply_work(s, u, now=NOW))

    assert work.watches == ()
    world.poll()
    assert world.gmail.calls == []  # nothing to watch: no Gmail call


# --- replies --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["history", "scan"])
def test_an_email_from_the_contact_after_the_first_send_stops_it(world: World, path: str) -> None:
    enrollment_id = world.enroll()
    if path == "history":
        baseline(world)
    world.gmail.deliver(email(f"Quorbelle <{QUORBELLE.upper()}>"), at=NOW - timedelta(hours=3))
    world.gmail.calls.clear()

    polled = world.poll()

    assert polled.caught_up == frozenset({world.mailbox_id})
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.exit_reason) == (EnrollmentStatus.REPLIED, "replied")
    assert enrollment.replied_at == NOW - timedelta(hours=3)
    [message] = world.inbound(enrollment_id)
    assert (message.channel, message.status) == (TemplateChannel.EMAIL, MessageStatus.RECEIVED)
    assert (message.subject, message.snippet) == ("Re: your note", "Happy to talk.")
    [interaction] = world.write(
        lambda s, u: list(
            s.scalars(
                scoped(u, Interaction).where(
                    Interaction.contact_id == enrollment.contact_id,
                    Interaction.kind == InteractionKind.EMAIL_IN,
                )
            )
        )
    )
    assert interaction.message_id == message.id
    # Read only: nothing labelled, created or changed in Gmail.
    assert {method for method, _ in world.gmail.calls} <= READ_ONLY

    # Idempotent: the next poll records nothing more.
    world.poll(NOW + timedelta(minutes=10))
    assert len(world.inbound(enrollment_id)) == 1


@pytest.mark.parametrize("path", ["history", "scan"])
def test_mail_from_before_the_first_send_or_from_an_unknown_address_is_ignored(
    world: World, path: str
) -> None:
    enrollment_id = world.enroll()
    if path == "history":
        baseline(world)
    world.gmail.deliver(email(QUORBELLE, "Old news"), at=FIRST_SEND - timedelta(hours=1))
    world.gmail.deliver(email("someone-else@fictionary.example"), at=NOW - timedelta(hours=1))

    world.poll()

    assert world.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE
    assert world.inbound(enrollment_id) == []


def test_an_unsubscribe_by_email_opts_it_out(world: World) -> None:
    enrollment_id = world.enroll()
    world.gmail.deliver(email(QUORBELLE, "Please remove me"), at=NOW - timedelta(hours=1))

    world.poll()

    assert world.enrollment(enrollment_id).status is EnrollmentStatus.OPTED_OUT


def test_with_no_mailbox_armed_nothing_is_read_and_nothing_changes(world: World) -> None:
    enrollment_id = world.enroll()
    world.gmail.deliver(email(QUORBELLE), at=NOW - timedelta(hours=1))
    world.set_mailbox(armed_at=None, send_armed_at=None)

    assert world.write(lambda s, u: replies.reply_work(s, u, now=NOW)) == []
    world.poll()

    assert world.gmail.calls == []
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE

    def watched(session: Session, user: User) -> bool:
        campaign = get_scoped(session, user, Campaign, world.campaign_id)
        assert campaign is not None
        return replies.email_replies_watched(session, user, campaign)

    assert not world.write(watched)
    world.set_mailbox(**ARMED_FOR_SEND)
    assert world.write(watched)


def test_a_campaign_with_its_own_disarmed_mailbox_is_not_watched_by_another(
    world: World,
) -> None:
    """A campaign's own mailbox watches it; another armed mailbox never stands in."""

    def own_mailbox(session: Session, user: User) -> None:
        mine = make_mailbox(session, user, email="other@example.test", keychain_ref="gmail/2")
        campaign = get_scoped(session, user, Campaign, world.campaign_id)
        assert campaign is not None
        campaign.status = campaign.status.DRAFT
        campaign.mailbox_id = mine.id
        campaign.status = campaign.status.ACTIVE

    world.write(own_mailbox)
    world.enroll()

    [work] = world.write(lambda s, u: replies.reply_work(s, u, now=NOW))

    assert (work.mailbox_id, work.watches) == (world.mailbox_id, ())


# --- review of #572 -------------------------------------------------------------------


@pytest.mark.parametrize("path", ["history", "scan"])
def test_a_mixed_campaign_counts_from_its_first_linkedin_send(world: World, path: str) -> None:
    """LinkedIn step 1 goes, the contact answers by email, and the campaign's first email
    (step 2) goes before any poll reads the answer: it is still a reply, as a reply to
    step 1 read only after step 2 went out is on an email-only campaign."""
    campaign_id = world.campaign(TemplateChannel.LINKEDIN, TemplateChannel.EMAIL, own_mailbox=True)
    enrollment_id = world.enroll(campaign_id=campaign_id)
    if path == "history":
        baseline(world)
    world.gmail.deliver(email(QUORBELLE, "About your note"), at=NOW - timedelta(hours=3))
    out = EmailMessage()
    out["From"] = world.gmail.address
    out["To"] = QUORBELLE
    out["Subject"] = "Hello"
    out.set_content("Step 2.")
    ref = world.gmail.send(out, purpose="test: the campaign's first email")
    world.sent(
        enrollment_id,
        2,
        NOW - timedelta(hours=1),
        gmail_message_id=ref.id,
        gmail_thread_id=ref.thread_id,
    )
    if path == "scan":
        world.set_mailbox(history_id=None)

    [work] = world.write(lambda s, u: replies.reply_work(s, u, now=NOW))
    [watch] = work.watches
    assert watch.first_sent_at == FIRST_SEND and len(watch.sent) == 1
    world.poll(labels=True)

    assert world.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED
    assert [m.subject for m in world.inbound(enrollment_id)] == ["About your note"]


def test_an_email_only_watch_still_counts_from_its_first_email(world: World) -> None:
    """The change for #571 leaves an email-only enrollment's watch as it was."""
    campaign_id = world.campaign(TemplateChannel.EMAIL, TemplateChannel.EMAIL, own_mailbox=True)
    enrollment_id = world.enroll(campaign_id=campaign_id, sent_at=None)
    world.sent(enrollment_id, 1, FIRST_SEND, gmail_message_id="m1", gmail_thread_id="t1")
    world.sent(
        enrollment_id, 2, NOW - timedelta(hours=1), gmail_message_id="m2", gmail_thread_id="t1"
    )

    [work] = world.write(lambda s, u: replies.reply_work(s, u, now=NOW))

    [watch] = work.watches
    assert watch.first_sent_at == FIRST_SEND
    assert [s.gmail_message_id for s in watch.sent] == ["m1", "m2"]
    assert watch.label is not None


def test_two_linkedin_sends_count_from_the_first(world: World) -> None:
    enrollment_id = world.enroll()
    world.sent(enrollment_id, 2, NOW - timedelta(hours=1))
    world.gmail.deliver(email(QUORBELLE), at=NOW - timedelta(hours=3))  # between the two

    world.poll()

    assert world.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED


@pytest.mark.parametrize(("days", "watched"), [(29, True), (31, False)])
def test_a_completed_linkedin_only_enrollment_is_watched_thirty_days_after_its_last_send(
    world: World, days: int, watched: bool
) -> None:
    enrollment_id = world.enroll(
        sent_at=NOW - timedelta(days=60), status=EnrollmentStatus.COMPLETED
    )
    world.sent(enrollment_id, 2, NOW - timedelta(days=days))

    [work] = world.write(lambda s, u: replies.reply_work(s, u, now=NOW))

    assert [w.enrollment_id for w in work.watches] == ([enrollment_id] if watched else [])


def test_an_out_of_office_does_not_stop_it(world: World) -> None:
    enrollment_id = world.enroll()
    auto = email(QUORBELLE, "Out of office", "Back next week.")
    auto["Auto-Submitted"] = "auto-replied"
    world.gmail.deliver(auto, at=NOW - timedelta(hours=1))

    world.poll()

    assert world.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE
    assert world.inbound(enrollment_id) == []


def test_the_scan_searches_each_address_once_from_its_earliest_send(world: World) -> None:
    """One contact in two campaigns: one ``from:`` search, from the earlier first send."""
    first = world.enroll()
    other = world.campaign(TemplateChannel.LINKEDIN)

    def enroll_again(session: Session, user: User) -> None:
        campaign = get_scoped(session, user, Campaign, other)
        enrollment = get_scoped(session, user, Enrollment, first)
        assert campaign is not None and enrollment is not None
        contact = get_scoped(session, user, Contact, enrollment.contact_id)
        assert contact is not None
        again = factories.make_enrollment(
            session, campaign, contact, current_step=1, next_action_at=NOW
        )
        factories.make_message(session, again, position=1, sent_at=NOW - timedelta(hours=5))

    world.write(enroll_again)
    second = world.write(
        lambda s, u: (
            s.scalars(scoped(u, Enrollment).where(Enrollment.campaign_id == other)).one().id
        )
    )
    # Between the two first sends: only the earlier search window holds it.
    world.gmail.deliver(email(QUORBELLE), at=NOW - timedelta(days=1))
    world.gmail.calls.clear()

    world.poll()

    assert [m for m, _ in world.gmail.calls].count("messages.list") == 1
    assert world.enrollment(first).status is EnrollmentStatus.REPLIED
    assert world.enrollment(second).status is EnrollmentStatus.ACTIVE
