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
import keyring
import pytest
from campaign_fakes import make_mailbox
from fastapi import FastAPI
from isolation.harness import acting_as
from sqlalchemy.orm import Session, sessionmaker
from test_login_item import FakeLaunchctl, make

from netkeep.crm.lists import create_list
from netkeep.crm.self_contact import update_self_contact
from netkeep.db import session_scope
from netkeep.linkedin.classify import Outcome
from netkeep.models import (
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
from netkeep.scoping import get_scoped_contact, scoped_contacts
from netkeep.services import keychain
from netkeep.services import setup as service
from netkeep.services.linkedin_accounts import arm_scheduled_runs, ensure_account
from netkeep.services.linkedin_session import flag_session, record_session_evidence
from netkeep.services.login_item import LoginItem
from netkeep.services.login_item import State as LoginItemState
from netkeep.services.login_item import Status as LoginItemStatus
from netkeep.services.settings_kv import get_setting, set_setting

URL = "/api/v1/setup"
CSRF = {"X-Netkeep-Client": "1"}
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
            rows = service.steps(session, user, readings or service.Readings())
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
        "Waits for Start netkeep's Chrome and sign in to LinkedIn."
    )
    assert steps[K.CAMPAIGN].reason == (
        "Waits for Import contacts and one of Connect Gmail or Start netkeep's Chrome"
        " and sign in to LinkedIn."
    )
    assert all(row.reason for row in steps.values())
    assert not service.complete(list(steps.values()))


def test_a_finished_install(db: Db, tmp_path: Path) -> None:
    item = make(FakeLaunchctl(), tmp_path, tmp_path / "data", lock_held=True)
    item.enable()
    with db.writer() as (session, user):
        _finish_everything(session, user)
    steps = db.states(service.Readings(login_item=item.status()))
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


def test_an_open_draft_with_contacts_is_a_note_not_a_gate(db: Db) -> None:
    """Three contacts and a draft left in preview: import is done, and nothing waits on it."""
    with db.writer() as (session, user):
        for _ in range(3):
            factories.make_contact(session, user)
        session.add(ImportRun(user_id=user.id, filename="people.csv", status=ImportStatus.DRAFT))
    steps = db.states()
    assert steps[K.IMPORT].state is S.DONE
    assert steps[K.IMPORT].reason == "3 contacts imported. 1 draft import waiting to be finished."
    assert steps[K.TRIAGE].state is S.TODO
    assert steps[K.BUILD_LIST].state is S.TODO
    with db.writer() as (session, user):
        make_mailbox(session, user)
    assert db.states()[K.CAMPAIGN].state is S.TODO


def test_a_draft_with_no_contacts_is_in_progress(db: Db) -> None:
    with db.writer() as (session, user):
        session.add(ImportRun(user_id=user.id, filename="people.csv", status=ImportStatus.DRAFT))
    step = db.states()[K.IMPORT]
    assert (step.state, step.reason) == (S.IN_PROGRESS, "1 draft import waiting to be finished.")
    assert step.next
    assert db.states()[K.TRIAGE].state is S.BLOCKED


@pytest.mark.parametrize(
    ("arrange", "state"),
    [
        (lambda s, u: None, S.TODO),
        (
            lambda s, u: set_setting(s, u, "gmail.setup", {"project_id": "quorbelle-1"}),
            S.IN_PROGRESS,
        ),
        (lambda s, u: make_mailbox(s, u, status=MailboxStatus.REAUTH_REQUIRED), S.IN_PROGRESS),
        (lambda s, u: make_mailbox(s, u, status=MailboxStatus.DISABLED), S.TODO),
        (lambda s, u: make_mailbox(s, u), S.DONE),
    ],
)
def test_gmail(db: Db, arrange: Any, state: S) -> None:
    """Done once a mailbox is connected; arming it to send is a decision of its own."""
    with db.writer() as (session, user):
        arrange(session, user)
    assert db.states()[K.GMAIL].state is state


def test_gmail_never_reads_the_keychain(db: Db, monkeypatch: pytest.MonkeyPatch) -> None:
    """The OAuth client is a secret: a saved one is not a signal, and nothing fetches it."""

    def refused(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("setup must not read a secret from the Keychain")

    monkeypatch.setattr(keychain, "get_secret", refused)
    monkeypatch.setattr(keychain, "get_install_secret", refused)
    monkeypatch.setattr(keychain, "_get", refused)
    monkeypatch.setattr(keyring, "get_password", refused)
    monkeypatch.setattr(keyring, "get_credential", refused)
    assert db.states()[K.GMAIL].state is S.TODO


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


def _status(**fields: Any) -> LoginItemStatus:
    values: dict[str, Any] = {
        "supported": True,
        "reason": None,
        "label": "fun.tnkr.netkeep",
        "plist_path": Path("/nowhere/fun.tnkr.netkeep.plist"),
        "log_path": Path("/nowhere/netkeep.log"),
        "installed": True,
        "disabled": False,
        "loaded": False,
        "this_serve_is_the_agent": False,
        "plist_executable": "/nowhere/netkeep",
        "stale": False,
        "legacy": False,
    }
    values.update(fields)
    return LoginItemStatus(**values)


@pytest.mark.parametrize(
    ("fields", "shown", "state", "says"),
    [
        ({"installed": False}, LoginItemState.OFF, S.TODO, "Turn on Run at login"),
        ({}, LoginItemState.REGISTERED, S.DONE, "Registered"),
        ({"loaded": True}, LoginItemState.RUNNING, S.DONE, "Running from login agent"),
        (
            {"stale": True, "loaded": True},
            LoginItemState.DIFFERENT_NETKEEP,
            S.TODO,
            "Points at a different netkeep",
        ),
        ({"legacy": True}, LoginItemState.NEEDS_REPAIR, S.TODO, "Needs repair"),
    ],
)
def test_the_login_item_is_done_only_registered_for_this_netkeep(
    db: Db, fields: dict[str, Any], shown: LoginItemState, state: S, says: str
) -> None:
    status = _status(**fields)
    assert status.state is shown
    step = db.states(service.Readings(login_item=status))[K.LOGIN_ITEM]
    assert (step.state, step.optional) == (state, True)
    assert says in step.reason


def test_the_login_item_unsupported_or_unreadable(db: Db, tmp_path: Path) -> None:
    item = make(FakeLaunchctl(), tmp_path, tmp_path / "data", lock_held=True)
    off = db.states(service.Readings(login_item=item.status()))[K.LOGIN_ITEM]
    assert off.state is S.TODO
    other = make(FakeLaunchctl(), tmp_path, tmp_path / "data", macos=False)
    unsupported = db.states(service.Readings(login_item=other.status()))
    assert "macOS" in unsupported[K.LOGIN_ITEM].reason
    unreadable = db.states(service.Readings(login_item_error="launchctl refused"))
    assert "launchctl refused" in unreadable[K.LOGIN_ITEM].reason


def test_next_skips_over_a_skipped_step(db: Db) -> None:
    """The first step that isn't done is skipped, so the next one to do is next."""
    with db.writer() as (session, user):
        service.skip(session, user, "about_you")
    steps = db.states()
    assert (steps[K.ABOUT_YOU].state, steps[K.ABOUT_YOU].next) == (S.SKIPPED, False)
    assert [key for key, row in steps.items() if row.next] == [K.IMPORT]


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


async def test_a_skip_without_the_csrf_header_is_refused(
    running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _install_login_item(running_app, tmp_path)
    for method in ("POST", "DELETE"):
        response = await client.request(method, f"{URL}/steps/gmail/skip")
        assert response.status_code == 403
    gmail = next(s for s in (await client.get(URL)).json()["steps"] if s["key"] == "gmail")
    assert gmail["skipped"] is False


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
    """Chrome's lock file is a file read and the Keychain is never asked for a secret.
    The login item's status is the only process (here a fake launchctl; in a real serve,
    ``launchctl print``, read-only): no socket, no browser, no Gmail."""
    _install_login_item(running_app, tmp_path)

    def refused(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("GET /setup must not connect or start a process")

    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(subprocess, "Popen", refused)
    monkeypatch.setattr(subprocess, "run", refused)
    # A user secret (the Gmail OAuth client, a token) is never fetched for setup.
    monkeypatch.setattr(keychain, "get_secret", refused)
    assert (await client.get(URL)).status_code == 200
