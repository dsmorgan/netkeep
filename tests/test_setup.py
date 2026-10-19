"""``/setup`` (#651, #667): each step's state from stored data, the gating, skips, auth."""

from __future__ import annotations

import socket
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import factories
import httpx
import pytest
from campaign_fakes import make_mailbox
from fastapi import FastAPI
from isolation.harness import acting_as
from sqlalchemy.orm import Session, sessionmaker
from test_login_item import FakeLaunchctl, make

from netkeeper.campaigns.gmail_oauth import OAuthClient
from netkeeper.crm.lists import create_list
from netkeeper.crm.self_contact import update_self_contact
from netkeeper.db import session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.models import (
    CampaignStatus,
    ContactMet,
    ImportRun,
    ImportStatus,
    ListKind,
    MailboxStatus,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    User,
    UserKind,
)
from netkeeper.scoping import get_scoped_contact, scoped_contacts
from netkeeper.services import mailboxes
from netkeeper.services import setup as service
from netkeeper.services.linkedin_accounts import arm_scheduled_runs, ensure_account
from netkeeper.services.linkedin_session import flag_session, record_session_evidence
from netkeeper.services.login_item import LoginItem
from netkeeper.services.settings_kv import get_setting, set_setting

URL = "/api/v1/setup"
CSRF = {"X-Netkeeper-Client": "1"}
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
S = service.StepState
K = service.StepKey


class Db:
    """One user's database, for the service tests: a writer session and the steps."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory
        with session_scope(factory, write=True) as session:
            self.user_id = factories.make_user(session).id

    @contextmanager
    def writer(self) -> Iterator[tuple[Session, User]]:
        with session_scope(self.factory, write=True) as session:
            yield session, session.get_one(User, self.user_id)

    def states(self, readings: service.Readings | None = None) -> dict[K, service.Step]:
        with self.writer() as (session, user):
            rows = service.steps(session, user, readings or service.Readings(oauth_client=False))
        return {row.key: row for row in rows}


@pytest.fixture
def db(session_factory: sessionmaker[Session]) -> Db:
    return Db(session_factory)


def _run(session: Session, user: User, kind: SyncRunKind, status: SyncRunStatus) -> None:
    account = ensure_account(session, user)
    session.add(
        SyncRun(
            user_id=user.id,
            linkedin_account_id=account.id,
            kind=kind,
            status=status,
            trigger=SyncRunTrigger.MANUAL,
            started_at=NOW,
            completed_at=None if status is SyncRunStatus.RUNNING else NOW,
            counts_json={"session_flagged": False, "connections": 3},
        )
    )
    session.flush()


def _finish_everything(session: Session, user: User) -> None:
    update_self_contact(session, user, {"first_name": "Quorbelle", "last_name": "Fictionary"})
    for _ in range(2):
        factories.make_contact(session, user, met=ContactMet.MET)
    make_mailbox(session, user)
    record_session_evidence(session, user, logged_in=True, source="preflight", now=NOW)
    _run(session, user, SyncRunKind.CONNECTIONS_FULL, SyncRunStatus.COMPLETED)
    _run(session, user, SyncRunKind.INBOX, SyncRunStatus.COMPLETED)
    create_list(session, user, "Old colleagues", ListKind.STATIC)
    factories.make_campaign(session, user, status=CampaignStatus.ACTIVE)
    arm_scheduled_runs(session, user, now=NOW)


# --- the graph and the keys --------------------------------------------------------------


def test_the_graph_is_pinned() -> None:
    graph = {
        step.key.value: (
            [k.value for k in step.needs],
            [k.value for k in step.needs_any],
            step.optional,
        )
        for step in service.STEPS
    }
    assert graph == {
        "about_you": ([], [], False),
        "import": ([], [], False),
        "gmail": ([], [], False),
        "chrome": ([], [], False),
        "first_sync": (["chrome"], [], False),
        "first_inbox": (["chrome"], [], False),
        "triage": (["import"], [], False),
        "build_list": (["import"], [], False),
        "campaign": (["import"], ["gmail", "chrome"], False),
        "arm_schedule": (["first_sync"], [], False),
        "login_item": ([], [], True),
    }
    assert service.SKIPPED_KEY == "setup.skipped"


def test_every_target_is_a_page() -> None:
    assert {step.target for step in service.STEPS} <= {
        "/settings",
        "/imports",
        "/linkedin",
        "/triage",
        "/lists",
        "/campaigns",
    }


# --- a fresh install and a finished one ----------------------------------------------------


def test_a_fresh_install(db: Db) -> None:
    steps = db.states()
    assert {key: row.state for key, row in steps.items()} == {
        K.ABOUT_YOU: S.TODO,
        K.IMPORT: S.TODO,
        K.GMAIL: S.TODO,
        K.CHROME: S.TODO,
        K.FIRST_SYNC: S.BLOCKED,
        K.FIRST_INBOX: S.BLOCKED,
        K.TRIAGE: S.BLOCKED,
        K.BUILD_LIST: S.BLOCKED,
        K.CAMPAIGN: S.BLOCKED,
        K.ARM_SCHEDULE: S.BLOCKED,
        K.LOGIN_ITEM: S.TODO,
    }
    # Only the first step to do is next on a fresh install.
    assert [key for key, row in steps.items() if row.next] == [K.ABOUT_YOU]
    assert steps[K.FIRST_SYNC].reason == (
        "Waits for Start netkeeper's Chrome and sign in to LinkedIn."
    )
    assert steps[K.CAMPAIGN].reason == (
        "Waits for Import contacts and one of Connect Gmail or Start netkeeper's Chrome"
        " and sign in to LinkedIn."
    )
    assert all(row.reason for row in steps.values())
    assert not service.complete(list(steps.values()))


def test_a_finished_install(db: Db, tmp_path: Path) -> None:
    item = make(FakeLaunchctl(), tmp_path, tmp_path / "data", lock_held=True)
    item.enable()
    with db.writer() as (session, user):
        _finish_everything(session, user)
    steps = db.states(service.Readings(oauth_client=True, login_item=item.status()))
    assert {row.state for row in steps.values()} == {S.DONE}
    assert service.complete(list(steps.values()))
    assert not any(row.next for row in steps.values())


def test_the_built_in_list_is_not_a_list_you_built(
    db: Db,
) -> None:
    with db.writer() as (session, user):
        factories.make_contact(session, user)
        row = create_list(session, user, "Validated", ListKind.STATIC)
        row.builtin = True
    assert db.states()[K.BUILD_LIST].state is S.TODO


# --- each step's in-between states ---------------------------------------------------------


def test_about_you_needs_a_first_name(db: Db) -> None:
    with db.writer() as (session, user):
        update_self_contact(session, user, {"current_company": "Fictionary Works"})
    assert db.states()[K.ABOUT_YOU].state is S.TODO
    with db.writer() as (session, user):
        update_self_contact(session, user, {"first_name": "Quorbelle"})
    assert db.states()[K.ABOUT_YOU].state is S.DONE


def test_a_draft_import_is_in_progress(db: Db) -> None:
    with db.writer() as (session, user):
        factories.make_contact(session, user)
        session.add(ImportRun(user_id=user.id, filename="people.csv", status=ImportStatus.DRAFT))
    step = db.states()[K.IMPORT]
    assert step.state is S.IN_PROGRESS
    assert step.reason == "1 draft import waiting to be finished."
    assert step.next


@pytest.mark.parametrize(
    ("arrange", "oauth", "state"),
    [
        (lambda s, u: None, False, S.TODO),
        (lambda s, u: None, None, S.TODO),
        (lambda s, u: None, True, S.IN_PROGRESS),
        (
            lambda s, u: set_setting(s, u, "gmail.setup", {"project_id": "quorbelle-1"}),
            False,
            S.IN_PROGRESS,
        ),
        (
            lambda s, u: make_mailbox(s, u, status=MailboxStatus.REAUTH_REQUIRED),
            True,
            S.IN_PROGRESS,
        ),
        (lambda s, u: make_mailbox(s, u, status=MailboxStatus.DISABLED), False, S.TODO),
        (lambda s, u: make_mailbox(s, u), False, S.DONE),
    ],
)
def test_gmail(db: Db, arrange: Any, oauth: bool | None, state: S) -> None:
    with db.writer() as (session, user):
        arrange(session, user)
    step = db.states(service.Readings(oauth_client=oauth))[K.GMAIL]
    assert step.state is state
    if oauth is None:
        assert "Keychain" in step.reason


def test_chrome_from_the_lock_file_and_the_session_evidence(
    db: Db,
) -> None:
    assert db.states()[K.CHROME].state is S.TODO
    running = db.states(service.Readings(chrome_pid=4242))[K.CHROME]
    assert running.state is S.IN_PROGRESS
    assert "running" in running.reason
    with db.writer() as (session, user):
        record_session_evidence(session, user, logged_in=False, source="preflight", now=NOW)
    assert db.states()[K.CHROME].state is S.IN_PROGRESS
    with db.writer() as (session, user):
        record_session_evidence(session, user, logged_in=True, source="preflight", now=NOW)
    steps = db.states()
    assert steps[K.CHROME].state is S.DONE
    assert steps[K.FIRST_SYNC].state is S.TODO
    assert steps[K.FIRST_INBOX].state is S.TODO
    with db.writer() as (session, user):
        flag_session(session, user, Outcome.CHECKPOINT, url="/checkpoint")
    flagged = db.states()
    assert flagged[K.CHROME].state is S.IN_PROGRESS
    assert "checkpoint" in flagged[K.CHROME].reason
    assert flagged[K.FIRST_SYNC].state is S.BLOCKED


def test_a_run_that_read_linkedin_is_sign_in_evidence(
    db: Db,
) -> None:
    with db.writer() as (session, user):
        _run(session, user, SyncRunKind.CONNECTIONS_FULL, SyncRunStatus.COMPLETED)
    steps = db.states()
    assert steps[K.CHROME].state is S.DONE
    assert steps[K.FIRST_SYNC].state is S.DONE
    assert steps[K.ARM_SCHEDULE].state is S.TODO


@pytest.mark.parametrize("kind", [SyncRunKind.CONNECTIONS_FULL, SyncRunKind.INBOX])
def test_a_running_run_is_in_progress(db: Db, kind: SyncRunKind) -> None:
    with db.writer() as (session, user):
        record_session_evidence(session, user, logged_in=True, source="preflight", now=NOW)
        _run(session, user, kind, SyncRunStatus.RUNNING)
    key = K.FIRST_SYNC if kind is SyncRunKind.CONNECTIONS_FULL else K.FIRST_INBOX
    step = db.states()[key]
    assert step.state is S.IN_PROGRESS
    assert step.next


def test_an_incremental_sync_is_not_the_first_full_one(
    db: Db,
) -> None:
    with db.writer() as (session, user):
        record_session_evidence(session, user, logged_in=True, source="preflight", now=NOW)
        _run(session, user, SyncRunKind.CONNECTIONS_INCREMENTAL, SyncRunStatus.COMPLETED)
        _run(session, user, SyncRunKind.CONNECTIONS_FULL, SyncRunStatus.FAILED)
    assert db.states()[K.FIRST_SYNC].state is S.TODO


def test_triage(db: Db) -> None:
    with db.writer() as (session, user):
        first = factories.make_contact(session, user)
        factories.make_contact(session, user)
    assert db.states()[K.TRIAGE].state is S.TODO
    with db.writer() as (session, user):
        contact = get_scoped_contact(session, user, first.id)
        assert contact is not None
        contact.met = ContactMet.SKIP
    step = db.states()[K.TRIAGE]
    assert (step.state, step.reason) == (S.IN_PROGRESS, "1 of 2 triaged.")
    with db.writer() as (session, user):
        factories.make_contact(session, user, met=ContactMet.NOT_MET)
        for contact in session.scalars(scoped_contacts(user)):
            if contact.met is ContactMet.UNKNOWN:
                contact.met = ContactMet.MET
    assert db.states()[K.TRIAGE].state is S.DONE


def test_a_draft_campaign_is_in_progress(db: Db) -> None:
    with db.writer() as (session, user):
        factories.make_contact(session, user)
        make_mailbox(session, user)
        factories.make_campaign(session, user, status=CampaignStatus.DRAFT)
    assert db.states()[K.CAMPAIGN].state is S.IN_PROGRESS


def test_a_campaign_needs_one_channel_not_both(db: Db) -> None:
    with db.writer() as (session, user):
        factories.make_contact(session, user)
    assert db.states()[K.CAMPAIGN].state is S.BLOCKED
    with db.writer() as (session, user):
        record_session_evidence(session, user, logged_in=True, source="preflight", now=NOW)
    assert db.states()[K.CAMPAIGN].state is S.TODO


def test_done_wins_over_its_own_gate(db: Db) -> None:
    """A list built before anything was imported reads done, not blocked."""
    with db.writer() as (session, user):
        create_list(session, user, "Old colleagues", ListKind.STATIC)
    assert db.states()[K.BUILD_LIST].state is S.DONE


def test_the_login_item(db: Db, tmp_path: Path) -> None:
    item = make(FakeLaunchctl(), tmp_path, tmp_path / "data", lock_held=True)
    off = db.states(service.Readings(login_item=item.status()))[K.LOGIN_ITEM]
    assert (off.state, off.optional) == (S.TODO, True)
    other = make(FakeLaunchctl(), tmp_path, tmp_path / "data", macos=False)
    unsupported = db.states(service.Readings(login_item=other.status()))
    assert "macOS" in unsupported[K.LOGIN_ITEM].reason
    unreadable = db.states(service.Readings(login_item_error="launchctl refused"))
    assert "launchctl refused" in unreadable[K.LOGIN_ITEM].reason


# --- skips ---------------------------------------------------------------------------------


def test_a_skip_persists_and_changes_neither_done_nor_blocked(
    db: Db,
) -> None:
    with db.writer() as (session, user):
        service.skip(session, user, "gmail")
        service.skip(session, user, "first_sync")
        service.skip(session, user, "about_you")
        update_self_contact(session, user, {"first_name": "Quorbelle"})
    steps = db.states()
    assert (steps[K.GMAIL].state, steps[K.GMAIL].skipped) == (S.SKIPPED, True)
    assert steps[K.GMAIL].reason.startswith("Skipped. ")
    assert (steps[K.FIRST_SYNC].state, steps[K.FIRST_SYNC].skipped) == (S.BLOCKED, True)
    assert (steps[K.ABOUT_YOU].state, steps[K.ABOUT_YOU].skipped) == (S.DONE, True)
    # A skipped step is not next, and skipping Gmail doesn't satisfy a campaign's channel.
    assert not steps[K.GMAIL].next
    assert steps[K.CAMPAIGN].state is S.BLOCKED
    with db.writer() as (session, user):
        service.unskip(session, user, "gmail")
        assert get_setting(session, user, "setup.skipped") == ["about_you", "first_sync"]
    assert db.states()[K.GMAIL].state is S.TODO


def test_skipping_every_required_step_completes_setup(db: Db) -> None:
    """Blocked steps count once skipped, so a person who skips everything reaches Overview;
    the optional login item never holds it back."""
    with db.writer() as (session, user):
        for definition in service.STEPS:
            if not definition.optional:
                service.skip(session, user, definition.key.value)
    rows = list(db.states().values())
    assert service.complete(rows)
    assert {row.state for row in rows if not row.optional} == {S.SKIPPED, S.BLOCKED}


def test_an_unknown_stored_key_is_ignored(db: Db) -> None:
    with db.writer() as (session, user):
        set_setting(session, user, "setup.skipped", ["gmail", "renamed_step", 7])
        assert service.skipped_keys(session, user) == {K.GMAIL}
        with pytest.raises(service.UnknownStep):
            service.skip(session, user, "renamed_step")


# --- the endpoint --------------------------------------------------------------------------


def _install_login_item(running_app: FastAPI, tmp_path: Path) -> LoginItem:
    item = make(FakeLaunchctl(), tmp_path, tmp_path / "data", lock_held=True)
    running_app.state.login_item = item
    return item


async def test_get_setup(running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path) -> None:
    _install_login_item(running_app, tmp_path)
    response = await client.get(URL)
    assert response.status_code == 200
    assert response.json()["complete"] is False
    steps = response.json()["steps"]
    assert [step["key"] for step in steps] == [key.value for key in K]
    first = steps[0]
    assert set(first) == {
        "key",
        "title",
        "state",
        "next",
        "needs",
        "needs_any",
        "reason",
        "target",
        "optional",
        "skipped",
    }
    campaign = next(step for step in steps if step["key"] == "campaign")
    assert campaign["needs"] == ["import"]
    assert campaign["needs_any"] == ["gmail", "chrome"]
    assert campaign["state"] == "blocked"


async def test_an_oauth_client_in_the_keychain_counts(
    running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _install_login_item(running_app, tmp_path)
    with session_scope(running_app.state.session_factory) as session:
        owner = session.scalars(_owner_query()).one()
    mailboxes.save_client(owner, OAuthClient(client_id="quorbelle.apps", client_secret="x"))
    steps = (await client.get(URL)).json()["steps"]
    gmail = next(step for step in steps if step["key"] == "gmail")
    assert gmail["state"] == "in_progress"


def _owner_query() -> Any:
    from sqlalchemy import select

    return select(User).where(User.kind == UserKind.LOCAL)


async def test_skip_and_unskip_over_http(
    running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _install_login_item(running_app, tmp_path)
    skipped = await client.post(f"{URL}/steps/login_item/skip", headers=CSRF)
    assert skipped.status_code == 200
    item = next(s for s in skipped.json()["steps"] if s["key"] == "login_item")
    assert (item["state"], item["skipped"]) == ("skipped", True)
    again = next(s for s in (await client.get(URL)).json()["steps"] if s["key"] == "login_item")
    assert again["skipped"] is True
    cleared = await client.delete(f"{URL}/steps/login_item/skip", headers=CSRF)
    item = next(s for s in cleared.json()["steps"] if s["key"] == "login_item")
    assert (item["state"], item["skipped"]) == ("todo", False)
    missing = await client.post(f"{URL}/steps/nothing_here/skip", headers=CSRF)
    assert missing.status_code == 404


async def test_a_second_user_gets_403(
    running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _install_login_item(running_app, tmp_path)
    with session_scope(running_app.state.session_factory, write=True) as session:
        other = factories.make_user(session, kind=UserKind.HOSTED).id
    with acting_as(running_app, other):
        assert (await client.get(URL)).status_code == 403
        assert (await client.post(f"{URL}/steps/gmail/skip", headers=CSRF)).status_code == 403
        assert (await client.delete(f"{URL}/steps/gmail/skip", headers=CSRF)).status_code == 403


async def test_without_the_local_key_it_is_refused(running_app: FastAPI, tmp_path: Path) -> None:
    _install_login_item(running_app, tmp_path)
    transport = httpx.ASGITransport(app=running_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as bare:
        assert (await bare.get(URL)).status_code == 401


async def test_reading_setup_touches_no_browser_gmail_or_network(
    running_app: FastAPI,
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chrome's lock file and the plist are files; nothing connects or runs a process."""
    _install_login_item(running_app, tmp_path)

    def refused(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("GET /setup must not connect or start a process")

    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(subprocess, "Popen", refused)
    monkeypatch.setattr(subprocess, "run", refused)
    assert (await client.get(URL)).status_code == 200
