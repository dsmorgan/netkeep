"""Lists from a campaign's results, and the builder's guard preview (#513).

``GET /campaigns/{id}/outcomes`` counts each outcome; ``POST /campaigns/{id}/lists``
makes a list of one outcome's people, static (a snapshot, the default) or smart (the
filter, re-evaluated). ``POST /campaigns/audience-preview`` says how many of a list's
or a filter's contacts each guard would skip in a campaign not saved yet. See
:mod:`netkeeper.services.campaign_lists`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from netkeeper.crm import lists as list_service
from netkeeper.crm.filters import CampaignOutcome, FilterError, FilterTree
from netkeeper.models import LIST_NAME_MAX_LENGTH, ListKind, TemplateChannel
from netkeeper.models.base import utcnow
from netkeeper.services import campaign_lists as service
from netkeeper.services import campaigns as campaign_service
from netkeeper.web.deps import CurrentUser, SessionDep, effective_settings, read_only

router = APIRouter(tags=["campaigns"])

Responses = dict[int | str, dict[str, Any]]
NOT_FOUND: Responses = {404: {"description": "No such campaign or list for this user"}}
CONFLICT: Responses = {409: {"description": "A list already has the name given"}}
INVALID: Responses = {422: {"description": "A value the list or the preview cannot be made from"}}


@contextmanager
def translate_errors() -> Iterator[None]:
    try:
        yield
    except (campaign_service.CampaignNotFound, list_service.ListNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except list_service.DuplicateListName as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (
        campaign_service.InvalidCampaign,
        list_service.InvalidListValue,
        list_service.BreaksAnotherList,
        FilterError,
    ) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class OutcomeCountOut(BaseModel):
    outcome: CampaignOutcome
    label: str
    count: int
    """Contacts the outcome's list would hold now: archived and merged-away ones are not
    counted, as a list leaves them out."""


class CampaignOutcomesOut(BaseModel):
    campaign_id: int
    outcomes: list[OutcomeCountOut]
    """Every outcome, in order, zero counts included. ``no_reply`` is ``active`` plus
    ``completed``; the others split the enrollments once."""


class OutcomeListIn(BaseModel):
    outcome: CampaignOutcome
    kind: Literal["static", "smart"] = "static"
    """``static``: a snapshot of who the outcome selects now, so a follow-up campaign's
    audience does not shift. ``smart``: the filter, re-evaluated whenever the list is read."""
    name: Annotated[str, Field(min_length=1, max_length=LIST_NAME_MAX_LENGTH)] | None = None
    """Left out: ``"<campaign>: <outcome>"``, with `` (2)`` and so on if a list has it."""


class OutcomeListOut(BaseModel):
    list_id: int
    name: str
    kind: Literal["static", "smart"]
    members: int


class AudiencePreviewIn(BaseModel):
    """The audience source the builder holds, and the first step's channel."""

    list_id: int | None = None
    filter: FilterTree | None = None
    channel: TemplateChannel


class GuardSkipsOut(BaseModel):
    reason: str
    """The guard's reason code, as ``GET .../review/guards`` gives it."""
    label: str
    count: int


class AudiencePreviewOut(BaseModel):
    audience: int
    will_start: int
    skipped: list[GuardSkipsOut]
    """Per guard, how many contacts it would skip, largest first; each contact counted
    once, under its first reason. Guards that skip nobody are left out."""
    summary: str
    contacted_within_days: int
    """The recent-contact window a new campaign would copy from the settings."""


@router.get(
    "/campaigns/{campaign_id}/outcomes",
    operation_id="get_campaign_outcomes",
    responses=NOT_FOUND,
)
def get_campaign_outcomes(
    campaign_id: int, session: SessionDep, user: CurrentUser
) -> CampaignOutcomesOut:
    """How many contacts each outcome's list would hold (#513)."""
    with translate_errors():
        counts = service.outcome_counts(session, user, campaign_id)
    return CampaignOutcomesOut(
        campaign_id=campaign_id,
        outcomes=[OutcomeCountOut(outcome=c.outcome, label=c.label, count=c.count) for c in counts],
    )


@router.post(
    "/campaigns/{campaign_id}/lists",
    operation_id="create_list_from_campaign",
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT, **INVALID},
)
def create_list_from_campaign(
    campaign_id: int, body: OutcomeListIn, session: SessionDep, user: CurrentUser
) -> OutcomeListOut:
    """A new list of the people one outcome selects in the campaign (#513). Sends nothing,
    enrolls nobody: a campaign built from it goes through the guards like any other."""
    kind = ListKind.STATIC if body.kind == "static" else ListKind.SMART
    with translate_errors():
        made = service.list_from_outcome(
            session, user, campaign_id, body.outcome, kind=kind, name=body.name
        )
    return OutcomeListOut(
        list_id=made.list.id, name=made.list.name, kind=body.kind, members=made.members
    )


@router.post(
    "/campaigns/audience-preview",
    operation_id="preview_campaign_audience",
    responses={**NOT_FOUND, **INVALID},
)
@read_only
def preview_campaign_audience(
    body: AudiencePreviewIn, request: Request, session: SessionDep, user: CurrentUser
) -> AudiencePreviewOut:
    """How many of the source's contacts each guard would skip, before the campaign is
    saved (#513). Gates nothing: the guards decide again at enrollment and every send."""
    settings = effective_settings(request, session, user)
    with translate_errors():
        preview = service.audience_preview(
            session,
            user,
            list_id=body.list_id,
            filter=body.filter,
            channel=body.channel,
            contacted_within_days=settings.campaigns.contacted_within_days_guard,
            now=utcnow(),
        )
    return AudiencePreviewOut(
        audience=preview.audience,
        will_start=preview.will_start,
        skipped=[
            GuardSkipsOut(reason=s.reason.value, label=s.label, count=s.count)
            for s in preview.skipped
        ],
        summary=preview.summary,
        contacted_within_days=preview.contacted_within_days,
    )
