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
from netkeep.services.linkedin_accounts import ensure_account, pause_schedule
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
        "linkedin_paused",
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
    assert body["linkedin_paused"] is False
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


async def test_paused_sending_and_the_keychain(app: FastAPI, client: httpx.AsyncClient) -> None:
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        ensure_account(session, user)
        pause_schedule(session, user, now=utcnow())
    app.state.campaign_engine = SimpleNamespace(busy=True, sender=None)
    app.state.local_api_key.persisted = False

    body = (await client.get(PATH)).json()

    assert body["linkedin_paused"] is True
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
        pause_schedule(session, other, now=utcnow())
        runs.create_run(
            session, other, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=utcnow()
        )
        mailbox_service.connect(session, other, "other@example.test", "rt-other", daily_cap=80)

    body = (await client.get(PATH)).json()

    assert _face(body["gmail"]) == "Gmail · Not set up"
    session_part = body["linkedin"]["parts"][0]
    assert (session_part["key"], session_part["tone"]) == ("session", "ok")
    assert body["linkedin_paused"] is False
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
