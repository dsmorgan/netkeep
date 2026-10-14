"""A contact's enrollments, and a response that came outside netkeeper (#512).

- ``GET /contacts/{contact_id}/enrollments`` lists the contact's enrollments, newest
  first, each saying whether it could still send.
- ``POST /contacts/{contact_id}/outside-response`` records a response by phone, text,
  another email address, another app, in person or otherwise, and ends what it
  answers as a detected reply would
  (:mod:`netkeeper.services.outside_responses`): every enrollment of the contact that
  could still send, or the one picked.

A contact that is not the user's answers ``404``, and so does an enrollment that is not
the contact's. ``409`` when nothing could be ended.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import AwareDatetime, BaseModel, Field

from netkeeper.models import EnrollmentStatus, ResponseOutcome, ResponseVia
from netkeeper.models.base import utcnow
from netkeeper.services import outside_responses as service
from netkeeper.web.deps import CurrentUser, SessionDep

router = APIRouter(tags=["campaigns"])

Responses = dict[int | str, dict[str, Any]]
NOT_FOUND: Responses = {404: {"description": "No such contact, or no such enrollment of it"}}
CONFLICT: Responses = {409: {"description": "Nothing left to end"}}


class ContactEnrollmentOut(BaseModel):
    id: int
    campaign_id: int
    campaign_name: str
    status: EnrollmentStatus
    current_step: int | None
    replied_at: datetime | None
    exit_reason: str | None
    can_still_send: bool
    """``pending``, ``active`` or ``paused``: a recorded response ends it."""


class OutsideResponseIn(BaseModel):
    """A response outside netkeeper. ``at`` must carry a timezone, and be neither in the
    future nor before the first message of an enrollment it ends went out. ``outcome``
    is optional; ``do_not_contact`` goes with ``opt_out`` only. Without
    ``enrollment_id``, every enrollment of the contact that could still send ends; with
    it, that one only (a ``completed`` one may be picked too, and stays ``completed``)."""

    via: ResponseVia
    at: AwareDatetime
    note: str | None = Field(default=None, max_length=service.NOTE_MAX_LENGTH)
    outcome: ResponseOutcome | None = None
    do_not_contact: bool | None = None
    """With ``opt_out``: set the contact do-not-contact and put their addresses on the
    do-not-send list. Omitted or ``null``, an opt-out does, as a detected unsubscribe
    does; ``false`` keeps them contactable."""
    enrollment_id: int | None = None


class EndedEnrollmentOut(BaseModel):
    id: int
    campaign_id: int
    status: EnrollmentStatus
    message_id: int
    """The inbound message recorded on it: an inbox item, already handled."""


class OutsideResponseOut(BaseModel):
    interaction_id: int
    enrollments: list[EndedEnrollmentOut]


@router.get(
    "/contacts/{contact_id}/enrollments",
    operation_id="list_contact_enrollments",
    responses=NOT_FOUND,
)
def list_contact_enrollments(
    contact_id: int, session: SessionDep, user: CurrentUser
) -> list[ContactEnrollmentOut]:
    """The contact's enrollments in every campaign, newest first."""
    try:
        rows = service.enrollments_of(session, user, contact_id)
    except service.ContactNotFound:
        raise HTTPException(status_code=404, detail="no such contact") from None
    return [
        ContactEnrollmentOut(
            id=enrollment.id,
            campaign_id=enrollment.campaign_id,
            campaign_name=name,
            status=enrollment.status,
            current_step=enrollment.current_step,
            replied_at=enrollment.replied_at,
            exit_reason=enrollment.exit_reason,
            can_still_send=enrollment.status in service.STOPPABLE,
        )
        for enrollment, name in rows
    ]


@router.post(
    "/contacts/{contact_id}/outside-response",
    operation_id="record_outside_response",
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
)
def record_outside_response(
    contact_id: int, body: OutsideResponseIn, session: SessionDep, user: CurrentUser
) -> OutsideResponseOut:
    """Record a response that came outside netkeeper, and end the enrollments it answers
    ``replied`` (``opted_out`` for an opt-out), as a detected reply does. No later step
    sends. ``422``, with the reason, for a time in the future or before the first send of
    an enrollment it would end, or ``do_not_contact`` without an opt-out."""
    try:
        result = service.record_outside_response(
            session,
            user,
            contact_id,
            via=body.via,
            at=body.at,
            now=utcnow(),
            note=body.note,
            outcome=body.outcome,
            do_not_contact=body.do_not_contact,
            enrollment_id=body.enrollment_id,
        )
    except service.ContactNotFound:
        raise HTTPException(status_code=404, detail="no such contact") from None
    except service.EnrollmentNotFound:
        raise HTTPException(status_code=404, detail="no such enrollment of this contact") from None
    except service.NothingToEnd as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return OutsideResponseOut(
        interaction_id=result.interaction.id,
        enrollments=[
            EndedEnrollmentOut(
                id=enrollment.id,
                campaign_id=enrollment.campaign_id,
                status=enrollment.status,
                message_id=message.id,
            )
            for enrollment, message in zip(result.enrollments, result.messages, strict=True)
        ],
    )
