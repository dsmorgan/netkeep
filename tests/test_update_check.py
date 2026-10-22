"""``serve``'s daily update check (#762): at most one request a day, none while the
setting is off, a quiet failure, and the answer the UI reads. Every request goes to a
fake ``fetch``; nothing here reaches GitHub."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from netkeep import __version__
from netkeep.config import Settings, UpdateSettings
from netkeep.db import session_scope
from netkeep.services import ui_settings, update, update_check
from netkeep.services.scheduled_runs import ServeExtractor
from netkeep.services.update_check import CheckResult, DailyUpdateCheck
from netkeep.services.update_check import github_fetch as REAL_FETCH  # before conftest's guard
from netkeep.services.users import ensure_local_user
from netkeep.web.app import create_app

CSRF = {"X-Netkeep-Client": "1"}
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)

_MAJOR, _MINOR, _PATCH = update.parse_version(__version__) or (0, 0, 0)
NEWER = f"{_MAJOR}.{_MINOR + 1}.0"


class FakeGitHub:
    """The latest-release endpoint: answers ``tag``, or raises ``error``. Records each
    request."""

    def __init__(self, tag: str = f"v{NEWER}") -> None:
        self.tag = tag
        self.error: Exception | None = None
        self.requests: list[str] = []

    def __call__(self, url: str, max_bytes: int) -> bytes:
        self.requests.append(url)
        if self.error is not None:
            raise self.error
        return json.dumps({"tag_name": self.tag, "name": "ignored"}).encode()


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def data(tmp_path: Path) -> Path:
    return tmp_path / "netkeep-data"


@pytest.fixture
def owner(session_factory: sessionmaker[Session]) -> Iterator[None]:
    with session_scope(session_factory, write=True) as session:
        ensure_local_user(session)
    yield


def _job(
    factory: sessionmaker[Session],
    data: Path,
    github: FakeGitHub,
    clock: Clock,
    base: Settings | None = None,
) -> DailyUpdateCheck:
    return DailyUpdateCheck(factory, base or Settings(), data, fetch=github, clock=clock)


# --- the check --------------------------------------------------------------------


def test_a_newer_release_is_recorded_with_when_it_was_checked(
    session_factory: sessionmaker[Session], data: Path, owner: None
) -> None:
    github, clock = FakeGitHub(), Clock()

    result = _job(session_factory, data, github, clock).check_if_due()

    assert github.requests == [update.LATEST_URL]
    assert result == CheckResult(checked_at=NOW, latest=NEWER, ok=True)
    assert update_check.read_result(data) == result
    assert update_check.newer_release(result) == NEWER


@pytest.mark.parametrize("tag", [f"v{__version__}", "v0.0.1"])
def test_the_same_or_an_older_release_is_not_offered(
    session_factory: sessionmaker[Session], data: Path, owner: None, tag: str
) -> None:
    result = _job(session_factory, data, FakeGitHub(tag), Clock()).check_if_due()

    assert result is not None and result.ok
    assert update_check.newer_release(result) is None


@pytest.mark.parametrize(
    "error",
    [update.UpdateError("could not download: HTTP 503"), OSError("network is down")],
)
def test_a_failed_check_logs_one_line_and_waits_a_day(
    session_factory: sessionmaker[Session],
    data: Path,
    owner: None,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    github, clock = FakeGitHub(), Clock()
    job = _job(session_factory, data, github, clock)
    github.error = error

    with caplog.at_level(logging.WARNING, logger="netkeep.services.update_check"):
        result = job.check_if_due()

    assert result == CheckResult(checked_at=NOW, latest=None, ok=False)
    assert [r.getMessage() for r in caplog.records] == [
        f"update check failed; trying again in a day: {error}"
    ]
    # Not retried at the next look, an hour on, nor by a new serve: a day on.
    clock.now = NOW + timedelta(hours=23)
    assert job.check_if_due() is None
    assert _job(session_factory, data, github, clock).check_if_due() is None
    assert len(github.requests) == 1
    github.error = None
    clock.now = NOW + timedelta(days=1)
    assert _job(session_factory, data, github, clock).check_if_due() == CheckResult(
        checked_at=clock.now, latest=NEWER, ok=True
    )


def test_a_failed_check_keeps_the_release_found_before(
    session_factory: sessionmaker[Session], data: Path, owner: None
) -> None:
    github, clock = FakeGitHub(), Clock()
    job = _job(session_factory, data, github, clock)
    job.check_if_due()
    github.error = update.UpdateError("could not download: HTTP 500")
    clock.now = NOW + timedelta(days=1)

    assert job.check_if_due() == CheckResult(checked_at=clock.now, latest=NEWER, ok=False)


def test_a_bad_answer_is_a_failed_check(
    session_factory: sessionmaker[Session], data: Path, owner: None
) -> None:
    result = _job(session_factory, data, FakeGitHub("nightly"), Clock()).check_if_due()

    assert result == CheckResult(checked_at=NOW, latest=None, ok=False)


def test_at_most_one_request_a_day_across_a_serve_restart(
    session_factory: sessionmaker[Session], data: Path, owner: None
) -> None:
    github, clock = FakeGitHub(), Clock()
    _job(session_factory, data, github, clock).check_if_due()

    for hours in (1, 12, 23):
        clock.now = NOW + timedelta(hours=hours, minutes=59)
        # A new job is a restarted serve: it reads when the last check ran from disk.
        assert _job(session_factory, data, github, clock).check_if_due() is None
    assert len(github.requests) == 1

    clock.now = NOW + timedelta(days=1)
    assert _job(session_factory, data, github, clock).check_if_due() is not None
    assert len(github.requests) == 2


def test_a_record_it_cannot_write_still_waits_a_day_in_this_process(
    session_factory: sessionmaker[Session],
    data: Path,
    owner: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unwritable(*_args: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(update_check, "write_result", unwritable)
    github, clock = FakeGitHub(), Clock()
    job = _job(session_factory, data, github, clock)

    assert job.check_if_due() is not None
    clock.now = NOW + timedelta(hours=1)
    assert job.check_if_due() is None
    assert len(github.requests) == 1


def test_a_clock_set_back_does_not_stop_the_check_for_good(
    session_factory: sessionmaker[Session], data: Path, owner: None
) -> None:
    update_check.write_result(
        CheckResult(checked_at=NOW + timedelta(days=300), latest=None, ok=True), data
    )
    github = FakeGitHub()

    assert _job(session_factory, data, github, Clock()).check_if_due() is not None
    assert len(github.requests) == 1


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "{}",
        '{"checked_at": "2026-10-22T09:00:00", "latest": null, "ok": true}',
        '{"checked_at": "2026-10-22T09:00:00+00:00", "latest": "../x", "ok": true}',
    ],
)
def test_a_record_netkeep_did_not_write_is_ignored(data: Path, text: str) -> None:
    path = update_check.check_path(data)
    path.parent.mkdir(parents=True)
    path.write_text(text)

    assert update_check.read_result(data) is None


# --- the setting ------------------------------------------------------------------


def test_the_setting_off_on_the_page_makes_no_request(
    session_factory: sessionmaker[Session], data: Path
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = ensure_local_user(session)
        ui_settings.write(session, user, Settings(), {"updates.check_daily": False})
    github = FakeGitHub()

    assert _job(session_factory, data, github, Clock()).check_if_due() is None
    assert github.requests == []
    assert not update_check.check_path(data).exists()


def test_the_setting_off_in_config_toml_makes_no_request(
    session_factory: sessionmaker[Session], data: Path, owner: None
) -> None:
    github = FakeGitHub()
    base = Settings(updates=UpdateSettings(check_daily=False))

    assert _job(session_factory, data, github, Clock(), base).check_if_due() is None
    assert github.requests == []


def test_the_check_is_on_by_default() -> None:
    assert UpdateSettings() == UpdateSettings(check_daily=True)
    assert ui_settings.BY_KEY["updates.check_daily"].kind == "bool"


def test_the_timings_are_pinned() -> None:
    assert timedelta(hours=24) == update_check.INTERVAL
    assert update_check.TICK_S == 3600.0
    assert update_check.FIRST_CHECK_S == 300.0
    assert update_check.UPDATE_COMMAND == "netkeep update"


def test_the_check_asks_only_github_for_the_latest_release() -> None:
    assert update.LATEST_URL == "https://api.github.com/repos/dsmorgan/netkeep/releases/latest"


def test_the_real_fetch_is_https_get(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, int]] = []

    def fake_get(url: str, *, max_bytes: int) -> bytes:
        seen.append((url, max_bytes))
        return b"{}"

    monkeypatch.setattr(update, "https_get", fake_get)

    assert REAL_FETCH(update.LATEST_URL, 1024) == b"{}"
    assert seen == [(update.LATEST_URL, 1024)]


# --- serve and the API --------------------------------------------------------------


async def test_serve_starts_and_stops_the_check(
    bare_engine: Engine,
    tmp_path: Path,
    _migrated_template: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import conftest

    conftest._copy_template(_migrated_template, tmp_path / "bare")
    monkeypatch.setenv("NETKEEP_FRONTEND_DIST", str(tmp_path / "no-dist"))
    started: list[DailyUpdateCheck] = []
    real_start = DailyUpdateCheck.start

    def spy(self: DailyUpdateCheck) -> None:
        started.append(self)
        real_start(self)

    monkeypatch.setattr(DailyUpdateCheck, "start", spy)
    extractor = ServeExtractor(executor=lambda factory, bus: object())  # type: ignore[arg-type,return-value]
    served = create_app(Settings(), engine=bare_engine, extractor=extractor)
    async with served.router.lifespan_context(served):
        (job,) = started
        assert job._task is not None and not job._task.done()
        assert job._first_check_s == 300.0  # nothing is asked while serve starts
    assert job._task is None


async def test_the_app_without_serve_starts_no_check(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[DailyUpdateCheck] = []
    monkeypatch.setattr(DailyUpdateCheck, "start", lambda self: started.append(self))
    async with app.router.lifespan_context(app):
        assert started == []


async def test_the_api_offers_a_newer_release_and_the_command(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    data = tmp_path / "netkeep-data"
    empty = (await client.get("/api/v1/update-check")).json()
    assert empty == {
        "enabled": True,
        "current": __version__,
        "checked_at": None,
        "ok": None,
        "latest": None,
        "available": None,
        "command": None,
    }

    update_check.write_result(CheckResult(checked_at=NOW, latest=NEWER, ok=True), data)
    found = (await client.get("/api/v1/update-check")).json()
    assert found["available"] == NEWER
    assert found["command"] == "netkeep update"
    assert found["checked_at"] == "2026-10-22T09:00:00Z"

    saved = await client.put(
        "/api/v1/settings/config", json={"values": {"updates.check_daily": False}}, headers=CSRF
    )
    assert saved.status_code == 200, saved.text
    off = (await client.get("/api/v1/update-check")).json()
    assert off["enabled"] is False
    assert off["available"] is None and off["command"] is None


async def test_the_api_offers_nothing_once_this_is_the_latest(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    update_check.write_result(
        CheckResult(checked_at=NOW, latest=__version__, ok=True), tmp_path / "netkeep-data"
    )
    body = (await client.get("/api/v1/update-check")).json()
    assert body["latest"] == __version__
    assert body["available"] is None
