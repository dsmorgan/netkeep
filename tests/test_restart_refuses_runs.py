"""#730: once a restart is triggered, netkeep starts no new run.

A safety change. The restart gate calls ``trigger()`` once nothing is running; until
uvicorn stops taking requests, a run asked for from the UI (a prefill, a Check now)
could still be submitted, and the shutdown's ``cancel_all`` would cut it off part way,
a prefill mid-typing. From the trigger on, the task runner refuses every submit, and
each run-starting route answers ``409`` "netkeep is restarting" before it records or
claims anything. Every test here runs on fakes: no browser is attached or launched.
The auto-send handler's side is in ``tests/test_auto_send.py``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from campaign_fakes import NOW
from fastapi import FastAPI
from sqlalchemy import select
from test_linkedin_steps_api import FakeExecutor, _messages, _seed, with_runner

from netkeep.db import session_scope
from netkeep.models import SyncRun, SyncRunStatus, User
from netkeep.models.base import utcnow
from netkeep.scoping import unscoped
from netkeep.services import restart, restart_gate, runs, scheduler
from netkeep.services.events import EventBus
from netkeep.services.linkedin_accounts import arm_scheduled_runs, ensure_account
from netkeep.services.scheduled_runs import REFUSED_RESTARTING, serve_registry
from netkeep.services.tasks import RESTARTING, SubmitRefused, TaskInfo, TaskRunner
from netkeep.web.api import linkedin_steps as steps_api

__all__ = ["with_runner"]  # the fixture, used by name

CSRF = {"X-Netkeep-Client": "1"}
RESTART = "/api/v1/settings/restart"


async def _until(done: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not done():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


# --- the task runner ----------------------------------------------------------------


async def test_a_runner_refusing_new_work_starts_nothing_and_leaves_what_runs() -> None:
    runner = TaskRunner(EventBus())
    release = asyncio.Event()

    async def held() -> None:
        await release.wait()

    before = runner.submit("before", held, user_id=1)
    started: list[str] = []

    async def after() -> None:
        started.append("after")

    runner.check_open()  # open: no refusal
    runner.refuse_new(RESTARTING)
    with pytest.raises(SubmitRefused, match=f"^{RESTARTING}$"):
        runner.submit("after", after, user_id=1)
    with pytest.raises(SubmitRefused):
        runner.check_open()
    runner.refuse_new("another reason")  # the first reason stays
    assert runner.refusing == RESTARTING
    assert [info.id for info in runner.active()] == [before.id]  # nothing new scheduled
    release.set()
    await runner.join()
    finished = runner.get(before.id)
    assert finished is not None and finished.status == "succeeded"  # never cancelled
    await asyncio.sleep(0)
    assert started == []


# --- the gate -----------------------------------------------------------------------


def _gate(tasks: TaskRunner, hook: restart.Restarter, busy: list[str]) -> restart_gate.RestartGate:
    async def look() -> list[str]:
        return list(busy)

    async def quiesce() -> None:
        return None

    return restart_gate.RestartGate(
        hook,
        busy=look,
        quiesce=quiesce,
        resume=lambda: None,
        refuse_runs=lambda: tasks.refuse_new(RESTARTING),
        poll_s=0.01,
    )


async def test_the_gate_refuses_new_runs_before_it_stops_serve() -> None:
    tasks = TaskRunner(EventBus())
    refusing_at_stop: list[str | None] = []
    hook = restart.Restarter(stop=lambda: refusing_at_stop.append(tasks.refusing), mode="exec")
    gate = _gate(tasks, hook, busy=[])
    gate.start()
    await _until(lambda: hook.requested)
    assert refusing_at_stop == [RESTARTING]  # refused first, then stopped


async def test_a_cancelled_restart_leaves_the_runner_open() -> None:
    tasks = TaskRunner(EventBus())
    hook = restart.Restarter(stop=lambda: None, mode="exec")
    gate = _gate(tasks, hook, busy=["a background task (linkedin.run) is running"])
    gate.start()
    await asyncio.sleep(0.05)
    assert await gate.cancel()
    assert not hook.requested and tasks.refusing is None

    async def work() -> None:
        return None

    info = tasks.submit("after-cancel", work, user_id=1)
    assert isinstance(info, TaskInfo)
    await tasks.join()


# --- the API ------------------------------------------------------------------------


class FakeLauncher:
    """Stands in for netkeep's Chrome launcher; counts the starts it was asked for."""

    def __init__(self) -> None:
        self.starts = 0

    async def start(self, *, first_run: bool = False) -> Any:
        self.starts += 1
        raise AssertionError("no Chrome may be launched once a restart is triggered")


@pytest.fixture
def fakes(running_app: FastAPI) -> tuple[FakeExecutor, FakeLauncher]:
    executor = FakeExecutor()
    launcher = FakeLauncher()
    running_app.state.executor = executor
    running_app.state.chrome_launcher = launcher
    return executor, launcher


async def _trigger(app: FastAPI, client: httpx.AsyncClient) -> None:
    """Restart through the real endpoint and gate, with a restarter that only records."""
    stops: list[None] = []
    app.state.restarter = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    response = await client.post(RESTART, headers=CSRF)
    assert response.status_code == 202, response.text
    await _until(lambda: len(stops) == 1)


def _run_rows(app: FastAPI) -> list[SyncRun]:
    with session_scope(app.state.session_factory) as session:
        return list(session.scalars(unscoped(select(SyncRun))))


RUN_STARTS = [
    pytest.param("/api/v1/linkedin/runs", {"kind": "connections_incremental"}, id="run"),
    pytest.param("/api/v1/linkedin/runs/1/resume", {}, id="resume"),
    pytest.param("/api/v1/linkedin/browser/check", None, id="check-connection"),
    pytest.param(
        "/api/v1/campaigns/linkedin/prefill",
        {"next": True, "retry": False, "no_bubble_open": False},
        id="prefill",
    ),
    pytest.param("/api/v1/campaigns/linkedin/messages/1/check", None, id="check-now"),
    pytest.param("/api/v1/linkedin/browser/start", {"first_run": False}, id="start-chrome"),
    pytest.param("/api/v1/tasks/ping", None, id="ping"),
]


@pytest.mark.usefixtures("inside_active_hours")
@pytest.mark.parametrize(("path", "body"), RUN_STARTS)
async def test_after_the_trigger_every_run_start_answers_409_and_starts_nothing(
    running_app: FastAPI,
    client: httpx.AsyncClient,
    fakes: tuple[FakeExecutor, FakeLauncher],
    path: str,
    body: dict[str, Any] | None,
) -> None:
    executor, launcher = fakes
    await _trigger(running_app, client)
    response = await client.post(path, headers=CSRF, json=body)
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": RESTARTING}
    assert running_app.state.tasks.active() == []
    assert executor.executed == [] and launcher.starts == 0
    assert _run_rows(running_app) == []  # refused before anything was recorded


@pytest.mark.usefixtures("inside_active_hours")
async def test_before_the_trigger_a_run_starts(
    running_app: FastAPI, client: httpx.AsyncClient, fakes: tuple[FakeExecutor, FakeLauncher]
) -> None:
    executor, _ = fakes
    response = await client.post(
        "/api/v1/linkedin/runs", headers=CSRF, json={"kind": "connections_incremental"}
    )
    assert response.status_code == 202, response.text
    await running_app.state.tasks.join()
    assert executor.executed == [(response.json()["run_id"], 1)]


@pytest.mark.usefixtures("inside_active_hours", "with_runner")
@pytest.mark.parametrize("triggered", [False, True], ids=["before", "after"])
async def test_a_prefill_is_claimed_only_before_the_trigger(
    running_app: FastAPI,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    triggered: bool,
) -> None:
    executor = FakeExecutor()
    running_app.state.executor = executor
    monkeypatch.setattr(steps_api, "utcnow", lambda: NOW)
    [enrollment_id] = _seed(running_app)
    if triggered:
        await _trigger(running_app, client)
    response = await client.post(
        "/api/v1/campaigns/linkedin/prefill",
        headers=CSRF,
        json={"enrollment_id": enrollment_id, "next": False, "retry": False},
    )
    await running_app.state.tasks.join()
    if triggered:
        assert response.status_code == 409, response.text
        assert response.json() == {"detail": RESTARTING}
        assert _messages(running_app) == [] and executor.executed == []  # nothing claimed
    else:
        assert response.status_code == 202, response.text
        assert [m.enrollment_id for m in _messages(running_app)] == [enrollment_id]
        assert executor.executed == [(response.json()["run_id"], 1)]


# --- the scheduler ------------------------------------------------------------------


class _RefusesAtSubmit(TaskRunner):
    """Open at the handler's first look, refused at the submit: a trigger that landed
    between the record and the submit."""

    def submit(self, name: str, factory: Any, *, user_id: int) -> TaskInfo:
        raise SubmitRefused(RESTARTING)


class _NeverRuns:
    def __init__(self) -> None:
        self.executed: list[int] = []

    async def execute(self, run_id: int, user_id: int) -> runs.RunOutcome:
        self.executed.append(run_id)
        return runs.RunOutcome.DONE


@pytest.fixture
def armed(running_app: FastAPI) -> tuple[int, int]:
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = session.scalars(select(User)).one()
        arm_scheduled_runs(session, user, now=utcnow())
        return user.id, ensure_account(session, user).id


def _context(user_id: int, account_id: int) -> scheduler.JobContext:
    return scheduler.JobContext(
        user_id=user_id,
        account_id=account_id,
        kind=scheduler.JobKind.CONNECTIONS_INCREMENTAL,
        due=utcnow(),
        catch_up=False,
    )


async def test_a_scheduled_fire_after_the_trigger_records_nothing_and_retries_later(
    running_app: FastAPI, armed: tuple[int, int], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="netkeep.services.scheduled_runs")
    tasks = TaskRunner(EventBus())
    tasks.refuse_new(RESTARTING)
    executor = _NeverRuns()
    registry = serve_registry(running_app.state.session_factory, executor, tasks, clock=utcnow)
    outcome = await registry[scheduler.JobKind.CONNECTIONS_INCREMENTAL](_context(*armed))
    assert outcome is scheduler.JobOutcome.RETRY_LATER
    assert _run_rows(running_app) == [] and executor.executed == []
    assert "not started: netkeep is restarting" in caplog.text


async def test_a_scheduled_run_refused_at_its_submit_is_ended_and_never_runs(
    running_app: FastAPI, armed: tuple[int, int], caplog: pytest.LogCaptureFixture
) -> None:
    executor = _NeverRuns()
    registry = serve_registry(
        running_app.state.session_factory,
        executor,
        _RefusesAtSubmit(EventBus()),
        clock=utcnow,
    )
    outcome = await registry[scheduler.JobKind.CONNECTIONS_INCREMENTAL](_context(*armed))
    assert outcome is scheduler.JobOutcome.RETRY_LATER
    [run] = _run_rows(running_app)
    assert (run.status, run.stop_reason) == (SyncRunStatus.ABORTED, REFUSED_RESTARTING)
    assert executor.executed == []
    assert "not started: netkeep is restarting" in caplog.text
