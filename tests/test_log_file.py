"""The rotating log file, ``<data>/logs/netkeeper.log`` (#602).

Every test here writes under the per-test ``NETKEEPER_DATA`` the conftest sets, and the
conftest closes the file after each one.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.cli import app as cli
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.logging_setup import (
    FILE_HANDLER_NAME,
    PERSONAL_CLOSE,
    PERSONAL_OPEN,
    PERSONAL_PATTERN,
    RedactQueryFilter,
    RotatingLogFile,
    log_file,
    personal,
    set_log_rotation,
    setup_logging,
)
from netkeeper.scoping import install_scope_guard
from netkeeper.services.users import ensure_local_user

CSRF = {"X-Netkeeper-Client": "1"}
FIXTURES_ARCHIVE = Path(__file__).parent / "fixtures" / "archive"
_LINE = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (DEBUG|INFO|WARNING|ERROR|CRITICAL) \S+ .*")


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    yield
    root.setLevel(level)
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)


def _handler() -> RotatingLogFile:
    found = [h for h in logging.getLogger().handlers if h.get_name() == FILE_HANDLER_NAME]
    assert len(found) == 1
    assert isinstance(found[0], RotatingLogFile)
    return found[0]


def _flush() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_the_log_file_is_in_the_data_directory_and_made_on_the_first_record(
    tmp_path: Path,
) -> None:
    path = tmp_path / "netkeeper-data" / "logs" / "netkeeper.log"
    assert log_file() == path
    setup_logging("INFO")
    assert not path.parent.exists()  # nothing logged yet, nothing created
    logging.getLogger("netkeeper.example").info("hello %s", "world")
    _flush()
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    assert _LINE.fullmatch(lines[0])
    assert lines[0].endswith(" INFO netkeeper.example hello world")
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_personal_value_is_plain_on_stderr_and_marked_in_the_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging("INFO")
    log = logging.getLogger("netkeeper.example")
    log.info("created list %d %r for %s", 4, personal("Quorbelle's friends"), personal("q@x.test"))
    _flush()
    err = capsys.readouterr().err
    assert err.endswith("""created list 4 "Quorbelle's friends" for q@x.test\n""")
    text = log_file().read_text()
    assert f"""created list 4 {PERSONAL_OPEN}"Quorbelle's friends"{PERSONAL_CLOSE} for""" in text
    assert f"{PERSONAL_OPEN}q@x.test{PERSONAL_CLOSE}" in text
    redacted = PERSONAL_PATTERN.sub("<personal>", text)
    assert "Quorbelle" not in redacted
    assert "q@x.test" not in redacted
    assert redacted.rstrip().endswith("created list 4 <personal> for <personal>")


def test_a_marker_inside_a_personal_value_cannot_end_its_span_early() -> None:
    setup_logging("INFO")
    logging.getLogger("netkeeper.example").info("name %s", personal(f"Quor{PERSONAL_CLOSE}belle"))
    _flush()
    redacted = PERSONAL_PATTERN.sub("<personal>", log_file().read_text())
    assert "belle" not in redacted
    assert redacted.rstrip().endswith("name <personal>")


def test_an_access_log_query_string_is_personal_in_the_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging("INFO")
    access = logging.getLogger("uvicorn.access")
    access.info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:5",
        "GET",
        "/api/v1/contacts?q=Quorbelle",
        "1.1",
        200,
    )
    _flush()
    assert "/api/v1/contacts?q=Quorbelle HTTP/1.1" in capsys.readouterr().err
    text = log_file().read_text()
    assert f"/api/v1/contacts?{PERSONAL_OPEN}q=Quorbelle{PERSONAL_CLOSE} HTTP/1.1" in text
    assert any(isinstance(f, RedactQueryFilter) for f in access.filters)


def test_the_file_rotates_by_size_and_keeps_the_count_set() -> None:
    setup_logging("INFO")
    set_log_rotation(max_mb=1, keep=2)
    log = logging.getLogger("netkeeper.example")
    line = "x" * 1000
    for _ in range(4 * 1024):  # about 4 MB: past three rotations at 1 MB
        log.info("%s", line)
    _flush()
    path = log_file()
    files = sorted(p.name for p in path.parent.iterdir())
    assert files == ["netkeeper.log", "netkeeper.log.1", "netkeeper.log.2"]
    for name in files:
        assert (path.parent / name).stat().st_size <= 1024 * 1024


def test_rotation_limits_below_one_are_ignored(caplog: pytest.LogCaptureFixture) -> None:
    setup_logging("INFO")
    set_log_rotation(max_mb=3, keep=4)
    with caplog.at_level(logging.WARNING, logger="netkeeper.logging_setup"):
        set_log_rotation(max_mb=0, keep=4)
    handler = _handler()
    assert (handler.maxBytes, handler.backupCount) == (3 * 1024 * 1024, 4)
    assert "ignoring log rotation" in caplog.text


def test_a_file_rotated_by_another_process_is_reopened() -> None:
    """``serve`` and a command share the file: after one rotates it, the other writes
    the new file, not on into the rotated one."""
    setup_logging("INFO")
    log = logging.getLogger("netkeeper.example")
    log.info("before")
    _flush()
    path = log_file()
    path.rename(path.with_name("netkeeper.log.1"))  # what the other process's rollover does
    log.info("after")
    _flush()
    assert path.read_text().rstrip().endswith("after")
    assert "after" not in path.with_name("netkeeper.log.1").read_text()


def test_the_stderr_level_can_be_raised_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The LaunchAgent sets it: serve.err.log keeps warnings; the file has everything."""
    monkeypatch.setenv("NETKEEPER_LOG_STDERR_LEVEL", "WARNING")
    setup_logging("INFO")
    log = logging.getLogger("netkeeper.example")
    log.info("routine")
    log.warning("worth a look")
    _flush()
    err = capsys.readouterr().err
    assert "routine" not in err
    assert "worth a look" in err
    text = log_file().read_text()
    assert "routine" in text and "worth a look" in text


def test_the_file_follows_the_data_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup_logging("INFO")
    set_log_rotation(max_mb=2, keep=3)
    first = log_file()
    monkeypatch.setenv("NETKEEPER_DATA", str(tmp_path / "other"))
    setup_logging("INFO")
    logging.getLogger("netkeeper.example").info("moved")
    _flush()
    assert not first.exists()
    assert "moved" in (tmp_path / "other" / "logs" / "netkeeper.log").read_text()
    handler = _handler()
    assert (handler.maxBytes, handler.backupCount) == (2 * 1024 * 1024, 3)  # kept


def test_an_unwritable_log_file_says_so_once_and_logging_goes_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the data directory would be")
    monkeypatch.setenv("NETKEEPER_DATA", str(blocker))
    setup_logging("INFO")
    log = logging.getLogger("netkeeper.example")
    log.info("one")
    log.info("two")
    err = capsys.readouterr().err
    assert err.count("cannot write the log file") == 1
    assert "Logging error" not in err
    assert "one" in err and "two" in err


# --- the CLI, serve, and the Settings page -------------------------------------------


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        ensure_local_user(session)
    yield factory
    engine.dispose()


def _fixture_bodies() -> list[str]:
    text = (FIXTURES_ARCHIVE / "messages.csv").read_text(encoding="utf-8")
    return [row["CONTENT"] for row in csv.DictReader(io.StringIO(text)) if row["CONTENT"]]


def test_a_cli_import_logs_to_the_file_and_never_a_message_body(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#517 for the file: the archive import reads every message body in the sample, at
    DEBUG, and none reaches the log; the archive's file name is marked personal."""
    monkeypatch.setenv("NETKEEPER_LOG_LEVEL", "DEBUG")
    result = CliRunner().invoke(cli, ["import", "archive", str(FIXTURES_ARCHIVE)])
    assert result.exit_code == 0, result.output
    _flush()
    text = log_file().read_text()
    assert "imported for user 1 as import run" in text
    assert f"archive {PERSONAL_OPEN}archive{PERSONAL_CLOSE} imported" in text
    bodies = _fixture_bodies()
    assert len(bodies) >= 10
    for body in bodies:
        for line in body.splitlines():
            if len(line) > 12:  # short lines ("Thanks!") could be in any sentence
                assert line not in text
    for line in text.splitlines():
        # A traceback's lines continue a record; there is none here.
        assert _LINE.fullmatch(line), line


def test_config_show_reports_the_log_file(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["config", "show"])
    assert result.exit_code == 0, result.output
    expected = tmp_path / "netkeeper-data" / "logs" / "netkeeper.log"
    assert f'log_file = "{expected}"' in result.output


def test_a_command_applies_the_logs_settings_from_config_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[logs]\nmax_mb = 3\nkeep = 7\n")
    result = CliRunner().invoke(cli, ["--config", str(config), "config", "show"])
    assert result.exit_code == 0, result.output
    assert "[logs]\nmax_mb = 3\nkeep = 7\n" in result.output
    handler = _handler()
    assert (handler.maxBytes, handler.backupCount) == (3 * 1024 * 1024, 7)


async def test_the_settings_page_reports_the_log_file_and_applies_new_limits(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    setup_logging("INFO")
    shown = await client.get("/api/v1/settings/config")
    assert shown.status_code == 200
    body = shown.json()
    assert body["log_file"] == str(tmp_path / "netkeeper-data" / "logs" / "netkeeper.log")
    fields = {f["key"]: f for f in body["fields"]}
    assert fields["logs.max_mb"]["value"] == 10
    assert fields["logs.keep"]["value"] == 5
    assert fields["logs.max_mb"]["group"] == "logs"
    saved = await client.put(
        "/api/v1/settings/config", json={"values": {"logs.max_mb": 2, "logs.keep": 9}}, headers=CSRF
    )
    assert saved.status_code == 200, saved.text
    handler = _handler()
    assert (handler.maxBytes, handler.backupCount) == (2 * 1024 * 1024, 9)
    refused = await client.put(
        "/api/v1/settings/config", json={"values": {"logs.keep": 0}}, headers=CSRF
    )
    assert refused.status_code == 422
