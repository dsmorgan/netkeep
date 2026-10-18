"""Database snapshots under ``<data_dir>/backups/`` (spec section 15).

A backup is one file, ``netkeeper-<UTC stamp>.sqlite3``, written by SQLite's
``VACUUM INTO`` from a read-only connection: a consistent, compacted copy taken
without blocking the server's writers. Retention keeps the newest ``[backup] keep``
files and touches nothing else in the directory. ``netkeeper serve`` takes one
nightly (:mod:`netkeeper.services.backup_job`, :func:`nightly_due`), and
:func:`restore_backup` puts one back in place of the database (P6-04).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Final

from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import Engine
from sqlalchemy.engine import URL, make_url

from netkeeper import migrations
from netkeeper.db import make_engine
from netkeeper.localtime import find_zone
from netkeeper.logging_setup import personal

BACKUPS_DIRNAME = "backups"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
# ISO 8601 basic form: sorts chronologically as text and has no characters that
# any filesystem or shell objects to (colons in particular).
_NAME = re.compile(r"^netkeeper-(?P<stamp>\d{8}T\d{6}Z)(?P<pre>-pre-restore)?\.sqlite3$")
PRE_RESTORE_SUFFIX = "-pre-restore"
"""Marks the backup ``netkeeper restore`` takes of the database it replaces."""

PRE_RESTORE_KEEP_DAYS = 30
"""How long the newest pre-restore backup (a restore's undo) is exempt from pruning."""

NIGHTLY_HOUR = 3
"""The local hour (in ``linkedin.timezone``) from which a night's backup is due."""

log = logging.getLogger(__name__)


class BackupError(Exception):
    """A backup could not be made: wrong database kind, no file yet, or SQLite refused."""


class RestoreError(Exception):
    """A backup could not be restored, and the database in place holds what it held
    before, unless the message says the restore went through and only ``db upgrade``
    after it failed."""


class DatabaseInUse(RestoreError):
    """Another connection has the database open, so restore refused before changing it."""


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
    auto_send_cleared: tuple[int, ...] = ()
    """The users whose Settings-page LinkedIn auto-send the backup had on, now off (#598)."""


@dataclass(frozen=True, slots=True)
class BackupInfo:
    """One backup file. ``created_at`` comes from the name, so copying the file keeps it."""

    path: Path
    created_at: datetime
    size: int
    pre_restore: bool = False
    """``netkeeper restore``'s backup of the database it replaced."""


def backup_name(created_at: datetime, *, pre_restore: bool = False) -> str:
    """The file name of a backup taken at ``created_at`` (timezone-aware)."""
    suffix = PRE_RESTORE_SUFFIX if pre_restore else ""
    return f"netkeeper-{_utc(created_at).strftime(STAMP_FORMAT)}{suffix}.sqlite3"


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
    log.info("backing up %s to %s", personal(source), personal(target))
    # A read-only connection cannot touch the source, and autocommit mode
    # (isolation_level=None) keeps pysqlite from opening the transaction that
    # VACUUM refuses to run inside.
    uri = f"{source.resolve().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as connection:
            _vacuum_into(connection, target)
    except (sqlite3.Error, OSError) as exc:
        raise BackupError(f"{source}: VACUUM INTO {target} failed: {exc}") from exc
    return target


PARTIAL_SUFFIX = ".partial"
STALE_PARTIAL_AGE = timedelta(hours=1)
"""A partial backup this old is from a process that died mid-write, never one running."""


def partial_path(target: Path) -> Path:
    """Where the backup bound for ``target`` is written first: ``.netkeeper-<stamp>.partial``
    beside it. The leading dot and the suffix keep it out of :func:`list_backups`."""
    return target.with_name(f".{target.stem}{PARTIAL_SUFFIX}")


def _vacuum_into(connection: sqlite3.Connection, target: Path) -> None:
    """``VACUUM INTO`` a temporary name, synced, then renamed to ``target``, so a backup
    killed mid-write never appears under a backup's name. On an error the temporary file
    is removed; after a kill, :func:`remove_stale_partials` removes it later."""
    partial = partial_path(target)
    _unlink_with_journal(partial)  # a leftover from an earlier death at this same second
    try:
        connection.execute("VACUUM INTO ?", (str(partial),))
        fd = os.open(partial, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        partial.replace(target)
    except BaseException:
        _unlink_with_journal(partial)
        raise


def _unlink_with_journal(path: Path) -> None:
    path.unlink(missing_ok=True)
    Path(f"{path}-journal").unlink(missing_ok=True)


def remove_stale_partials(backups_dir: Path, *, now: datetime | None = None) -> list[Path]:
    """Delete the partial backups (and their ``-journal`` files) a process killed
    mid-write left in ``backups_dir`` more than :data:`STALE_PARTIAL_AGE` ago, and any
    ``-journal`` beside a backup that old. Returns what was removed."""
    if not backups_dir.is_dir():
        return []
    cutoff = (datetime.now(UTC) if now is None else _utc(now)) - STALE_PARTIAL_AGE
    removed: list[Path] = []
    for path in backups_dir.iterdir():
        name = path.name
        partial = name.startswith(".netkeeper-") and (
            name.endswith(PARTIAL_SUFFIX) or name.endswith(f"{PARTIAL_SUFFIX}-journal")
        )
        journal = name.endswith(".sqlite3-journal") and _NAME.match(name[: -len("-journal")])
        if not (partial or journal) or not path.is_file():
            continue
        if datetime.fromtimestamp(path.stat().st_mtime, UTC) >= cutoff:
            continue
        path.unlink(missing_ok=True)
        log.info("removed a partial backup left by an interrupted write: %s", personal(path))
        removed.append(path)
    return removed


def prune_backups(backups_dir: Path, keep: int, *, now: datetime | None = None) -> list[Path]:
    """Delete all but the newest ``keep`` backups in ``backups_dir``; return what was removed.

    Newest means by the timestamp in the name, not by mtime. Only files named like
    a backup are considered, so anything else in the directory (a renamed backup
    someone wants to keep, notes, a restore in progress) is left alone. The newest
    pre-restore backup, the last restore's undo, is neither pruned nor counted until
    it is :data:`PRE_RESTORE_KEEP_DAYS` days old (at ``now``) or a later restore takes
    its place. Removed paths come back oldest first. Partial backups older than an hour
    go too (:func:`remove_stale_partials`), and are not in the list returned.
    """
    if keep < 1:
        raise ValueError(f"keep must be at least 1, got {keep}")
    at = datetime.now(UTC) if now is None else _utc(now)
    remove_stale_partials(backups_dir, now=at)
    found = list_backups(backups_dir)
    undo = next((info for info in found if info.pre_restore), None)
    if undo is not None and at - undo.created_at < timedelta(days=PRE_RESTORE_KEEP_DAYS):
        found.remove(undo)
    removed: list[Path] = []
    for info in reversed(found[keep:]):
        info.path.unlink(missing_ok=True)
        log.info("pruned backup %s", personal(info.path))
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
        found.append(
            BackupInfo(
                path=path,
                created_at=created_at,
                size=path.stat().st_size,
                pre_restore=path.name.endswith(f"{PRE_RESTORE_SUFFIX}.sqlite3"),
            )
        )
    found.sort(key=lambda info: info.created_at, reverse=True)
    return found


def nightly_due(newest: datetime | None, now: datetime, timezone: str) -> bool:
    """Whether the nightly backup is due at ``now``: no backup since the latest
    :data:`NIGHTLY_HOUR` o'clock in ``timezone`` at or before ``now``.

    So the night's backup is taken at the first check after 03:00, and a machine asleep
    then takes it at its first check after waking. A backup made by hand after 03:00
    counts as that night's; ``newest`` leaves out pre-restore backups, which never do.
    A zone that cannot be read counts as UTC.
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

    ``netkeeper restore`` holds the serve lock, so no ``serve`` starts meanwhile. In
    order:

    1. The backup is checked: SQLite's integrity check, and a revision these
       migrations know.
    2. The database in place is opened and locked exclusively
       (:func:`_lock_exclusively`). Any other connection to it, from any process,
       makes this fail with :class:`DatabaseInUse`, before anything is written.
    3. Through that connection, the database is backed up into ``backups_dir`` as a
       pre-restore backup, then the backup at ``source`` is written over it with
       SQLite's backup API: one transaction under a rollback journal, so a crash or a
       failure leaves the old contents and a success leaves all of the new ones.
       Because it is the same file, a symlinked database stays a symlink, and a
       process that opened the path while the lock was held reads the restored
       database once it gets in.
       Before that write, the backup's Settings-page LinkedIn auto-send rows are
       removed from the copy that is written (:func:`_clear_auto_send`, #598): only a
       person's confirmation on the page turns auto-send on, and a restore is not that.
       The clear and the restore land together or not at all, before any migration, so
       no failure after it can leave the backup's "on" in place. ``config.toml`` still
       wins where it sets the key.
    4. The migrations run to head (``db upgrade``).

    Nothing is pruned: the backup of the database replaced must outlive the restore.
    Between the exclusive connection closing and the migrations, a process that was
    waiting can read the restored database at its own, older revision; only a backup
    from an older netkeeper has one, and ``serve`` and ``db upgrade`` cannot get in then
    (the serve lock).
    """
    try:
        target = _sqlite_path(database_url)
    except BackupError as exc:
        raise RestoreError(str(exc)) from exc
    source = source.resolve()
    if target.exists() and source == target.resolve():
        raise RestoreError(f"{source} is the database itself")
    source_revision = _check_restorable(source)
    stamp = datetime.now(UTC) if now is None else _utc(now)
    created = not target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    previous: Path | None = None
    try:
        with closing(_lock_exclusively(target)) as connection:
            if not created:
                previous = _pre_restore_backup(connection, target, backups_dir, stamp)
            try:
                cleared = _write_over(connection, source)
            except AutoSendClearError as exc:
                raise RestoreError(
                    f"could not turn LinkedIn auto-send off in {source}, so nothing was"
                    f" restored and {target} holds what it held before: {exc}"
                ) from exc
            except sqlite3.Error as exc:
                raise RestoreError(
                    f"restoring {source} failed, and {target} holds what it held before: {exc}"
                ) from exc
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
    log.info("restored %s over %s", personal(source), personal(target))
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
        auto_send_cleared=cleared,
    )


IN_USE = "another netkeeper process has the database open; stop it and retry"


def _lock_exclusively(database: Path) -> sqlite3.Connection:
    """A connection holding ``database`` exclusively until it closes, or
    :class:`DatabaseInUse` when any other connection has it open.

    ``locking_mode=EXCLUSIVE`` and ``BEGIN EXCLUSIVE`` (never waiting: ``timeout=0``)
    fail while another connection is open, even an idle one, and write nothing. Then
    the write-ahead log is folded in, every frame of it (``busy`` 0, all frames
    checkpointed), and the database leaves WAL mode (``journal_mode=DELETE``), which
    SQLite itself refuses while anyone else could still read the log. Only then is the
    log gone, by SQLite's own hand. Up to this point the database's contents are
    unchanged; a refusal after the checkpoint leaves the same rows in the main file.
    The next ``make_engine`` puts it back in WAL mode.
    """
    connection = sqlite3.connect(database, isolation_level=None, timeout=0)
    try:
        try:
            connection.execute("PRAGMA locking_mode=EXCLUSIVE")
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("COMMIT")
            busy, frames, copied = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy != 0 or frames != copied:
                raise DatabaseInUse(
                    f"{database}: {IN_USE} (its write-ahead log could not be folded in:"
                    f" busy={busy}, {copied} of {frames} frames)"
                )
            (mode,) = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        except sqlite3.OperationalError as exc:
            raise DatabaseInUse(f"{database}: {IN_USE} ({exc})") from exc
        if str(mode).lower() != "delete":
            raise DatabaseInUse(f"{database}: {IN_USE} (it stayed in {mode} mode)")
    except BaseException:
        connection.close()
        raise
    return connection


#: The ``settings_kv`` key of the Settings page's LinkedIn auto-send (#598). The table and
#: its ``user_id``, ``key`` and ``value`` columns are unchanged since migration 0001, so
#: plain SQL reads them in a backup from any netkeeper.
AUTO_SEND_ROW_KEY: Final = "config.campaigns.linkedin_auto_send"


class AutoSendClearError(Exception):
    """The backup's Settings-page auto-send could not be removed; nothing was written."""


def _write_over(connection: sqlite3.Connection, source: Path) -> tuple[int, ...]:
    """Copy ``source`` over the database ``connection`` holds, in one transaction: a
    failure rolls back to what was there. (``source`` passed the integrity check.)

    The copy goes through memory first, where :func:`_clear_auto_send` removes the
    backup's Settings-page auto-send; only that cleared copy is written, so the restore
    and the clear happen together or not at all. The ids of the users it was on for."""
    with closing(sqlite3.connect(":memory:")) as staged:
        with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as reader:
            reader.backup(staged)
        try:
            cleared = _clear_auto_send(staged)
        except sqlite3.Error as exc:
            raise AutoSendClearError(str(exc)) from exc
        staged.backup(connection)
    for user_id in cleared:
        log.info(
            "restore: turned off user %d's LinkedIn auto-send from the Settings page;"
            " turn it on again there, with its confirmation, to use it",
            user_id,
        )
    return cleared


def _clear_auto_send(connection: sqlite3.Connection) -> tuple[int, ...]:
    """Delete every user's Settings-page auto-send row in ``connection``, committed. The
    users whose row said on (JSON ``true``)."""
    rows = connection.execute(
        "SELECT user_id, value FROM settings_kv WHERE key = ?", (AUTO_SEND_ROW_KEY,)
    ).fetchall()
    connection.execute("DELETE FROM settings_kv WHERE key = ?", (AUTO_SEND_ROW_KEY,))
    connection.commit()
    return tuple(sorted(int(uid) for uid, value in rows if _is_true(value)))


def _is_true(value: object) -> bool:
    try:
        return json.loads(value) is True if isinstance(value, str | bytes) else value is True
    except ValueError:
        return False


def _pre_restore_backup(
    connection: sqlite3.Connection, database: Path, backups_dir: Path, stamp: datetime
) -> Path:
    """``VACUUM INTO`` a pre-restore backup through the exclusive ``connection``, so it
    holds exactly what the restore replaces."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    target = backups_dir / backup_name(stamp, pre_restore=True)
    if target.exists():
        raise RestoreError(f"could not back up {database} first: {target} already exists")
    log.info("backing up %s to %s before the restore", personal(database), personal(target))
    try:
        _vacuum_into(connection, target)
    except (sqlite3.Error, OSError) as exc:
        raise RestoreError(
            f"could not back up {database} first, and it holds what it held before: {exc}"
        ) from exc
    return target


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
