"""``/support``: the support bundle, for Settings, Support (#603).

- ``GET /support/levels`` says what each level holds, for the page's preview.
- ``GET /support/logs`` returns the last lines of the log file, filtered and redacted
  (#655): the Logs view's read.
- ``GET /support/bundle`` makes the ``.zip`` and sends it as a download. The full level
  holds real names, so it is refused (409) without ``confirm_full=true``, which the page
  sends only after you tick its confirmation.

A download is a plain ``GET``, which the CSRF guard does not check, so this one checks
for itself, at every level: a page on another site that navigated here would save a
bundle into your Downloads folder, and for the full level skip the confirmation. The
request must say ``Sec-Fetch-Site: same-origin`` (the page's link) or ``none`` (typed
into the address bar); one without the header must carry ``X-Netkeep-Client: 1``
(the CLI's or a script's request, which a cross-site page cannot set).

Both read only, and never attach to a browser (posture comes from stored state). The
bundle holds the log file, which belongs to the whole data directory rather than to a
user, so only the local owner account may make one (403 for anyone else), as with
``/backups``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from netkeep.models import User
from netkeep.services import log_tail, support_bundle
from netkeep.web.deps import CurrentUser, SessionDep, effective_settings, require_owner
from netkeep.web.security import CLIENT_HEADER, CLIENT_HEADER_VALUE

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


class LogLineOut(BaseModel):
    at: datetime
    """Local time, with its offset."""
    level: str
    logger: str
    message: str
    """Redacted: marked personal values, addresses and credentials are placeholders."""


class LogTailOut(BaseModel):
    lines: list[LogLineOut]
    """Oldest first, at most ``MAX_LINES``."""
    reason: log_tail.Reason | None
    """Why ``lines`` is empty: no log file, an empty one, or nothing passing the filters."""


def _owner(session: SessionDep, user: CurrentUser) -> User:
    return require_owner(session, user, "only the local owner account makes a support bundle")


Owner = Annotated[User, Depends(_owner)]
_MAX_DAYS = support_bundle.MAX_SINCE.days
_SAME_SITE: Final = frozenset({"same-origin", "none"})


def cross_site(request: Request) -> bool:
    """Whether the download could have come from another site's page (module docstring)."""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site.strip().lower() not in _SAME_SITE
    return request.headers.get(CLIENT_HEADER) != CLIENT_HEADER_VALUE


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


@router.get("/support/logs", operation_id="get_recent_logs")
def get_logs(
    request: Request,
    owner: Owner,
    lines: Annotated[int, Query(ge=1, le=log_tail.MAX_LINES)] = log_tail.DEFAULT_LINES,
    level: log_tail.LevelName | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> LogTailOut:
    """The newest ``lines`` log records at ``level`` or above, within ``since``..``until``
    (a time without an offset is local), oldest first, redacted like the support bundle."""
    key = request.app.state.local_api_key
    tail = log_tail.recent_lines(
        lines=lines,
        level=level,
        since=since,
        until=until,
        secrets=(key.key, key.session_value()),
    )
    return LogTailOut(
        lines=[
            LogLineOut(at=item.at, level=item.level, logger=item.logger, message=item.message)
            for item in tail.lines
        ],
        reason=tail.reason,
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
        403: {
            "description": "Only the local owner account makes a support bundle, and only"
            " from this site's own page or a request with X-Netkeep-Client"
        },
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
    if cross_site(request):
        raise HTTPException(
            status_code=403,
            detail="a support bundle downloads only from netkeep's own Settings page",
        )
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
