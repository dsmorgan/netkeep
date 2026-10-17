"""P6-04: ``netkeeper restore``, the nightly backup in ``serve``, the serve lock, and
``/backups`` for the Settings page."""

from __future__ import annotations

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

from netkeeper import migrations
from netkeeper.cli import app as cli
from netkeeper.config import BackupSettings, Settings
from netkeeper.db import (
    DATABASE_FILENAME,
    database_url,
    make_engine,
    make_session_factory,
    session_scope,
)
from netkeeper.models import Base, SettingKV
from netkeeper.scoping import install_scope_guard
from netkeeper.services import backup as backup_service
from netkeeper.services import serve_lock, ui_settings
from netkeeper.services.backup import (
    RestoreError,
    backup_name,
    create_backup,
    find_backup,
    list_backups,
    nightly_due,
    restore_backup,
)
from netkeeper.services.backup_job import NightlyBackups, backs_up
from netkeeper.services.scheduled_runs import ServeExtractor
from netkeeper.services.users import ensure_local_user
from netkeeper.web.app import create_app

CSRF = {"X-Netkeeper-Client": "1"}
NY = "America/New_York"
# 2026-10-16 02:30 in New York (EDT, UTC-4): before that night's 03:00.
BEFORE_THREE = datetime(2026, 10, 16, 6, 30, tzinfo=UTC)
AFTER_THREE = datetime(2026, 10, 16, 7, 30, tzinfo=UTC)  # 03:30 EDT


# --- fixtures and helpers -----------------------------------------------------------


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """NETKEEPER_DATA (the CLI's data directory) holding a migrated database with the
    local user and one marker row."""
    directory = tmp_path / "data"
    monkeypatch.setenv("NETKEEPER_DATA", str(directory))
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


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if ".restore-" in p.name)


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


def test_a_backup_from_an_older_netkeeper_is_upgraded_to_head(data: Path) -> None:
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

    assert result.previous == data / "backups" / backup_name(when)
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


def test_a_failed_copy_leaves_the_database_as_it_was(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    _mark(data, "keep me")
    database = data / DATABASE_FILENAME
    before = database.read_bytes()

    def half_written(source: Path, destination: Path) -> None:
        destination.write_bytes(source.read_bytes()[:4096])
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(backup_service, "_copy_database", half_written)
    with pytest.raises(RestoreError, match="is unchanged: disk I/O error"):
        restore_backup(saved, database_url(data), data / "backups")

    assert database.read_bytes() == before
    assert _marker(database) == "keep me"
    assert _leftovers(data) == []


def test_a_failed_rename_leaves_the_database_as_it_was(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    _mark(data, "keep me")

    def refuse(self: Path, target: Path) -> Path:
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(RestoreError, match="read-only file system"):
        restore_backup(saved, database_url(data), data / "backups")

    assert _marker(data / DATABASE_FILENAME) == "keep me"
    assert _leftovers(data) == []


def test_a_write_ahead_log_left_by_the_old_database_is_not_replayed(data: Path) -> None:
    """A crash can leave the old database's -wal behind. Renamed over, the restored file
    would pick it up on next open and replay the old database's pages into it."""
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

    with pytest.raises(RestoreError, match="not a netkeeper database"):
        restore_backup(junk, database_url(data), data / "backups")

    assert (data / DATABASE_FILENAME).read_bytes() == before
    assert list_backups(data / "backups") == []  # no backup of the database in place either


def test_a_backup_from_a_newer_netkeeper_is_refused(data: Path) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    with closing(sqlite3.connect(saved)) as connection:
        connection.execute("UPDATE alembic_version SET version_num = '9999'")
        connection.commit()

    with pytest.raises(RestoreError, match="migration 9999, which this netkeeper does not know"):
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
        find_backup("netkeeper-20200101T000000Z.sqlite3", data / "backups")


# --- netkeeper restore --------------------------------------------------------------


def test_restore_command_says_what_it_did(data: Path) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    _mark(data, "changed")

    result = CliRunner().invoke(cli, ["restore", saved.name])

    assert result.exit_code == 0, result.output
    (previous,) = [i.path for i in list_backups(data / "backups") if i.path != saved]
    assert f"backed up the database in place to {previous}" in result.stdout
    assert f"restored {saved}" in result.stdout
    assert f"migration {migrations.head_revision()}" in result.stdout
    assert f"to undo, run: netkeeper restore {previous.name}" in result.stdout
    assert _marker(data / DATABASE_FILENAME) == "original"


def test_restore_command_refuses_while_serve_runs(data: Path) -> None:
    saved = _backup_now(data, datetime(2026, 10, 1, tzinfo=UTC))
    _mark(data, "live")
    held = serve_lock.claim()  # what a running serve holds
    try:
        result = CliRunner().invoke(cli, ["restore", saved.name])
    finally:
        held.release()

    assert result.exit_code == 1
    assert "netkeeper serve is running" in result.stderr
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

    monkeypatch.setattr("netkeeper.cli.restore_backup", spy)
    result = CliRunner().invoke(cli, ["restore", saved.name])

    assert result.exit_code == 0, result.output
    assert seen == [True]
    serve_lock.claim().release()  # and lets go after


def test_restore_command_with_an_unknown_name_fails(data: Path) -> None:
    result = CliRunner().invoke(cli, ["restore", "netkeeper-20200101T000000Z.sqlite3"])
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
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", str(tmp_path / "no-dist"))
    extractor = ServeExtractor(executor=lambda factory, bus: object())  # type: ignore[arg-type,return-value]
    served = create_app(Settings(), engine=bare_engine, extractor=extractor)
    async with served.router.lifespan_context(served):
        with pytest.raises(serve_lock.ServeRunning):
            serve_lock.claim()
        second = create_app(Settings(), engine=bare_engine, extractor=extractor)
        with pytest.raises(RuntimeError, match="netkeeper serve cannot start"):
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
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", str(tmp_path / "no-dist"))
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
        assert job.backups_dir == tmp_path / "netkeeper-data" / "backups"
    assert job._task is None


# --- /backups -----------------------------------------------------------------------


async def test_get_backups_lists_them_with_the_restore_command(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    empty = (await client.get("/api/v1/backups")).json()
    assert empty == {
        "directory": str(tmp_path / "netkeeper-data" / "backups"),
        "available": True,
        "backups": [],
    }
    made = await client.post("/api/v1/backups", headers=CSRF)
    assert made.status_code == 201, made.text
    (listed,) = made.json()["backups"]
    assert listed["restore_command"] == f"netkeeper restore {listed['name']}"
    assert listed["size"] > 0
    assert (await client.get("/api/v1/backups")).json()["backups"] == [listed]


async def test_back_up_now_prunes_to_the_keep_in_force(
    client: httpx.AsyncClient, running_app: FastAPI, tmp_path: Path
) -> None:
    directory = tmp_path / "netkeeper-data" / "backups"
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
        "netkeeper.web.api.backups.create_backup",
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
    assert "netkeeper serve is already running" in result.stderr
