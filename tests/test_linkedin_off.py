"""LinkedIn Off refuses every run a person starts (#829).

Off is disarmed, so nothing runs on a schedule; these tests cover the runs a person
starts: Start on the LinkedIn page, Check connection, "I sent it, check now", a message
check, and the CLI's run commands. The prefill claim's refusal is in
``test_linkedin_steps.py``. Every browser here is a
fake that counts attaches, and every contact is invented.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import factories
import pytest
from run_fakes import Clock, fake_provider
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from test_runs_serve import HEADERS, START, _rows, client_for, served
from typer.testing import CliRunner

from netkeep import migrations
from netkeep.cli import app as cli
from netkeep.config import Settings
from netkeep.db import database_url, make_engine, make_session_factory, session_scope
from netkeep.models import SyncRunKind, SyncRunTrigger, User, UserKind
from netkeep.scoping import install_scope_guard
from netkeep.services import automation, linkedin_steps, message_check, runs
from netkeep.services.automation import Position
from netkeep.services.users import ensure_local_user

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)

pytestmark = pytest.mark.usefixtures("inside_active_hours")


@pytest.fixture
def no_frontend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NETKEEP_FRONTEND_DIST", str(tmp_path / "no-dist"))


def _turn_off(session: Session, user: User) -> None:
    automation.set_linkedin(session, user, Position.OFF, confirm=False, now=NOW)


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def off_user(writer: Session) -> User:
    user = factories.make_user(writer)
    _turn_off(writer, user)
    return user


# --- the one refusal every run a person starts meets --------------------------------------


@pytest.mark.parametrize(
    ("kind", "gate"),
    [
        (SyncRunKind.CONNECTIONS_FULL, None),
        (SyncRunKind.CONNECTIONS_INCREMENTAL, None),
        (SyncRunKind.ENRICH, None),
        (SyncRunKind.INBOX, None),
        (SyncRunKind.MESSAGE_SEND, runs.MESSAGE_SEND_GATE),
        (SyncRunKind.MESSAGE_SEND, runs.MESSAGE_CHECK_GATE),
        (SyncRunKind.SESSION_CHECK, runs.SESSION_CHECK_GATE),
    ],
)
def test_off_refuses_every_manual_run_and_records_none(
    writer: Session, off_user: User, kind: SyncRunKind, gate: object
) -> None:
    extra: dict[str, Any] = {} if gate is None else {"gate": gate}
    with pytest.raises(runs.LinkedInOff, match="LinkedIn is Off"):
        runs.create_run(writer, off_user, kind, trigger=SyncRunTrigger.MANUAL, now=NOW, **extra)
    assert runs.list_runs(writer, off_user)[1] == 0


@pytest.mark.parametrize("position", [Position.MANUAL, Position.AUTOMATIC])
def test_manual_and_automatic_still_start_runs(
    writer: Session, off_user: User, position: Position
) -> None:
    automation.set_linkedin(writer, off_user, position, confirm=True, now=NOW)
    run = runs.create_run(
        writer, off_user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
    )
    assert run.id is not None


def test_a_scheduled_run_on_off_is_refused_as_disarmed(writer: Session, off_user: User) -> None:
    """Off is always disarmed: the schedule's own refusal answers first."""
    with pytest.raises(runs.ScheduledRunsDisarmed):
        runs.create_run(
            writer, off_user, SyncRunKind.INBOX, trigger=SyncRunTrigger.SCHEDULED, now=NOW
        )


# --- prefills, "I sent it", and the message check -----------------------------------------


def test_check_sent_is_refused(writer: Session, off_user: User, monkeypatch: Any) -> None:
    monkeypatch.setattr(linkedin_steps, "_waiting_message", lambda *_: None)
    with pytest.raises(runs.LinkedInOff):
        linkedin_steps.check_sent(writer, off_user, 1, now=NOW, settings=Settings())
    assert runs.list_runs(writer, off_user)[1] == 0


def test_a_message_check_is_refused(writer: Session, off_user: User) -> None:
    contact = factories.make_contact(
        writer, off_user, li_urn="urn:li:fsd_profile:ACoAAATEST01", li_public_id="first-last"
    )
    with pytest.raises(runs.LinkedInOff):
        message_check.start(writer, off_user, contact.id, now=NOW, settings=Settings())
    assert runs.list_runs(writer, off_user)[1] == 0


# --- the web --------------------------------------------------------------------------------


def _local(session: Session) -> User:
    from sqlalchemy import select

    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


async def test_start_resume_and_check_connection_answer_409_and_attach_nothing(
    bare_engine: Engine, no_frontend: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, connector = fake_provider()
    async with served(bare_engine, Settings(), provider, Clock(START)) as app:
        with session_scope(app.state.session_factory, write=True) as session:
            _turn_off(session, _local(session))

        def closed(*_: Any, **__: Any) -> None:
            raise runs.OutsideActiveHours("outside active hours")

        async with client_for(app) as client:
            start = await client.post(
                "/api/v1/linkedin/runs", json={"kind": "inbox"}, headers=HEADERS
            )
            check = await client.post("/api/v1/linkedin/browser/check", headers=HEADERS)
            # Off is the reason given, ahead of active hours.
            monkeypatch.setattr(runs, "refuse_if_outside_active_hours", closed)
            closed_start = await client.post(
                "/api/v1/linkedin/runs", json={"kind": "enrich"}, headers=HEADERS
            )
    for response in (start, check, closed_start):
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == runs.LINKEDIN_OFF_REFUSAL
    assert connector.attaches == 0 and _rows(bare_engine) == []


# --- the CLI --------------------------------------------------------------------------------


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEP_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        _turn_off(session, ensure_local_user(session, settings=Settings()))
    yield factory
    engine.dispose()


@pytest.mark.usefixtures("no_browser_for_cli_runs")
@pytest.mark.parametrize(
    "command", [["linkedin", "sync"], ["linkedin", "enrich"], ["linkedin", "inbox"]]
)
def test_the_cli_refuses_runs_while_off(cli_db: sessionmaker[Session], command: list[str]) -> None:
    result = CliRunner().invoke(cli, command)
    assert result.exit_code == 1
    assert "LinkedIn is Off" in result.output
    with session_scope(cli_db) as session:
        user = ensure_local_user(session, settings=Settings())
        assert runs.list_runs(session, user)[1] == 0
