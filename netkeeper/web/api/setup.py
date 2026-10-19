"""``/setup``: where first-run setup stands, for the dashboard's Steps tab (#651, #667).

- ``GET /setup`` answers the ordered steps, each with its state, what it waits for,
  a plain reason and the page that handles it.
- ``POST /setup/steps/{key}/skip`` and ``DELETE /setup/steps/{key}/skip`` skip and
  un-skip a step, per user. A skip changes neither ``done`` nor ``blocked``.

Every state is a read of netkeeper's database, Chrome's profile lock file, and the login
item's plist and ``launchctl print`` / ``print-disabled`` (read-only, the same status
``/login-item`` shows; :mod:`netkeeper.services.setup`). The Keychain is never read.
Nothing here attaches to Chrome, calls Gmail, or starts a run. The login item belongs
to the machine's user, so only the local owner account may read setup (403 for any
other, as with ``/login-item``).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from netkeeper.models import User
from netkeeper.services import setup as service
from netkeeper.services.login_item import LoginItem, LoginItemError
from netkeeper.services.posture import netkeeper_chrome_pid
from netkeeper.services.users import local_owner
from netkeeper.web.deps import CurrentUser, SessionDep

router = APIRouter(prefix="/setup", tags=["setup"])

Responses = dict[int | str, dict[str, Any]]
OWNER_ONLY: Responses = {403: {"description": "Only the local owner account reads setup"}}
NOT_FOUND: Responses = {404: {"description": "No setup step has that key"}}


class SetupStepOut(BaseModel):
    key: str
    title: str
    state: service.StepState
    """``done``, ``in_progress``, ``todo``, ``blocked`` (a prerequisite isn't done) or
    ``skipped``. Done wins over a skip; blocked does too."""
    next: bool
    """One of the steps to do now: every step in progress, and the first one to do."""
    needs: list[str]
    """Keys of the steps that must all be done first."""
    needs_any: list[str]
    """Keys of steps of which one must be done first; empty when there are none."""
    reason: str
    """A plain sentence: why the step isn't done, or what shows it is."""
    target: str
    """The UI route that handles the step."""
    optional: bool
    skipped: bool
    """The person skipped it, whatever ``state`` says."""


class SetupOut(BaseModel):
    steps: list[SetupStepOut]
    complete: bool
    """Every step that isn't optional is done or skipped: the dashboard then opens on
    Overview rather than Steps."""


def _owner(session: SessionDep, user: CurrentUser) -> User:
    owner = local_owner(session)
    if owner is None or owner.id != user.id:
        raise HTTPException(status_code=403, detail="only the local owner account reads setup")
    return owner


Owner = Annotated[User, Depends(_owner)]


def _readings(request: Request) -> service.Readings:
    item: LoginItem | None = getattr(request.app.state, "login_item", None)
    try:
        login_item = (item if item is not None else LoginItem()).status()
        error = None
    except LoginItemError as exc:
        login_item, error = None, str(exc)
    return service.Readings(
        chrome_pid=netkeeper_chrome_pid(),
        login_item=login_item,
        login_item_error=error,
    )


def _out(request: Request, session: SessionDep, user: User) -> SetupOut:
    steps = service.steps(session, user, _readings(request))
    return SetupOut(
        complete=service.complete(steps),
        steps=[
            SetupStepOut(
                key=step.key.value,
                title=step.title,
                state=step.state,
                next=step.next,
                needs=[key.value for key in step.needs],
                needs_any=[key.value for key in step.needs_any],
                reason=step.reason,
                target=step.target,
                optional=step.optional,
                skipped=step.skipped,
            )
            for step in steps
        ],
    )


@router.get("", operation_id="get_setup", responses=OWNER_ONLY)
def get_setup(request: Request, session: SessionDep, owner: Owner) -> SetupOut:
    """Every setup step in order, with its state, from what netkeeper already stores."""
    return _out(request, session, owner)


@router.post(
    "/steps/{key}/skip", operation_id="skip_setup_step", responses={**OWNER_ONLY, **NOT_FOUND}
)
def skip_setup_step(key: str, request: Request, session: SessionDep, owner: Owner) -> SetupOut:
    """Skip a step. It stays listed as a reminder; done and blocked still win."""
    try:
        service.skip(session, owner, key)
    except service.UnknownStep as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _out(request, session, owner)


@router.delete(
    "/steps/{key}/skip", operation_id="unskip_setup_step", responses={**OWNER_ONLY, **NOT_FOUND}
)
def unskip_setup_step(key: str, request: Request, session: SessionDep, owner: Owner) -> SetupOut:
    """Clear a step's skip."""
    try:
        service.unskip(session, owner, key)
    except service.UnknownStep as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _out(request, session, owner)
