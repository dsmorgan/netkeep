"""``/backups``: the database's backups, for the Settings page (P6-04, spec section 15).

- ``GET /backups`` lists the backups in ``<data_dir>/backups/``, newest first, with the
  command that restores each.
- ``POST /backups`` takes one now and prunes to ``backup.keep``, as ``netkeeper backup
  create`` does.

Restore stays in the terminal (``netkeeper restore``): it needs ``serve`` stopped.
Backups belong to the database, not to a user, so only the local owner account may list
or take them (403 for any other user, #616), and the prune uses the owner's ``keep``, the
nightly job's value, never the requester's.
"""

from __future__ import annotations

import os
import shlex
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import Engine

from netkeeper.models import User
from netkeeper.paths import DATA_ENV, data_dir
from netkeeper.services.backup import (
    BACKUPS_DIRNAME,
    BackupError,
    BackupInfo,
    create_backup,
    list_backups,
    prune_backups,
)
from netkeeper.services.backup_job import backs_up
from netkeeper.web.deps import CurrentUser, SessionDep, effective_settings, read_only, require_owner

router = APIRouter(tags=["backups"])


class BackupOut(BaseModel):
    name: str
    """The file name, as ``netkeeper backup list`` prints it."""
    created_at: datetime
    size: int
    """Bytes."""
    restore_command: str
    """What to run in a terminal, with ``serve`` stopped, to restore this backup: its
    full path, after ``NETKEEPER_DATA=<dir>`` when serve runs on a data directory set
    that way."""
    pre_restore: bool
    """A restore's backup of the database it replaced: restoring it undoes that restore."""


class BackupsOut(BaseModel):
    directory: str
    available: bool
    """False when the database is not a SQLite file (PostgreSQL has its own tools)."""
    backups: list[BackupOut]
    """Newest first."""


def _directory() -> Path:
    return data_dir() / BACKUPS_DIRNAME


def restore_command(backup: Path) -> str:
    """The command that restores ``backup``, by its full path. It names the data
    directory too when ``serve`` was given one (``NETKEEPER_DATA``), since a terminal
    without it would restore over the default data directory's database."""
    command = f"netkeeper restore {shlex.quote(str(backup))}"
    if os.environ.get(DATA_ENV):
        return f"{DATA_ENV}={shlex.quote(str(data_dir()))} {command}"
    return command


def _backup_out(info: BackupInfo) -> BackupOut:
    return BackupOut(
        name=info.path.name,
        created_at=info.created_at,
        size=info.size,
        pre_restore=info.pre_restore,
        restore_command=restore_command(info.path),
    )


def _owner(session: SessionDep, user: CurrentUser) -> User:
    """The requester, when they are the local owner; 403 for anyone else."""
    return require_owner(session, user, "only the local owner account manages backups")


Owner = Annotated[User, Depends(_owner)]


def _out(request: Request) -> BackupsOut:
    engine: Engine = request.app.state.engine
    directory = _directory()
    return BackupsOut(
        directory=str(directory),
        available=backs_up(engine),
        backups=[_backup_out(info) for info in list_backups(directory)],
    )


@router.get(
    "/backups",
    operation_id="list_backups",
    responses={403: {"description": "Only the local owner account manages backups"}},
)
def get_backups(request: Request, owner: Owner) -> BackupsOut:
    """The backups, newest first."""
    return _out(request)


@router.post(
    "/backups",
    operation_id="create_backup",
    status_code=201,
    responses={
        403: {"description": "Only the local owner account manages backups"},
        409: {"description": "No backup could be made: the reason is in detail"},
    },
)
@read_only  # it writes a file, never a row, so it never takes the database's write lock
def post_backup(request: Request, session: SessionDep, owner: Owner) -> BackupsOut:
    """Back up the database now, then delete the oldest past the owner's ``backup.keep``."""
    keep = effective_settings(request, session, owner).backup.keep
    if keep < 1:
        raise HTTPException(status_code=409, detail=f"backup.keep must be at least 1, got {keep}")
    engine: Engine = request.app.state.engine
    directory = _directory()
    try:
        create_backup(engine, directory)
    except BackupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    prune_backups(directory, keep)
    return _out(request)
