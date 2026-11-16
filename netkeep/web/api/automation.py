"""``/automation``: one switch per service, Off, Manual or Automatic (#829).

``GET /automation`` answers both switches. ``PUT /automation/linkedin`` and
``PUT /automation/gmail`` move one and answer both. A move that does more (to
Automatic, or LinkedIn's depth from ``quick`` to ``full``) answers ``422`` without
``confirm: true``, as arming always did; a move to the left never needs it. Gmail
Automatic answers ``409`` while a connected mailbox lacks its Message-ID check
(#510), with the reason ``GET`` also gives in ``gmail.blocked_reason``.

See :mod:`netkeep.services.automation` for how each position maps onto arming.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from netkeep.models import ScheduleDepth, User
from netkeep.models.base import utcnow
from netkeep.services import automation as service
from netkeep.services.scheduled_runs import seed_served_schedule
from netkeep.web.deps import CurrentUser, SessionDep, effective_settings

router = APIRouter(tags=["automation"])


class LinkedInAutomationOut(BaseModel):
    position: service.Position
    depth: ScheduleDepth
    """``quick``: the daily incremental sync and the inbox check. ``full``: also the
    weekly full sync and the profile refresh. Read only while ``position`` is
    ``automatic``; kept while it isn't."""
    changed_at: datetime | None


class GmailAutomationOut(BaseModel):
    position: service.Position
    automatic_ready: bool
    """Whether a move to ``automatic`` is allowed now."""
    blocked_reason: str | None
    """Why ``automatic`` isn't allowed now, in a sentence; null when it is, or already is."""
    changed_at: datetime | None


class AutomationOut(BaseModel):
    linkedin: LinkedInAutomationOut
    gmail: GmailAutomationOut


class LinkedInAutomationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: service.Position
    depth: ScheduleDepth | None = None
    """Omitted: keep the stored depth."""
    confirm: bool = False
    """Needed for a move to ``automatic``, or from ``quick`` to ``full``."""


class GmailAutomationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: service.Position
    confirm: bool = False
    """Needed for a move to ``automatic``."""


REFUSED: dict[int | str, dict[str, Any]] = {
    409: {"description": "Not allowed now; the detail says why"},
    422: {"description": "The move does more and confirm was not true"},
}


def _out(session: Session, user: User) -> AutomationOut:
    linkedin = service.linkedin_state(session, user)
    gmail = service.gmail_state(session, user, now=utcnow())
    return AutomationOut(
        linkedin=LinkedInAutomationOut(
            position=linkedin.position, depth=linkedin.depth, changed_at=linkedin.changed_at
        ),
        gmail=GmailAutomationOut(
            position=gmail.position,
            automatic_ready=gmail.automatic_ready,
            blocked_reason=gmail.blocked_reason,
            changed_at=gmail.changed_at,
        ),
    )


def _refused(exc: service.AutomationRefused) -> HTTPException:
    status = 422 if isinstance(exc, service.ConfirmRequired) else 409
    return HTTPException(status_code=status, detail=str(exc))


@router.get("/automation", operation_id="get_automation")
def get_automation(user: CurrentUser, session: SessionDep) -> AutomationOut:
    return _out(session, user)


@router.put("/automation/linkedin", operation_id="set_linkedin_automation", responses=REFUSED)
def set_linkedin_automation(
    body: LinkedInAutomationIn, request: Request, user: CurrentUser, session: SessionDep
) -> AutomationOut:
    """Move the LinkedIn switch. Automatic arms the schedule; Manual and Off disarm it.
    A run already going is not cancelled."""
    now = utcnow()
    try:
        state = service.set_linkedin(
            session, user, body.position, body.depth, confirm=body.confirm, now=now
        )
    except service.AutomationRefused as exc:
        raise _refused(exc) from exc
    if state.position is service.Position.AUTOMATIC:
        # As arming does (#327): a kind added since the schedule was set gets a due time.
        settings = effective_settings(request, session, user).linkedin
        seed_served_schedule(session, user, settings, now=now)
    session.commit()  # the change stands before anything is read back for display
    return _out(session, user)


@router.put("/automation/gmail", operation_id="set_gmail_automation", responses=REFUSED)
def set_gmail_automation(
    body: GmailAutomationIn, user: CurrentUser, session: SessionDep
) -> AutomationOut:
    """Move the Gmail switch for every connected mailbox. Applies from the campaign
    engine's next claim; what is already in the sender's hands is not recalled."""
    try:
        service.set_gmail(
            session,
            user,
            body.position,
            confirm=body.confirm,
            by=f"web (user {user.id})",
            now=utcnow(),
        )
    except service.AutomationRefused as exc:
        raise _refused(exc) from exc
    session.commit()
    return _out(session, user)
