"""``/support``: the support bundle, for Settings, Support (#603).

- ``GET /support/levels`` says what each level holds, for the page's preview.
- ``GET /support/bundle`` makes the ``.zip`` and sends it as a download. The full level
  holds real names, so it is refused (409) without ``confirm_full=true``, which the page
  sends only after you tick its confirmation.

Both read only, and never attach to a browser (posture comes from stored state). The
bundle holds the log file, which belongs to the whole data directory rather than to a
user, so only the local owner account may make one (403 for anyone else), as with
``/backups``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from netkeeper.models import User
from netkeeper.services import support_bundle
from netkeeper.services.users import local_owner
from netkeeper.web.deps import CurrentUser, SessionDep, effective_settings

router = APIRouter(tags=["support"])


class SupportLevelOut(BaseModel):
    level: support_bundle.Level
    title: str
    summary: str
    includes: list[str]
    leaves_out: list[str]
    default_since_days: int
    redacted: bool
    """False only for the full level: real names and identifiers."""
    needs_confirmation: bool
    warning: str | None


class SupportLevelsOut(BaseModel):
    levels: list[SupportLevelOut]
    max_since_days: int


def _owner(session: SessionDep, user: CurrentUser) -> User:
    owner = local_owner(session)
    if owner is None or owner.id != user.id:
        raise HTTPException(
            status_code=403, detail="only the local owner account makes a support bundle"
        )
    return owner


Owner = Annotated[User, Depends(_owner)]
_MAX_DAYS = support_bundle.MAX_SINCE.days


@router.get("/support/levels", operation_id="list_support_levels")
def get_levels(owner: Owner) -> SupportLevelsOut:
    """What each level of the support bundle includes and leaves out."""
    return SupportLevelsOut(
        levels=[
            SupportLevelOut(
                level=spec.level,
                title=spec.title,
                summary=spec.summary,
                includes=list(spec.includes),
                leaves_out=list(spec.leaves_out),
                default_since_days=spec.default_since_days,
                redacted=spec.redacted,
                needs_confirmation=spec.needs_confirmation,
                warning=spec.warning,
            )
            for spec in support_bundle.LEVELS.values()
        ],
        max_since_days=_MAX_DAYS,
    )


@router.get(
    "/support/bundle",
    operation_id="download_support_bundle",
    response_class=Response,
    responses={
        200: {
            "description": "The support bundle, a .zip file.",
            "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
        },
        403: {"description": "Only the local owner account makes a support bundle"},
        409: {"description": "The full level without confirm_full=true"},
    },
)
def download_bundle(
    request: Request,
    session: SessionDep,
    owner: Owner,
    level: support_bundle.Level = support_bundle.Level.STANDARD,
    since_days: Annotated[int | None, Query(ge=1, le=_MAX_DAYS)] = None,
    confirm_full: bool = False,
) -> Response:
    """Make the bundle at ``level`` covering ``since_days`` (the level's default when
    absent), and send it as an attachment."""
    spec = support_bundle.LEVELS[level]
    if spec.needs_confirmation and not confirm_full:
        raise HTTPException(
            status_code=409,
            detail="the full level holds real names and ids: confirm it first (confirm_full)",
        )
    settings = effective_settings(request, session, owner)
    bundle = support_bundle.build_bundle(
        session,
        owner,
        settings=settings,
        level=level,
        since=None if since_days is None else timedelta(days=since_days),
    )
    return Response(
        content=bundle.data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.filename}"',
            "Cache-Control": "no-store",
        },
    )
