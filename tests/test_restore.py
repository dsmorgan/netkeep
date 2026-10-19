"""P6-04: ``netkeep restore``, the nightly backup in ``serve``, the serve lock, and
``/backups`` for the Settings page."""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi import FastAPI
from sqlalchemy import Engine
from typer.testing import CliRunner

from netkeep import migrations
from netkeep.cli import app as cli
from netkeep.config import BackupSettings, Settings
from netkeep.db import (
    DATABASE_FILENAME,
    database_url,
    make_engine,
    make_session_factory,
    session_scope,
)
from netkeep.models import Base, SettingKV
from netkeep.scoping import install_scope_guard
from netkeep.services import backup as backup_service
from netkeep.services import serve_lock, ui_settings
from netkeep.services.backup import (
    BackupError,
    DatabaseInUse,
    RestoreError,
    backup_name,
    create_backup,
    find_backup,
    list_backups,
    nightly_due,
    prune_backups,
    restore_backup,
)
from netkeep.services.backup_job import NightlyBackups, backs_up
from netkeep.services.scheduled_runs import ServeExtractor
from netkeep.services.users import ensure_local_user
from netkeep.web.api.backups import restore_command
from netkeep.web.app import create_app

CSRF = {"X-Netkeep-Client": "1"}
NY = "America/New_York"
# 2026-10-16 02:30 in New York (EDT, UTC-4): before that night's 03:00.
BEFORE_THREE = datetime(2026, 10, 16, 6, 30, tzinfo=UTC)
AFTER_THREE = datetime(2026, 10, 16, 7, 30, tzinfo=UTC)  # 03:30 EDT
STAMP_10_02 = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)


# --- fixtures and helpers -----------------------------------------------------------


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """NETKEEP_DATA (the CLI's data directory) holding a migrated database with the
    local user and one marker row."""
    directory = tmp_path / "data"
    monkeypatch.setenv("NETKEEP_DATA", str(directory))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    _mark(directory, "original")
    return directory


def _mark(directory: Path, value: str) -> None:
    """Migrate the database in ``directory`` and set its marker row to ``value``."""
    engine = make_engine(database_url(directory))
    try:
        migrations.upgrade(engine)
        with make_session_factory(engine)() as session:
            user = ensure_local_user(session)
            row = session.query(SettingKV).filter_by(user_id=user.id, key="marker").one_or_none()
            if row is None:
                session.add(SettingKV(user_id=user.id, key="marker", value=value))
            else:
                row.value = value
            session.commit()
    finally:
        engine.dispose()


def _marker(database: Path) -> str:
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute("SELECT value FROM settings_kv WHERE key = 'marker'").fetchone()
    assert row is not None
    return str(row[0]).strip('"')


def _diff_against_models(engine: Engine) -> list[Any]:
    """tests/test_migrations.py's check: the schema the migrations made is the models'."""
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        diff: list[Any] = compare_metadata(context, Base.metadata)
        return diff


def _backup_now(data: Path, when: datetime) -> Path:
    return create_backup(database_url(data), data / "backups", now=when)


def _rows(database: Path) -> list[Any]:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute("SELECT * FROM settings_kv ORDER BY id").fetchall()


# --- restore_backup -----------------------------------------------------------------


def test_restore_yields_a_database_that_passes_the_migration_diff(data: Path) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    _mark(data, "changed since")

    result = restore_backup(saved, database_url(data), data / "backups")

    database = data / DATABASE_FILENAME
    assert result.database == database
    assert _marker(database) == "original"
    assert result.revision == migrations.head_revision()
    engine = make_engine(database_url(data))
    try:
        assert migrations.current_revision(engine) == migrations.head_revision()
        assert _diff_against_models(engine) == []
    finally:
        engine.dispose()
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_a_backup_from_an_older_netkeep_is_upgraded_to_head(data: Path) -> None:
    engine = make_engine(database_url(data))
    try:
        migrations.downgrade(engine, "-3")
        older = migrations.current_revision(engine)
    finally:
        engine.dispose()
    assert older is not None and older != migrations.head_revision()
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    _mark(data, "at head again")

    result = restore_backup(saved, database_url(data), data / "backups")

    assert result.source_revision == older
    assert result.revision == migrations.head_revision()
    engine = make_engine(database_url(data))
    try:
        assert _diff_against_models(engine) == []
    finally:
        engine.dispose()


def test_restore_backs_up_the_database_in_place_first(data: Path) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    _mark(data, "work since the backup")
    when = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)

    result = restore_backup(saved, database_url(data), data / "backups", now=when)

    assert result.previous == data / "backups" / backup_name(when, pre_restore=True)
    assert result.previous.name == "netkeep-20261002T090000Z-pre-restore.sqlite3"
    assert _marker(result.previous) == "work since the backup"
    assert _marker(data / DATABASE_FILENAME) == "original"
    # Nothing was pruned: both backups are still there.
    assert [info.path for info in list_backups(data / "backups")] == [result.previous, saved]


def test_restore_into_an_empty_data_directory_backs_nothing_up(data: Path, tmp_path: Path) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    fresh = tmp_path / "fresh"
    fresh.mkdir()

    result = restore_backup(saved, database_url(fresh), fresh / "backups")

    assert result.previous is None
    assert _marker(fresh / DATABASE_FILENAME) == "original"


def test_a_failed_write_leaves_the_database_as_it_was(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    _mark(data, "keep me")
    database = data / DATABASE_FILENAME
    rows_before = _rows(database)

    def half_written(connection: sqlite3.Connection, source: Path) -> None:
        connection.execute("BEGIN")
        connection.execute("DELETE FROM settings_kv")
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(backup_service, "_write_over", half_written)
    with pytest.raises(RestoreError, match="holds what it held before: disk I/O error"):
        restore_backup(saved, database_url(data), data / "backups")

    assert _rows(database) == rows_before
    assert _marker(database) == "keep me"
    with closing(sqlite3.connect(database)) as check:
        assert check.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def _files(directory: Path) -> dict[str, bytes]:
    """Every file beside the database, by name: the database, its -wal and -shm."""
    return {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}


@pytest.mark.parametrize(
    "hold",
    [
        pytest.param(lambda c: c.execute("SELECT 1 FROM users").fetchall(), id="idle"),
        pytest.param(
            lambda c: (c.execute("BEGIN"), c.execute("SELECT * FROM users").fetchall()),
            id="reader",
        ),
        pytest.param(
            lambda c: (
                c.execute("BEGIN IMMEDIATE"),
                c.execute("UPDATE settings_kv SET value = '\"open writer\"'"),
            ),
            id="writer",
        ),
    ],
)
def test_another_open_connection_refuses_the_restore_and_changes_nothing(
    data: Path, hold: Any
) -> None:
    """The review's case: a CLI process, a sqlite3 shell, or a launchd db upgrade can
    have the database open while serve is stopped. Restore must refuse before writing."""
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    _mark(data, "in use")
    database = data / DATABASE_FILENAME
    other = sqlite3.connect(database, isolation_level=None)
    try:
        other.execute("PRAGMA journal_mode=WAL")
        other.execute("PRAGMA wal_autocheckpoint=0")
        other.execute("UPDATE settings_kv SET value = '\"in the wal\"' WHERE key = 'marker'")
        hold(other)
        before = _files(data)
        assert f"{DATABASE_FILENAME}-wal" in before

        with pytest.raises(DatabaseInUse, match="another netkeep process has the database"):
            restore_backup(saved, database_url(data), data / "backups")

        assert _files(data) == before  # byte for byte, the WAL and the shm included
        assert len(list_backups(data / "backups")) == 1  # no pre-restore backup either
    finally:
        other.close()
    assert _marker(database) == "in the wal"


def test_a_symlinked_database_stays_a_symlink(data: Path, tmp_path: Path) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    real = tmp_path / "elsewhere" / "real.sqlite3"
    real.parent.mkdir()
    database = data / DATABASE_FILENAME
    database.replace(real)
    database.symlink_to(real)
    _mark(data, "through the link")

    restore_backup(saved, database_url(data), data / "backups")

    assert database.is_symlink() and database.resolve() == real
    assert _marker(real) == "original"


def test_a_write_ahead_log_left_by_the_old_database_is_not_replayed(data: Path) -> None:
    """A crash can leave the old database's -wal behind. It must never be replayed into
    the restored database."""
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    database = data / DATABASE_FILENAME
    wal = Path(f"{database}-wal")
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("UPDATE settings_kv SET value = '\"in the wal\"' WHERE key = 'marker'")
        stranded = wal.read_bytes()
    finally:
        connection.close()  # checkpoints and removes the WAL...
    wal.write_bytes(stranded)  # ...which a crash would have left behind

    restore_backup(saved, database_url(data), data / "backups")

    assert _marker(database) == "original"
    with closing(sqlite3.connect(database)) as check:
        assert check.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_a_file_that_is_not_a_database_is_refused_before_anything_changes(
    data: Path, tmp_path: Path
) -> None:
    junk = tmp_path / "notes.sqlite3"
    junk.write_bytes(b"not a database at all" * 100)
    before = (data / DATABASE_FILENAME).read_bytes()

    with pytest.raises(RestoreError, match="not a netkeep database"):
        restore_backup(junk, database_url(data), data / "backups")

    assert (data / DATABASE_FILENAME).read_bytes() == before
    assert list_backups(data / "backups") == []  # no backup of the database in place either


def test_a_backup_from_a_newer_netkeep_is_refused(data: Path) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    with closing(sqlite3.connect(saved)) as connection:
        connection.execute("UPDATE alembic_version SET version_num = '9999'")
        connection.commit()

    with pytest.raises(RestoreError, match="migration 9999, which this netkeep does not know"):
        restore_backup(saved, database_url(data), data / "backups")


def test_the_database_itself_is_refused(data: Path) -> None:
    with pytest.raises(RestoreError, match="is the database itself"):
        restore_backup(data / DATABASE_FILENAME, database_url(data), data / "backups")


def test_postgresql_is_refused(data: Path) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    with pytest.raises(RestoreError, match="PostgreSQL is not supported here"):
        restore_backup(saved, "postgresql://u:secret@db.example.test/nk", data / "backups")


def test_find_backup_takes_a_name_or_a_path(data: Path, tmp_path: Path) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    assert find_backup(saved.name, data / "backups") == saved
    elsewhere = tmp_path / "copy.sqlite3"
    shutil.copyfile(saved, elsewhere)
    assert find_backup(str(elsewhere), data / "backups") == elsewhere
    with pytest.raises(RestoreError, match="no such backup"):
        find_backup("netkeep-20200101T000000Z.sqlite3", data / "backups")


# --- netkeep restore --------------------------------------------------------------


def test_restore_command_says_what_it_did(data: Path) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    _mark(data, "changed")

    result = CliRunner().invoke(cli, ["restore", saved.name])

    assert result.exit_code == 0, result.output
    (previous,) = [i.path for i in list_backups(data / "backups") if i.path != saved]
    assert f"backed up the database in place to {previous}" in result.stdout
    assert f"restored {saved}" in result.stdout
    assert f"migration {migrations.head_revision()}" in result.stdout
    assert f"to undo, run: netkeep restore {previous.name}" in result.stdout
    assert _marker(data / DATABASE_FILENAME) == "original"


def _page_auto_send(directory: Path, on: bool | None) -> bool:
    """Set (or, with None, only read) the local user's Settings-page auto-send; the value
    in force over the defaults."""
    engine = make_engine(database_url(directory))
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory, write=True) as session:
            user = ensure_local_user(session)
            if on is not None:
                ui_settings.write(
                    session, user, Settings(), {ui_settings.AUTO_SEND_KEY: on}, confirm_auto_send=on
                )
            return ui_settings.auto_send_on(session, user, Settings())
    finally:
        engine.dispose()


def test_a_restore_turns_a_page_auto_send_from_the_backup_off(data: Path) -> None:
    """#598 review: a backup taken with auto-send on never turns it on again unconfirmed."""
    assert _page_auto_send(data, True) is True
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    assert _page_auto_send(data, False) is False

    result = CliRunner().invoke(cli, ["restore", saved.name])

    assert result.exit_code == 0, result.output
    assert "LinkedIn auto-send was on in the Settings page of the backup" in result.stdout
    assert _page_auto_send(data, None) is False
    assert _marker(data / DATABASE_FILENAME) == "original"  # the rest came back


def _stored_auto_send(database: Path) -> list[Any]:
    """The Settings-page auto-send rows, read with plain SQL: no migration needed."""
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(
            "SELECT user_id, value FROM settings_kv WHERE key = ?",
            (backup_service.AUTO_SEND_ROW_KEY,),
        ).fetchall()


def test_a_failed_upgrade_still_leaves_the_backup_s_auto_send_off(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#639 re-review: the clear lands with the restore, before any migration, so an
    upgrade that fails afterwards cannot leave the backup's page "on" in place."""
    assert _page_auto_send(data, True) is True
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))

    def broken(engine: Engine) -> None:
        raise RuntimeError("migration exploded")

    monkeypatch.setattr(migrations, "upgrade", broken)
    with pytest.raises(RestoreError, match="db upgrade failed"):
        restore_backup(saved, database_url(data), data / "backups")
    assert _stored_auto_send(data / DATABASE_FILENAME) == []
    assert _marker(data / DATABASE_FILENAME) == "original"  # the restore itself landed
    monkeypatch.undo()
    assert _page_auto_send(data, None) is False


def test_a_failed_clear_restores_nothing_and_says_so(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _page_auto_send(data, True) is True
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    _mark(data, "changed")

    def broken(connection: sqlite3.Connection) -> tuple[int, ...]:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(backup_service, "_clear_auto_send", broken)
    with pytest.raises(RestoreError) as refused:
        restore_backup(saved, database_url(data), data / "backups")
    message = str(refused.value)
    assert "could not turn LinkedIn auto-send off" in message
    assert "nothing was restored" in message and "db upgrade" not in message
    assert _marker(data / DATABASE_FILENAME) == "changed"  # what was there before


def test_the_clear_reads_a_backup_at_the_first_migration(tmp_path: Path) -> None:
    """The plain SQL needs only migration 0001's settings_kv."""
    path = tmp_path / "old.sqlite3"
    engine = make_engine(f"sqlite:///{path}")
    try:
        migrations.upgrade(engine, "0001")
    finally:
        engine.dispose()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "INSERT INTO users (id, kind, timezone, created_at)"
            " VALUES (1, 'local', 'UTC', '2026-01-01 00:00:00')"
        )
        connection.execute(
            "INSERT INTO settings_kv (user_id, key, value, created_at, updated_at)"
            " VALUES (1, ?, 'true', '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
            (backup_service.AUTO_SEND_ROW_KEY,),
        )
        connection.commit()
        assert backup_service._clear_auto_send(connection) == (1,)
    assert _stored_auto_send(path) == []


def test_a_restore_with_auto_send_off_says_nothing_about_it(data: Path) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    result = restore_backup(saved, database_url(data), data / "backups")
    assert result.auto_send_cleared == ()


def test_restore_command_refuses_while_serve_runs(data: Path) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    _mark(data, "live")
    held = serve_lock.claim()  # what a running serve holds
    try:
        result = CliRunner().invoke(cli, ["restore", saved.name])
    finally:
        held.release()

    assert result.exit_code == 1
    assert "netkeep serve is running" in result.stderr
    assert _marker(data / DATABASE_FILENAME) == "live"
    assert len(list_backups(data / "backups")) == 1


def test_restore_command_holds_the_serve_lock_while_it_works(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    seen: list[bool] = []
    real = backup_service.restore_backup

    def spy(*args: Any, **kwargs: Any) -> Any:
        with pytest.raises(serve_lock.ServeRunning):
            serve_lock.claim()
        seen.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr("netkeep.cli.restore_backup", spy)
    result = CliRunner().invoke(cli, ["restore", saved.name])

    assert result.exit_code == 0, result.output
    assert seen == [True]
    serve_lock.claim().release()  # and lets go after


def test_restore_command_with_an_unknown_name_fails(data: Path) -> None:
    result = CliRunner().invoke(cli, ["restore", "netkeep-20200101T000000Z.sqlite3"])
    assert result.exit_code == 1
    assert "no such backup" in result.stderr


# --- the serve lock -----------------------------------------------------------------


async def test_serve_holds_the_serve_lock_and_a_second_one_refuses(
    bare_engine: Engine,
    tmp_path: Path,
    _migrated_template: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import conftest

    conftest._copy_template(_migrated_template, tmp_path / "bare")
    monkeypatch.setenv("NETKEEP_FRONTEND_DIST", str(tmp_path / "no-dist"))
    extractor = ServeExtractor(executor=lambda factory, bus: object())  # type: ignore[arg-type,return-value]
    served = create_app(Settings(), engine=bare_engine, extractor=extractor)
    async with served.router.lifespan_context(served):
        with pytest.raises(serve_lock.ServeRunning):
            serve_lock.claim()
        second = create_app(Settings(), engine=bare_engine, extractor=extractor)
        with pytest.raises(RuntimeError, match="netkeep serve cannot start"):
            async with second.router.lifespan_context(second):
                pass
    serve_lock.claim().release()  # released at shutdown


async def test_the_app_without_serve_takes_no_lock(running_app: FastAPI) -> None:
    serve_lock.claim().release()


# --- the nightly backup -------------------------------------------------------------


@pytest.mark.parametrize(
    ("newest", "now", "due"),
    [
        (None, BEFORE_THREE, True),
        # Yesterday 03:10 EDT; it is 02:30 now: last night's backup is still the latest.
        (datetime(2026, 10, 15, 7, 10, tzinfo=UTC), BEFORE_THREE, False),
        (datetime(2026, 10, 15, 7, 10, tzinfo=UTC), AFTER_THREE, True),
        # Two nights back, before 03:00: last night's was missed, so one is due.
        (datetime(2026, 10, 14, 7, 10, tzinfo=UTC), BEFORE_THREE, True),
        # Made by hand at 03:05 today: that counts as tonight's.
        (datetime(2026, 10, 16, 7, 5, tzinfo=UTC), AFTER_THREE, False),
        # Made at 02:55 today, before the mark: one is still due after 03:00.
        (datetime(2026, 10, 16, 6, 55, tzinfo=UTC), AFTER_THREE, True),
    ],
)
def test_nightly_due(newest: datetime | None, now: datetime, due: bool) -> None:
    assert nightly_due(newest, now, NY) is due


def test_nightly_due_reads_the_hour_in_the_timezone() -> None:
    # 03:30 UTC is 23:30 the evening before in New York: not yet that night's.
    newest = datetime(2026, 10, 15, 7, 10, tzinfo=UTC)
    now = datetime(2026, 10, 16, 3, 30, tzinfo=UTC)
    assert nightly_due(newest, now, NY) is False
    assert nightly_due(newest, now, "UTC") is True
    assert nightly_due(newest, now, "Not/AZone") is True  # read as UTC


def test_the_nightly_hour_is_three() -> None:
    assert backup_service.NIGHTLY_HOUR == 3


def _job(data: Path, now: datetime, base: Settings | None = None) -> NightlyBackups:
    engine = make_engine(database_url(data))
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    return NightlyBackups(engine, factory, base or Settings(), data / "backups", clock=lambda: now)


def test_the_nightly_job_backs_up_when_due_and_prunes_to_keep(data: Path) -> None:
    old = [datetime(2026, 10, d, 7, 10, tzinfo=UTC) for d in (10, 11, 12)]
    for stamp in old:
        _backup_now(data, stamp)
    job = _job(data, AFTER_THREE, Settings(backup=BackupSettings(keep=2)))

    written = job.back_up_if_due()

    assert written == data / "backups" / backup_name(AFTER_THREE)
    assert _marker(written) == "original"
    assert [i.created_at for i in list_backups(data / "backups")] == [AFTER_THREE, old[2]]
    assert job.back_up_if_due() is None  # once a night


def test_the_nightly_job_waits_for_the_night(data: Path) -> None:
    _backup_now(data, datetime(2026, 10, 15, 7, 10, tzinfo=UTC))
    assert _job(data, BEFORE_THREE).back_up_if_due() is None


def test_the_nightly_switch_turns_it_off(data: Path) -> None:
    job = _job(data, AFTER_THREE, Settings(backup=BackupSettings(nightly=False)))
    assert job.back_up_if_due() is None
    assert list_backups(data / "backups") == []


def test_the_nightly_job_reads_the_settings_page_each_check(data: Path) -> None:
    job = _job(data, AFTER_THREE)
    with session_scope(job._factory, write=True) as session:
        user = ensure_local_user(session)
        ui_settings.write(session, user, Settings(), {"backup.nightly": False})
    assert job.back_up_if_due() is None
    with session_scope(job._factory, write=True) as session:
        user = ensure_local_user(session)
        ui_settings.write(session, user, Settings(), {"backup.nightly": None})
    assert job.back_up_if_due() is not None


def test_the_nightly_job_runs_only_on_a_sqlite_file(tmp_path: Path) -> None:
    assert backs_up(make_engine(database_url(tmp_path)))
    assert not backs_up(make_engine("sqlite://"))


def test_the_nightly_default_is_on_and_keeps_fourteen() -> None:
    """The switch's default is what the job does with no setting at all."""
    assert BackupSettings() == BackupSettings(nightly=True, keep=14)
    nightly = ui_settings.BY_KEY["backup.nightly"]
    assert "Nothing reads it" not in nightly.applies_note
    assert "not scheduled" not in nightly.help


def test_the_llm_switch_is_not_on_the_settings_page() -> None:
    assert "llm.enabled" not in ui_settings.BY_KEY
    assert not any(spec.key.startswith("llm.") for spec in ui_settings.FIELDS)


async def test_serve_starts_and_stops_the_nightly_job(
    bare_engine: Engine,
    tmp_path: Path,
    _migrated_template: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import conftest

    conftest._copy_template(_migrated_template, tmp_path / "bare")
    monkeypatch.setenv("NETKEEP_FRONTEND_DIST", str(tmp_path / "no-dist"))
    started: list[NightlyBackups] = []
    real_start = NightlyBackups.start

    def spy(self: NightlyBackups) -> None:
        started.append(self)
        real_start(self)

    monkeypatch.setattr(NightlyBackups, "start", spy)
    extractor = ServeExtractor(executor=lambda factory, bus: object())  # type: ignore[arg-type,return-value]
    served = create_app(Settings(), engine=bare_engine, extractor=extractor)
    async with served.router.lifespan_context(served):
        (job,) = started
        assert job._task is not None and not job._task.done()
        assert job.backups_dir == tmp_path / "netkeep-data" / "backups"
    assert job._task is None


# --- /backups -----------------------------------------------------------------------


async def test_get_backups_lists_them_with_the_restore_command(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    empty = (await client.get("/api/v1/backups")).json()
    assert empty == {
        "directory": str(tmp_path / "netkeep-data" / "backups"),
        "available": True,
        "backups": [],
    }
    made = await client.post("/api/v1/backups", headers=CSRF)
    assert made.status_code == 201, made.text
    (listed,) = made.json()["backups"]
    data_dir = tmp_path / "netkeep-data"
    assert listed["restore_command"] == (
        f"NETKEEP_DATA={data_dir} netkeep restore {data_dir / 'backups' / listed['name']}"
    )
    assert listed["pre_restore"] is False
    assert listed["size"] > 0
    assert (await client.get("/api/v1/backups")).json()["backups"] == [listed]


async def test_back_up_now_prunes_to_the_keep_in_force(
    client: httpx.AsyncClient, running_app: FastAPI, tmp_path: Path
) -> None:
    directory = tmp_path / "netkeep-data" / "backups"
    directory.mkdir(parents=True)
    for day in (1, 2, 3):
        (directory / backup_name(datetime(2026, 1, day, tzinfo=UTC))).write_bytes(b"x")
    saved = await client.put(
        "/api/v1/settings/config", json={"values": {"backup.keep": 2}}, headers=CSRF
    )
    assert saved.status_code == 200, saved.text

    made = await client.post("/api/v1/backups", headers=CSRF)

    assert made.status_code == 201, made.text
    names = [b["name"] for b in made.json()["backups"]]
    assert len(names) == 2
    assert names[1] == backup_name(datetime(2026, 1, 3, tzinfo=UTC))


async def test_back_up_now_in_the_same_second_answers_409(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    real = backup_service.create_backup
    monkeypatch.setattr(
        "netkeep.web.api.backups.create_backup",
        lambda engine, directory: real(engine, directory, now=fixed),
    )
    assert (await client.post("/api/v1/backups", headers=CSRF)).status_code == 201
    again = await client.post("/api/v1/backups", headers=CSRF)
    assert again.status_code == 409
    assert "already exists" in again.json()["detail"]


def test_serve_command_refuses_cleanly_while_the_lock_is_held(data: Path) -> None:
    held = serve_lock.claim()
    try:
        result = CliRunner().invoke(cli, ["serve", "--port", "1"])
    finally:
        held.release()
    assert result.exit_code == 1
    assert "netkeep serve is already running" in result.stderr


def test_the_restore_command_names_the_data_directory_only_when_one_was_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = Path("/Volumes/My Disk/backups/netkeep-20261016T031000Z.sqlite3")
    monkeypatch.delenv("NETKEEP_DATA", raising=False)
    assert restore_command(backup) == (
        "netkeep restore '/Volumes/My Disk/backups/netkeep-20261016T031000Z.sqlite3'"
    )
    monkeypatch.setenv("NETKEEP_DATA", "/Volumes/My Disk")
    assert restore_command(backup).startswith("NETKEEP_DATA='/Volumes/My Disk' netkeep ")


def test_a_corrupt_backup_is_refused_by_the_integrity_check(data: Path) -> None:
    """Valid SQLite that SQLite's integrity check fails: two indexes with their root
    pages swapped, so every row is missing from each."""
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    with closing(sqlite3.connect(saved, isolation_level=None)) as connection:
        connection.execute("CREATE TABLE probe(a, b)")
        connection.execute("CREATE INDEX probe_a ON probe(a)")
        connection.execute("CREATE INDEX probe_b ON probe(b)")
        connection.executemany(
            "INSERT INTO probe VALUES (?, ?)", [(i, 1000 - i) for i in range(200)]
        )
        pages = dict(
            connection.execute(
                "SELECT name, rootpage FROM sqlite_master WHERE name IN ('probe_a', 'probe_b')"
            ).fetchall()
        )
        connection.execute("PRAGMA writable_schema=ON")
        for name, other in (("probe_a", "probe_b"), ("probe_b", "probe_a")):
            connection.execute(
                "UPDATE sqlite_master SET rootpage = ? WHERE name = ?", (pages[other], name)
            )
    before = (data / DATABASE_FILENAME).read_bytes()

    with pytest.raises(RestoreError, match="failed SQLite's integrity check"):
        restore_backup(saved, database_url(data), data / "backups")

    assert (data / DATABASE_FILENAME).read_bytes() == before
    assert len(list_backups(data / "backups")) == 1


# --- pre-restore backups and retention ----------------------------------------------


def _seed(directory: Path, names: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"x")


def _names(directory: Path) -> list[str]:
    return [info.path.name for info in list_backups(directory)]


def test_the_newest_pre_restore_backup_is_neither_pruned_nor_counted(tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    undo = backup_name(datetime(2026, 10, 10, tzinfo=UTC), pre_restore=True)
    nightly = [backup_name(datetime(2026, 10, d, 7, tzinfo=UTC)) for d in (11, 12, 13)]
    _seed(directory, [undo, *nightly])

    removed = prune_backups(directory, 1, now=datetime(2026, 10, 13, 8, tzinfo=UTC))

    assert [p.name for p in removed] == [nightly[0], nightly[1]]
    assert _names(directory) == [nightly[2], undo]


def test_the_undo_is_pruned_after_thirty_days(tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    undo = backup_name(datetime(2026, 9, 1, tzinfo=UTC), pre_restore=True)
    nightly = backup_name(datetime(2026, 10, 13, 7, tzinfo=UTC))
    _seed(directory, [undo, nightly])
    prune_backups(directory, 1, now=datetime(2026, 9, 30, 23, tzinfo=UTC))
    assert _names(directory) == [nightly, undo]  # 29 days: kept
    prune_backups(directory, 1, now=datetime(2026, 10, 1, 1, tzinfo=UTC))
    assert _names(directory) == [nightly]


def test_a_later_restore_ends_the_older_undos_exemption(tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    older = backup_name(datetime(2026, 10, 10, tzinfo=UTC), pre_restore=True)
    newer = backup_name(datetime(2026, 10, 12, tzinfo=UTC), pre_restore=True)
    nightly = backup_name(datetime(2026, 10, 13, 7, tzinfo=UTC))
    _seed(directory, [older, newer, nightly])

    prune_backups(directory, 1, now=datetime(2026, 10, 13, 8, tzinfo=UTC))

    assert _names(directory) == [nightly, newer]


def test_a_pre_restore_backup_does_not_count_as_the_nightly(data: Path) -> None:
    _backup_now(data, datetime(2026, 10, 15, 7, 10, tzinfo=UTC))  # last night's
    (data / "backups" / backup_name(AFTER_THREE, pre_restore=True)).write_bytes(b"x")
    written = _job(data, AFTER_THREE.replace(minute=40)).back_up_if_due()
    assert written is not None and not written.name.endswith("-pre-restore.sqlite3")


def test_db_upgrade_refuses_while_the_serve_lock_is_held(data: Path) -> None:
    held = serve_lock.claim()  # a running serve, or a restore in progress
    try:
        result = CliRunner().invoke(cli, ["db", "upgrade"])
    finally:
        held.release()
    assert result.exit_code == 1
    assert "netkeep serve or netkeep restore is running" in result.stderr
    assert CliRunner().invoke(cli, ["db", "upgrade"]).exit_code == 0


# --- a backup interrupted mid-write -------------------------------------------------


def _abort_after(connection: sqlite3.Connection, steps: int) -> sqlite3.Connection:
    """Make ``connection``'s next long statement fail partway, as a kill would stop it."""
    calls = iter(range(steps))
    connection.set_progress_handler(lambda: 0 if next(calls, None) is not None else 1, 100)
    return connection


def _backup_like_files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.startswith("netkeep-"))


def test_a_backup_failing_mid_write_leaves_no_backup_file(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = sqlite3.connect
    started: list[sqlite3.Connection] = []

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection: sqlite3.Connection = real_connect(*args, **kwargs)
        if kwargs.get("uri"):  # create_backup's read-only source connection
            started.append(_abort_after(connection, 20))
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(BackupError, match="interrupted"):
        create_backup(database_url(data), data / "backups", now=STAMP_10_02)

    assert started
    assert _backup_like_files(data / "backups") == []
    assert list(data.joinpath("backups").iterdir()) == []  # the partial went too
    assert list_backups(data / "backups") == []


def test_a_pre_restore_backup_failing_mid_write_leaves_no_undo(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    _mark(data, "keep me")
    real_lock = backup_service._lock_exclusively
    monkeypatch.setattr(
        backup_service, "_lock_exclusively", lambda path: _abort_after(real_lock(path), 20)
    )

    with pytest.raises(RestoreError, match="holds what it held before: interrupted"):
        restore_backup(saved, database_url(data), data / "backups", now=STAMP_10_02)

    assert _backup_like_files(data / "backups") == [saved.name]
    assert [info.path for info in list_backups(data / "backups")] == [saved]
    assert _marker(data / DATABASE_FILENAME) == "keep me"


def test_a_partial_left_by_a_kill_is_never_listed_and_is_cleaned_up_after_an_hour(
    data: Path,
) -> None:
    """What a SIGKILL mid-VACUUM leaves (the review's crash2.py): the temporary file
    and its journal. Settings never lists them, and pruning removes them once stale."""
    directory = data / "backups"
    directory.mkdir()
    old = backup_service.partial_path(directory / backup_name(STAMP_10_02, pre_restore=True))
    fresh = backup_service.partial_path(directory / backup_name(AFTER_THREE))
    stray_journal = directory / f"{backup_name(STAMP_10_02)}-journal"
    for path in (old, Path(f"{old}-journal"), fresh, stray_journal):
        path.write_bytes(b"half a database")
    hour_ago = (AFTER_THREE.timestamp() - 3601,) * 2
    for path in (old, Path(f"{old}-journal"), stray_journal):
        os.utime(path, hour_ago)
    os.utime(fresh, (AFTER_THREE.timestamp(),) * 2)

    assert list_backups(directory) == []
    assert _backup_like_files(directory) == [stray_journal.name]  # never a .sqlite3

    prune_backups(directory, 1, now=AFTER_THREE)

    assert sorted(p.name for p in directory.iterdir()) == [fresh.name]


async def test_back_up_now_failing_mid_write_lists_nothing_on_settings(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_connect = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection: sqlite3.Connection = real_connect(*args, **kwargs)
        return _abort_after(connection, 20) if kwargs.get("uri") else connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    failed = await client.post("/api/v1/backups", headers=CSRF)
    monkeypatch.setattr(sqlite3, "connect", real_connect)

    assert failed.status_code == 409
    assert (await client.get("/api/v1/backups")).json()["backups"] == []
    directory = tmp_path / "netkeep-data" / "backups"
    assert not directory.exists() or _backup_like_files(directory) == []


@pytest.mark.parametrize("pre_restore", [False, True], ids=["backup", "pre-restore"])
def test_a_backup_killed_mid_write_leaves_only_a_hidden_partial(
    data: Path, monkeypatch: pytest.MonkeyPatch, pre_restore: bool
) -> None:
    """A kill runs no cleanup: stand in for one by failing mid-VACUUM with cleanup off.
    What is left must not be named, listed or kept like a backup."""
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    monkeypatch.setattr(backup_service, "_unlink_with_journal", lambda path: None)
    if pre_restore:
        real_lock = backup_service._lock_exclusively
        monkeypatch.setattr(
            backup_service, "_lock_exclusively", lambda path: _abort_after(real_lock(path), 20)
        )
        with pytest.raises(RestoreError):
            restore_backup(saved, database_url(data), data / "backups", now=STAMP_10_02)
    else:
        real_connect = sqlite3.connect

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            connection: sqlite3.Connection = real_connect(*args, **kwargs)
            return _abort_after(connection, 20) if kwargs.get("uri") else connection

        monkeypatch.setattr(sqlite3, "connect", connect)
        with pytest.raises(BackupError):
            create_backup(database_url(data), data / "backups", now=STAMP_10_02)
        monkeypatch.setattr(sqlite3, "connect", real_connect)

    directory = data / "backups"
    assert _backup_like_files(directory) == [saved.name]
    (left,) = [p for p in directory.iterdir() if p.name.endswith(".partial")]
    assert left.name.startswith(".netkeep-20261002T090000Z")
    assert [info.path for info in list_backups(directory)] == [saved]
