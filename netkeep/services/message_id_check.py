"""The Message-ID check that arming a mailbox to send waits for (#277, #304, #510).

Arming to send needs :attr:`~netkeep.models.Mailbox.message_id_verified_at`: the
live proof that Gmail keeps the Message-ID netkeep sets, which reconcile depends on.
The drafts check (:mod:`netkeep.services.campaign_sender`) records it the first time
a search for ``rfc822msgid:<netkeep's own Message-ID>`` finds a message, in any
folder. What it searches for, on an armed mailbox not verified yet:

- the newest test drafts made there (:func:`~netkeep.services.mailboxes.recent_test_drafts`);
- the oldest campaign draft netkeep still tracks there;
- the campaign drafts netkeep saw sent there (:func:`sent_drafts_to_verify`): a draft
  the person sent keeps the Message-ID it was made with, so a draft sent before any
  check found it still counts. A message Gmail sent under a Message-ID of its own
  making is not found, and verifies nothing.

Nothing here arms a mailbox. Arming to send stays a person's action
(:func:`netkeep.services.mailboxes.arm`). :func:`arm_steps` says where a mailbox is
on the way there, for the Settings page and ``netkeep gmail status``. Read-only.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import Select
from sqlalchemy.orm import Session

from netkeep.campaigns.compose import ComposeError, message_id_for
from netkeep.models import (
    Campaign,
    CampaignStatus,
    Enrollment,
    Mailbox,
    MailboxArm,
    MailboxStatus,
    Message,
    MessageDirection,
    MessageStatus,
    TemplateChannel,
    User,
)
from netkeep.scoping import get_scoped, scoped
from netkeep.services import mailboxes as mailbox_service

log = logging.getLogger(__name__)

SENT_DRAFTS_CHECKED: Final = 3
"""The most sent campaign drafts one drafts check searches for, per mailbox, newest first."""

SENT_DRAFTS_WITHIN: Final = timedelta(days=14)
"""How long after it was seen sent a campaign draft still counts toward the check."""


@dataclass(frozen=True, slots=True)
class SentDraftCheck:
    """A campaign draft netkeep saw sent, to search for by its Message-ID."""

    mailbox_id: int
    message_id: int
    rfc822_message_id: str


def _on_mailbox(user: User, statement: Select[Message], mailbox_id: int) -> Select[Message]:
    """``statement`` narrowed to messages of campaigns that send from ``mailbox_id``."""
    return (
        statement.join(Enrollment, Enrollment.id == Message.enrollment_id)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .where(
            Enrollment.user_id == user.id,
            Campaign.user_id == user.id,
            Campaign.mailbox_id == mailbox_id,
        )
    )


def _sent_drafts(
    session: Session, user: User, mailbox: Mailbox, *, now: datetime
) -> list[SentDraftCheck]:
    statement = scoped(user, Message).where(
        Message.channel == TemplateChannel.EMAIL,
        Message.direction == MessageDirection.OUT,
        Message.status == MessageStatus.SENT,
        Message.gmail_draft_id.is_not(None),
        Message.sent_at >= now - SENT_DRAFTS_WITHIN,
    )
    rows = session.scalars(
        _on_mailbox(user, statement, mailbox.id)
        .order_by(Message.sent_at.desc(), Message.id.desc())
        .limit(SENT_DRAFTS_CHECKED)
    )
    out: list[SentDraftCheck] = []
    for row in rows:
        try:
            rfc822 = message_id_for(
                user_id=user.id, message_id=row.id, created_at=row.created_at, address=mailbox.email
            )
        except ComposeError:
            log.error("message %d has no Message-ID to search for; not checked", row.id)
            continue
        out.append(SentDraftCheck(mailbox.id, row.id, rfc822))
    return out


def _unverified_armed(session: Session, user: User) -> list[Mailbox]:
    return list(
        session.scalars(
            scoped(user, Mailbox)
            .where(Mailbox.armed_at.is_not(None), Mailbox.message_id_verified_at.is_(None))
            .order_by(Mailbox.id)
            .execution_options(populate_existing=True)
        )
    )


def sent_drafts_to_verify(session: Session, user: User, *, now: datetime) -> list[SentDraftCheck]:
    """On each armed mailbox not verified yet, the newest :data:`SENT_DRAFTS_CHECKED`
    campaign drafts netkeep saw sent within :data:`SENT_DRAFTS_WITHIN`. Nothing for a
    disarmed or verified mailbox. Read-only."""
    return [
        check
        for mailbox in _unverified_armed(session, user)
        for check in _sent_drafts(session, user, mailbox, now=now)
    ]


def record_checked(session: Session, user: User, mailbox_id: int, *, now: datetime) -> None:
    """The drafts check searched Gmail for one of the mailbox's drafts and got an answer.
    Needs a writer session."""
    mailbox = get_scoped(session, user, Mailbox, mailbox_id)
    if mailbox is None:
        return
    mailbox.message_id_checked_at = now
    session.flush()


# --- the way to arming to send ------------------------------------------------------


class MessageIdWait(enum.StrEnum):
    """Why the Message-ID check has not passed yet, in the order it is read."""

    DISCONNECTED = "disconnected"
    NOT_ARMED = "not_armed"
    """Not armed for drafts: the check runs only on an armed mailbox."""
    REAUTH_REQUIRED = "reauth_required"
    """The mailbox needs you to sign in again before Gmail can be searched."""
    NO_DRAFT = "no_draft"
    """No draft netkeep made there to search for: no test draft, none waiting, none
    seen sent recently."""
    TEST_DRAFT_NOT_FOUND = "test_draft_not_found"
    """Only test drafts to search for, and the last check found none of them."""
    NOT_CHECKED = "not_checked"
    """A draft to search for, and no check has looked yet."""
    NOT_FOUND = "not_found"
    """The last check searched and has not found one yet (Gmail's search can lag)."""


class ArmNextStep(enum.StrEnum):
    """The button that moves a mailbox along."""

    ARM_DRAFTS = "arm_drafts"
    REAUTHORIZE = "reauthorize"
    SEND_TEST = "send_test"
    CHECK_NOW = "check_now"
    ARM_SEND = "arm_send"


@dataclass(frozen=True, slots=True)
class ArmSteps:
    """Where a mailbox is on the way to arming to send. ``can_arm_send`` is exactly
    what :func:`~netkeep.services.mailboxes.arm` allows."""

    mailbox_id: int
    armed_for_drafts: bool
    armed_to_send: bool
    verified_at: datetime | None
    waiting: MessageIdWait | None
    """None once verified."""
    checked_at: datetime | None
    """When the last Message-ID check got an answer from Gmail."""
    can_arm_send: bool
    next_step: ArmNextStep | None
    """None when the mailbox is armed to send, or disconnected."""
    test_campaign_id: int | None
    """A campaign in review on this mailbox, where you can send a test; None if none."""


def _has_tracked_draft(session: Session, user: User, mailbox: Mailbox) -> bool:
    statement = scoped(user, Message).where(
        Message.channel == TemplateChannel.EMAIL,
        Message.status == MessageStatus.DRAFTED,
        Message.gmail_draft_id.is_not(None),
    )
    found = session.scalars(_on_mailbox(user, statement, mailbox.id).limit(1)).first()
    return found is not None


def _test_campaign(session: Session, user: User, mailbox: Mailbox) -> int | None:
    return session.scalar(
        scoped(user, Campaign)
        .with_only_columns(Campaign.id)
        .where(
            Campaign.mailbox_id == mailbox.id,
            Campaign.status.in_((CampaignStatus.DRAFT, CampaignStatus.REVIEWING)),
        )
        .order_by(Campaign.updated_at.desc(), Campaign.id.desc())
        .limit(1)
    )


def _waiting(session: Session, user: User, mailbox: Mailbox, *, now: datetime) -> MessageIdWait:
    if mailbox.status is MailboxStatus.DISABLED:
        return MessageIdWait.DISCONNECTED
    if mailbox.armed_at is None:
        return MessageIdWait.NOT_ARMED
    if mailbox.status is not MailboxStatus.OK:
        return MessageIdWait.REAUTH_REQUIRED
    tests = mailbox_service.recent_test_drafts(session, user, mailbox)
    campaign = _has_tracked_draft(session, user, mailbox) or bool(
        _sent_drafts(session, user, mailbox, now=now)
    )
    if not tests and not campaign:
        return MessageIdWait.NO_DRAFT
    if not campaign and all(row.not_found_at is not None for row in tests):
        return MessageIdWait.TEST_DRAFT_NOT_FOUND
    if mailbox.message_id_checked_at is None:
        return MessageIdWait.NOT_CHECKED
    return MessageIdWait.NOT_FOUND


_NEXT: Final = {
    MessageIdWait.DISCONNECTED: None,
    MessageIdWait.NOT_ARMED: ArmNextStep.ARM_DRAFTS,
    MessageIdWait.REAUTH_REQUIRED: ArmNextStep.REAUTHORIZE,
    MessageIdWait.NO_DRAFT: ArmNextStep.SEND_TEST,
    MessageIdWait.TEST_DRAFT_NOT_FOUND: ArmNextStep.SEND_TEST,
    MessageIdWait.NOT_CHECKED: ArmNextStep.CHECK_NOW,
    MessageIdWait.NOT_FOUND: ArmNextStep.CHECK_NOW,
}


def arm_steps(session: Session, user: User, mailbox: Mailbox, *, now: datetime) -> ArmSteps:
    """Where ``mailbox`` is on the way to arming to send. Read-only."""
    verified = mailbox.message_id_verified_at
    arm = mailbox.arm
    waiting = None if verified is not None else _waiting(session, user, mailbox, now=now)
    disconnected = mailbox.status is MailboxStatus.DISABLED
    can_arm_send = not disconnected and arm is MailboxArm.DRAFT and verified is not None
    next_step: ArmNextStep | None
    if disconnected or arm is MailboxArm.SEND:
        next_step = None
    elif waiting is not None:
        next_step = _NEXT[waiting]
    elif arm is None:
        next_step = ArmNextStep.ARM_DRAFTS
    else:
        next_step = ArmNextStep.ARM_SEND
    return ArmSteps(
        mailbox_id=mailbox.id,
        armed_for_drafts=arm is not None,
        armed_to_send=arm is MailboxArm.SEND,
        verified_at=verified,
        waiting=waiting,
        checked_at=mailbox.message_id_checked_at,
        can_arm_send=can_arm_send,
        next_step=next_step,
        test_campaign_id=_test_campaign(session, user, mailbox),
    )


def _at(value: datetime) -> str:
    return f"{value:%Y-%m-%d %H:%M} UTC"


WAITING_TEXT: Final = {
    MessageIdWait.DISCONNECTED: "the mailbox is disconnected; connect it again first",
    MessageIdWait.NOT_ARMED: "arm it for drafts first; netkeep checks only an armed mailbox",
    MessageIdWait.REAUTH_REQUIRED: "waiting: the mailbox needs you to sign in again",
    MessageIdWait.NO_DRAFT: "waiting: no draft yet; send a test from a campaign's review",
    MessageIdWait.TEST_DRAFT_NOT_FOUND: (
        "waiting: the last check didn't find the test draft; if you discarded it,"
        " send a new test from a campaign's review"
    ),
    MessageIdWait.NOT_CHECKED: "waiting: not checked yet; the next drafts check looks for it",
    MessageIdWait.NOT_FOUND: "waiting: not found yet",
}


def describe(steps: ArmSteps) -> list[str]:
    """The three steps, one line each, for ``netkeep gmail status``."""
    drafts = "done" if steps.armed_for_drafts else "not yet"
    if steps.verified_at is not None:
        found = f"done {_at(steps.verified_at)}"
    else:
        assert steps.waiting is not None
        found = WAITING_TEXT[steps.waiting]
        if steps.waiting is MessageIdWait.NOT_FOUND and steps.checked_at is not None:
            found += f"; last checked {_at(steps.checked_at)}"
    if steps.armed_to_send:
        send = "done; `netkeep gmail disarm` stops it"
    elif steps.can_arm_send:
        send = "ready: `netkeep gmail arm --send`"
    else:
        send = "not yet: waits for the steps above"
    return [
        f"1. armed for drafts: {drafts}",
        f"2. netkeep found its own draft: {found}",
        f"3. arm to send: {send}",
    ]
