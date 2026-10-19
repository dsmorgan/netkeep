"""``GET /gmail/activity``: what the Gmail page shows that no other endpoint answers (#449).

Today's sends against each mailbox's daily cap, and the newest email messages
(``/gmail/activity``), and the drafts netkeep left in Gmail for you to clean up by
hand (``/gmail/leftover-drafts``, #543). The
mailbox itself is ``/mailboxes/status``, the reply poll ``/poll-status``, the posture
rows ``/posture``. Read-only and per-user: a database read, never a Gmail call. See
:mod:`netkeep.services.gmail_activity`.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

from netkeep.models import EnrollmentStatus, MessageDirection, MessageStatus
from netkeep.models.base import utcnow
from netkeep.services import gmail_activity as service
from netkeep.services import leftover_drafts
from netkeep.services.leftover_drafts import DraftWarning, LeftoverKind
from netkeep.web.deps import CurrentUser, SessionDep, effective_settings

router = APIRouter(prefix="/gmail", tags=["gmail"])


class CampaignCapOut(BaseModel):
    campaign_id: int
    name: str
    sent_today: int
    daily_cap: int
    """The campaign's own cap, lower than the mailbox's."""


class MailboxSendsOut(BaseModel):
    mailbox_id: int
    email: str
    sent_today: int
    """Recipients fired today across every campaign on the mailbox, any status."""
    daily_cap: int
    """The most the engine sends from it in a day: its cap, never over the hard maximum."""
    lower_campaign_caps: list[CampaignCapOut]
    """Active campaigns on it whose own cap is lower: each stops sending at its own cap."""


class RecentMessageOut(BaseModel):
    id: int
    direction: MessageDirection
    status: MessageStatus
    at: datetime
    """When it was sent, else scheduled, else made."""
    campaign_id: int | None
    campaign_name: str | None
    step_position: int | None
    contact_id: int
    contact_name: str
    error: str | None


class GmailActivityOut(BaseModel):
    day_start: datetime
    day_end: datetime
    timezone: str
    """The zone the day is counted in."""
    mailboxes: list[MailboxSendsOut]
    """Every live mailbox (not a disconnected one), oldest first."""
    recent: list[RecentMessageOut]
    """The newest email messages, sent or received, newest first."""


@router.get("/activity", operation_id="get_gmail_activity")
def get_gmail_activity(
    request: Request, user: CurrentUser, session: SessionDep
) -> GmailActivityOut:
    """Today's sends against the cap, and recent email. Read-only: it never calls Gmail."""
    settings = effective_settings(request, session, user)
    activity = service.gmail_activity(session, user, now=utcnow(), settings=settings)
    return GmailActivityOut(
        day_start=activity.day_start,
        day_end=activity.day_end,
        timezone=activity.timezone,
        mailboxes=[
            MailboxSendsOut(
                mailbox_id=box.mailbox_id,
                email=box.email,
                sent_today=box.sent_today,
                daily_cap=box.daily_cap,
                lower_campaign_caps=[
                    CampaignCapOut(
                        campaign_id=c.campaign_id,
                        name=c.name,
                        sent_today=c.sent_today,
                        daily_cap=c.daily_cap,
                    )
                    for c in box.lower_campaign_caps
                ],
            )
            for box in activity.mailboxes
        ],
        recent=[
            RecentMessageOut(
                id=row.id,
                direction=row.direction,
                status=row.status,
                at=row.at,
                campaign_id=row.campaign_id,
                campaign_name=row.campaign_name,
                step_position=row.step_position,
                contact_id=row.contact_id,
                contact_name=row.contact_name,
                error=row.error,
            )
            for row in activity.recent
        ],
    )


class GmailLeftoverDraftOut(BaseModel):
    kind: LeftoverKind
    """``discarded`` (a merge discarded it), ``ended`` (its enrollment ended), ``waiting``
    (its enrollment is live, but sending it would be wrong) or ``test`` (a test send)."""
    message_id: int | None
    test_send_id: int | None
    campaign_id: int
    campaign_name: str
    step_position: int | None
    contact_id: int | None
    contact_name: str | None
    enrollment_status: EnrollmentStatus | None
    warning: DraftWarning | None
    """Why sending it would be wrong: the contact opted out, bounced, is marked do not
    contact, or has an address on the do-not-send list. None when nothing is wrong."""
    low_priority: bool
    """A draft left after a reply, which you may still want to send."""
    at: datetime
    """When the draft was made."""
    mailbox_email: str | None
    rfc822_message_id: str | None
    """The Message-ID the draft carries, to find it in Gmail."""
    keep_for_check: bool
    """A test draft the mailbox's Message-ID check still needs."""


class GmailLeftoverDraftsOut(BaseModel):
    items: list[GmailLeftoverDraftOut]
    """Flagged drafts first, then discarded and ended ones, test drafts, and drafts
    left after a reply; newest first in each."""
    more: bool
    """True when there are more than the list shows."""


@router.get("/leftover-drafts", operation_id="get_gmail_leftover_drafts")
def get_leftover_drafts(user: CurrentUser, session: SessionDep) -> GmailLeftoverDraftsOut:
    """Drafts netkeep left in Gmail, for you to clean up by hand. Read-only: it never
    calls Gmail, and netkeep deletes nothing there."""
    found = leftover_drafts.leftover_drafts(session, user)
    return GmailLeftoverDraftsOut(
        items=[
            GmailLeftoverDraftOut(
                kind=row.kind,
                message_id=row.message_id,
                test_send_id=row.test_send_id,
                campaign_id=row.campaign_id,
                campaign_name=row.campaign_name,
                step_position=row.step_position,
                contact_id=row.contact_id,
                contact_name=row.contact_name,
                enrollment_status=row.enrollment_status,
                warning=row.warning,
                low_priority=row.low_priority,
                at=row.at,
                mailbox_email=row.mailbox_email,
                rfc822_message_id=row.rfc822_message_id,
                keep_for_check=row.keep_for_check,
            )
            for row in found.items
        ],
        more=found.more,
    )
