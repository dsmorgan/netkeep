"""``GET /attention``: the nav counts, the warning flags and the activity state (#508, #347).

The app shell polls it once a minute, and refetches it on a relevant event or a change
in the same tab. See :mod:`netkeeper.services.attention` for where each one comes from.

Read-only and per-user: every answer is a database read. Nothing here touches the
browser or Gmail.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from netkeeper.models.base import utcnow
from netkeeper.services import attention as service
from netkeeper.web.deps import CurrentUser, SessionDep, effective_settings
from netkeeper.web.schemas import (
    ActivityOut,
    AttentionCountsOut,
    AttentionFlagsOut,
    AttentionOut,
)

router = APIRouter(tags=["attention"])


@router.get("/attention", operation_id="get_attention")
def get_attention(request: Request, user: CurrentUser, session: SessionDep) -> AttentionOut:
    """Each nav count and flag, and what LinkedIn is doing. Read-only."""
    now = utcnow()
    settings = effective_settings(request, session, user)
    found = service.attention(session, user, now=now, settings=settings)
    counts, flags, activity = found.counts, found.flags, found.activity
    return AttentionOut(
        checked_at=now,
        counts=AttentionCountsOut(
            inbox=counts.inbox,
            dashboard=counts.dashboard,
            waiting=counts.waiting,
            try_again=counts.try_again,
            triage=counts.triage,
            imports=counts.imports,
        ),
        flags=AttentionFlagsOut(
            dashboard=flags.dashboard,
            gmail=flags.gmail,
            linkedin=flags.linkedin,
            settings=flags.settings,
        ),
        activity=ActivityOut(
            state=activity.state,
            label=activity.label,
            run_id=activity.run_id,
            run_kind=activity.run_kind,
        ),
        title_total=found.title_total,
    )
