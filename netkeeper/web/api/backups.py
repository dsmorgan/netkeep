"""``/backups``: the database's backups, for the Settings page (P6-04, spec section 15).

- ``GET /backups`` lists the backups in ``<data_dir>/backups/``, newest first, with the
  command that restores each.
- ``POST /backups`` takes one now and prunes to ``backup.keep``, as ``netkeeper backup
  create`` does.

Restore stays in the terminal (``netkeeper restore``): it needs ``serve`` stopped.
Backups belong to the database, not to a user, so the list is the same for everyone.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import Engine

from netkeeper.paths import data_dir
from netkeeper.services.backup import (
    BACKUPS_DIRNAME,
    BackupError,
    BackupInfo,
    create_backup,
    list_backups,
    prune_backups,
)
from netkeeper.services.backup_job import backs_up
from netkeeper.web.deps import CurrentUser, SessionDep, effective_settings, read_only

router = APIRouter(tags=["backups"])


class BackupOut(BaseModel):
    name: str
    """The file name, as ``netkeeper backup list`` prints it."""
    created_at: datetime
    size: int
    """Bytes."""
    restore_command: str
    """What to run in a terminal, with ``serve`` stopped, to restore this backup."""


class BackupsOut(BaseModel):
    directory: str
    available: bool
    """False when the database is not a SQLite file (PostgreSQL has its own tools)."""
    backups: list[BackupOut]
    """Newest first."""


def _directory() -> Path:
    return data_dir() / BACKUPS_DIRNAME


def _backup_out(info: BackupInfo) -> BackupOut:
    return BackupOut(
        name=info.path.name,
        created_at=info.created_at,
        size=info.size,
        restore_command=f"netkeeper restore {info.path.name}",
    )


def _out(request: Request) -> BackupsOut:
    engine: Engine = request.app.state.engine
    directory = _directory()
    return BackupsOut(
        directory=str(directory),
        available=backs_up(engine),
        backups=[_backup_out(info) for info in list_backups(directory)],
    )


@router.get("/backups", operation_id="list_backups")
def get_backups(request: Request, user: CurrentUser) -> BackupsOut:
    """The backups, newest first."""
    return _out(request)


@router.post(
    "/backups",
    operation_id="create_backup",
    status_code=201,
    responses={409: {"description": "No backup could be made: the reason is in detail"}},
)
@read_only  # it writes a file, never a row, so it never takes the database's write lock
def post_backup(request: Request, session: SessionDep, user: CurrentUser) -> BackupsOut:
    """Back up the database now, then delete the oldest past ``backup.keep``."""
    keep = effective_settings(request, session, user).backup.keep
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
