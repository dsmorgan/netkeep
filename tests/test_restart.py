"""**Restart netkeep** (#597): ``POST /settings/restart``, how ``serve`` stops and comes
back under launchd and standalone, and the serve lock (#590) across it."""

from __future__ import annotations

import asyncio
import plistlib
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from local_api import LocalKey
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeep import cli
from netkeep.cli import app as cli_app
from netkeep.config import Settings, load_settings
from netkeep.db import session_scope
from netkeep.linkedin import activity_lock
from netkeep.models import SyncRunKind, SyncRunTrigger, User
from netkeep.models.base import utcnow
from netkeep.services import login_item, restart, restart_gate, runs, serve_lock
from netkeep.services.events import EventBus
from netkeep.services.linkedin_accounts import local_account_id
from netkeep.services.scheduled_runs import ServeExtractor
from netkeep.web.api import health
from netkeep.web.app import create_app

CSRF = {"X-Netkeep-Client": "1"}
URL = "/api/v1/settings/restart"


# --- the service ------------------------------------------------------------------


def test_the_launchd_constants_match_the_agent() -> None:
    """The label the login item writes, and a status launchd restarts after."""
    assert restart.LAUNCHD_LABEL == login_item.LABEL_BASE == "fun.tnkr.netkeep"
    assert restart.LAUNCHD_RESTART_EXIT == 75
    # KeepAlive restarts only a job that did not exit successfully.
    item = login_item.LoginItem(
        data=Path("/nk"),
        agents_dir=Path("/nowhere"),
        runner=lambda _args: login_item.RunResult(0),
        executable=Path("/bin/netkeep"),
        macos=True,
    )
    assert plistlib.loads(item.render_plist())["KeepAlive"] == {"SuccessfulExit": False}
    assert restart.OLD_AGENT_MARK == "NETKEEPER_LAUNCHD_LOG_DIR"


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"XPC_SERVICE_NAME": "fun.tnkr.netkeep"}, "launchd"),
        ({"XPC_SERVICE_NAME": "fun.tnkr.netkeep.6f70bec19518"}, "launchd"),
        ({"XPC_SERVICE_NAME": "fun.tnkr.netkeepx"}, "exec"),
        # an agent from before the rename (#661): its definition points into the moved folder
        ({"XPC_SERVICE_NAME": "fun.tnkr.netkeeper"}, "exec"),
        ({"XPC_SERVICE_NAME": "fun.tnkr.netkeeper.6f70bec19518"}, "exec"),
        ({"XPC_SERVICE_NAME": "fun.tnkr.netkeeperx"}, "exec"),
        # started by the removed script's agent: launchd would run a script that is gone
        (
            {"XPC_SERVICE_NAME": "fun.tnkr.netkeep", "NETKEEPER_LAUNCHD_LOG_DIR": "/d/logs"},
            "exec",
        ),
        ({"XPC_SERVICE_NAME": "0"}, "exec"),
        ({"XPC_SERVICE_NAME": "application.com.apple.Terminal.1234"}, "exec"),
        ({}, "exec"),
    ],
)
def test_mode_is_launchd_only_under_the_login_item(environ: dict[str, str], expected: str) -> None:
    assert restart.mode(environ) == expected


def test_relaunch_under_launchd_exits_non_zero_and_never_execs() -> None:
    exits: list[int] = []
    execs: list[Any] = []
    with pytest.raises(RuntimeError, match="relaunch returned"):
        restart.relaunch("launchd", exit=exits.append, execv=lambda *args: execs.append(args))
    assert exits == [75] and execs == []


def test_relaunch_standalone_re_execs_the_same_command() -> None:
    execs: list[tuple[str, list[str]]] = []
    exits: list[int] = []
    with pytest.raises(RuntimeError, match="relaunch returned"):
        restart.relaunch(
            "exec",
            argv=["python3.12", "/venv/bin/netkeep", "--config", "/c.toml", "serve"],
            executable="/venv/bin/python",
            execv=lambda path, args: execs.append((path, args)),
            exit=exits.append,
        )
    assert execs == [
        (
            "/venv/bin/python",
            ["/venv/bin/python", "/venv/bin/netkeep", "--config", "/c.toml", "serve"],
        )
    ]
    assert exits == []


def test_a_restarter_stops_once() -> None:
    stops: list[None] = []
    restarter = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    restarter.trigger()
    restarter.trigger()
    assert restarter.requested and len(stops) == 1


# --- the endpoint -----------------------------------------------------------------


async def test_without_a_restarter_the_endpoint_refuses(client: httpx.AsyncClient) -> None:
    response = await client.post(URL, headers=CSRF)
    assert response.status_code == 409
    assert "cannot restart itself" in response.json()["detail"]
    config = (await client.get("/api/v1/settings/config")).json()
    assert config["can_restart"] is False


async def test_the_endpoint_answers_then_stops_the_server(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    stops: list[None] = []
    running_app.state.restarter = restart.Restarter(stop=lambda: stops.append(None), mode="launchd")
    assert (await client.get("/api/v1/settings/config")).json()["can_restart"] is True
    response = await client.post(URL, headers=CSRF)
    assert response.status_code == 202, response.text
    assert response.json() == {"mode": "launchd", "boot_id": health.BOOT_ID, "waiting_on": []}
    await _until(lambda: len(stops) == 1)  # after the answer, once the gate saw idle
    await asyncio.sleep(0.05)
    assert len(stops) == 1


async def _until(done: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not done():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


def _local_user(app: FastAPI) -> User:
    with session_scope(app.state.session_factory) as session:
        user = session.scalars(select(User)).one()
        session.expunge(user)
        return user


# --- #597 review blocker 2: never in the middle of work -----------------------------


async def test_a_running_linkedin_run_refuses_the_restart(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    stops: list[None] = []
    running_app.state.restarter = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    user = _local_user(running_app)
    with session_scope(running_app.state.session_factory, write=True) as session:
        run = runs.create_run(
            session,
            session.get(User, user.id),  # type: ignore[arg-type]
            SyncRunKind.CONNECTIONS_INCREMENTAL,
            trigger=SyncRunTrigger.MANUAL,
            now=utcnow(),
        )
        run_id = run.id
    body = (await client.get("/api/v1/settings/config")).json()
    assert any(f"(#{run_id}) is going" in reason for reason in body["busy"])
    response = await client.post(URL, headers=CSRF)
    assert response.status_code == 409
    assert "netkeep is busy: a LinkedIn connections incremental run" in response.json()["detail"]
    await asyncio.sleep(0.05)
    assert stops == []


async def test_a_held_browser_lock_refuses_the_restart(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    stops: list[None] = []
    running_app.state.restarter = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    with session_scope(running_app.state.session_factory) as session:
        account = local_account_id(session)
    assert account is not None
    held = activity_lock.try_claim(activity_lock.account_key(account))
    assert held is not None
    try:
        response = await client.post(URL, headers=CSRF)
        assert response.status_code == 409
        assert "the browser is in use by" in response.json()["detail"]
    finally:
        held.release()
    assert stops == []


async def test_a_background_task_and_a_campaign_send_refuse_the_restart(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    stops: list[None] = []
    running_app.state.restarter = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    gate = asyncio.Event()
    running_app.state.tasks.submit("imports.csv", gate.wait, user_id=1)
    running_app.state.campaign_engine = _Engine(busy=True)
    response = await client.post(URL, headers=CSRF)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "a background task (imports.csv) is running" in detail
    assert "a campaign send is in progress" in detail
    gate.set()
    running_app.state.campaign_engine = None
    assert stops == []


class _Engine:
    """The campaign engine's slice the gate reads: ``busy``, and ``stop``, which waits
    for the tick in flight as the real one does."""

    def __init__(self, *, busy: bool) -> None:
        self.busy = busy
        self.stopped = 0

    async def stop(self) -> bool:
        self.stopped += 1
        self.busy = False
        return True

    def start(self) -> None:
        self.started = getattr(self, "started", 0) + 1


async def test_a_run_left_running_by_a_crashed_process_never_blocks_a_restart(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    """``runs.is_stale``: no lock held and past the start grace, the row is a leftover."""
    stops: list[None] = []
    running_app.state.restarter = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    user = _local_user(running_app)
    with session_scope(running_app.state.session_factory, write=True) as session:
        runs.create_run(
            session,
            session.get(User, user.id),  # type: ignore[arg-type]
            SyncRunKind.CONNECTIONS_INCREMENTAL,
            trigger=SyncRunTrigger.MANUAL,
            now=utcnow() - runs.STALE_AFTER - timedelta(seconds=1),
        )
    assert (await client.get("/api/v1/settings/config")).json()["busy"] == []
    response = await client.post(URL, headers=CSRF)
    assert response.status_code == 202, response.text
    await _until(lambda: len(stops) == 1)


async def test_the_nightly_backup_refuses_the_restart(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    stops: list[None] = []
    running_app.state.restarter = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    running_app.state.nightly_backups = _Engine(busy=True)
    response = await client.post(URL, headers=CSRF)
    assert response.status_code == 409
    assert "the nightly backup is being written" in response.json()["detail"]
    running_app.state.nightly_backups = None
    assert stops == []


def test_the_nightly_backup_is_busy_only_while_it_writes(
    session_factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from netkeep.services import backup_job

    seen: list[bool] = []
    job = backup_job.NightlyBackups(
        session_factory.kw["bind"], session_factory, Settings(), tmp_path / "backups"
    )
    monkeypatch.setattr(backup_job, "nightly_due", lambda *_: True)

    def create(*_: object, **__: object) -> Path:
        seen.append(job.busy)
        return tmp_path / "b"

    monkeypatch.setattr(backup_job, "create_backup", create)
    monkeypatch.setattr(backup_job, "prune_backups", lambda *_, **__: [])
    assert not job.busy
    job.back_up_if_due()
    assert seen == [True] and not job.busy


async def test_cancel_stops_the_wait_and_nothing_restarts(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    stops: list[None] = []
    running_app.state.restarter = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    running_app.state.restart_gate = None
    gate = asyncio.Event()
    running_app.state.tasks.submit("linkedin.run", gate.wait, user_id=1)
    response = await client.post(URL, headers=CSRF, json={"when_idle": True})
    assert response.status_code == 202
    cancelled = await client.delete(URL, headers=CSRF)
    assert cancelled.status_code == 200 and cancelled.json() == {"cancelled": True}
    assert (await client.get("/api/v1/settings/config")).json()["restart_waiting"] is False
    gate.set()
    await asyncio.sleep(0.1)
    assert stops == []
    again = await client.delete(URL, headers=CSRF)
    assert again.json() == {"cancelled": False}


async def test_cancel_after_the_quiesce_resumes_sends_and_scheduled_runs() -> None:
    """Idle once, so the gate quiesced; then a run started by hand. Cancel puts the
    scheduler and the engine back."""
    stops: list[None] = []
    hook = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    answers = [[], ["a LinkedIn run (#3) is going"]]
    calls: list[str] = []

    async def busy() -> list[str]:
        return answers.pop(0) if len(answers) > 1 else answers[0]

    async def quiesce() -> None:
        calls.append("quiesce")

    gate = restart_gate.RestartGate(
        hook, busy=busy, quiesce=quiesce, resume=lambda: calls.append("resume"), poll_s=0.01
    )
    gate.start()
    await _until(lambda: calls == ["quiesce"] and gate.waiting_on != [])
    waiting_before: bool = gate.waiting
    assert waiting_before and stops == []
    assert await gate.cancel()
    waiting_after: bool = gate.waiting
    assert calls == ["quiesce", "resume"] and not waiting_after
    assert stops == [] and not hook.requested


async def test_restart_when_idle_waits_for_the_work_then_quiesces_and_restarts(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    stops: list[None] = []
    running_app.state.restarter = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    running_app.state.restart_gate = None
    gate = asyncio.Event()
    running_app.state.tasks.submit("linkedin.run", gate.wait, user_id=1)
    engine = _Engine(busy=False)
    running_app.state.campaign_engine = engine
    response = await client.post(URL, headers=CSRF, json={"when_idle": True})
    assert response.status_code == 202, response.text
    assert response.json()["waiting_on"] == ["a background task (linkedin.run) is running"]
    body = (await client.get("/api/v1/settings/config")).json()
    assert body["restart_waiting"] is True
    # Asking again while it waits is the same restart, not a refusal.
    again = await client.post(URL, headers=CSRF)
    assert again.status_code == 202
    await asyncio.sleep(0.1)
    assert stops == [] and engine.stopped == 0  # nothing is cut off
    gate.set()
    await _until(lambda: len(stops) == 1, timeout_s=8)
    assert engine.stopped == 1  # quiesced first: no new send can start
    running_app.state.campaign_engine = None


async def test_the_endpoint_needs_the_csrf_header(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    stops: list[None] = []
    running_app.state.restarter = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    assert (await client.post(URL)).status_code == 403
    assert stops == []


async def test_a_config_file_that_no_longer_loads_blocks_the_restart(
    running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[backup]\nkeep = 5\n")
    running_app.state.settings = load_settings(path)
    stops: list[None] = []
    running_app.state.restarter = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    body = (await client.get("/api/v1/settings/config")).json()
    assert (body["config_changed"], body["restart_pending"]) == (False, False)

    path.write_text("[backup]\nkeep = 7\n")
    body = (await client.get("/api/v1/settings/config")).json()
    assert (body["config_changed"], body["restart_pending"], body["config_error"]) == (
        True,
        True,
        None,
    )

    path.write_text("[backup\nkeep = 7\n")
    body = (await client.get("/api/v1/settings/config")).json()
    assert body["config_error"] and "invalid TOML" in body["config_error"]
    response = await client.post(URL, headers=CSRF)
    assert response.status_code == 409
    assert "would not start" in response.json()["detail"]
    assert stops == []


# --- serve ------------------------------------------------------------------------


class _FakeServer:
    """Stands in for ``uvicorn.Server``: running it asks for a restart, as the endpoint
    would, and returns once the restarter stopped it."""

    def __init__(self, config: uvicorn.Config) -> None:
        self.config = config
        self.should_exit = False

    def run(self) -> None:
        app = self.config.app
        assert isinstance(app, FastAPI)
        restarter = app.state.restarter
        if app.state.ask_restart:
            restarter.trigger()
        assert self.should_exit is app.state.ask_restart


@pytest.mark.parametrize(
    ("environ", "ask", "relaunched"),
    [
        ({"XPC_SERVICE_NAME": "fun.tnkr.netkeep"}, True, ["launchd"]),
        ({"XPC_SERVICE_NAME": "0"}, True, ["exec"]),
        ({}, False, []),
    ],
)
def test_serve_relaunches_only_when_a_restart_was_asked_for(
    monkeypatch: pytest.MonkeyPatch,
    environ: dict[str, str],
    ask: bool,
    relaunched: list[str],
) -> None:
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    application = FastAPI()
    application.state.ask_restart = ask
    monkeypatch.setattr(cli, "serve_app", lambda settings, **_: application)
    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    calls: list[str] = []
    monkeypatch.setattr(restart, "relaunch", calls.append)
    result = CliRunner().invoke(cli_app, ["serve", "--port", "0"])
    assert result.exit_code == 0, result.output
    assert calls == relaunched


@pytest.mark.wall_clock
async def test_a_real_server_stops_on_restart_with_an_event_stream_open(
    bare_engine: Engine,
    tmp_path: Path,
    _migrated_template: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The standalone path end to end, short of the exec, on this thread's loop as
    serve runs (uvicorn installs its signal handlers, which is how the event streams
    learn the server is stopping): an open event stream ends, the server stops, and its
    teardown releases the serve lock before serve relaunches."""
    import conftest

    conftest._copy_template(_migrated_template, tmp_path / "bare")
    monkeypatch.setenv("NETKEEP_FRONTEND_DIST", str(tmp_path / "no-dist"))
    extractor = ServeExtractor(executor=lambda factory, bus: object())  # type: ignore[arg-type,return-value]
    served = create_app(Settings(), engine=bare_engine, extractor=extractor)
    server = uvicorn.Server(
        uvicorn.Config(served, host="127.0.0.1", port=0, log_config=None, lifespan="on")
    )
    restarter = cli._restart_hook(server)
    served.state.restarter = restarter
    serving = asyncio.create_task(server.serve())
    await _until(lambda: server.started, timeout_s=8)
    port = server.servers[0].sockets[0].getsockname()[1]
    with pytest.raises(serve_lock.ServeRunning):
        serve_lock.claim()  # serve holds it while it runs
    async with (
        httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=10, auth=LocalKey(served)
        ) as client,
        client.stream("GET", "/api/v1/events") as stream,
    ):
        assert stream.status_code == 200
        response = await client.post(URL, headers=CSRF)
        assert response.status_code == 202, response.text
        assert response.json()["mode"] == "exec"
        async for _ in stream.aiter_bytes():
            pass  # ends when the server drains the stream
    await asyncio.wait_for(serving, timeout=8)
    assert restarter.requested
    serve_lock.claim().release()  # free for the serve that comes next


def test_web_reaches_the_exec_module_only_through_app_state() -> None:
    """The one module that execs is imported by the CLI alone; ``netkeep/web`` and the
    gate it uses see a :class:`~netkeep.services.restart_gate.RestartHook`."""
    package = Path(restart.__file__).parents[1]
    importers = sorted(
        str(path.relative_to(package)) for path in package.rglob("*.py") if _imports_restart(path)
    )
    assert importers == ["cli.py"]
    assert isinstance(restart.Restarter(stop=lambda: None, mode="exec"), restart_gate.RestartHook)


def _imports_restart(path: Path) -> bool:
    import ast

    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.module == "netkeep.services.restart":
                return True
            if node.module == "netkeep.services" and any(
                alias.name == "restart" for alias in node.names
            ):
                return True
        if isinstance(node, ast.Import) and any(
            alias.name == "netkeep.services.restart" for alias in node.names
        ):
            return True
    return False


def test_serve_attaches_at_the_chrome_address_the_settings_page_set(
    session_factory: sessionmaker[Session],
) -> None:
    """The address the restart is for: serve's provider is built at startup from the
    local user's value in force, not the file's alone."""
    import factories

    from netkeep.linkedin.browser import AttachBrowserProvider
    from netkeep.services import ui_settings
    from netkeep.worker import BrowserWorker, serve_extractor

    built = serve_extractor(Settings()).executor(session_factory, EventBus())
    assert isinstance(built, BrowserWorker) and isinstance(built.provider, AttachBrowserProvider)
    assert built.provider.cdp_url == "http://127.0.0.1:9222"  # no user yet: the file's
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        ui_settings.write(session, user, Settings(), {"linkedin.cdp_url": "http://127.0.0.1:9333"})
    rebuilt = serve_extractor(Settings()).executor(session_factory, EventBus())
    assert isinstance(rebuilt, BrowserWorker) and isinstance(
        rebuilt.provider, AttachBrowserProvider
    )
    assert rebuilt.provider.cdp_url == "http://127.0.0.1:9333"


class _SlowEngine:
    """A campaign engine whose stop waits out a send in flight until ``sent`` is set."""

    def __init__(self) -> None:
        self.busy = False
        self.stopping = False
        self.started = 0
        self.sent = asyncio.Event()

    async def stop(self) -> bool:
        self.stopping = True
        await self.sent.wait()
        return True

    def start(self) -> None:
        self.started += 1


class _Backups:
    def __init__(self) -> None:
        self.busy = False
        self.calls: list[str] = []

    def pause(self) -> None:
        self.calls.append("pause")

    def resume(self) -> None:
        self.calls.append("resume")


async def test_cancel_during_the_quiesce_resumes_the_real_scheduler_the_engine_and_backups(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    """#597 final review: a Cancel that lands while the engine waits out a send must
    still undo the whole quiesce. The real APScheduler pins pause over shutdown: a
    shut-down scheduler cannot resume, so scheduled runs would never come back."""
    from apscheduler.schedulers.base import STATE_PAUSED, STATE_RUNNING

    from netkeep.services.scheduler import HeartbeatScheduler

    stops: list[None] = []
    running_app.state.restarter = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    running_app.state.restart_gate = None
    scheduler = HeartbeatScheduler()  # what serve builds (#707)
    scheduler.start()
    engine, backups = _SlowEngine(), _Backups()
    running_app.state.scheduler = scheduler
    running_app.state.campaign_engine = engine
    running_app.state.nightly_backups = backups
    try:
        response = await client.post(URL, headers=CSRF)
        assert response.status_code == 202, response.text
        await _until(lambda: engine.stopping)  # mid-quiesce: the engine waits out a send
        assert scheduler.state == STATE_PAUSED and backups.calls == ["pause"]
        cancelling = asyncio.create_task(client.delete(URL, headers=CSRF))
        await asyncio.sleep(0.05)
        assert not cancelling.done()  # the quiesce is never cut off part way
        engine.sent.set()
        cancelled = await cancelling
        assert cancelled.json() == {"cancelled": True}
        assert scheduler.state == STATE_RUNNING
        assert engine.started == 1
        assert backups.calls == ["pause", "resume"]
        await asyncio.sleep(0.05)
        assert stops == []
    finally:
        running_app.state.scheduler = None
        running_app.state.campaign_engine = None
        running_app.state.nightly_backups = None
        if scheduler.running:
            scheduler.shutdown(wait=False)


def test_a_paused_nightly_backup_starts_no_new_write(
    session_factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from netkeep.services import backup_job

    job = backup_job.NightlyBackups(
        session_factory.kw["bind"], session_factory, Settings(), tmp_path / "backups"
    )
    written: list[Path] = []

    def create(*_: object, **__: object) -> Path:
        written.append(tmp_path / "b")
        return tmp_path / "b"

    monkeypatch.setattr(backup_job, "nightly_due", lambda *_: True)
    monkeypatch.setattr(backup_job, "create_backup", create)
    monkeypatch.setattr(backup_job, "prune_backups", lambda *_, **__: [])
    job.pause()
    assert job.back_up_if_due() is None and written == []
    job.resume()
    assert job.back_up_if_due() == tmp_path / "b" and len(written) == 1


async def test_a_restart_asked_for_during_a_cancel_is_refused_until_it_finishes(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    """#597 review nit: a second **Restart when idle** that lands while a Cancel is still
    resuming must not stop serve while the Cancel restarts the engine in it."""
    stops: list[None] = []
    running_app.state.restarter = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    running_app.state.restart_gate = None
    engine = _SlowEngine()
    running_app.state.campaign_engine = engine
    try:
        assert (await client.post(URL, headers=CSRF)).status_code == 202
        await _until(lambda: engine.stopping)  # mid-quiesce
        cancelling = asyncio.create_task(client.delete(URL, headers=CSRF))
        await _until(lambda: running_app.state.restart_gate.cancelling)
        second = await client.post(URL, headers=CSRF, json={"when_idle": True})
        assert second.status_code == 409
        assert "a cancel is in progress" in second.json()["detail"]
        engine.sent.set()
        assert (await cancelling).json() == {"cancelled": True}
        assert engine.started == 1
        await asyncio.sleep(0.05)
        assert stops == []  # nothing restarted under the resumed engine
        # Once the Cancel is done, a new restart is taken as usual.
        engine.stopping = False
        again = await client.post(URL, headers=CSRF)
        assert again.status_code == 202
        await _until(lambda: engine.stopping)
        engine.sent.set()
        await _until(lambda: len(stops) == 1)
    finally:
        running_app.state.campaign_engine = None


# --- #707: heartbeats in flight, errors, the owner --------------------------------


async def test_a_heartbeat_in_flight_holds_the_restart_until_it_returns(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    """#707 (B6): between claiming a fire and submitting its run, a heartbeat shows in no
    task and no run row. The gate reads the scheduler's own count, so a restart asked for
    then waits for the heartbeat (and the run it waits on) instead of cutting it off."""
    from netkeep.services.scheduler import HeartbeatScheduler

    stops: list[None] = []
    running_app.state.restarter = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    running_app.state.restart_gate = None
    scheduler = HeartbeatScheduler()
    scheduler.start()
    running_app.state.scheduler = scheduler
    release = asyncio.Event()
    heartbeat = asyncio.ensure_future(scheduler.counted(_held(release)))
    try:
        await asyncio.sleep(0)
        refused = await client.post(URL, headers=CSRF)
        assert refused.status_code == 409
        assert "the scheduler is starting or finishing a scheduled run" in refused.json()["detail"]
        waiting = await client.post(URL, headers=CSRF, json={"when_idle": True})
        assert waiting.status_code == 202
        await asyncio.sleep(0.1)
        assert stops == []
        release.set()
        await heartbeat
        await _until(lambda: len(stops) == 1)
    finally:
        release.set()
        running_app.state.scheduler = None
        if scheduler.running:
            scheduler.shutdown(wait=False)


async def test_a_heartbeat_that_starts_during_the_quiesce_is_waited_for() -> None:
    """#707 (B6): the gate's last look, after the quiesce paused the scheduler, sees a
    heartbeat that began before the pause."""
    from netkeep.services.scheduler import HeartbeatScheduler

    stops: list[None] = []
    hook = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    scheduler = HeartbeatScheduler()
    release = asyncio.Event()
    heartbeats: list[asyncio.Future[None]] = []

    async def busy() -> list[str]:
        return restart_gate.live_busy(None, None, None, scheduler)

    async def quiesce() -> None:
        # a heartbeat the scheduler ran just before it paused
        heartbeats.append(asyncio.ensure_future(scheduler.counted(_held(release))))
        await asyncio.sleep(0)

    gate = restart_gate.RestartGate(
        hook, busy=busy, quiesce=quiesce, resume=lambda: None, poll_s=0.01
    )
    gate.start()
    await _until(lambda: gate.waiting_on != [])
    await asyncio.sleep(0.05)
    assert stops == [] and gate.waiting
    release.set()
    await heartbeats[0]
    await _until(lambda: len(stops) == 1)


@pytest.mark.parametrize("where", ["busy", "quiesce"])
async def test_an_error_while_the_gate_waits_is_busy_and_it_looks_again(where: str) -> None:
    """#707 (B18): an error in a look, or in the quiesce, never ends the wait with the
    scheduler and the engine paused. It counts as busy, the gate looks again, and the
    restart goes ahead only after a clean look following a quiesce that finished."""
    stops: list[None] = []
    hook = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    calls: list[str] = []
    seen: list[list[str]] = []
    failures = [1]

    def fail_once(at: str) -> None:
        if where == at and failures:
            failures.pop()
            raise OSError("the disk hiccuped")

    async def busy() -> list[str]:
        calls.append("busy")
        if "quiesce" in calls or where == "busy":
            fail_once("busy")
        return []

    async def quiesce() -> None:
        calls.append("quiesce")
        fail_once("quiesce")

    gate = restart_gate.RestartGate(
        hook, busy=busy, quiesce=quiesce, resume=lambda: calls.append("resume"), poll_s=0.01
    )

    async def watch() -> None:
        while not stops:
            seen.append(list(gate.waiting_on))
            await asyncio.sleep(0.001)

    watcher = asyncio.ensure_future(watch())
    gate.start()
    await _until(lambda: len(stops) == 1)
    await watcher
    assert [restart_gate.CHECK_FAILED] in seen
    assert failures == []
    # The restart came only after a quiesce that finished, and a clean look after it.
    last_quiesce = len(calls) - 1 - calls[::-1].index("quiesce")
    assert calls[last_quiesce + 1 :] == ["busy"]
    assert calls.count("quiesce") == (2 if where == "quiesce" else 1)


async def test_cancel_after_an_error_resumes_what_the_gate_paused() -> None:
    """#707 (B18): before the fix, the gate's task died on the error, and Cancel answered
    False with the scheduler and the engine left paused."""
    stops: list[None] = []
    hook = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    calls: list[str] = []

    async def busy() -> list[str]:
        if "quiesce" in calls:
            raise OSError("the database is locked")
        return []

    async def quiesce() -> None:
        calls.append("quiesce")

    gate = restart_gate.RestartGate(
        hook, busy=busy, quiesce=quiesce, resume=lambda: calls.append("resume"), poll_s=0.01
    )
    gate.start()
    await _until(lambda: gate.waiting_on == [restart_gate.CHECK_FAILED])
    await asyncio.sleep(0.05)
    still: bool = gate.waiting
    assert still and stops == []
    assert await gate.cancel()
    assert calls == ["quiesce", "resume"] and stops == []


async def test_an_unreadable_browser_lock_counts_as_busy(
    running_app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#707 (B18): an OSError reading the browser lock refuses the restart (409), never
    answers 500 or idle."""

    def unreadable(*_: object, **__: object) -> activity_lock.LockState:
        raise PermissionError("the lock folder cannot be read")

    stops: list[None] = []
    running_app.state.restarter = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    monkeypatch.setattr(activity_lock, "inspect", unreadable)
    response = await client.post(URL, headers=CSRF)
    assert response.status_code == 409
    assert "could not tell whether the browser is in use" in response.json()["detail"]
    assert stops == []


@pytest.mark.parametrize("method", ["POST", "DELETE"])
async def test_only_the_local_owner_restarts_or_cancels(
    running_app: FastAPI, client: httpx.AsyncClient, method: str
) -> None:
    """#707 (I14): a restart stops serve for everyone, and Cancel resumes everyone's
    sends; another user gets 403 and nothing changes."""
    import factories
    from isolation.harness import acting_as

    from netkeep.models import UserKind

    stops: list[None] = []
    running_app.state.restarter = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    running_app.state.restart_gate = None
    with session_scope(running_app.state.session_factory, write=True) as session:
        other = factories.make_user(session, kind=UserKind.LOCAL).id
    with acting_as(running_app, other):
        response = await client.request(method, URL, headers=CSRF)
    assert response.status_code == 403
    assert response.json()["detail"] == "only the local owner account restarts netkeep"
    assert running_app.state.restart_gate is None
    await asyncio.sleep(0.05)
    assert stops == []
    # the owner still can
    assert (await client.request(method, URL, headers=CSRF)).status_code in (200, 202)


def _held(release: asyncio.Event) -> Callable[[], Awaitable[None]]:
    """A heartbeat body that runs until ``release`` is set."""

    async def heartbeat() -> None:
        await release.wait()

    return heartbeat


async def test_an_error_that_persists_logs_one_traceback_then_a_line_a_minute(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#707 review: a look that keeps failing logs its traceback once, and again only for
    another kind of error; in between, one line at most every ERROR_LOG_S. The reasons it
    waits on are logged when they change, not at every look."""
    stops: list[None] = []
    hook = restart.Restarter(stop=lambda: stops.append(None), mode="exec")
    now = [0.0]
    errors: list[BaseException] = (
        [OSError("locked")] * 50  # 50 looks within a second
        + [OSError("locked")]  # a minute later: one line
        + [RuntimeError("other")] * 3  # another kind: its own traceback
    )
    looks = [0]

    async def busy() -> list[str]:
        looks[0] += 1
        if looks[0] == 51:
            now[0] += restart_gate.ERROR_LOG_S
        elif looks[0] <= 50:
            now[0] += 0.01
        if errors:
            raise errors.pop(0)
        return ["a LinkedIn run (#3) is going"]

    async def quiesce() -> None:
        pass

    gate = restart_gate.RestartGate(
        hook,
        busy=busy,
        quiesce=quiesce,
        resume=lambda: None,
        poll_s=0,
        monotonic=lambda: now[0],
    )
    with caplog.at_level("INFO", logger=restart_gate.__name__):
        gate.start()
        await _until(lambda: looks[0] >= 60)
        assert await gate.cancel()
    records = [r for r in caplog.records if r.name == restart_gate.__name__]
    tracebacks = [r for r in records if r.exc_info is not None]
    assert [r.exc_info[0] for r in tracebacks if r.exc_info] == [OSError, RuntimeError]
    still = [r.getMessage() for r in records if "still could not check" in r.getMessage()]
    assert still == [
        "restart: still could not check what is running (OSError, 50 more times); trying again"
    ]
    waits = [r.getMessage() for r in records if r.getMessage().startswith("restart waits")]
    assert waits == [
        f"restart waits: {restart_gate.CHECK_FAILED}",
        "restart waits: a LinkedIn run (#3) is going",
    ]
    assert stops == []
