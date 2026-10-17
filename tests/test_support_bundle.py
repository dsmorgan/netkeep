"""The support bundle (#603): each level, built over invented personal data and secrets.

A scratch database and data directory are seeded with invented values a real one
could hold (a contact's name, email, phone, slug and URN; a campaign; run notes; a
session flag's URL; a config file with an unknown secret key; Keychain entries) and a
log file written through the real logging setup, with marked values, unmarked
values, and secrets that should never have been logged. Then every file of the zip
is scanned: standard and detailed hold no invented personal value and no secret;
full holds no secret, and does hold the real values (so the scan is not vacuous).
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import factories
import httpx
import pytest
from fastapi import FastAPI
from isolation.harness import acting_as
from sqlalchemy.orm import Session, sessionmaker
from test_runs_serve import HEADERS
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.cli import app as cli
from netkeeper.config import Settings, load_settings
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.logging_setup import personal, remove_file_handler, setup_logging
from netkeeper.models import SyncRun, SyncRunKind, SyncRunStatus, SyncRunTrigger, User, UserKind
from netkeeper.paths import data_dir
from netkeeper.scoping import install_scope_guard
from netkeeper.services import keychain, support_bundle
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import flag_session
from netkeeper.services.settings_kv import set_setting
from netkeeper.services.support_bundle import Level, build_bundle, log_excerpt
from netkeeper.services.users import ensure_local_user

NOW = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)

FIRST, LAST = "Quorbelle", "Fictionary"
EMAIL = "quorbelle.fictionary@example.test"
PHONE = "+1 555 010 4477"
SLUG = "quorbelle-fictionary-4x7"
URN = "urn:li:fsd_profile:ACoAAQuorbelleX9"
OWNER_EMAIL = "zephyrine.owner@example.test"
CAMPAIGN = "Zephyr Outreach Plan"
NOTE = "called Quorbelle about the Imaginaria offer"
USER_DIR = "quorbelleuser"
EMAIL_LOCAL = "quorbelle.f"

PERSONAL = (
    FIRST,
    LAST,
    EMAIL,
    "010 4477",
    "5550104477",
    SLUG,
    "ACoAAQuorbelleX9",
    OWNER_EMAIL,
    "zephyrine",
    CAMPAIGN,
    "Imaginaria",
    USER_DIR,
)
SECRETS = (
    "ya29.a0AinventedAccessTok3n",
    "0inventedRefreshTokenValue",
    "GOCSPX-inventedClientSecret9",
    "AQEDinventedLiAtCookie",
    "ajax:8765432109",
    "0inventedAuthCode",
    "inventedColonToken",
    "AQEDinventedCookieDict",
    "invented-client-secret-value",
    "inventedKeychainGmailToken",
    "inventedKeychainClientJson",
    "inventedConfigSecret",
    "inventedSettingsKvToken",
    "sk-ant-api03-inventedKey",
)


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A home directory named after an invented macOS user, and the data directory in it."""
    path = tmp_path / "Users" / USER_DIR
    data = path / "Library" / "Application Support" / "netkeeper"
    data.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: path))
    monkeypatch.setenv("NETKEEPER_DATA", str(data))
    return path


def _seed_log() -> None:
    """The log file, through the real setup, as a busy week might leave it."""
    setup_logging()
    try:
        log = logging.getLogger("netkeeper.test_support_bundle")
        log.info("saved contact %s %s", personal(f"{FIRST} {LAST}"), personal(EMAIL))
        log.info(
            "visiting %s (%s)", personal(f"https://www.linkedin.com/in/{SLUG}/"), personal(URN)
        )
        log.info("campaign %s step 2 for %s", personal(CAMPAIGN), personal(OWNER_EMAIL))
        log.info("opened %s", personal(data_dir() / "netkeeper.sqlite3"))
        log.info("cut short %.12s", personal(f"{FIRST} {LAST} and more"))
        # Unmarked: what the second line of defense is for.
        log.warning("reply from %s, phone %s, at linkedin.com/in/%s", EMAIL, PHONE, SLUG)
        log.warning("bare urn %s and path %s/Downloads/x.csv", URN, Path.home())
        # Secrets that should never be logged, in each shape.
        log.error("token ya29.a0AinventedAccessTok3n refresh 1//0inventedRefreshTokenValue")
        log.error("client GOCSPX-inventedClientSecret9 key sk-ant-api03-inventedKey")
        log.error("Cookie: li_at=AQEDinventedLiAtCookie; JSESSIONID=ajax:8765432109")
        log.error("GET /api/v1/mailboxes/oauth/callback?state=s&code=4/0inventedAuthCode")
        log.error('{"client_secret": "invented-client-secret-value"}')
        log.error("csrf-token: ajax:8765432109 and token: inventedColonToken")
        log.error("{'name': 'li_at', 'value': 'AQEDinventedCookieDict', 'path': '/'}")
        try:
            raise ValueError(f"no such contact {FIRST} {LAST}\nsecond line {EMAIL_LOCAL}")
        except ValueError:
            log.exception("traceback for %s", personal(f"{FIRST} {LAST}"))
        # An unmarked message with SQLAlchemy's and pydantic's detail lines.
        try:
            try:
                raise KeyError(SLUG)
            except KeyError as exc:
                raise RuntimeError(
                    f"(sqlite3.IntegrityError) UNIQUE\n[SQL: INSERT]\n[parameters: ('{FIRST}',)]"
                ) from exc
        except RuntimeError:
            log.exception("insert failed")
        try:
            raise ExceptionGroup("two failed", [ValueError(f"{LAST} bad"), KeyError(SLUG)])
        except ExceptionGroup:
            log.exception("group")
        log.warning(
            "validation\n  name\n    Input should be short [input_value='%s %s']", FIRST, LAST
        )
    finally:
        remove_file_handler()


def _seed(session: Session) -> User:
    user = ensure_local_user(session, settings=Settings())
    user.email = OWNER_EMAIL
    factories.make_contact(
        session,
        user,
        first_name=FIRST,
        last_name=LAST,
        li_public_id=SLUG,
        li_urn=URN,
        emails=[EMAIL],
        phones=[PHONE],
    )
    factories.make_campaign(session, user, name=CAMPAIGN)
    account = ensure_account(session, user)
    session.add(
        SyncRun(
            user_id=user.id,
            linkedin_account_id=account.id,
            kind=SyncRunKind.ENRICH,
            status=SyncRunStatus.FAILED,
            trigger=SyncRunTrigger.MANUAL,
            started_at=NOW - timedelta(hours=2),
            completed_at=NOW - timedelta(hours=1),
            stop_reason="route_changed",
            counts_json={"completed": 3, "unreadable_visits": [[1, 7, "no_profile"]]},
            notes=NOTE,
            error=f"ValueError: no contact named {FIRST} {LAST}",
        )
    )
    session.add(
        SyncRun(
            user_id=user.id,
            linkedin_account_id=account.id,
            kind=SyncRunKind.INBOX,
            status=SyncRunStatus.FAILED,
            trigger=SyncRunTrigger.MANUAL,
            started_at=NOW - timedelta(hours=3),
            completed_at=NOW - timedelta(hours=3),
            error=f"KeyError: '{SLUG}'",
        )
    )
    flag_session(
        session,
        user,
        Outcome.CHECKPOINT,
        url=f"https://www.linkedin.com/checkpoint/challenge/{SLUG}",
    )
    set_setting(session, user, "gmail.oauth_pending", {"token": "inventedSettingsKvToken"})
    keychain.set_secret(user.id, "gmail_token_1", "inventedKeychainGmailToken")
    keychain.set_secret(user.id, "gmail_client", '{"x": "inventedKeychainClientJson"}')
    session.flush()
    return user


def _settings(home: Path) -> Settings:
    config = data_dir() / "config.toml"
    config.write_text(
        '[gmail]\nclient_secret = "inventedConfigSecret"\n[campaigns]\nmailbox_daily_cap = 40\n'
    )
    return load_settings(config)


def _contents(data: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name).decode() for name in archive.namelist()}


def _build(session_factory: sessionmaker[Session], home: Path, level: Level) -> dict[str, str]:
    settings = _settings(home)
    _seed_log()
    with session_scope(session_factory, write=True) as session:
        _seed(session)
    with session_scope(session_factory) as session:
        user = ensure_local_user(session, settings=Settings())
        bundle = build_bundle(
            session, user, settings=settings, level=level, now=NOW + timedelta(days=0)
        )
    assert bundle.filename == f"netkeeper-support-{level.value}-20261017-150000.zip"
    return _contents(bundle.data)


def _found(files: dict[str, str], values: tuple[str, ...]) -> list[tuple[str, str]]:
    return [
        (name, value)
        for name, text in files.items()
        for value in values
        if value.lower() in text.lower()
    ]


@pytest.mark.parametrize("level", [Level.STANDARD, Level.DETAILED])
def test_a_redacted_level_holds_no_personal_value_and_no_secret(
    session_factory: sessionmaker[Session], home: Path, level: Level
) -> None:
    files = _build(session_factory, home, level)
    assert _found(files, PERSONAL) == [], files["posture.txt"]
    assert _found(files, SECRETS) == []
    # It is not empty: the log, the run and the settings all made it in.
    log_text = files["logs/netkeeper.log"]
    assert "<personal-1>" in log_text
    assert "<email-" in log_text
    assert "<secret>" in log_text
    runs = json.loads(files["runs.json"])
    assert runs["runs"][0]["stop_reason"] == "route_changed"
    assert "notes" not in runs["runs"][0]
    assert [run["error"] for run in runs["runs"]] == [
        "ValueError: <message left out>",
        "KeyError: <message left out>",
    ]
    assert "ValueError: <message left out>" in log_text
    assert "sqlite3" not in log_text
    settings = json.loads(files["settings.json"])["values"]
    assert settings["campaigns.mailbox_daily_cap"] == {"value": 40, "source": "config.toml"}


def test_the_full_level_holds_real_values_and_no_secret(
    session_factory: sessionmaker[Session], home: Path
) -> None:
    files = _build(session_factory, home, Level.FULL)
    assert _found(files, SECRETS) == []
    found = {value for _, value in _found(files, PERSONAL)}
    assert {FIRST, EMAIL, SLUG, CAMPAIGN, "Imaginaria"} <= found
    first = json.loads(files["runs.json"])["runs"][0]
    assert first["notes"] == NOTE
    assert first["error"] == f"ValueError: no contact named {FIRST} {LAST}"
    assert "WARNING" in files["README.txt"]


def test_every_bundle_has_a_readme_and_a_manifest_of_its_files(
    session_factory: sessionmaker[Session], home: Path
) -> None:
    files = _build(session_factory, home, Level.DETAILED)
    manifest = json.loads(files["manifest.json"])
    assert manifest["level"] == "detailed"
    assert manifest["netkeeper_version"]
    assert manifest["created_at"] == NOW.isoformat()
    assert manifest["problems"] == []
    listed = {item["name"] for item in manifest["files"]}
    assert listed == set(files) - {"manifest.json"}
    for name in (
        "versions.json",
        "settings.json",
        "posture.txt",
        "breakers.json",
        "jobs.json",
        "runs.json",
        "database.json",
        "logs/netkeeper.log",
        "redactions.json",
    ):
        assert name in files
    assert "page-structure captures: netkeeper does not store any" in files["README.txt"].lower()
    types = json.loads(files["redactions.json"])["types"]
    assert types["personal"] >= 1 and types["email"] >= 1


def test_the_standard_level_has_no_redactions_file(
    session_factory: sessionmaker[Session], home: Path
) -> None:
    files = _build(session_factory, home, Level.STANDARD)
    assert "redactions.json" not in files
    assert "Page-structure" not in files["README.txt"]


def test_settings_are_an_allowlist_that_covers_every_config_field() -> None:
    # A new config.py field fails here until someone decides whether it goes in.
    keys = set(support_bundle.settings_leaf_keys())
    decided = set(support_bundle.SETTINGS_ALLOWLIST) | support_bundle.SETTINGS_LEFT_OUT
    assert keys == decided


def test_chrome_version_comes_from_the_app_not_the_browser(tmp_path: Path) -> None:
    import plistlib

    plist = tmp_path / "Info.plist"
    plist.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "150.0.7000.1"}))
    assert support_bundle.chrome_version([tmp_path / "missing.plist", plist]) == "150.0.7000.1"
    assert support_bundle.chrome_version([tmp_path / "missing.plist"]) is None


def _record(when: datetime, text: str) -> str:
    return f"{when:%Y-%m-%d %H:%M:%S} INFO x {text}\n"


def test_the_log_excerpt_keeps_whole_records_since_the_cutoff(tmp_path: Path) -> None:
    start = datetime(2026, 10, 1, 12, 0, 0)
    (tmp_path / "netkeeper.log.2").write_text(
        "orphan tail of an older file\n" + _record(start, "oldest")
    )
    (tmp_path / "netkeeper.log.1").write_text(
        _record(start + timedelta(days=5), "middle") + "Traceback line\n"
    )
    (tmp_path / "netkeeper.log").write_text(_record(start + timedelta(days=9), "newest"))
    (tmp_path / "netkeeper.log.lock").write_text("")
    excerpt = log_excerpt(tmp_path, since_local=start + timedelta(days=1), max_bytes=10_000)
    assert excerpt.files == ("netkeeper.log.2", "netkeeper.log.1", "netkeeper.log")
    assert "".join(excerpt.parts).splitlines() == [
        _record(start + timedelta(days=5), "middle").strip(),
        "Traceback line",
        _record(start + timedelta(days=9), "newest").strip(),
    ]
    assert not excerpt.truncated


def test_the_log_excerpt_drops_the_oldest_records_past_its_size(tmp_path: Path) -> None:
    start = datetime(2026, 10, 1, 12, 0, 0)
    (tmp_path / "netkeeper.log").write_text(
        "".join(_record(start + timedelta(minutes=n), f"record {n}") for n in range(10))
    )
    size = len(_record(start, "record 0").encode())
    excerpt = log_excerpt(tmp_path, since_local=start, max_bytes=size * 3)
    assert excerpt.truncated
    assert [line.rsplit(" ", 1)[1] for line in "".join(excerpt.parts).splitlines()] == [
        "7",
        "8",
        "9",
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("7d", timedelta(days=7)), ("36h", timedelta(hours=36)), ("2W", timedelta(weeks=2))],
)
def test_parse_since(text: str, expected: timedelta) -> None:
    assert support_bundle.parse_since(text) == expected


@pytest.mark.parametrize("text", ["", "7", "0d", "366d", "1y", "-1d"])
def test_parse_since_refuses(text: str) -> None:
    with pytest.raises(support_bundle.SupportBundleError):
        support_bundle.parse_since(text)


def test_the_levels_pin_what_they_promise() -> None:
    levels = support_bundle.LEVELS
    assert [spec.redacted for spec in levels.values()] == [True, True, False]
    assert [spec.needs_confirmation for spec in levels.values()] == [False, False, True]
    assert [spec.default_since_days for spec in levels.values()] == [7, 30, 30]


# --- the CLI ------------------------------------------------------------------------------


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, home: Path) -> Iterator[sessionmaker[Session]]:
    url = database_url(data_dir())
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        _seed(session)
    yield factory
    engine.dispose()


def test_the_cli_writes_a_standard_bundle(
    cli_db: sessionmaker[Session], home: Path, tmp_path: Path
) -> None:
    target = tmp_path / "out.zip"
    result = CliRunner().invoke(cli, ["support", "bundle", "-o", str(target)])
    assert result.exit_code == 0, result.output
    assert target.stat().st_mode & 0o777 == 0o600
    files = _contents(target.read_bytes())
    assert json.loads(files["manifest.json"])["level"] == "standard"
    assert json.loads(files["database.json"])["revision"] == migrations.head_revision()
    assert _found(files, PERSONAL) == []
    # Never overwrites.
    again = CliRunner().invoke(cli, ["support", "bundle", "-o", str(target)])
    assert again.exit_code == 1
    assert "already exists" in again.output


def test_the_cli_full_level_needs_the_typed_confirmation(
    cli_db: sessionmaker[Session], home: Path, tmp_path: Path
) -> None:
    target = tmp_path / "full.zip"
    refused = CliRunner().invoke(
        cli, ["support", "bundle", "--level", "full", "-o", str(target)], input="yes\n"
    )
    assert refused.exit_code == 1
    assert not target.exists()
    confirmed = CliRunner().invoke(
        cli, ["support", "bundle", "--level", "full", "-o", str(target)], input="full\n"
    )
    assert confirmed.exit_code == 0, confirmed.output
    assert json.loads(_contents(target.read_bytes())["manifest.json"])["level"] == "full"
    scripted = tmp_path / "scripted.zip"
    result = CliRunner().invoke(
        cli, ["support", "bundle", "--level", "full", "--confirm-full", "-o", str(scripted)]
    )
    assert result.exit_code == 0, result.output


def test_the_cli_refuses_a_bad_since(cli_db: sessionmaker[Session], tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli, ["support", "bundle", "--since", "1y", "-o", str(tmp_path / "x.zip")]
    )
    assert result.exit_code == 2
    assert not (tmp_path / "x.zip").exists()


# --- the API ------------------------------------------------------------------------------

URL = "/api/v1/support"


async def test_the_levels_endpoint_previews_each_level(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{URL}/levels")
    assert response.status_code == 200
    levels = response.json()["levels"]
    assert [item["level"] for item in levels] == ["standard", "detailed", "full"]
    assert levels[2]["needs_confirmation"] is True
    assert levels[2]["warning"]


async def test_the_bundle_endpoint_downloads_a_zip(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    with session_scope(running_app.state.session_factory, write=True) as session:
        _seed(session)
    response = await client.get(
        f"{URL}/bundle", params={"since_days": 3}, headers={"Sec-Fetch-Site": "same-origin"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment;" in response.headers["content-disposition"]
    files = _contents(response.content)
    assert json.loads(files["manifest.json"])["level"] == "standard"
    assert _found(files, PERSONAL) == []
    assert _found(files, SECRETS) == []


async def test_the_full_level_needs_confirmation(client: httpx.AsyncClient) -> None:
    refused = await client.get(f"{URL}/bundle", params={"level": "full"}, headers=HEADERS)
    assert refused.status_code == 409
    confirmed = await client.get(
        f"{URL}/bundle", params={"level": "full", "confirm_full": True}, headers=HEADERS
    )
    assert confirmed.status_code == 200


@pytest.mark.parametrize("path", ["/levels", "/bundle"])
async def test_only_the_owner_makes_a_bundle(
    client: httpx.AsyncClient, running_app: FastAPI, path: str
) -> None:
    with session_scope(running_app.state.session_factory, write=True) as session:
        other = factories.make_user(session, kind=UserKind.HOSTED).id
    with acting_as(running_app, other):
        response = await client.get(f"{URL}{path}", headers=HEADERS)
    assert response.status_code == 403


@pytest.mark.parametrize("level", ["standard", "detailed", "full"])
@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "cross-site", **HEADERS},
        {},
    ],
)
async def test_a_download_another_site_could_start_is_refused(
    client: httpx.AsyncClient, level: str, headers: dict[str, str]
) -> None:
    response = await client.get(
        f"{URL}/bundle", params={"level": level, "confirm_full": True}, headers=headers
    )
    assert response.status_code == 403
    assert "Settings page" in response.json()["detail"]


@pytest.mark.parametrize("site", ["same-origin", "none", "Same-Origin"])
async def test_the_pages_own_link_and_the_address_bar_download(
    client: httpx.AsyncClient, site: str
) -> None:
    response = await client.get(f"{URL}/bundle", headers={"Sec-Fetch-Site": site})
    assert response.status_code == 200


def test_the_cdp_url_keeps_only_its_host_and_port(
    session_factory: sessionmaker[Session], home: Path
) -> None:
    from dataclasses import replace

    settings = Settings()
    settings = replace(
        settings,
        linkedin=replace(
            settings.linkedin, cdp_url="ws://127.0.0.1:9222/devtools/browser/0b1d-invented"
        ),
    )
    with session_scope(session_factory, write=True) as session:
        _seed(session)
    with session_scope(session_factory) as session:
        user = ensure_local_user(session, settings=Settings())
        for level in Level:
            files = _contents(build_bundle(session, user, settings=settings, level=level).data)
            value = json.loads(files["settings.json"])["values"]["linkedin.cdp_url"]["value"]
            assert value == "ws://127.0.0.1:9222"


@pytest.mark.parametrize(
    ("error", "kept"),
    [
        ("ValueError: no contact named Quorbelle Fictionary", "ValueError: <message left out>"),
        ("TimeoutError", "TimeoutError"),
        ("TimeoutError: ", "TimeoutError"),
        ("Conflict: li_public_id 'quorbelle-x' already belongs", "Conflict: <message left out>"),
        ("jane.doe: hi", "<message left out>"),
        ("Quorbelle Fictionary was here", "<message left out>"),
        (None, None),
    ],
)
def test_a_run_error_keeps_only_its_type(error: str | None, kept: str | None) -> None:
    assert support_bundle.error_type(error) == kept
