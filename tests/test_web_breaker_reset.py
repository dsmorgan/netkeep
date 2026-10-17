"""``/linkedin``: **Reset breaker** and **Forget inbox owner** (#583).

The web half of ``netkeeper linkedin schedule reset-breaker`` and ``netkeeper linkedin
inbox-forget-owner``. Each calls the command's own service function
(``route_breaker.reset``, ``inbox_apply.forget_owner``), needs ``confirm: true``, acts
for the current user only, and refuses with ``409`` when there is nothing to reset or
forget. Resetting lets scheduled LinkedIn runs start again, so it also needs the set of
tripped breakers the person was shown. The posture rows offer each button only while it
applies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from isolation.harness import acting_as
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_runs_serve import HEADERS

from netkeeper.crm import inbox_apply
from netkeeper.crm.self_contact import ensure_self_contact
from netkeeper.db import session_scope
from netkeeper.models import SyncRunKind, User, UserKind
from netkeeper.services import posture, route_breaker
from netkeeper.services.linkedin_accounts import account_id_for

RESET_URL = "/api/v1/linkedin/schedule/reset-breaker"
FORGET_URL = "/api/v1/linkedin/inbox/forget-owner"
NOW = datetime(2030, 3, 4, 9, 0, tzinfo=UTC)
OWNER = "urn:li:fsd_profile:QUORBELLEINVENTED"


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


def _trip_route_changed(session: Session, user: User) -> None:
    account_id = account_id_for(session, user)
    for _ in range(route_breaker.THRESHOLD):
        route_breaker.record(
            session, user, account_id, route_changed=True, succeeded=False, now=NOW
        )


def _trip_inbox_owner(session: Session, user: User, *, times: int = 2) -> None:
    account_id = account_id_for(session, user)
    for _ in range(times):
        route_breaker.record_inbox_owner(
            session, user, account_id, owner_mismatch=True, completed=False, now=NOW
        )


def _with_local(app: FastAPI, *steps: Any) -> None:
    with session_scope(app.state.session_factory, write=True) as session:
        user = _local(session)
        for step in steps:
            step(session, user)


def _record_owner(session: Session, user: User) -> None:
    assert inbox_apply.owner_matches(session, user, OWNER)


async def _rows(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    report = (await client.get("/api/v1/posture")).json()
    return {row["key"]: row for row in report["protections"] if row["key"] is not None}


def _tripped(app: FastAPI) -> frozenset[str]:
    with session_scope(app.state.session_factory) as session:
        user = _local(session)
        return posture.tripped_breakers(session, user, account_id_for(session, user))


# --- Reset breaker ----------------------------------------------------------------------


async def test_reset_clears_the_tripped_breaker_through_the_cli_service(
    client: httpx.AsyncClient, running_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_local(running_app, _trip_route_changed)
    rows = await _rows(client)
    row = rows["route_changed_breaker"]
    assert row["actions"] == ["reset_breaker"]
    assert "click **Reset breaker** under Posture on the Settings page" in row["warnings"][0]
    assert "`netkeeper linkedin schedule reset-breaker`" in row["warnings"][0]
    shown = [key for key, r in rows.items() if "reset_breaker" in r["actions"]]

    calls: list[int] = []
    real_reset = route_breaker.reset

    def spy(session: Session, user: User, account_id: int) -> route_breaker.BreakerState:
        calls.append(user.id)
        return real_reset(session, user, account_id)

    monkeypatch.setattr(route_breaker, "reset", spy)
    response = await client.post(
        RESET_URL, json={"confirm": True, "tripped": shown}, headers=HEADERS
    )

    assert response.status_code == 200
    assert response.json() == {"cleared": True, "reset": ["route_changed_breaker"]}
    assert len(calls) == 1
    assert _tripped(running_app) == frozenset()
    after = await _rows(client)
    assert after["route_changed_breaker"]["actions"] == []
    assert after["route_changed_breaker"]["warnings"] == []


async def test_reset_refuses_when_nothing_is_tripped(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    # One route_changed run is a count, not a trip: no row offers the button, and the
    # endpoint refuses rather than wiping it.
    def one(session: Session, user: User) -> None:
        route_breaker.record(
            session,
            user,
            account_id_for(session, user),
            route_changed=True,
            succeeded=False,
            now=NOW,
        )

    _with_local(running_app, one)
    rows = await _rows(client)
    assert all("reset_breaker" not in row["actions"] for row in rows.values())

    response = await client.post(RESET_URL, json={"confirm": True, "tripped": []}, headers=HEADERS)

    assert response.status_code == 409
    assert "nothing to reset" in response.json()["detail"]
    with session_scope(running_app.state.session_factory) as session:
        user = _local(session)
        assert route_breaker.state(session, user, account_id_for(session, user)).count == 1


async def test_reset_refuses_without_confirm(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    _with_local(running_app, _trip_route_changed)

    response = await client.post(
        RESET_URL,
        json={"confirm": False, "tripped": ["route_changed_breaker"]},
        headers=HEADERS,
    )

    assert response.status_code == 422
    assert _tripped(running_app) == {"route_changed_breaker"}


async def test_reset_refuses_a_breaker_that_tripped_after_the_person_looked(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    _with_local(running_app, _trip_route_changed)
    shown = ["route_changed_breaker"]
    _with_local(running_app, _trip_inbox_owner)

    response = await client.post(
        RESET_URL, json={"confirm": True, "tripped": shown}, headers=HEADERS
    )

    assert response.status_code == 409
    assert "reload Posture" in response.json()["detail"]
    assert _tripped(running_app) == {"route_changed_breaker", "inbox_owner_breaker"}


async def test_reset_refuses_a_breaker_that_cleared_after_the_person_looked(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """The person saw two tripped; one cleared since (a poll completed). Sending the larger
    set they saw is refused too, so a reset only ever answers exactly what was shown."""
    _with_local(running_app, _trip_route_changed, _trip_inbox_owner)
    shown = ["inbox_owner_breaker", "route_changed_breaker"]

    def completed(session: Session, user: User) -> None:
        route_breaker.record_inbox_owner(
            session,
            user,
            account_id_for(session, user),
            owner_mismatch=False,
            completed=True,
            now=NOW,
        )

    _with_local(running_app, completed)

    response = await client.post(
        RESET_URL, json={"confirm": True, "tripped": shown}, headers=HEADERS
    )

    assert response.status_code == 409
    assert "reload Posture" in response.json()["detail"]
    assert _tripped(running_app) == {"route_changed_breaker"}


async def test_reset_clears_an_unreadable_breaker(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """A corrupt row reads as tripped (fail closed) and its row offers the button, so the
    button has to clear it, as the command does."""
    from netkeeper.services.settings_kv import set_setting

    def corrupt(session: Session, user: User) -> None:
        account_id = account_id_for(session, user)
        set_setting(session, user, f"linkedin.contact_info_breaker.{account_id}", "garbage")

    _with_local(running_app, corrupt)
    tripped = _tripped(running_app)
    assert tripped == {"contact_info_breaker"}
    rows = await _rows(client)
    assert rows["contact_info_breaker"]["status"] == "unknown"
    assert rows["contact_info_breaker"]["actions"] == ["reset_breaker"]

    response = await client.post(
        RESET_URL, json={"confirm": True, "tripped": sorted(tripped)}, headers=HEADERS
    )

    assert response.status_code == 200
    assert _tripped(running_app) == frozenset()


async def test_reset_is_scoped_to_the_current_user(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """Another user's tripped breaker neither shows the button to this user nor lets this
    user's reset through, and this user's reset leaves it tripped."""
    with session_scope(running_app.state.session_factory, write=True) as session:
        other = factories.make_user(session, kind=UserKind.HOSTED)
        _trip_route_changed(session, other)
        other_id = other.id

    rows = await _rows(client)
    assert rows["route_changed_breaker"]["actions"] == []
    refused = await client.post(
        RESET_URL, json={"confirm": True, "tripped": ["route_changed_breaker"]}, headers=HEADERS
    )
    assert refused.status_code == 409

    _with_local(running_app, _trip_inbox_owner)
    mine = await client.post(
        RESET_URL, json={"confirm": True, "tripped": ["inbox_owner_breaker"]}, headers=HEADERS
    )
    assert mine.status_code == 200
    with session_scope(running_app.state.session_factory) as session:
        other_user = session.get(User, other_id)
        assert other_user is not None
        assert route_breaker.tripped(session, other_user, account_id_for(session, other_user))

    with acting_as(running_app, other_id):
        theirs = await client.post(
            RESET_URL, json={"confirm": True, "tripped": ["route_changed_breaker"]}, headers=HEADERS
        )
    assert theirs.status_code == 200
    with session_scope(running_app.state.session_factory) as session:
        other_user = session.get(User, other_id)
        assert other_user is not None
        assert not route_breaker.tripped(session, other_user, account_id_for(session, other_user))


async def test_every_tripped_breaker_row_offers_reset(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    def trip_all(session: Session, user: User) -> None:
        account_id = account_id_for(session, user)
        _trip_route_changed(session, user)
        for _ in range(route_breaker.ANSWER_LOST_THRESHOLD):
            route_breaker.record_answer_lost(
                session,
                user,
                account_id,
                kind=SyncRunKind.CONNECTIONS_FULL,
                answer_lost=True,
                clean_end=False,
                now=NOW,
            )
        for _ in range(route_breaker.CONTACT_INFO_THRESHOLD):
            route_breaker.record_contact_info(
                session, user, account_id, answer_lost=True, clean_end=False, now=NOW
            )
        for _ in range(route_breaker.INBOX_THRESHOLD):
            route_breaker.record_inbox(
                session, user, account_id, route_changed=True, completed=False, now=NOW
            )
        _trip_inbox_owner(session, user)

    _with_local(running_app, trip_all)
    rows = await _rows(client)
    keys = {
        "route_changed_breaker",
        "answer_lost_limit",
        "contact_info_breaker",
        "inbox_breaker",
        "inbox_owner_breaker",
    }
    assert {key for key, row in rows.items() if "reset_breaker" in row["actions"]} == keys
    assert _tripped(running_app) == keys
    for key in keys:
        assert "click **Reset breaker** under Posture" in rows[key]["warnings"][0], key

    response = await client.post(
        RESET_URL, json={"confirm": True, "tripped": sorted(keys)}, headers=HEADERS
    )

    assert response.json() == {"cleared": True, "reset": sorted(keys)}
    assert _tripped(running_app) == frozenset()


# --- Forget inbox owner -----------------------------------------------------------------


async def test_forget_owner_forgets_the_recorded_mailbox(
    client: httpx.AsyncClient, running_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_local(running_app, _record_owner, _trip_inbox_owner)
    row = (await _rows(client))["inbox_owner_breaker"]
    assert row["actions"] == ["reset_breaker", "forget_inbox_owner"]
    assert "click **Forget inbox owner** under Posture" in row["warnings"][0]
    assert "`netkeeper linkedin inbox-forget-owner`" in row["warnings"][0]

    from netkeeper.web.api import linkedin as linkedin_api

    calls: list[int] = []
    real = inbox_apply.forget_owner
    # The route calls the command's own function.
    assert vars(linkedin_api)["forget_owner"] is inbox_apply.forget_owner

    def spy(session: Session, user: User) -> bool:
        calls.append(user.id)
        return real(session, user)

    monkeypatch.setattr(linkedin_api, "forget_owner", spy)

    response = await client.post(FORGET_URL, json={"confirm": True}, headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == {"forgotten": True}
    assert len(calls) == 1
    with session_scope(running_app.state.session_factory) as session:
        assert not inbox_apply.owner_recorded(session, _local(session))
    # Forgetting fixes the cause; the breaker still waits for a poll or a reset.
    assert (await _rows(client))["inbox_owner_breaker"]["actions"] == ["reset_breaker"]

    again = await client.post(FORGET_URL, json={"confirm": True}, headers=HEADERS)
    assert again.status_code == 409
    assert "nothing to forget" in again.json()["detail"]


async def test_forget_owner_refuses_when_none_is_recorded(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    response = await client.post(FORGET_URL, json={"confirm": True}, headers=HEADERS)
    assert response.status_code == 409


async def test_forget_owner_refuses_without_confirm(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    _with_local(running_app, _record_owner)

    response = await client.post(FORGET_URL, json={"confirm": False}, headers=HEADERS)

    assert response.status_code == 422
    with session_scope(running_app.state.session_factory) as session:
        assert inbox_apply.owner_recorded(session, _local(session))


async def test_forget_owner_is_scoped_to_the_current_user(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    with session_scope(running_app.state.session_factory, write=True) as session:
        other = factories.make_user(session, kind=UserKind.HOSTED)
        _record_owner(session, other)
        other_id = other.id

    refused = await client.post(FORGET_URL, json={"confirm": True}, headers=HEADERS)
    assert refused.status_code == 409
    with session_scope(running_app.state.session_factory) as session:
        other_user = session.get(User, other_id)
        assert other_user is not None
        assert inbox_apply.owner_recorded(session, other_user)

    with acting_as(running_app, other_id):
        theirs = await client.post(FORGET_URL, json={"confirm": True}, headers=HEADERS)
    assert theirs.status_code == 200


async def test_forget_owner_is_not_offered_when_it_cannot_fix_the_mismatch(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """With a LinkedIn ID on the self contact, that is compared first, so forgetting the
    recorded mailbox would change nothing: the row does not offer it."""

    def self_urn(session: Session, user: User) -> None:
        ensure_self_contact(session, user).li_urn = "urn:li:fsd_profile:INVENTEDSELF"

    _with_local(running_app, self_urn, _trip_inbox_owner)
    row = (await _rows(client))["inbox_owner_breaker"]
    assert row["actions"] == ["reset_breaker"]
    assert "Forget inbox owner" not in row["warnings"][0]


async def test_forget_owner_is_not_offered_with_no_mismatch(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    _with_local(running_app, _record_owner)
    rows = await _rows(client)
    assert all("forget_inbox_owner" not in row["actions"] for row in rows.values())


async def test_one_mismatch_offers_forget_but_not_reset(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    def once(session: Session, user: User) -> None:
        _trip_inbox_owner(session, user, times=1)

    _with_local(running_app, _record_owner, once)
    assert (await _rows(client))["inbox_owner_breaker"]["actions"] == ["forget_inbox_owner"]


# --- methods and the CSRF guard ---------------------------------------------------------


@pytest.mark.parametrize("url", [RESET_URL, FORGET_URL])
async def test_neither_endpoint_answers_a_get(
    client: httpx.AsyncClient, running_app: FastAPI, url: str
) -> None:
    _with_local(running_app, _record_owner, _trip_route_changed)

    response = await client.get(url)

    # Nothing answers a GET there: the API's 404 for an unknown route, never the action.
    assert response.status_code in (404, 405)
    assert _tripped(running_app) == {"route_changed_breaker"}
    with session_scope(running_app.state.session_factory) as session:
        assert inbox_apply.owner_recorded(session, _local(session))


@pytest.mark.parametrize(
    ("url", "body"),
    [
        (RESET_URL, {"confirm": True, "tripped": ["route_changed_breaker"]}),
        (FORGET_URL, {"confirm": True}),
    ],
)
async def test_both_go_through_the_csrf_guard(
    client: httpx.AsyncClient, running_app: FastAPI, url: str, body: dict[str, Any]
) -> None:
    _with_local(running_app, _record_owner, _trip_route_changed)

    missing = await client.post(url, json=body)
    cross_site = await client.post(
        url, json=body, headers={**HEADERS, "Origin": "https://elsewhere.invalid"}
    )

    assert missing.status_code == 403
    assert cross_site.status_code == 403
    assert _tripped(running_app) == {"route_changed_breaker"}
    with session_scope(running_app.state.session_factory) as session:
        assert inbox_apply.owner_recorded(session, _local(session))


def test_both_routes_are_post_only_and_take_a_writer_session() -> None:
    """Both write, so neither carries ``@read_only`` (CLAUDE.md, writer sessions), and
    neither answers any other method."""
    from fastapi.routing import APIRoute

    from netkeeper.web.api.linkedin import router
    from netkeeper.web.deps import READ_ONLY_ATTR

    paths = {"/linkedin/schedule/reset-breaker", "/linkedin/inbox/forget-owner"}
    routes = [r for r in router.routes if isinstance(r, APIRoute) and r.path in paths]
    assert {route.path for route in routes} == paths
    assert len(routes) == 2
    for route in routes:
        assert route.methods == {"POST"}
        assert not getattr(route.endpoint, READ_ONLY_ATTR, False)
