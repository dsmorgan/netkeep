"""``/api/v1/history/*`` (#601): the old-tool history import from the UI, against the same
services as the CLI. Gmail is a :class:`FakeGmail` set as ``app.state.gmail_opener``;
every person is invented."""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

import factories
import httpx
import pytest
from campaign_fakes import make_mailbox
from fastapi import FastAPI
from history_fixtures import Tab, workbook_bytes
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns.gmail import GmailRateLimited
from netkeeper.campaigns.gmail_fake import FakeGmail
from netkeeper.db import session_scope
from netkeeper.models import (
    Contact,
    HistoryCampaign,
    HistoryRecipient,
    Interaction,
    User,
    UserKind,
)
from netkeeper.scoping import scoped_count
from netkeeper.services import history_scan

CSRF = {"X-Netkeeper-Client": "1"}
ME = "me@example.test"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
COUNTED = (HistoryCampaign, HistoryRecipient, Interaction, Contact)


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


def _counts(app: FastAPI) -> dict[str, int]:
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory) as session:
        user = _local(session)
        return {m.__name__: session.scalar(scoped_count(user, m)) or 0 for m in COUNTED}


@pytest.fixture
def gmail(running_app: FastAPI) -> FakeGmail:
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        factories.make_contact(session, user, emails=["ada@example.test"])
        box = make_mailbox(session, user, email=ME)
        session.flush()
        mailbox_id = box.id
    fake = FakeGmail(ME, mailbox_id=mailbox_id, clock=lambda: datetime(2026, 10, 3, tzinfo=UTC))
    reply = EmailMessage()
    reply["From"] = "ada@example.test"
    reply["To"] = ME
    reply["Subject"] = "Re: Catching up"
    reply.set_content("Good to hear from you.")
    fake.deliver(reply, at=datetime(2026, 3, 4, 15, 0, tzinfo=UTC))
    running_app.state.gmail_opener = lambda user_id, mid: fake
    return fake


async def _import(
    client: httpx.AsyncClient, data: bytes | None = None, **query: Any
) -> httpx.Response:
    data = workbook_bytes(Tab()) if data is None else data
    return await client.post(
        "/api/v1/history/import",
        params=query,
        files={"file": ("old.xlsx", data, XLSX)},
        headers=CSRF,
    )


async def _scan(client: httpx.AsyncClient, **body: Any) -> httpx.Response:
    return await client.post("/api/v1/history/scan-gmail", json=body, headers=CSRF)


async def test_import_is_a_dry_run_unless_asked_to_apply(
    running_app: FastAPI, client: httpx.AsyncClient, gmail: FakeGmail
) -> None:
    before = _counts(running_app)

    dry = await _import(client)

    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert body["applied"] is False
    assert body["campaigns"][0]["matched"] == 1
    assert body["new_campaigns"] == 1
    assert _counts(running_app) == before

    applied = await _import(client, apply=True)

    assert applied.json()["applied"] is True
    after = _counts(running_app)
    assert after["HistoryCampaign"] == before["HistoryCampaign"] + 1
    assert after["Interaction"] == before["Interaction"] + 1
    again = await _import(client, apply=True)
    assert again.json()["new_interactions"] == 0  # re-running adds nothing twice
    assert _counts(running_app) == after


async def test_import_refuses_a_file_that_is_not_a_workbook(client: httpx.AsyncClient) -> None:
    response = await _import(client, b"not a workbook")
    assert response.status_code == 422
    assert "xlsx" in response.json()["detail"]


async def test_scan_dry_run_reads_gmail_read_only_and_writes_nothing(
    running_app: FastAPI, client: httpx.AsyncClient, gmail: FakeGmail
) -> None:
    await _import(client, apply=True)
    before = _counts(running_app)

    response = await _scan(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is False
    assert body["flagged"] == 1
    assert body["samples"] == [{"kind": "reply", "addresses": ["ada@example.test"]}]
    assert _counts(running_app) == before
    assert {method for method, _ in gmail.calls} <= history_scan.READ_ONLY_METHODS


async def test_scan_apply_writes_then_has_nothing_left_to_scan(
    running_app: FastAPI, client: httpx.AsyncClient, gmail: FakeGmail
) -> None:
    await _import(client, apply=True)
    before = _counts(running_app)

    first = await _scan(client, apply=True, mailbox=ME)

    assert first.json()["applied"] is True
    assert _counts(running_app)["Interaction"] == before["Interaction"] + 1
    calls = len(gmail.calls)
    again = (await _scan(client, apply=True)).json()
    assert again["nothing_to_scan"] is True
    assert len(gmail.calls) == calls  # the second run had nothing to ask Gmail


async def test_scan_stopped_by_a_rate_limit_reports_how_to_resume(
    client: httpx.AsyncClient, gmail: FakeGmail
) -> None:
    await _import(client, apply=True)
    gmail.fail_next("messages.list", GmailRateLimited("429", code="rateLimitExceeded"))

    response = await _scan(client, apply=True)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stopped"] == "rateLimitExceeded"
    assert body["remaining"] > 0


async def test_scan_names_a_missing_mailbox(client: httpx.AsyncClient, gmail: FakeGmail) -> None:
    response = await _scan(client, mailbox="other@example.test")
    assert response.status_code == 409
    assert "no mailbox other@example.test" in response.json()["detail"]


async def test_scan_without_any_mailbox_is_409(client: httpx.AsyncClient) -> None:
    response = await _scan(client)
    assert response.status_code == 409
