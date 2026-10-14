"""Switch a running campaign's LinkedIn step between Prefill and Auto-send (#536).

- ``GET /campaigns/{id}/steps/{step_id}/mode`` shows the step's mode, whether auto-send
  is on, how many contacts are queued for the step (they keep Prefill on a switch to
  Auto-send), the step's messages under way, and why it cannot switch, if it cannot.
  Changes nothing.
- ``PUT /campaigns/{id}/steps/{step_id}/mode`` sets it. A switch to ``auto_send`` needs
  ``confirm: true`` (``422`` otherwise); a switch to ``prefill`` does not. ``409`` for a
  campaign that is not active or paused, ``422`` for an email step.
- ``GET /campaigns/{id}/mode-changes`` lists the campaign's switches, newest first.

:mod:`netkeeper.services.step_mode` holds the rules: a switch never sends anything by
itself, and work already queued keeps Prefill. ``netkeeper campaigns step-mode``
mirrors the first two routes.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from netkeeper.models import CampaignStatus, StepMode
from netkeeper.models.base import utcnow
from netkeeper.services import step_mode as service
from netkeeper.web.api.campaigns import (
    CONFLICT,
    INVALID,
    NOT_FOUND,
    CampaignOut,
    _detail,
    _settings,
    translate_errors,
)
from netkeeper.web.deps import CurrentUser, SessionDep

router = APIRouter(tags=["campaigns"])


class ModePreviewOut(BaseModel):
    campaign_id: int
    campaign_status: CampaignStatus
    step_id: int
    position: int
    mode: StepMode
    auto_send_on: bool
    """Whether auto-send is on (``[campaigns] linkedin_auto_send``). Off, an ``auto_send``
    step sends nothing: it waits for you to prefill it."""
    queued: int
    """Contacts queued for the step now. On a switch to ``auto_send`` they keep Prefill."""
    kept_prefill: int
    """Contacts that keep the step as a prefill from an earlier switch."""
    claimed: int
    """The step's messages claimed and not yet recorded: a prefill or an auto-send under
    way. On a switch to ``prefill``, an auto-send under way stops before it clicks Send."""
    prefilled: int
    """The step's messages typed into LinkedIn, waiting for you to send them."""
    refusal: str | None
    """Why the step cannot switch; null when it can."""
    confirm_text: str
    """What a switch to ``auto_send`` changes: the confirm says it."""


class ModeIn(BaseModel):
    mode: StepMode
    confirm: bool = False
    """Required for ``auto_send``: you read ``confirm_text``. Ignored for ``prefill``."""


class ModeSwitchOut(BaseModel):
    from_mode: StepMode
    to_mode: StepMode
    changed: bool
    """False when the step already had that mode: nothing changed."""
    kept_prefill: int
    """Contacts already queued for the step that keep Prefill."""
    auto_send_on: bool
    """Whether auto-send is on. Off, an ``auto_send`` step sends nothing yet."""
    changed_at: datetime
    campaign: CampaignOut


class ModeChangeOut(BaseModel):
    """One switch, as the campaign's history keeps it. Never message text."""

    id: int
    step_id: int
    position: int
    from_mode: StepMode
    to_mode: StepMode
    changed_at: datetime
    changed_by: int
    kept_prefill: int
    auto_send_on: bool


@router.get(
    "/campaigns/{campaign_id}/steps/{step_id}/mode",
    operation_id="step_mode_preview",
    responses=NOT_FOUND,
)
def get_mode(
    campaign_id: int, step_id: int, request: Request, session: SessionDep, user: CurrentUser
) -> ModePreviewOut:
    """The step's mode and what switching it now would change. Changes nothing."""
    settings = _settings(request, session, user)
    with translate_errors():
        p = service.preview(
            session,
            user,
            campaign_id,
            step_id,
            now=utcnow(),
            auto_send_on=settings.campaigns.linkedin_auto_send,
        )
    return ModePreviewOut(
        campaign_id=p.campaign_id,
        campaign_status=p.campaign_status,
        step_id=p.step_id,
        position=p.position,
        mode=p.mode,
        auto_send_on=p.auto_send_on,
        queued=p.queued,
        kept_prefill=p.kept_prefill,
        claimed=p.claimed,
        prefilled=p.prefilled,
        refusal=p.refusal,
        confirm_text=service.AUTO_SEND_CONFIRM,
    )


@router.put(
    "/campaigns/{campaign_id}/steps/{step_id}/mode",
    operation_id="set_step_mode",
    responses={**NOT_FOUND, **CONFLICT, **INVALID},
)
def set_mode(
    campaign_id: int,
    step_id: int,
    body: ModeIn,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
) -> ModeSwitchOut:
    """Switch a LinkedIn step between ``prefill`` and ``auto_send``. Nothing is sent by the
    switch; contacts already queued for the step keep Prefill. ``auto_send`` needs
    ``confirm: true``."""
    if body.mode is StepMode.AUTO_SEND and not body.confirm:
        raise HTTPException(
            status_code=422,
            detail=f"confirm the switch: set confirm to true. {service.AUTO_SEND_CONFIRM}",
        )
    settings = _settings(request, session, user)
    with translate_errors():
        result = service.switch(
            session,
            user,
            campaign_id,
            step_id,
            mode=body.mode,
            confirm=body.confirm,
            now=utcnow(),
            auto_send_on=settings.campaigns.linkedin_auto_send,
        )
        return ModeSwitchOut(
            from_mode=result.from_mode,
            to_mode=result.to_mode,
            changed=result.changed,
            kept_prefill=result.kept_prefill,
            auto_send_on=result.auto_send_on,
            changed_at=result.changed_at,
            campaign=_detail(session, user, campaign_id, request),
        )


@router.get(
    "/campaigns/{campaign_id}/mode-changes",
    operation_id="list_step_mode_changes",
    responses=NOT_FOUND,
)
def list_mode_changes(
    campaign_id: int, session: SessionDep, user: CurrentUser
) -> list[ModeChangeOut]:
    """The campaign's step mode switches, newest first (at most 200)."""
    with translate_errors():
        rows = service.history(session, user, campaign_id)
    return [
        ModeChangeOut(
            id=r.id,
            step_id=r.step_id,
            position=r.position,
            from_mode=r.from_mode,
            to_mode=r.to_mode,
            changed_at=r.changed_at,
            changed_by=r.changed_by,
            kept_prefill=r.kept_prefill,
            auto_send_on=r.auto_send_on,
        )
        for r in rows
    ]
