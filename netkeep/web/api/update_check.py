"""``/update-check``: what ``serve``'s daily update check found (#762).

``GET /update-check`` reads what the check recorded (:mod:`netkeep.services.update_check`)
and never makes a request itself. ``available`` is the newer release, or null: none was
found, the check is off, or this netkeep is already that version. ``command`` is what to
run then; nothing here installs anything (ADR 0011 rule 9). The check is install-wide, so
``enabled`` is the local owner's setting.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

from netkeep import __version__
from netkeep.paths import data_dir
from netkeep.services import ui_settings, update_check
from netkeep.services.users import local_owner
from netkeep.web.deps import CurrentUser, SessionDep, file_settings

router = APIRouter(tags=["update-check"])


class UpdateCheckOut(BaseModel):
    enabled: bool
    """**Check for updates daily** is on (the local owner's value, or config.toml's)."""
    current: str
    """This netkeep's version."""
    checked_at: datetime | None
    """When the last check ran, or null when none has."""
    ok: bool | None
    """The last check got an answer; null when none has run."""
    latest: str | None
    """The newest release the last successful check found."""
    available: str | None
    """A newer release than this one, while the check is on; null otherwise."""
    command: str | None
    """What to run to update, when ``available`` is set."""


@router.get("/update-check", operation_id="get_update_check")
def get_update_check(request: Request, session: SessionDep, user: CurrentUser) -> UpdateCheckOut:
    owner = local_owner(session) or user
    enabled = ui_settings.resolve(session, owner, file_settings(request)).updates.check_daily
    result = update_check.read_result(data_dir())
    available = update_check.newer_release(result) if enabled else None
    return UpdateCheckOut(
        enabled=enabled,
        current=__version__,
        checked_at=result.checked_at if result is not None else None,
        ok=result.ok if result is not None else None,
        latest=result.latest if result is not None else None,
        available=available,
        command=update_check.UPDATE_COMMAND if available is not None else None,
    )
