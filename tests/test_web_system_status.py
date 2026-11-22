"""``GET /system-status`` (#761): its shape, that it writes nothing, its scoping, and the
fields the menu bar's actions need besides the chips."""

# The UI's words, typographic apostrophes included.
# ruff: noqa: RUF001

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from netkeep.db import session_scope
from netkeep.linkedin.classify import Outcome
from netkeep.models import SyncRunKind, SyncRunTrigger, User, UserKind
from netkeep.models.base import utcnow
from netkeep.services import mailboxes as mailbox_service
from netkeep.services import runs
from netkeep.services.events import Event
from netkeep.services.linkedin_accounts import ensure_account
from netkeep.services.linkedin_session import flag_session

PATH = "/api/v1/system-status"


class _Writes:
    """Every statement that would change the database."""

    def __init__(self, engine: Engine) -> None:
        self.statements: list[str] = []
        self.engine = engine
        event.listen(engine, "before_cursor_execute", self._record)

    def _record(self, _conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
            self.statements.append(statement)

    def close(self) -> None:
        event.remove(self.engine, "before_cursor_execute", self._record)


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


def _face(chip: dict[str, Any]) -> str:
    fact = "" if chip["fact"] == "" else f" · {chip['fact']}"
    return f"{chip['name']} · {chip['word']}{fact}"


async def test_all_well_reads_three_chips_and_writes_nothing(
    app: FastAPI, client: httpx.AsyncClient, bare_engine: Engine
) -> None:
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        ensure_account(session, _local(session))

    writes = _Writes(bare_engine)
    try:
        response = await client.get(PATH)
    finally:
        writes.close()

    assert response.status_code == 200
    assert writes.statements == []
    body = response.json()
    assert set(body) == {
        "checked_at",
        "gmail",
        "linkedin",
        "netkeep",
        "linkedin_run",
        "campaign_sending",
        "update_available",
    }
    assert [body[key]["system"] for key in ("gmail", "linkedin", "netkeep")] == [
        "gmail",
        "linkedin",
        "netkeep",
    ]
    assert _face(body["gmail"]) == "Gmail · Not set up"
    assert body["gmail"]["target"] == {"to": "/settings", "hash": "gmail"}
    assert body["linkedin"]["parts"][0]["key"] == "session"
    assert body["linkedin_run"] is None
    assert body["campaign_sending"] is False
    assert body["update_available"] is None
    # Not serve: nothing runs the background checks.
    assert _face(body["netkeep"]) == "netkeep · No background checks · serve isn’t running"
    assert body["netkeep"]["page"] == "Settings › Support"


async def test_times_come_back_absolute_with_their_style(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    started = datetime(2026, 10, 21, 11, 57, tzinfo=UTC)
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        ensure_account(session, user)
        run = runs.create_run(
            session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=started
        )
        run_id = run.id

    body = (await client.get(PATH)).json()

    linkedin = body["linkedin"]
    running = next(part for part in linkedin["parts"] if part["key"] == "running")
    assert running["fact"] == "started {run_started}"
    assert running["detail"] == "Enrichment · started {run_started}"
    stamp = linkedin["times"]["run_started"]
    assert stamp["style"] == "ago"
    assert datetime.fromisoformat(stamp["at"]) == started  # UTC, with its offset
    assert body["linkedin_run"] == {
        "id": run_id,
        "kind": "enrich",
        "label": "Enrichment",
        "started_at": stamp["at"],
    }


async def test_live_progress_and_a_send_batch_come_from_this_processs_events(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        ensure_account(session, user)
        run = runs.create_run(
            session, user, SyncRunKind.CONNECTIONS_FULL, trigger=SyncRunTrigger.MANUAL, now=utcnow()
        )
        run_id, user_id = run.id, user.id
    bus = app.state.bus
    bus.publish(
        Event(
            type="run.progress",
            data={"run_id": run_id, "connections": 340, "total": 626},
            user_id=user_id,
        )
    )
    bus.publish(Event(type="campaign.tick", data={"sent": 1}, user_id=user_id))
    # Another user's are not this one's.
    bus.publish(Event(type="campaign.tick", data={"sent": 1}, user_id=user_id + 1))

    body = (await client.get(PATH)).json()

    assert _face(body["linkedin"]) == "LinkedIn · Full sync running · 340 of 626"
    assert _face(body["gmail"]) == "Gmail · Sending now · campaign batch"


async def test_sending_and_the_keychain(app: FastAPI, client: httpx.AsyncClient) -> None:
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        ensure_account(session, user)
    app.state.campaign_engine = SimpleNamespace(busy=True, sender=None)
    app.state.local_api_key.persisted = False

    body = (await client.get(PATH)).json()

    assert "linkedin_paused" not in body  # gone with the pause (#834)
    assert body["campaign_sending"] is True
    assert _face(body["netkeep"]) == "netkeep · Keychain refused · key in memory"


async def test_another_users_state_never_shows(app: FastAPI, client: httpx.AsyncClient) -> None:
    """The endpoint's two-user test from the local user's side; ``test_isolation`` has both."""
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        ensure_account(session, _local(session))
        other = factories.make_user(session, UserKind.HOSTED)
        ensure_account(session, other)
        flag_session(session, other, Outcome.CHECKPOINT, url="/checkpoint/challenge")
        runs.create_run(
            session, other, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=utcnow()
        )
        mailbox_service.connect(session, other, "other@example.test", "rt-other", daily_cap=80)

    body = (await client.get(PATH)).json()

    assert _face(body["gmail"]) == "Gmail · Not set up"
    session_part = body["linkedin"]["parts"][0]
    assert (session_part["key"], session_part["tone"]) == ("session", "ok")
    assert body["linkedin_run"] is None


async def test_another_users_events_never_show(app: FastAPI, client: httpx.AsyncClient) -> None:
    """Only another user's ``campaign.tick`` and ``run.progress``: neither shows here."""
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        ensure_account(session, user)
        run = runs.create_run(
            session, user, SyncRunKind.CONNECTIONS_FULL, trigger=SyncRunTrigger.MANUAL, now=utcnow()
        )
        run_id, other_id = run.id, user.id + 1
    bus = app.state.bus
    # The other user's event names this user's run id: only the user can tell them apart.
    progress = {"run_id": run_id, "connections": 340, "total": 626}
    bus.publish(Event(type="run.progress", data=progress, user_id=other_id))
    bus.publish(Event(type="campaign.tick", data={"sent": 1}, user_id=other_id))

    body = (await client.get(PATH)).json()

    assert _face(body["linkedin"]) == "LinkedIn · Full sync running · started {run_started}"
    assert "batch" not in {part["key"] for part in body["gmail"]["parts"]}


async def test_linkedin_reads_its_switch_position_with_or_without_serve(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    """#829: on Manual the pill says Manual, not "Not running" or "Off", whether serve's
    scheduler runs or not; on Off it says Off, and its inbox check says why."""
    from netkeep.services import automation

    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        ensure_account(session, _local(session))

    for scheduler in (None, SimpleNamespace()):
        running_app.state.scheduler = scheduler
        body = (await client.get(PATH)).json()
        assert _face(body["linkedin"]) == "LinkedIn · Manual · you start runs"
        assert body["linkedin"]["tone"] == "ok"
        inbox = next(p for p in body["linkedin"]["parts"] if p["key"] == "inbox")
        assert (inbox["tone"], inbox["word"]) == ("ok", "When you start it")

    with session_scope(factory, write=True) as session:
        automation.set_linkedin(
            session, _local(session), automation.Position.OFF, confirm=False, now=utcnow()
        )
    body = (await client.get(PATH)).json()
    assert _face(body["linkedin"]) == "LinkedIn · Off · runs don’t start"
    inbox = next(p for p in body["linkedin"]["parts"] if p["key"] == "inbox")
    assert "LinkedIn is Off; move it to Manual or Automatic" in inbox["detail"]
    running_app.state.scheduler = None


class _Engine:
    """Stands in for serve's campaign engine: the status reads its sender and busy flag."""

    def __init__(self, sender: object) -> None:
        self.sender = sender
        self.busy = False


def _no_gmail(user_id: int, mailbox_id: int) -> Any:
    raise AssertionError("a status read opened Gmail")


@pytest.mark.parametrize("serving", [False, True], ids=["no-serve", "serve"])
@pytest.mark.parametrize(
    ("position", "linkedin_word", "gmail_word"),
    [
        ("off", "Off", "Off"),
        ("manual", "Manual", "Drafts only"),
        ("automatic", "Automatic", "Automatic"),
    ],
)
async def test_each_pill_says_its_position_with_or_without_serve(
    running_app: FastAPI,
    client: httpx.AsyncClient,
    serving: bool,
    position: str,
    linkedin_word: str,
    gmail_word: str,
) -> None:
    """#829 review: the position leads, not "Not running" or "Off", with a realistic
    inbox poll behind it; only LinkedIn Automatic without a scheduler says so."""
    from campaign_fakes import make_mailbox

    from netkeep.models import SyncRunStatus
    from netkeep.services import automation
    from netkeep.services.campaign_sender import GmailSender

    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        make_mailbox(session, user, message_id_verified_at=utcnow())
        inbox = runs.create_run(
            session, user, SyncRunKind.INBOX, trigger=SyncRunTrigger.MANUAL, now=utcnow()
        )
        runs.finish_run(session, user, inbox.id, status=SyncRunStatus.COMPLETED, now=utcnow())
        to = automation.Position(position)
        automation.set_linkedin(session, user, to, confirm=True, now=utcnow())
        automation.set_gmail(session, user, to, confirm=True, by="test", now=utcnow())
    if serving:
        running_app.state.scheduler = SimpleNamespace()
        running_app.state.campaign_engine = _Engine(GmailSender(factory, opener=_no_gmail))
    try:
        body = (await client.get(PATH)).json()
    finally:
        running_app.state.scheduler = None
        running_app.state.campaign_engine = None

    expected_linkedin = linkedin_word
    if position == "automatic" and not serving:
        expected_linkedin = "No scheduler"  # a real problem, so it still leads
    assert body["linkedin"]["word"] == expected_linkedin
    if position == "automatic" and not serving:
        # Set to send, and nothing does: it takes over, like No scheduler.
        assert _face(body["gmail"]) == "Gmail · Not running · nothing sends"
        assert body["gmail"]["tone"] == "warn"
    else:
        assert body["gmail"]["word"] == gmail_word
        assert body["gmail"]["tone"] == ("off" if position == "off" else "ok")
