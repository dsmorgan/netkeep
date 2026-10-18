"""Check connection: ``POST /linkedin/browser/check`` and its ``session_check`` run (#652).

A safety change: a new path that attaches to netkeeper's Chrome and reads the LinkedIn
session. Every test drives the real app lifespan and the real worker on a fake Chrome
(``browser_fakes``), so nothing here reaches a browser, a port, or linkedin.com. Each
guard the check keeps has a test that fails without it: the browser lock, one run at a
time, the dedicated-profile refusal, the scoped cookie read on the run's own tab, no
navigation, no budget, the gate on the run kind, and the owner and local-key rules.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import factories
import httpx
import pytest
from browser_fakes import FakeBrowser, FakeConnector, FakeContext
from fastapi import FastAPI
from isolation.harness import FixedUser
from run_fakes import CDP_URL, Clock, worker_extractor
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from test_preflight import FAKE_SESSION_VALUE, cookie, make_context
from test_runs_serve import HEADERS, client_for, serving

from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.linkedin import activity_lock
from netkeeper.linkedin.browser import ActivityLocks, AttachBrowserProvider, BrowserUnavailable
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.preflight import COOKIE_URLS
from netkeeper.models import SyncRunKind, SyncRunStatus, SyncRunTrigger, User, UserKind
from netkeeper.models.base import utcnow
from netkeeper.services import budgets, runs, session_check
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import flag_session, recorded_evidence, session_flag

CHECK = "/api/v1/linkedin/browser/check"
HEALTH = "/api/v1/linkedin/browser/health"


def _provider(
    context: FakeContext | None = None, **connector: Any
) -> tuple[AttachBrowserProvider, FakeConnector, FakeContext]:
    context = make_context() if context is None else context
    fake = FakeConnector([FakeBrowser([context])], **connector)
    return AttachBrowserProvider(CDP_URL, connector=fake, locks=ActivityLocks()), fake, context


@asynccontextmanager
async def _served(engine: Engine, provider: AttachBrowserProvider) -> AsyncIterator[FastAPI]:
    settings = Settings()
    extractor = worker_extractor(provider, settings, clock=Clock(utcnow()))
    async with serving(engine, settings, extractor) as app:
        yield app


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


async def _check(app: FastAPI) -> tuple[httpx.Response, dict[str, Any], dict[str, Any]]:
    """Post a check, let the worker run it, and read the run and health back."""
    async with client_for(app) as client:
        response = await client.post(CHECK, headers=HEADERS)
        await app.state.tasks.join()
        run: dict[str, Any] = {}
        if response.status_code == 202:
            run = (await client.get(f"/api/v1/linkedin/runs/{response.json()['run_id']}")).json()
        health = (await client.get(HEALTH)).json()
    return response, run, health


@pytest.fixture
def _no_frontend(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[None]:
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", str(tmp_path / "no-dist"))
    yield


pytestmark = pytest.mark.usefixtures("_no_frontend")


# --- what a check reads, and what it shows --------------------------------------------


async def test_a_signed_in_check_shows_on_the_run_and_in_health(bare_engine: Engine) -> None:
    provider, connector, _ = _provider()
    async with _served(bare_engine, provider) as app:
        response, run, health = await _check(app)

    assert response.status_code == 202
    assert set(response.json()) == {"run_id", "task_id"}
    assert (run["kind"], run["status"], run["trigger"]) == ("session_check", "completed", "manual")
    assert run["stop_reason"] == "session_check"
    assert run["stop_reason_text"] == (
        "read the LinkedIn session from Chrome's cookie jar; visited nothing"
    )
    assert run["counts"]["session_status"] == "on"
    assert run["counts"]["session_summary"] == "logged in (li_at, JSESSIONID)"
    assert run["counts"]["session_warnings"] == []
    assert run["counts"]["cookie_names"] == ["li_at", "JSESSIONID"]
    assert health["session_status"] == "on"
    assert f"by check run {run['id']}" in health["session_summary"]
    assert health["running_run_id"] is None
    assert (connector.attaches, connector.detaches) == (1, 1)


async def test_a_signed_out_check_shows_no_session(bare_engine: Engine) -> None:
    jar = [cookie("JSESSIONID", "ajax:0000"), cookie("bcookie", "x")]
    provider, _, _ = _provider(make_context(cookies=jar))
    async with _served(bare_engine, provider) as app:
        _, run, health = await _check(app)

    assert (run["status"], run["stop_reason"]) == ("completed", "session_check")
    assert run["counts"]["session_status"] == "off"
    assert run["counts"]["session_summary"] == "no session in this profile"
    assert health["session_status"] == "off"
    assert "no session at" in health["session_summary"]
    assert f"by check run {run['id']}" in health["session_summary"]


async def test_an_unreadable_jar_records_unknown_and_no_evidence(bare_engine: Engine) -> None:
    """An unreadable jar says nothing about the session, as for `netkeeper preflight`."""
    context = make_context()
    context.cookie_error = RuntimeError("Protocol error")
    provider, _, _ = _provider(context)
    async with _served(bare_engine, provider) as app:
        _, run, health = await _check(app)
        with session_scope(app.state.session_factory) as session:
            assert recorded_evidence(session, _local(session)) is None

    assert run["status"] == "completed"
    assert run["counts"]["session_status"] == "unknown"
    assert any("login state is unknown" in w for w in run["counts"]["warnings"])
    assert health["session_status"] == "unknown"


async def test_chrome_unreachable_fails_the_run_and_shows_in_health(bare_engine: Engine) -> None:
    provider, _, context = _provider(error=BrowserUnavailable("no Chrome on the port"))
    async with _served(bare_engine, provider) as app:
        response, run, health = await _check(app)

    assert response.status_code == 202
    assert (run["status"], run["stop_reason"]) == ("failed", "browser_unavailable")
    assert health["chrome_unreachable_run_id"] == run["id"]
    assert health["chrome_unreachable_at"] is not None
    assert context.pages == [] and context.cookie_calls == 0


async def test_another_profile_is_refused_before_any_tab_or_cookie(bare_engine: Engine) -> None:
    """ADR 0012 rule 6 (#624): the attach's dedicated-profile check stops the run."""
    provider, connector, context = _provider(browser_pid=None)
    async with _served(bare_engine, provider) as app:
        _, run, health = await _check(app)

    assert (run["status"], run["stop_reason"]) == ("failed", "profile_refused")
    assert context.new_page_calls == 0
    assert context.cookie_calls == 0
    assert connector.detaches == 1
    assert health["chrome_unreachable_run_id"] is None, "Chrome is up, just not netkeeper's"


async def test_the_check_reads_cookies_scoped_on_its_own_tab_and_navigates_nowhere(
    bare_engine: Engine,
) -> None:
    """#633: one ``Network.getCookies`` with LinkedIn's URL, on a session for the run's own
    blank tab, detached after; #632/#634: one tab, the run's, closed at the end."""
    provider, _, context = _provider()
    async with _served(bare_engine, provider) as app:
        await _check(app)

    assert COOKIE_URLS == ("https://www.linkedin.com/",)
    [tab] = context.pages
    assert tab.goto_calls == [], f"the check navigated to {tab.goto_calls}"
    assert tab.is_closed()
    [cookie_session] = context.cookie_sessions
    assert cookie_session.calls == [("Network.getCookies", {"urls": ["https://www.linkedin.com/"]})]
    assert cookie_session.detached
    assert "session-id" not in context.last_cookies_returned


async def test_no_cookie_value_reaches_the_run_health_or_the_log(
    bare_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    provider, _, _ = _provider()
    with caplog.at_level(logging.DEBUG):
        async with _served(bare_engine, provider) as app:
            _, run, health = await _check(app)

    assert FAKE_SESSION_VALUE not in repr(run)
    assert FAKE_SESSION_VALUE not in repr(health)
    assert FAKE_SESSION_VALUE not in caplog.text


# --- the browser lock: one check, or one browser run, at a time -----------------------


async def test_a_lock_another_process_holds_answers_409_and_records_nothing(
    bare_engine: Engine,
) -> None:
    provider, connector, _ = _provider()
    async with _served(bare_engine, provider) as app:
        with session_scope(app.state.session_factory, write=True) as session:
            account_id = ensure_account(session, _local(session)).id
        claim = activity_lock.try_claim(activity_lock.account_key(account_id))
        assert claim is not None
        try:
            async with client_for(app) as client:
                response = await client.post(CHECK, headers=HEADERS)
                listed = (await client.get("/api/v1/linkedin/runs")).json()
        finally:
            claim.release()

    assert response.status_code == 409
    assert "holds the browser for this account" in response.json()["detail"]
    assert listed["total"] == 0
    assert connector.attaches == 0


async def test_a_running_run_answers_409_with_its_reason(bare_engine: Engine) -> None:
    provider, connector, _ = _provider()
    async with _served(bare_engine, provider) as app:
        with session_scope(app.state.session_factory, write=True) as session:
            busy = runs.create_run(
                session,
                _local(session),
                SyncRunKind.ENRICH,
                trigger=SyncRunTrigger.MANUAL,
                now=utcnow(),
                browser_held=lambda _: True,
            ).id
        async with client_for(app) as client:
            response = await client.post(CHECK, headers=HEADERS)

    assert response.status_code == 409
    assert f"run {busy} (enrich) is still running" in response.json()["detail"]
    assert connector.attaches == 0


async def test_a_second_check_while_one_runs_answers_409(bare_engine: Engine) -> None:
    """The first check's row is ``running`` until the worker ends it: a second is refused."""
    provider, _, _ = _provider()
    async with _served(bare_engine, provider) as app:
        with session_scope(app.state.session_factory, write=True) as session:
            first = session_check.start(session, _local(session), now=utcnow()).id
        async with client_for(app) as client:
            response = await client.post(CHECK, headers=HEADERS)

    assert response.status_code == 409
    assert f"run {first} (session_check) is still running" in response.json()["detail"]


async def test_a_lock_taken_after_the_request_ends_the_run_busy(bare_engine: Engine) -> None:
    """Between the 409 check and the attach, another process can still take the lock:
    the worker's own lock refuses, and the run ends ``browser_busy`` without attaching."""
    provider, connector, _ = _provider()
    async with _served(bare_engine, provider) as app:
        factory = app.state.session_factory
        with session_scope(factory, write=True) as session:
            user = _local(session)
            user_id, run_id = user.id, session_check.start(session, user, now=utcnow()).id
            account_id = ensure_account(session, user).id
        claim = activity_lock.try_claim(activity_lock.account_key(account_id))
        assert claim is not None
        try:
            await app.state.executor.execute(run_id, user_id)
        finally:
            claim.release()
        with session_scope(factory) as session:
            run = runs.get_run(session, _local(session), run_id)
            assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "browser_busy")
    assert connector.attaches == 0


# --- what the check doesn't spend, and doesn't gate on --------------------------------


async def test_the_check_spends_no_budget_unit(
    bare_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It visits no page, so it consumes nothing: a consume would fail the run."""

    def no_spending(*_: Any, **__: Any) -> None:
        raise AssertionError("a check spends no budget unit")

    monkeypatch.setattr(budgets, "consume", no_spending)
    provider, _, _ = _provider()
    async with _served(bare_engine, provider) as app:
        async with client_for(app) as client:
            before = (await client.get("/api/v1/linkedin/budget")).json()["budgets"]
        _, run, _ = await _check(app)
        async with client_for(app) as client:
            after = (await client.get("/api/v1/linkedin/budget")).json()["budgets"]

    assert run["status"] == "completed"
    assert after == before


async def test_a_flag_and_closed_hours_dont_stop_it_and_it_never_clears_the_flag(
    bare_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No request reaches linkedin.com, so the flag and active hours don't gate it, as
    they don't gate `netkeeper preflight`. Clearing the flag stays a person's act."""

    def closed(*_: Any, **__: Any) -> None:
        raise runs.OutsideActiveHours("outside active hours")

    monkeypatch.setattr(runs, "refuse_if_outside_active_hours", closed)
    provider, _, _ = _provider()
    async with _served(bare_engine, provider) as app:
        with session_scope(app.state.session_factory, write=True) as session:
            flag_session(session, _local(session), Outcome.LOGGED_OUT, url="/login")
        _, run, health = await _check(app)
        with session_scope(app.state.session_factory) as session:
            assert session_flag(session, _local(session)) is not None

    assert (run["status"], run["counts"]["session_status"]) == ("completed", "on")
    assert health["session_status"] == "off", "the flag still wins in health"


# --- who may start one, and how -------------------------------------------------------


async def test_the_route_needs_the_local_key_and_the_csrf_header(bare_engine: Engine) -> None:
    provider, connector, _ = _provider()
    async with _served(bare_engine, provider) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as bare:
            keyless = await bare.post(CHECK, headers=HEADERS)
        async with client_for(app) as client:
            headerless = await client.post(CHECK)
            listed = (await client.get("/api/v1/linkedin/runs")).json()

    assert keyless.status_code == 401
    assert headerless.status_code == 403
    assert listed["total"] == 0
    assert connector.attaches == 0


async def test_only_the_owner_checks(bare_engine: Engine) -> None:
    provider, connector, _ = _provider()
    async with _served(bare_engine, provider) as app:
        with session_scope(app.state.session_factory, write=True) as session:
            other = factories.make_user(session, kind=UserKind.HOSTED).id
        app.state.auth = FixedUser(other)
        async with client_for(app) as client:
            response = await client.post(CHECK, headers=HEADERS)

    assert response.status_code == 403
    assert connector.attaches == 0


async def test_without_a_worker_it_answers_503(client: httpx.AsyncClient) -> None:
    response = await client.post(CHECK, headers=HEADERS)

    assert response.status_code == 503
    assert "no browser worker" in response.json()["detail"]


@pytest.mark.usefixtures("inside_active_hours")
async def test_a_session_check_run_has_one_door(bare_engine: Engine) -> None:
    """Not through ``POST /linkedin/runs``, not without the gate, never scheduled."""
    provider, connector, _ = _provider()
    async with _served(bare_engine, provider) as app:
        async with client_for(app) as client:
            response = await client.post(
                "/api/v1/linkedin/runs", json={"kind": "session_check"}, headers=HEADERS
            )
        with session_scope(app.state.session_factory, write=True) as session:
            user = _local(session)
            with pytest.raises(runs.RunError, match="only by Check connection"):
                runs.create_run(
                    session,
                    user,
                    SyncRunKind.SESSION_CHECK,
                    trigger=SyncRunTrigger.MANUAL,
                    now=utcnow(),
                )
            with pytest.raises(runs.RunError, match="only by Check connection"):
                runs.create_run(
                    session,
                    user,
                    SyncRunKind.SESSION_CHECK,
                    trigger=SyncRunTrigger.SCHEDULED,
                    now=utcnow(),
                    gate=runs.SESSION_CHECK_GATE,
                )

    assert response.status_code == 422
    assert "only by Check connection" in response.json()["detail"]
    assert connector.attaches == 0


def test_the_check_records_evidence_with_its_run_as_the_source(
    session_factory: Any,
) -> None:
    from netkeeper.services.posture import SessionProbe

    now = utcnow()
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session, timezone="UTC")
        user_id = user.id
        run_id = session_check.start(session, user, now=now).id
    probe = SessionProbe(attached=True, logged_in=True, cookie_names=("li_at",))
    session_check.finish(
        session_factory, user_id, run_id, session_check.CheckResult(probe), now=now
    )
    with session_scope(session_factory) as session:
        owner = session.get(User, user_id)
        assert owner is not None
        evidence = recorded_evidence(session, owner)
        assert evidence is not None
        assert (evidence.logged_in, evidence.source) == (True, f"check run {run_id}")
        assert evidence.cookie_names == ("li_at",)
        assert runs.get_run(session, owner, run_id).notes == session_check.SESSION_CHECK_NOTE


def test_the_check_stop_reason_and_run_kind_are_pinned() -> None:
    assert SyncRunKind.SESSION_CHECK.value == "session_check"
    assert session_check.SESSION_CHECK_STOP == "session_check"
    assert SyncRunKind.SESSION_CHECK in runs.RUNNABLE_KINDS
