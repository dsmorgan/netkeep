"""The rotating log file, ``<data>/logs/netkeep.log`` (#602).

Every test here writes under the per-test ``NETKEEP_DATA`` the conftest sets, and the
conftest closes the file after each one.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeep import migrations
from netkeep.campaigns.templates import RemovedFieldCampaign, log_removed_field_campaigns
from netkeep.cli import app as cli
from netkeep.db import database_url, make_engine, make_session_factory, session_scope
from netkeep.linkedin.voyager import RouteChanged, _resolve_included
from netkeep.logging_setup import (
    FILE_HANDLER_NAME,
    PERSONAL_CLOSE,
    PERSONAL_OPEN,
    PERSONAL_PATTERN,
    RedactQueryFilter,
    RotatingLogFile,
    log_file,
    personal,
    remove_file_handler,
    set_log_rotation,
    setup_logging,
)
from netkeep.models import CampaignStatus
from netkeep.scoping import install_scope_guard
from netkeep.services.users import ensure_local_user

CSRF = {"X-Netkeep-Client": "1"}
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
    path = tmp_path / "netkeep-data" / "logs" / "netkeep.log"
    assert log_file() == path
    setup_logging("INFO")
    assert not path.parent.exists()  # nothing logged yet, nothing created
    logging.getLogger("netkeep.example").info("hello %s", "world")
    _flush()
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    assert _LINE.fullmatch(lines[0])
    assert lines[0].endswith(" INFO netkeep.example hello world")
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_personal_value_is_plain_on_stderr_and_marked_in_the_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging("INFO")
    log = logging.getLogger("netkeep.example")
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
    logging.getLogger("netkeep.example").info("name %s", personal(f"Quor{PERSONAL_CLOSE}belle"))
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
    log = logging.getLogger("netkeep.example")
    line = "x" * 1000
    for _ in range(4 * 1024):  # about 4 MB: past three rotations at 1 MB
        log.info("%s", line)
    _flush()
    path = log_file()
    files = sorted(p.name for p in path.parent.iterdir() if p.name.startswith("netkeep"))
    assert files == ["netkeep.log", "netkeep.log.1", "netkeep.log.2"]
    for name in files:
        assert (path.parent / name).stat().st_size <= 1024 * 1024


def test_rotation_limits_below_one_are_ignored(caplog: pytest.LogCaptureFixture) -> None:
    setup_logging("INFO")
    set_log_rotation(max_mb=3, keep=4)
    with caplog.at_level(logging.WARNING, logger="netkeep.logging_setup"):
        set_log_rotation(max_mb=0, keep=4)
    handler = _handler()
    assert (handler.maxBytes, handler.backupCount) == (3 * 1024 * 1024, 4)
    assert "ignoring log rotation" in caplog.text


def test_a_file_rotated_by_another_process_is_reopened() -> None:
    """``serve`` and a command share the file: after one rotates it, the other writes
    the new file, not on into the rotated one."""
    setup_logging("INFO")
    log = logging.getLogger("netkeep.example")
    log.info("before")
    _flush()
    path = log_file()
    path.rename(path.with_name("netkeep.log.1"))  # what the other process's rollover does
    log.info("after")
    _flush()
    assert path.read_text().rstrip().endswith("after")
    assert "after" not in path.with_name("netkeep.log.1").read_text()


def test_the_stderr_level_can_be_raised_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The LaunchAgent sets it: serve.err.log keeps warnings; the file has everything."""
    monkeypatch.setenv("NETKEEP_LOG_STDERR_LEVEL", "WARNING")
    setup_logging("INFO")
    log = logging.getLogger("netkeep.example")
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
    monkeypatch.setenv("NETKEEP_DATA", str(tmp_path / "other"))
    setup_logging("INFO")
    logging.getLogger("netkeep.example").info("moved")
    _flush()
    assert not first.exists()
    assert "moved" in (tmp_path / "other" / "logs" / "netkeep.log").read_text()
    handler = _handler()
    assert (handler.maxBytes, handler.backupCount) == (2 * 1024 * 1024, 3)  # kept


def test_an_unwritable_log_file_says_so_once_and_logging_goes_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the data directory would be")
    monkeypatch.setenv("NETKEEP_DATA", str(blocker))
    setup_logging("INFO")
    log = logging.getLogger("netkeep.example")
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
    monkeypatch.setenv("NETKEEP_DATABASE_URL", url)
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
    monkeypatch.setenv("NETKEEP_LOG_LEVEL", "DEBUG")
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
    expected = tmp_path / "netkeep-data" / "logs" / "netkeep.log"
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
    assert body["log_file"] == str(tmp_path / "netkeep-data" / "logs" / "netkeep.log")
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


# --- review of #622: processes, modes, and what else is personal ---------------------

_WRITER = """
import logging, sys
from netkeep.logging_setup import set_log_rotation, setup_logging
setup_logging("INFO")
set_log_rotation(max_mb=1, keep=100)
log = logging.getLogger("netkeep.writer")
tag, count = sys.argv[1], int(sys.argv[2])
pad = "y" * 250
for i in range(count):
    log.info("line %s-%d %s", tag, i, pad)
"""


def test_processes_sharing_the_file_lose_no_line_through_rotation(tmp_path: Path) -> None:
    """Four writers at once under a 1 MB limit: every line lands exactly once, no
    process prints a logging error, and none turns its file logging off."""
    data = tmp_path / "netkeep-data"
    env = {
        "PATH": "/usr/bin:/bin",
        "NETKEEP_DATA": str(data),
        "NETKEEP_LOG_STDERR_LEVEL": "WARNING",
    }
    count, tags = 3000, ["a", "b", "c", "d"]
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, tag, str(count)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for tag in tags
    ]
    errors = [proc.communicate(timeout=120)[1] for proc in procs]
    assert [proc.returncode for proc in procs] == [0] * len(tags)
    for err in errors:
        assert "Logging error" not in err
        assert "cannot write the log file" not in err
    logs = data / "logs"
    seen: dict[str, int] = {}
    files = [f for f in logs.iterdir() if f.name.startswith("netkeep.log")]
    assert len(files) >= 4  # about 4 MB in 1 MB files: it rotated, more than once
    for file in files:
        assert file.stat().st_mode & 0o777 == 0o600
        for line in file.read_text().splitlines():
            assert _LINE.fullmatch(line), line[:80]
            key = line.split(" line ", 1)[1].split(" ", 1)[0]
            seen[key] = seen.get(key, 0) + 1
    assert len(seen) == count * len(tags)
    assert set(seen.values()) == {1}


def test_a_removed_log_directory_is_made_again_at_the_next_record() -> None:
    setup_logging("INFO")
    log = logging.getLogger("netkeep.example")
    log.info("first")
    shutil.rmtree(log_file().parent)
    log.info("second")
    _flush()
    assert not _handler().failed
    assert log_file().read_text().rstrip().endswith("second")


def test_the_log_directory_is_private_and_an_old_file_is_made_private(tmp_path: Path) -> None:
    logs = tmp_path / "netkeep-data" / "logs"
    setup_logging("INFO")
    logging.getLogger("netkeep.example").info("one")
    _flush()
    assert logs.stat().st_mode & 0o777 == 0o700
    assert (logs / ".netkeep.log.lock").stat().st_mode & 0o777 == 0o600
    remove_file_handler()
    log_file().chmod(0o644)  # a file from before, or another tool's
    setup_logging("INFO")
    logging.getLogger("netkeep.example").info("two")
    _flush()
    assert log_file().stat().st_mode & 0o777 == 0o600


def test_a_handler_after_the_file_still_sees_plain_values() -> None:
    """The file's formatter marks a copy of the record, never the record itself."""
    setup_logging("INFO")
    seen: list[str] = []

    class After(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    after = After()
    logging.getLogger().addHandler(after)  # the restore fixture removes it
    logging.getLogger("netkeep.example").info("hi %s", personal("Quorbelle"))
    assert seen == ["hi Quorbelle"]


def test_a_cut_short_span_is_redacted_to_the_end_of_its_line() -> None:
    setup_logging("INFO")
    logging.getLogger("netkeep.example").info("name %.4s", personal("Quorbelle"))
    logging.getLogger("netkeep.example").info("next line")
    _flush()
    redacted = PERSONAL_PATTERN.sub("<personal>", log_file().read_text())
    assert "Quor" not in redacted
    assert "next line" in redacted


@pytest.mark.parametrize(
    "path",
    ["/api/v1/templates/dictionary/Quorbelle", "/api/v1/imports/presets/Quorbelle"],
)
def test_a_typed_word_in_an_access_log_path_is_personal(
    path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    setup_logging("INFO")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "DELETE", path, "1.1", 204
    )
    _flush()
    assert f"{path} HTTP/1.1" in capsys.readouterr().err
    text_ = log_file().read_text()
    prefix = path.removesuffix("Quorbelle")
    assert f"{prefix}{PERSONAL_OPEN}Quorbelle{PERSONAL_CLOSE} HTTP/1.1" in text_


def test_campaign_names_are_personal_in_the_removed_field_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging("INFO")
    blocked = [RemovedFieldCampaign(3, "Quorbelle reunion", CampaignStatus.ACTIVE, (1, 2))]
    log_removed_field_campaigns(logging.getLogger("netkeep.web.app"), blocked)
    _flush()
    assert "'Quorbelle reunion' (campaign 3, active, steps 1, 2)" in capsys.readouterr().err
    assert f"{PERSONAL_OPEN}'Quorbelle reunion'{PERSONAL_CLOSE}" in log_file().read_text()


def test_a_route_change_never_names_the_urn_it_could_not_resolve(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with pytest.raises(RouteChanged) as raised:
        _resolve_included("connections", {}, "elements[0]", "urn:li:fsd_profile:ACoAQuorbelle")
    assert "ACoAQuorbelle" not in str(raised.value)
    assert "urn:li:fsd_profile:…" in str(raised.value)
    assert "ACoAQuorbelle" not in caplog.text


def test_a_failed_write_never_quotes_its_bound_body(tmp_path: Path) -> None:
    """``hide_parameters``: a database error's text, logged anywhere, carries no body."""
    engine = make_engine(f"sqlite:///{tmp_path / 'scratch.sqlite3'}")
    body = "Hi Quorbelle, lovely to catch up"
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE m (id INTEGER PRIMARY KEY, body TEXT)"))
            connection.execute(text("INSERT INTO m (id, body) VALUES (1, 'x')"))
        with pytest.raises(IntegrityError) as raised, engine.begin() as connection:
            connection.execute(text("INSERT INTO m (id, body) VALUES (1, :b)"), {"b": body})
    finally:
        engine.dispose()
    assert "UNIQUE" in str(raised.value)
    assert body not in str(raised.value)


def test_openapi_export_writes_no_log_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETKEEP_LOG_LEVEL", "DEBUG")
    out = tmp_path / "openapi.json"
    result = CliRunner().invoke(cli, ["openapi", "export", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.is_file()
    assert not log_file().parent.exists()
