"""Database snapshots under ``<data_dir>/backups/`` (spec section 15).

A backup is one file, ``netkeeper-<UTC stamp>.sqlite3``, written by SQLite's
``VACUUM INTO`` from a read-only connection: a consistent, compacted copy taken
without blocking the server's writers. Retention keeps the newest ``[backup] keep``
files and touches nothing else in the directory. ``netkeeper serve`` takes one
nightly (:mod:`netkeeper.services.backup_job`, :func:`nightly_due`), and
:func:`restore_backup` puts one back in place of the database (P6-04).
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import Engine
from sqlalchemy.engine import URL, make_url

from netkeeper import migrations
from netkeeper.db import make_engine
from netkeeper.localtime import find_zone

BACKUPS_DIRNAME = "backups"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
# ISO 8601 basic form: sorts chronologically as text and has no characters that
# any filesystem or shell objects to (colons in particular).
_NAME = re.compile(r"^netkeeper-(?P<stamp>\d{8}T\d{6}Z)\.sqlite3$")

NIGHTLY_HOUR = 3
"""The local hour (in ``linkedin.timezone``) from which a night's backup is due."""

log = logging.getLogger(__name__)


class BackupError(Exception):
    """A backup could not be made: wrong database kind, no file yet, or SQLite refused."""


class RestoreError(Exception):
    """A backup could not be restored. The database in place was not changed, unless the
    message says the restore went through and only ``db upgrade`` after it failed."""


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """What :func:`restore_backup` did."""

    source: Path
    database: Path
    previous: Path | None
    """The backup of the database that was in place, or None when there was none."""
    source_revision: str
    revision: str | None
    """The revision after ``db upgrade``: the head of this netkeeper."""


@dataclass(frozen=True, slots=True)
class BackupInfo:
    """One backup file. ``created_at`` comes from the name, so copying the file keeps it."""

    path: Path
    created_at: datetime
    size: int


def backup_name(created_at: datetime) -> str:
    """The file name of a backup taken at ``created_at`` (timezone-aware)."""
    return f"netkeeper-{_utc(created_at).strftime(STAMP_FORMAT)}.sqlite3"


def parse_backup_name(name: str) -> datetime | None:
    """The UTC time encoded in a backup file name, or None when ``name`` is not one."""
    match = _NAME.match(name)
    if match is None:
        return None
    return datetime.strptime(match["stamp"], STAMP_FORMAT).replace(tzinfo=UTC)


def create_backup(
    engine_or_url: Engine | str, backups_dir: Path, *, now: datetime | None = None
) -> Path:
    """Snapshot the SQLite database behind ``engine_or_url`` into ``backups_dir``.

    Returns the file written. The directory is created when missing. ``now``
    (timezone-aware) fixes the timestamp in the name; the default is the current
    UTC time. Raises :class:`BackupError` when the database is not SQLite
    (PostgreSQL has its own tools and is not covered here), when the database file
    does not exist yet, when a backup with the same name already exists, and when
    SQLite cannot write the copy.
    """
    source = _sqlite_file(engine_or_url)
    stamp = datetime.now(UTC) if now is None else _utc(now)
    backups_dir.mkdir(parents=True, exist_ok=True)
    target = backups_dir / backup_name(stamp)
    if target.exists():
        raise BackupError(f"{target}: already exists (backups are named to the second)")
    log.info("backing up %s to %s", source, target)
    # A read-only connection cannot touch the source, and autocommit mode
    # (isolation_level=None) keeps pysqlite from opening the transaction that
    # VACUUM refuses to run inside.
    uri = f"{source.resolve().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as connection:
            connection.execute("VACUUM INTO ?", (str(target),))
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        raise BackupError(f"{source}: VACUUM INTO {target} failed: {exc}") from exc
    return target


def prune_backups(backups_dir: Path, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` backups in ``backups_dir``; return what was removed.

    Newest means by the timestamp in the name, not by mtime. Only files named like
    a backup are considered, so anything else in the directory (a renamed backup
    someone wants to keep, notes, a restore in progress) is left alone. Removed
    paths come back oldest first.
    """
    if keep < 1:
        raise ValueError(f"keep must be at least 1, got {keep}")
    removed: list[Path] = []
    for info in reversed(list_backups(backups_dir)[keep:]):
        info.path.unlink(missing_ok=True)
        log.info("pruned backup %s", info.path)
        removed.append(info.path)
    return removed


def list_backups(backups_dir: Path) -> list[BackupInfo]:
    """The backups in ``backups_dir``, newest first. A missing directory lists as empty."""
    if not backups_dir.is_dir():
        return []
    found: list[BackupInfo] = []
    for path in backups_dir.iterdir():
        created_at = parse_backup_name(path.name)
        if created_at is None or not path.is_file():
            continue
        found.append(BackupInfo(path=path, created_at=created_at, size=path.stat().st_size))
    found.sort(key=lambda info: info.created_at, reverse=True)
    return found


def nightly_due(newest: datetime | None, now: datetime, timezone: str) -> bool:
    """Whether the nightly backup is due at ``now``: no backup since the latest
    :data:`NIGHTLY_HOUR` o'clock in ``timezone`` at or before ``now``.

    So the night's backup is taken at the first check after 03:00, and a machine asleep
    then takes it at its first check after waking. A backup made by hand after 03:00
    counts as that night's. A zone that cannot be read counts as UTC.
    """
    tz: tzinfo = find_zone(timezone) or UTC
    local = _utc(now).astimezone(tz)
    mark = local.replace(hour=NIGHTLY_HOUR, minute=0, second=0, microsecond=0)
    if mark > local:
        mark -= timedelta(days=1)
    return newest is None or _utc(newest) < mark


def find_backup(name_or_path: str, backups_dir: Path) -> Path:
    """The backup ``name_or_path`` names: a file name in ``backups_dir`` (as ``netkeeper
    backup list`` prints it), else a path. :class:`RestoreError` when it is not a file."""
    if os.sep not in name_or_path and (backups_dir / name_or_path).is_file():
        return backups_dir / name_or_path
    path = Path(name_or_path).expanduser()
    if not path.is_file():
        raise RestoreError(f"{name_or_path}: no such backup (see netkeeper backup list)")
    return path


def restore_backup(
    source: Path, database_url: str, backups_dir: Path, *, now: datetime | None = None
) -> RestoreResult:
    """Put the backup at ``source`` in place of the SQLite database at ``database_url``.

    The caller makes sure nothing has the database open (``netkeeper restore`` holds the
    serve lock). In order: the backup is checked (an intact SQLite file at a revision
    this netkeeper knows), the database in place is backed up into ``backups_dir``, the
    backup is copied with SQLite's backup API to a temporary file beside the database
    and checked again, the old database's WAL is checkpointed and cleared, the copy is
    renamed over the database, and the migrations run to head (``db upgrade``). Up to
    the rename, a failure leaves the database as it was (:class:`RestoreError`); the
    rename itself is atomic, so the database is always either the old file or the
    whole restored one. Nothing is pruned: the backup of the database replaced must
    outlive the restore.
    """
    try:
        target = _sqlite_path(database_url)
    except BackupError as exc:
        raise RestoreError(str(exc)) from exc
    source = source.resolve()
    if target.exists() and source == target.resolve():
        raise RestoreError(f"{source} is the database itself")
    source_revision = _check_restorable(source)
    previous: Path | None = None
    if target.is_file():
        try:
            previous = create_backup(database_url, backups_dir, now=now)
        except BackupError as exc:
            raise RestoreError(f"could not back up the database in place first: {exc}") from exc
    temporary = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}")
    try:
        _copy_database(source, temporary)
        if target.is_file():
            _clear_wal(target)
        temporary.replace(target)
    except (OSError, sqlite3.Error) as exc:
        raise RestoreError(f"restoring {source} failed; {target} is unchanged: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{temporary}{suffix}").unlink(missing_ok=True)
    _fsync_directory(target.parent)
    log.info("restored %s over %s", source, target)
    engine = make_engine(database_url)
    try:
        migrations.upgrade(engine)
        revision = migrations.current_revision(engine)
    except Exception as exc:
        before = (
            "" if previous is None else f"; the database it replaced is backed up at {previous}"
        )
        raise RestoreError(
            f"restored {source} over {target}, but db upgrade failed: {exc}. Run netkeeper db"
            f" upgrade to try again{before}"
        ) from exc
    finally:
        engine.dispose()
    return RestoreResult(
        source=source,
        database=target,
        previous=previous,
        source_revision=source_revision,
        revision=revision,
    )


def _check_restorable(source: Path) -> str:
    """The revision of the netkeeper database at ``source``, which must pass SQLite's
    integrity check and be at a revision these migrations know."""
    try:
        with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as connection:
            check = connection.execute("PRAGMA integrity_check").fetchall()
            if check != [("ok",)]:
                raise RestoreError(f"{source}: failed SQLite's integrity check: {check[:3]}")
            rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    except sqlite3.Error as exc:
        raise RestoreError(f"{source}: not a netkeeper database: {exc}") from exc
    if len(rows) != 1:
        raise RestoreError(f"{source}: not a netkeeper database (no single migration revision)")
    revision = str(rows[0][0])
    script = ScriptDirectory.from_config(migrations.alembic_config())
    try:
        known = script.get_revision(revision) is not None
    except CommandError:
        known = False
    if not known:
        raise RestoreError(
            f"{source}: at migration {revision}, which this netkeeper does not know (a newer"
            " netkeeper made it); upgrade netkeeper first"
        )
    return revision


def _copy_database(source: Path, destination: Path) -> None:
    """A consistent copy of ``source`` at ``destination`` through SQLite's backup API,
    checked and synced to disk."""
    uri = f"{source.as_uri()}?mode=ro"
    with (
        closing(sqlite3.connect(uri, uri=True)) as reader,
        closing(sqlite3.connect(destination)) as writer,
    ):
        reader.backup(writer)
        if writer.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise sqlite3.DatabaseError(f"the copy at {destination} failed its check")
    fd = os.open(destination, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _clear_wal(database: Path) -> None:
    """Fold ``database``'s write-ahead log into it and remove the log, so a WAL the old
    file left behind is never replayed into the restored one. Its content is already in
    the backup taken first."""
    with closing(sqlite3.connect(database, isolation_level=None)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for suffix in ("-wal", "-shm"):
        Path(f"{database}{suffix}").unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        pass  # some filesystems refuse a directory fsync; the rename is still atomic
    finally:
        os.close(fd)


def _sqlite_path(engine_or_url: Engine | str) -> Path:
    """The database file behind ``engine_or_url``, checked to be SQLite (it may not exist)."""
    url: URL = engine_or_url.url if isinstance(engine_or_url, Engine) else make_url(engine_or_url)
    if url.get_backend_name() != "sqlite":
        shown = url.render_as_string(hide_password=True)
        raise BackupError(
            f"{shown}: only a SQLite database can be backed up this way;"
            " PostgreSQL is not supported here (use pg_dump)"
        )
    if not url.database or url.database == ":memory:":
        raise BackupError("an in-memory database has no file to back up")
    return Path(url.database)


def _sqlite_file(engine_or_url: Engine | str) -> Path:
    """The database file behind ``engine_or_url``, checked to be SQLite and present."""
    source = _sqlite_path(engine_or_url)
    if not source.is_file():
        raise BackupError(
            f"{source}: no database file to back up (run `netkeeper db upgrade` first)"
        )
    return source


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("backup timestamps must be timezone-aware")
    return value.astimezone(UTC)
