"""Gmail drafts netkeeper no longer wants, for you to clean up by hand (#543). Read-only.

netkeeper deletes nothing in Gmail (ADR 0003, ADR 0009), so a draft it stops needing
stays in your Drafts. This lists them, from ``docs/gmail-deletes.md``'s cleanup list:

- **Discarded:** a ``discarded`` message that still has a ``gmail_draft_id`` (case 1:
  a contact merge sets only the status). A discarded draft you send is recorded
  ``sent``, and one you delete loses its draft id (#544), so either drops off.
- **Ended:** a ``drafted`` message whose enrollment ended (cases 2 to 6). A draft
  left after a reply is low priority: you may want to send it as your answer.
- **Waiting:** a ``drafted`` message on a live enrollment, listed only when it is
  flagged (case 7): no later step fires while a draft waits, so the guards never
  run, and you could send it from Gmail.
- **Test:** a test send that still has a ``gmail_draft_id`` (case 11).

A draft is flagged (:class:`DraftWarning`) when its enrollment ended ``opted_out`` or
``bounced``, its contact is marked do not contact, or an address of its contact is
on the do-not-send list. Flagged rows come first, replies last.

Nothing here calls Gmail or writes, and nothing logs a subject or a body: each row
carries only ids, names, statuses and the Message-ID to search Gmail for.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import ColumnElement, and_, case, exists, or_, select
from sqlalchemy.orm import Session

from netkeeper.campaigns.compose import ComposeError, message_id_for
from netkeeper.models import (
    Campaign,
    CampaignStep,
    Contact,
    ContactEmail,
    DoNotSendAddress,
    Enrollment,
    EnrollmentStatus,
    Mailbox,
    Message,
    MessageDirection,
    MessageStatus,
    TemplateChannel,
    TestSend,
    User,
)
from netkeeper.scoping import scoped
from netkeeper.services.campaign_engine import LIVE_STATUSES

log = logging.getLogger(__name__)

MESSAGES_MAX: Final = 200
"""The most campaign drafts one answer lists, flagged ones first."""
TESTS_MAX: Final = 50
"""The most test drafts one answer lists, newest first."""


class LeftoverKind(enum.StrEnum):
    DISCARDED = "discarded"
    ENDED = "ended"
    WAITING = "waiting"
    TEST = "test"


class DraftWarning(enum.StrEnum):
    """Why sending the draft would be wrong, strongest first."""

    OPTED_OUT = "opted_out"
    BOUNCED = "bounced"
    DO_NOT_CONTACT = "do_not_contact"
    DO_NOT_SEND = "do_not_send"


_ENDED_WARNINGS: Final = {
    EnrollmentStatus.OPTED_OUT: DraftWarning.OPTED_OUT,
    EnrollmentStatus.BOUNCED: DraftWarning.BOUNCED,
}


@dataclass(frozen=True, slots=True)
class LeftoverDraft:
    kind: LeftoverKind
    message_id: int | None
    test_send_id: int | None
    campaign_id: int
    campaign_name: str
    step_position: int | None
    contact_id: int | None
    contact_name: str | None
    enrollment_status: EnrollmentStatus | None
    warning: DraftWarning | None
    low_priority: bool
    """A draft left after a reply: you may want to send it as your answer."""
    at: datetime
    """When the draft was made."""
    mailbox_email: str | None
    rfc822_message_id: str | None
    """The Message-ID the draft carries, to search Gmail for; None when not known."""
    keep_for_check: bool
    """A test draft on a mailbox whose Message-ID check has not passed: the check
    searches for it (#304)."""


@dataclass(frozen=True, slots=True)
class LeftoverDrafts:
    items: tuple[LeftoverDraft, ...]
    more: bool
    """True when there were more than the answer lists."""


def _on_do_not_send(user: User) -> ColumnElement[bool]:
    """An address of the message's contact is on the do-not-send list."""
    return exists(
        select(ContactEmail.id)
        .join(
            DoNotSendAddress,
            and_(
                DoNotSendAddress.email == ContactEmail.email,
                DoNotSendAddress.user_id == user.id,
            ),
        )
        .where(ContactEmail.user_id == user.id, ContactEmail.contact_id == Message.contact_id)
    )


def _flagged(user: User) -> ColumnElement[bool]:
    return or_(
        Enrollment.status.in_(tuple(_ENDED_WARNINGS)),
        Contact.do_not_contact.is_(True),
        _on_do_not_send(user),
    )


def _leftover(user: User) -> ColumnElement[bool]:
    """A campaign email draft netkeeper no longer wants, or one it should not send."""
    return and_(
        Message.channel == TemplateChannel.EMAIL,
        Message.direction == MessageDirection.OUT,
        Message.gmail_draft_id.is_not(None),
        or_(
            Message.status == MessageStatus.DISCARDED,
            and_(
                Message.status == MessageStatus.DRAFTED,
                or_(Enrollment.status.not_in(tuple(LIVE_STATUSES)), _flagged(user)),
            ),
        ),
    )


def flagged_waiting(session: Session, user: User) -> bool:
    """Whether any Gmail draft waits for a contact who opted out, bounced, is do not
    contact or is on the do-not-send list: one indexed
    statement (``ix_messages_user_id_status``), for the Gmail nav dot (#508)."""
    statement = (
        scoped(user, Message)
        .with_only_columns(Message.id)
        .join(
            Enrollment, and_(Enrollment.id == Message.enrollment_id, Enrollment.user_id == user.id)
        )
        .join(Contact, and_(Contact.id == Message.contact_id, Contact.user_id == user.id))
        .where(
            Message.status.in_((MessageStatus.DRAFTED, MessageStatus.DISCARDED)),
            Message.channel == TemplateChannel.EMAIL,
            Message.direction == MessageDirection.OUT,
            Message.gmail_draft_id.is_not(None),
            _flagged(user),
        )
        .limit(1)
    )
    return session.scalar(statement) is not None


def _name(contact: Contact) -> str:
    first = contact.preferred_name or contact.first_name
    return " ".join(part for part in (first, contact.last_name) if part)


def _warning(status: EnrollmentStatus, do_not_contact: bool, on_list: bool) -> DraftWarning | None:
    if status in _ENDED_WARNINGS:
        return _ENDED_WARNINGS[status]
    if do_not_contact:
        return DraftWarning.DO_NOT_CONTACT
    return DraftWarning.DO_NOT_SEND if on_list else None


def _rfc822(user: User, message: Message, mailbox: Mailbox | None) -> str | None:
    if mailbox is None:
        return None
    try:
        return message_id_for(
            user_id=user.id,
            message_id=message.id,
            created_at=message.created_at,
            address=mailbox.email,
        )
    except ComposeError:
        log.warning("message %d has no Message-ID to search Gmail for", message.id)
        return None


def _messages(session: Session, user: User) -> tuple[list[LeftoverDraft], bool]:
    flagged = _flagged(user)
    on_list = _on_do_not_send(user)
    order = case(
        (flagged, 0),
        (
            and_(
                Message.status == MessageStatus.DRAFTED,
                Enrollment.status == EnrollmentStatus.REPLIED,
            ),
            2,
        ),
        else_=1,
    )
    rows = session.execute(
        scoped(user, Message)
        .with_only_columns(
            Message,
            Enrollment.status,
            Campaign.id,
            Campaign.name,
            CampaignStep.position,
            Contact,
            Mailbox,
            on_list,
        )
        .join(
            Enrollment, and_(Enrollment.id == Message.enrollment_id, Enrollment.user_id == user.id)
        )
        .join(Campaign, and_(Campaign.id == Enrollment.campaign_id, Campaign.user_id == user.id))
        .join(Contact, and_(Contact.id == Message.contact_id, Contact.user_id == user.id))
        .outerjoin(Mailbox, and_(Mailbox.id == Campaign.mailbox_id, Mailbox.user_id == user.id))
        .outerjoin(
            CampaignStep, and_(CampaignStep.id == Message.step_id, CampaignStep.user_id == user.id)
        )
        .where(_leftover(user))
        .order_by(order, Message.id.desc())
        .limit(MESSAGES_MAX + 1)
    ).all()
    found: list[LeftoverDraft] = []
    for message, status, campaign_id, campaign_name, position, contact, mailbox, listed in rows[
        :MESSAGES_MAX
    ]:
        if message.status is MessageStatus.DISCARDED:
            kind = LeftoverKind.DISCARDED
        elif status in LIVE_STATUSES:
            kind = LeftoverKind.WAITING
        else:
            kind = LeftoverKind.ENDED
        warning = _warning(status, contact.do_not_contact, bool(listed))
        found.append(
            LeftoverDraft(
                kind=kind,
                message_id=message.id,
                test_send_id=None,
                campaign_id=campaign_id,
                campaign_name=campaign_name,
                step_position=position,
                contact_id=contact.id,
                contact_name=_name(contact),
                enrollment_status=status,
                warning=warning,
                low_priority=(
                    warning is None
                    and kind is LeftoverKind.ENDED
                    and status is EnrollmentStatus.REPLIED
                ),
                at=message.created_at,
                mailbox_email=None if mailbox is None else mailbox.email,
                rfc822_message_id=_rfc822(user, message, mailbox),
                keep_for_check=False,
            )
        )
    return found, len(rows) > MESSAGES_MAX


def _tests(session: Session, user: User) -> tuple[list[LeftoverDraft], bool]:
    rows = session.execute(
        scoped(user, TestSend)
        .with_only_columns(TestSend, Campaign.name, CampaignStep.position, Mailbox)
        .join(Campaign, and_(Campaign.id == TestSend.campaign_id, Campaign.user_id == user.id))
        .outerjoin(Mailbox, and_(Mailbox.id == Campaign.mailbox_id, Mailbox.user_id == user.id))
        .outerjoin(
            CampaignStep,
            and_(CampaignStep.id == TestSend.step_id, CampaignStep.user_id == user.id),
        )
        .where(TestSend.gmail_draft_id.is_not(None))
        .order_by(TestSend.sent_at.desc(), TestSend.id.desc())
        .limit(TESTS_MAX + 1)
    ).all()
    found = [
        LeftoverDraft(
            kind=LeftoverKind.TEST,
            message_id=None,
            test_send_id=test.id,
            campaign_id=test.campaign_id,
            campaign_name=campaign_name,
            step_position=position,
            contact_id=None,
            contact_name=None,
            enrollment_status=None,
            warning=None,
            low_priority=False,
            at=test.sent_at,
            mailbox_email=None if mailbox is None else mailbox.email,
            rfc822_message_id=test.rfc822_message_id,
            keep_for_check=mailbox is not None and mailbox.message_id_verified_at is None,
        )
        for test, campaign_name, position, mailbox in rows[:TESTS_MAX]
    ]
    return found, len(rows) > TESTS_MAX


def _rank(row: LeftoverDraft) -> int:
    if row.warning is not None:
        return 0
    if row.low_priority:
        return 3
    return 2 if row.kind is LeftoverKind.TEST else 1


def leftover_drafts(session: Session, user: User) -> LeftoverDrafts:
    """Every leftover draft of ``user``'s: flagged first, then discarded and ended
    drafts, then test drafts, then drafts left after a reply; newest first in each."""
    messages, more_messages = _messages(session, user)
    tests, more_tests = _tests(session, user)
    rows = sorted(messages + tests, key=lambda r: r.at, reverse=True)
    rows.sort(key=_rank)  # stable: newest first within a rank
    return LeftoverDrafts(items=tuple(rows), more=more_messages or more_tests)
