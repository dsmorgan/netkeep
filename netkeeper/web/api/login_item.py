"""``/login-item``: start netkeeper when you log in, for the Settings page (#600).

- ``GET /login-item`` shows the state, read from the launchd plist and ``launchctl``.
- ``PUT /login-item`` with ``{"enabled": true|false}`` installs or removes the agent,
  as ``netkeeper login-item on|off`` does.

This writes outside the data directory (``~/Library/LaunchAgents``) and is not a
LinkedIn safety change. The agent belongs to the machine's user, not to a netkeeper
user, so only the local owner account may read or change it (403 for any other, as
with ``/backups``). Turning it on here never starts a second ``serve``: this process
is a ``serve`` and holds the serve lock, so the agent is registered for the next login
(:mod:`netkeeper.services.login_item`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from netkeeper.models import User
from netkeeper.services.login_item import LoginItem, LoginItemError, Status
from netkeeper.services.users import local_owner
from netkeeper.web.deps import CurrentUser, SessionDep, read_only

router = APIRouter(tags=["login-item"])


class LoginItemOut(BaseModel):
    supported: bool
    """False off macOS: the switch is disabled and ``unsupported_reason`` says why."""
    unsupported_reason: str | None
    enabled: bool
    """netkeeper starts at login: the plist is there and launchd will load it."""
    running_under_launchd: bool
    """launchd has the job loaded now."""
    this_server_is_the_login_item: bool
    """The server answering was started by the login item, so turning it off stops it."""
    label: str
    plist_path: str
    log_path: str
    """``netkeeper.log`` in the data directory."""
    points_elsewhere: str | None
    """The program the plist runs, when that is not the installed ``netkeeper``: turn
    the switch off and on to point it at this one. Null when it matches."""
    message: str | None = None
    """What a change did, such as "starts at your next login, not now"; null on a read."""


def _out(status: Status, message: str | None = None) -> LoginItemOut:
    return LoginItemOut(
        supported=status.supported,
        unsupported_reason=status.reason,
        enabled=status.enabled,
        running_under_launchd=status.loaded,
        this_server_is_the_login_item=status.this_serve_is_the_agent,
        label=status.label,
        plist_path=str(status.plist_path),
        log_path=str(status.log_path),
        points_elsewhere=status.plist_executable if status.stale else None,
        message=message,
    )


class LoginItemIn(BaseModel):
    enabled: bool


def _owner(session: SessionDep, user: CurrentUser) -> User:
    owner = local_owner(session)
    if owner is None or owner.id != user.id:
        raise HTTPException(
            status_code=403, detail="only the local owner account manages the login item"
        )
    return owner


Owner = Annotated[User, Depends(_owner)]


def _item(request: Request) -> LoginItem:
    """The app's login item; tests put a fake one on ``app.state``."""
    item: LoginItem | None = getattr(request.app.state, "login_item", None)
    return item if item is not None else LoginItem()


@router.get("/login-item", operation_id="get_login_item")
def get_login_item(request: Request, owner: Owner) -> LoginItemOut:
    """Whether netkeeper starts at login, from the plist and launchctl."""
    return _out(_item(request).status())


@router.put(
    "/login-item",
    operation_id="set_login_item",
    responses={
        403: {"description": "Only the local owner account manages the login item"},
        409: {"description": "Not macOS, or launchctl refused: the reason is in detail"},
    },
)
@read_only  # it writes a plist, never a row, so it never takes the database's write lock
def put_login_item(request: Request, owner: Owner, body: LoginItemIn) -> LoginItemOut:
    """Install or remove the login item."""
    item = _item(request)
    try:
        outcome = (
            item.enable(serving_here=True) if body.enabled else item.disable(serving_here=True)
        )
    except LoginItemError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _out(outcome.status, outcome.message)
