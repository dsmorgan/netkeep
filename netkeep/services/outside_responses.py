"""A response that came outside netkeep, recorded by a person (#512).

A contact who gets a campaign email or LinkedIn message may answer another way: a
phone call, a text, an email from an address netkeep does not know, another app,
or in person. Neither poll sees it, so the person records it
(``POST /contacts/{id}/outside-response``, ``netkeep campaigns record-response``),
and it does what a detected reply does.

What it ends
------------
By default, **every enrollment of the contact that could still send**: ``pending``,
``active`` or ``paused`` (:data:`STOPPABLE`). The contact answered you, whichever
campaign asked, so none of them should send the next step. A person may pick one
enrollment instead; that one may also be ``completed``, to count a late answer in its
results, and then stays ``completed``, as for a detected reply. An enrollment that
already ended ``replied``, ``bounced``, ``opted_out`` or ``removed`` is refused.

What it writes
--------------
A response is dated when they responded: never in the future (:data:`FUTURE_SKEW`
aside), and never before the first message of an enrollment it ends went out, since it
then answers nothing the campaign sent (:class:`TooEarly`). An enrollment with nothing
sent yet, a pending one say, takes any time up to now.

For each enrollment, an inbound ``messages`` row, as the polls write for a reply:
``in``, ``received``, ``sent_at`` when they responded, the step of the latest send
before it, the note (if any) as its snippet, ``outside_via`` and ``outside_outcome``,
and ``asks_unsubscribe`` for an opt-out. Its ``channel`` is the channel of the step it
answers (the latest send's, else the campaign's first step's), since the column holds
only ``email`` or ``linkedin``; ``outside_via`` is what marks it outside. It is
``handled_at`` now: the person recording it has handled it, so the inbox shows it
already handled.

Then each row goes through :func:`netkeep.services.campaign_replies._after_reply`,
the end a detected reply takes: ``replied_at``, and a live enrollment ends ``replied``
(``opted_out`` for an opt-out) through
:func:`netkeep.services.campaign_engine.end_enrollment`. An opt-out sets the contact
``do_not_contact``, with every address of theirs on the do-not-send list, as a detected
unsubscribe does, unless the person says not to. A ``pending`` enrollment, which
detection never sees, ends the same way through ``end_enrollment``.

One interaction records it on the timeline, linked to the first message: ``call_in``
for a call, ``meeting_in`` in person, ``email_in`` for an email from another address,
``other_in`` for anything else. None of them is outbound, so ``last_contacted_at`` and
the recent-contact guard do not move.

Why nothing sends after it
--------------------------
Two things stop every send path, each on its own. The enrollment is no longer
``active``, and every claim selects only ``active`` ones. And the inbound row is a reply
to every check that reads one (``campaign_engine._reply_at``): the tick's claim, the
Gmail sender's read just before it sends, the LinkedIn prefill claim, and auto-send's
read just before it clicks Send. An open LinkedIn prefill is left as a detected reply
leaves it: the composer is the person's, so it stays listed until they send or
discard it, and neither moves the ended enrollment on.

Nothing here logs the note.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import func
from sqlalchemy.orm import Session

from netkeep.crm.interactions import add_interaction
from netkeep.db import is_writer
from netkeep.models import (
    MESSAGE_SNIPPET_MAX_LENGTH,
    Campaign,
    CampaignStep,
    ContactSource,
    Enrollment,
    EnrollmentStatus,
    Interaction,
    InteractionKind,
    Message,
    MessageDirection,
    MessageStatus,
    ResponseOutcome,
    ResponseVia,
    TemplateChannel,
    User,
)
from netkeep.models.base import utcnow
from netkeep.scoping import get_scoped, get_scoped_contact, scoped
from netkeep.services import campaign_engine as engine
from netkeep.services import campaign_replies

log = logging.getLogger(__name__)

STOPPABLE: Final = frozenset(
    {EnrollmentStatus.PENDING, EnrollmentStatus.ACTIVE, EnrollmentStatus.PAUSED}
)
"""An enrollment in one of these could still send: a response ends it."""

RECORDABLE: Final = STOPPABLE | {EnrollmentStatus.COMPLETED}
"""What a response may be recorded on when a person picks the enrollment. A completed
one keeps its status, as for a detected reply."""

FUTURE_SKEW: Final = timedelta(minutes=5)
"""How far after now a response's time may be, for a clock a little ahead."""

NOTE_MAX_LENGTH: Final = MESSAGE_SNIPPET_MAX_LENGTH
"""The note is the message's snippet, so it is held to the snippet's length."""

KIND_FOR: Final[dict[ResponseVia, InteractionKind]] = {
    ResponseVia.CALL: InteractionKind.CALL_IN,
    ResponseVia.IN_PERSON: InteractionKind.MEETING_IN,
    ResponseVia.OTHER_EMAIL: InteractionKind.EMAIL_IN,
    ResponseVia.TEXT: InteractionKind.OTHER_IN,
    ResponseVia.OTHER_APP: InteractionKind.OTHER_IN,
    ResponseVia.OTHER: InteractionKind.OTHER_IN,
}
"""The timeline kind of each way to respond: all inbound, none outbound."""

VIA_WORDS: Final[dict[ResponseVia, str]] = {
    ResponseVia.CALL: "by phone",
    ResponseVia.TEXT: "by text",
    ResponseVia.OTHER_EMAIL: "by email from another address",
    ResponseVia.OTHER_APP: "on another app",
    ResponseVia.IN_PERSON: "in person",
    ResponseVia.OTHER: "another way",
}

OUTCOME_WORDS: Final[dict[ResponseOutcome, str]] = {
    ResponseOutcome.POSITIVE: "positive",
    ResponseOutcome.NEUTRAL: "neutral",
    ResponseOutcome.NOT_INTERESTED: "not interested",
    ResponseOutcome.OPT_OUT: "asked to stop",
}


class ContactNotFound(LookupError):
    """No contact of the user's has that id."""


class EnrollmentNotFound(LookupError):
    """The contact has no enrollment with that id."""


class TooEarly(ValueError):
    """The response is dated before an enrollment's first send: it answers nothing sent."""


class NothingToEnd(ValueError):
    """No enrollment of the contact could still send, or the one picked has ended."""


@dataclass(frozen=True, slots=True)
class OutsideResponse:
    interaction: Interaction
    messages: tuple[Message, ...]
    """One inbound message per enrollment it was recorded on, in enrollment order."""
    enrollments: tuple[Enrollment, ...]
    """The enrollments, after: ended ``replied`` or ``opted_out``, or still
    ``completed``."""


def stoppable_enrollments(session: Session, user: User, contact_id: int) -> list[Enrollment]:
    """The contact's enrollments that could still send, oldest first. Read-only."""
    return list(
        session.scalars(
            scoped(user, Enrollment)
            .where(Enrollment.contact_id == contact_id, Enrollment.status.in_(STOPPABLE))
            .order_by(Enrollment.id)
        )
    )


def summary_for(via: ResponseVia, outcome: ResponseOutcome | None, note: str | None) -> str:
    """The interaction's summary: how, what it said, and the note."""
    text = f"Responded to a campaign {VIA_WORDS[via]}, outside netkeep"
    if outcome is not None:
        text += f" ({OUTCOME_WORDS[outcome]})"
    if note:
        text += f": {note}"
    return text


def record_outside_response(
    session: Session,
    user: User,
    contact_id: int,
    *,
    via: ResponseVia,
    at: datetime,
    now: datetime,
    note: str | None = None,
    outcome: ResponseOutcome | None = None,
    do_not_contact: bool | None = None,
    enrollment_id: int | None = None,
) -> OutsideResponse:
    """Record a response from outside netkeep and end what it answers (module
    docstring). ``enrollment_id`` picks one enrollment; without it, every enrollment of
    the contact that could still send. ``do_not_contact`` goes with an opt-out only;
    None means an opt-out sets it, as a detected unsubscribe does.

    Raises :class:`ContactNotFound`, :class:`EnrollmentNotFound`, :class:`NothingToEnd`,
    and ``ValueError`` (:class:`TooEarly` among them) for a time that is naive, in the
    future, or before an enrollment's first send, a note that is too long, or
    ``do_not_contact`` without an opt-out. Needs a writer session."""
    if not is_writer(session):
        raise RuntimeError("record_outside_response needs a writer session")
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at must be timezone-aware")
    if at > now + FUTURE_SKEW:
        raise ValueError("a response cannot be in the future")
    note = (note or "").strip() or None
    if note is not None and len(note) > NOTE_MAX_LENGTH:
        raise ValueError(f"the note is longer than {NOTE_MAX_LENGTH} characters")
    if do_not_contact and outcome is not ResponseOutcome.OPT_OUT:
        raise ValueError("do_not_contact goes with an opt-out only")
    opt_out = outcome is ResponseOutcome.OPT_OUT
    set_do_not_contact = opt_out if do_not_contact is None else do_not_contact
    contact = get_scoped_contact(session, user, contact_id)
    if contact is None:
        raise ContactNotFound(f"no contact {contact_id}")

    if enrollment_id is None:
        targets = stoppable_enrollments(session, user, contact.id)
        if not targets:
            raise NothingToEnd("the contact has no enrollment that could still send")
    else:
        picked = get_scoped(session, user, Enrollment, enrollment_id)
        if picked is None or picked.contact_id != contact.id:
            raise EnrollmentNotFound(f"no enrollment {enrollment_id} for contact {contact.id}")
        if picked.status not in RECORDABLE:
            raise NothingToEnd(f"enrollment {picked.id} has already ended ({picked.status})")
        targets = [picked]
    for enrollment in targets:
        first = _first_send(session, user, enrollment)
        if first is not None and at < first:
            raise TooEarly(
                f"the response is dated before the first message of that campaign went out"
                f" ({first.isoformat(timespec='minutes')}); it cannot answer it"
            )

    handled = utcnow()
    rows: list[Message] = []
    for enrollment in targets:
        step_id, channel = _answers(session, user, enrollment, at)
        row = Message(
            user_id=user.id,
            enrollment_id=enrollment.id,
            step_id=step_id,
            contact_id=enrollment.contact_id,
            channel=channel,
            direction=MessageDirection.IN,
            status=MessageStatus.RECEIVED,
            snippet=note,
            asks_unsubscribe=opt_out,
            sent_at=at,
            handled_at=handled,
            outside_via=via,
            outside_outcome=outcome,
        )
        session.add(row)
        session.flush()
        rows.append(row)
        pending = enrollment.status is EnrollmentStatus.PENDING
        campaign_replies._after_reply(
            session, user, enrollment, row, at=at, do_not_contact=set_do_not_contact
        )
        if pending:
            # Detection never sees a pending enrollment (nothing was sent); the same end.
            if opt_out:
                engine.end_enrollment(
                    session,
                    user,
                    enrollment,
                    EnrollmentStatus.OPTED_OUT,
                    campaign_replies.UNSUBSCRIBE_REASON,
                )
            else:
                engine.end_enrollment(
                    session, user, enrollment, EnrollmentStatus.REPLIED, "replied"
                )
        log.info(
            "enrollment %d: a response from outside netkeep recorded as message %d; it is %s",
            enrollment.id,
            row.id,
            enrollment.status,
        )

    interaction = add_interaction(
        session,
        user,
        contact.id,
        KIND_FOR[via],
        at,
        summary=summary_for(via, outcome, note),
        message_id=rows[0].id,
        source=ContactSource.MANUAL,
    )
    session.flush()
    return OutsideResponse(interaction, tuple(rows), tuple(targets))


def _first_send(session: Session, user: User, enrollment: Enrollment) -> datetime | None:
    """When the enrollment's first message went out, or None when nothing has (a pending
    enrollment, or an active one before its first step)."""
    return session.scalar(
        scoped(user, Message)
        .with_only_columns(func.min(Message.sent_at))
        .where(
            Message.enrollment_id == enrollment.id,
            Message.direction == MessageDirection.OUT,
            Message.sent_at.is_not(None),
        )
    )


def _answers(
    session: Session, user: User, enrollment: Enrollment, at: datetime
) -> tuple[int | None, TemplateChannel]:
    """The step a response at ``at`` answers, and its channel: the latest send at or
    before it, as for a detected reply; with none, no step and the campaign's first
    step's channel (email when it has none)."""
    latest = session.execute(
        scoped(user, Message)
        .with_only_columns(Message.step_id, Message.channel)
        .where(
            Message.enrollment_id == enrollment.id,
            Message.direction == MessageDirection.OUT,
            Message.sent_at.is_not(None),
            Message.sent_at <= at,
        )
        .order_by(Message.sent_at.desc(), Message.id.desc())
        .limit(1)
    ).first()
    if latest is not None:
        return latest[0], latest[1]
    first = session.scalar(
        scoped(user, CampaignStep)
        .with_only_columns(CampaignStep.channel)
        .where(CampaignStep.campaign_id == enrollment.campaign_id)
        .order_by(CampaignStep.position)
        .limit(1)
    )
    return None, first or TemplateChannel.EMAIL


def stoppable_count(session: Session, user: User, contact_id: int) -> int:
    """How many of the contact's enrollments could still send. Read-only."""
    return (
        session.scalar(
            scoped(user, Enrollment)
            .with_only_columns(func.count(Enrollment.id))
            .where(Enrollment.contact_id == contact_id, Enrollment.status.in_(STOPPABLE))
        )
        or 0
    )


def enrollments_of(session: Session, user: User, contact_id: int) -> list[tuple[Enrollment, str]]:
    """Every enrollment of the contact, newest first, with its campaign's name. Raises
    :class:`ContactNotFound`. Read-only."""
    contact = get_scoped_contact(session, user, contact_id)
    if contact is None:
        raise ContactNotFound(f"no contact {contact_id}")
    rows = session.execute(
        scoped(user, Enrollment)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .where(Enrollment.contact_id == contact.id, Campaign.user_id == user.id)
        .with_only_columns(Enrollment, Campaign.name)
        .order_by(Enrollment.id.desc())
    )
    return [(enrollment, name) for enrollment, name in rows]
