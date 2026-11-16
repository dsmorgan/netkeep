"""``netkeep automation``: one switch per service from the terminal (#829).

Every mailbox here is invented; nothing opens Gmail or a browser.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from campaign_fakes import make_mailbox
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeep import migrations
from netkeep.cli import app as cli
from netkeep.config import Settings
from netkeep.db import database_url, make_engine, make_session_factory, session_scope
from netkeep.models import Mailbox, MailboxArm, ScheduleDepth, User
from netkeep.scoping import install_scope_guard, scoped
from netkeep.services import automation
from netkeep.services.automation import Position
from netkeep.services.users import ensure_local_user

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEP_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        ensure_local_user(session, settings=Settings())
    yield factory
    engine.dispose()


def _user(session: Session) -> User:
    return ensure_local_user(session, settings=Settings())


def _linkedin(factory: sessionmaker[Session]) -> automation.LinkedInAutomation:
    with session_scope(factory) as session:
        return automation.linkedin_state(session, _user(session))


def _arm(factory: sessionmaker[Session]) -> MailboxArm | None:
    with session_scope(factory) as session:
        return session.scalars(scoped(_user(session), Mailbox)).one().arm


def test_linkedin_automatic_asks_first_and_no_changes_nothing(
    cli_db: sessionmaker[Session],
) -> None:
    runner = CliRunner()
    declined = runner.invoke(cli, ["automation", "linkedin", "automatic"], input="n\n")
    assert declined.exit_code == 1 and "LinkedIn stays manual" in declined.output
    assert _linkedin(cli_db).position is Position.MANUAL

    quick = runner.invoke(
        cli, ["automation", "linkedin", "automatic", "--depth", "quick"], input="y\n"
    )
    assert quick.exit_code == 0, quick.output
    assert "LinkedIn: automatic, quick checks" in quick.output
    state = _linkedin(cli_db)
    assert (state.position, state.depth) == (Position.AUTOMATIC, ScheduleDepth.QUICK)


def test_quick_to_full_asks_and_yes_skips_the_question(cli_db: sessionmaker[Session]) -> None:
    runner = CliRunner()
    args = ["automation", "linkedin", "automatic", "--depth"]
    assert runner.invoke(cli, [*args, "quick", "--yes"]).exit_code == 0
    declined = runner.invoke(cli, [*args, "full"], input="n\n")
    assert declined.exit_code == 1 and "weekly full sync" in declined.output
    assert _linkedin(cli_db).depth is ScheduleDepth.QUICK
    assert runner.invoke(cli, [*args, "full", "--yes"]).exit_code == 0
    assert _linkedin(cli_db).depth is ScheduleDepth.FULL


@pytest.mark.parametrize("position", ["manual", "off"])
def test_moving_left_never_asks(cli_db: sessionmaker[Session], position: str) -> None:
    runner = CliRunner()
    assert runner.invoke(cli, ["automation", "linkedin", "automatic", "--yes"]).exit_code == 0
    result = runner.invoke(cli, ["automation", "linkedin", position])  # no input to give
    assert result.exit_code == 0, result.output
    assert _linkedin(cli_db).position is Position(position)


def test_gmail_automatic_is_refused_until_the_message_id_check_passed(
    cli_db: sessionmaker[Session],
) -> None:
    with session_scope(cli_db, write=True) as session:
        make_mailbox(session, _user(session), armed_at=NOW)
    result = CliRunner().invoke(cli, ["automation", "gmail", "automatic", "--yes"])
    assert result.exit_code == 1
    assert "Message-ID" in result.output
    assert _arm(cli_db) is MailboxArm.DRAFT


def test_gmail_moves_between_positions_and_asks_before_automatic(
    cli_db: sessionmaker[Session],
) -> None:
    with session_scope(cli_db, write=True) as session:
        make_mailbox(session, _user(session), message_id_verified_at=NOW)
    runner = CliRunner()
    manual = runner.invoke(cli, ["automation", "gmail", "manual"])
    assert manual.exit_code == 0 and "Gmail: drafts only" in manual.output
    assert _arm(cli_db) is MailboxArm.DRAFT
    declined = runner.invoke(cli, ["automation", "gmail", "automatic"], input="n\n")
    assert declined.exit_code == 1 and _arm(cli_db) is MailboxArm.DRAFT
    automatic = runner.invoke(cli, ["automation", "gmail", "automatic"], input="y\n")
    assert automatic.exit_code == 0 and _arm(cli_db) is MailboxArm.SEND
    off = runner.invoke(cli, ["automation", "gmail", "off"])
    assert off.exit_code == 0 and _arm(cli_db) is None


def test_gmail_without_a_mailbox_refuses_moving_right(cli_db: sessionmaker[Session]) -> None:
    result = CliRunner().invoke(cli, ["automation", "gmail", "manual"])
    assert result.exit_code == 1 and "Connect a Gmail mailbox first" in result.output


def test_status_shows_both_switches(cli_db: sessionmaker[Session]) -> None:
    result = CliRunner().invoke(cli, ["automation", "status"])
    assert result.exit_code == 0
    assert "LinkedIn: manual" in result.output
    assert "Gmail: off" in result.output
    assert "Connect a Gmail mailbox first" in result.output


# --- the old stop commands never do more (#829 review) ------------------------------------


@pytest.mark.parametrize("alias", ["disarm", "pause"])
def test_a_stop_alias_moves_automatic_to_manual(cli_db: sessionmaker[Session], alias: str) -> None:
    runner = CliRunner()
    assert runner.invoke(cli, ["automation", "linkedin", "automatic", "--yes"]).exit_code == 0
    result = runner.invoke(cli, ["linkedin", "schedule", alias])
    assert result.exit_code == 0, result.output
    assert _linkedin(cli_db).position is Position.MANUAL


@pytest.mark.parametrize("alias", ["disarm", "pause"])
def test_a_stop_alias_leaves_off_off(cli_db: sessionmaker[Session], alias: str) -> None:
    runner = CliRunner()
    assert runner.invoke(cli, ["automation", "linkedin", "off"]).exit_code == 0
    result = runner.invoke(cli, ["linkedin", "schedule", alias])
    assert result.exit_code == 0, result.output
    assert "LinkedIn: off" in result.output
    assert _linkedin(cli_db).position is Position.OFF


def test_arm_yes_at_quick_checks_stays_quick(cli_db: sessionmaker[Session]) -> None:
    runner = CliRunner()
    args = ["automation", "linkedin", "manual", "--depth", "quick"]
    assert runner.invoke(cli, args).exit_code == 0
    result = runner.invoke(cli, ["linkedin", "schedule", "arm", "--yes"])
    assert result.exit_code == 0, result.output
    state = _linkedin(cli_db)
    assert (state.position, state.depth) == (Position.AUTOMATIC, ScheduleDepth.QUICK)


# --- a change between the read and the write that does more is refused --------------------


def test_linkedin_refuses_a_move_that_became_more_since_it_read(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read saw Automatic, so nothing was asked; by the write it is Manual, and moving
    to Automatic then needs a confirmation nobody gave."""
    real = automation.linkedin_state
    seen = automation.LinkedInAutomation(Position.AUTOMATIC, ScheduleDepth.FULL, None)
    calls = iter([seen])
    monkeypatch.setattr(
        automation, "linkedin_state", lambda *a, **k: next(calls, None) or real(*a, **k)
    )
    result = CliRunner().invoke(cli, ["automation", "linkedin", "automatic"])
    assert result.exit_code == 1 and "run it again" in result.output
    assert _linkedin(cli_db).position is Position.MANUAL


def test_gmail_refuses_a_move_that_became_more_since_it_read(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_scope(cli_db, write=True) as session:
        make_mailbox(session, _user(session), armed_at=NOW, message_id_verified_at=NOW)
    real = automation.gmail_state
    seen = automation.GmailAutomation(Position.AUTOMATIC, True, None, None)
    calls = iter([seen])
    monkeypatch.setattr(
        automation, "gmail_state", lambda *a, **k: next(calls, None) or real(*a, **k)
    )
    result = CliRunner().invoke(cli, ["automation", "gmail", "automatic"])
    assert result.exit_code == 1 and "confirm" in result.output
    assert _arm(cli_db) is MailboxArm.DRAFT
