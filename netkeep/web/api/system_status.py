"""``GET /system-status``: the three status chips' server-side parts, rolled up (#761).

The UI's top-bar chips and the menu bar (ADR 0014 rule 1) read this one roll-up. Each
adds what only it knows, by the same worst-wins rule: whether it reaches the server,
and for the UI, live updates. See :mod:`netkeep.services.system_status` for the parts.

Times come back absolute (UTC, ISO 8601) in each chip's ``times``, and a fact or a
detail names them with ``{placeholders}``: each client formats them when it renders.

Read-only and per-user: every answer is a database read or this process's memory.
Nothing here touches the browser or Gmail, and nothing is written.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from netkeep.models import SyncRunKind
from netkeep.models.base import utcnow
from netkeep.services import system_status as service
from netkeep.services.campaign_engine import TICK_EVENT
from netkeep.services.events import EventBus
from netkeep.web.api.poll_status import serving_of
from netkeep.web.deps import CurrentUser, SessionDep, effective_settings

router = APIRouter(tags=["system-status"])

PROGRESS_EVENT = "run.progress"


class StatusTargetOut(BaseModel):
    """A page, and the section on it."""

    to: service.Page
    hash: str | None


class StatusTimeOut(BaseModel):
    """A time a client writes into a ``{placeholder}``: ``ago`` ("3 min ago"), ``next``
    ("in 7 min", "14:20") or ``when`` (a date and time)."""

    at: datetime
    style: service.TimeStyle


class StatusPartOut(BaseModel):
    key: str
    label: str
    tone: service.Tone
    word: str
    """What the chip says when this part sets its tone."""
    fact: str | None
    """The chip's muted fact when this part sets it; the chip's own when null."""
    detail: str
    target: StatusTargetOut | None


class StatusChipOut(BaseModel):
    system: service.SystemKey
    name: str
    tone: service.Tone
    word: str
    fact: str
    parts: list[StatusPartOut]
    """In the popover's order."""
    target: StatusTargetOut
    page: str
    times: dict[str, StatusTimeOut]
    """Every ``{name}`` in the chip's fact and its parts' facts and details."""


class RunInProgressOut(BaseModel):
    id: int
    kind: SyncRunKind
    label: str
    started_at: datetime


class SystemStatusOut(BaseModel):
    checked_at: datetime
    gmail: StatusChipOut
    linkedin: StatusChipOut
    netkeep: StatusChipOut
    """The server's parts only: background checks, and the Keychain when it refused.
    Each client rolls it up again with whether it reaches the server."""
    linkedin_paused: bool
    """LinkedIn scheduled runs are paused."""
    linkedin_run: RunInProgressOut | None
    """The LinkedIn run in progress, if any."""
    campaign_sending: bool
    """A campaign tick is in flight in this process, and may be sending."""
    update_available: str | None
    """The newer netkeep version the daily check found (#762); null until then."""


def _target(target: service.Target) -> StatusTargetOut:
    return StatusTargetOut(to=target.to, hash=target.hash)


def _chip(chip: service.Chip) -> StatusChipOut:
    return StatusChipOut(
        system=chip.key,
        name=chip.name,
        tone=chip.tone,
        word=chip.word,
        fact=chip.fact,
        parts=[
            StatusPartOut(
                key=part.key,
                label=part.label,
                tone=part.tone,
                word=part.word,
                fact=part.fact,
                detail=part.detail,
                target=None if part.target is None else _target(part.target),
            )
            for part in chip.parts
        ],
        target=_target(chip.target),
        page=chip.page,
        times={
            name: StatusTimeOut(at=time.at, style=time.style) for name, time in chip.times.items()
        },
    )


def _process(request: Request, user_id: int) -> service.Process:
    """What this process knows, read from ``app.state``; none of it is started here."""
    state = request.app.state
    bus: EventBus = state.bus
    tick = bus.latest(TICK_EVENT, user_id)
    progress = bus.latest(PROGRESS_EVENT, user_id)
    engine = state.campaign_engine
    data: Mapping[str, Any] | None = None if progress is None else progress.data
    return service.Process(
        serving=serving_of(request, user_id),
        scheduler_running=state.scheduler is not None,
        key_persisted=state.local_api_key.persisted,
        last_sent_at=None if tick is None else tick.ts,
        progress=data,
        campaign_busy=engine is not None and engine.busy,
    )


@router.get("/system-status", operation_id="get_system_status")
def get_system_status(request: Request, user: CurrentUser, session: SessionDep) -> SystemStatusOut:
    """The Gmail, LinkedIn and netkeep chips, and what the menu bar's confirmations name.
    Read-only."""
    now = utcnow()
    settings = effective_settings(request, session, user)
    reads = service.read_all(
        session, user, now=now, settings=settings, process=_process(request, user.id)
    )
    status = service.system_status(reads)
    run = status.linkedin_run
    return SystemStatusOut(
        checked_at=now,
        gmail=_chip(status.gmail),
        linkedin=_chip(status.linkedin),
        netkeep=_chip(status.netkeep),
        linkedin_paused=status.linkedin_paused,
        linkedin_run=None
        if run is None
        else RunInProgressOut(id=run.id, kind=run.kind, label=run.label, started_at=run.started_at),
        campaign_sending=status.campaign_sending,
        update_available=None,
    )
