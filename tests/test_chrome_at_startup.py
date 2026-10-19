"""Start netkeep's Chrome when netkeep starts, and what the button reads (#599).

``serve`` calls the one launcher (#636) once it is listening, only when
``linkedin.start_chrome_at_startup`` is on, and the **Start Chrome** button reads
whether netkeep's Chrome is running from ``GET /linkedin/browser/health``. Fakes only:
no test starts Chrome or knocks on a port.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine

from netkeep import worker
from netkeep.chrome_spawn import ChromeLaunch, ChromeLauncher
from netkeep.config import Settings
from netkeep.db import session_scope
from netkeep.linkedin import activity_lock
from netkeep.linkedin.browser import ActivityLocks
from netkeep.models import User
from netkeep.paths import data_dir
from netkeep.services import ui_settings
from netkeep.services.chrome_launch import (
    LAUNCH_EVENT,
    LAUNCH_TASK,
    LaunchResult,
    LaunchStatus,
    PortProbe,
    Refusal,
)
from netkeep.services.events import Event
from netkeep.services.scheduled_runs import ServeExtractor
from netkeep.services.users import local_owner
from netkeep.web.app import create_app
from netkeep.worker import launcher_probe

CSRF = {"X-Netkeep-Client": "1"}
SETTINGS_URL = "/api/v1/settings/config"
HEALTH_URL = "/api/v1/linkedin/browser/health"
KEY = "linkedin.start_chrome_at_startup"


@dataclass
class _FakeSpawner:
    result: LaunchResult = field(
        default_factory=lambda: LaunchResult(LaunchStatus.STARTED, "started", pid=31)
    )
    calls: list[bool] = field(default_factory=list)
    gate: asyncio.Event | None = None

    async def start(self, *, first_run: bool = False) -> LaunchResult:
        self.calls.append(first_run)
        if self.gate is not None:
            await self.gate.wait()
        return self.result


async def _no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


def _turn_on(app: FastAPI, value: bool = True) -> None:
    with session_scope(app.state.session_factory, write=True) as session:
        owner = local_owner(session)
        assert owner is not None
        ui_settings.write(session, owner, app.state.settings, {KEY: value})


def _ready(app: FastAPI, spawner: _FakeSpawner, *, on: bool = True) -> None:
    app.state.chrome_launcher = spawner
    app.state.listening = lambda: True
    if on:
        _turn_on(app)


async def _start(app: FastAPI, **kwargs: Any) -> str | None:
    kwargs.setdefault("sleep", _no_sleep)
    kwargs.setdefault("run_holds_browser", lambda: False)
    task_id = await worker.start_chrome_at_startup(app, **kwargs)
    await app.state.tasks.join()
    return task_id


def _hold_profile(pid: int) -> None:
    """netkeep's profile held by ``pid``, as Chrome's ``SingletonLock`` names it."""
    profile = data_dir() / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "SingletonLock").symlink_to(f"{socket.gethostname()}-{pid}")


# --- the setting ---------------------------------------------------------------------------


def test_the_setting_is_off_by_default() -> None:
    assert Settings().linkedin.start_chrome_at_startup is False
    spec = ui_settings.BY_KEY[KEY]
    assert (spec.kind, spec.group, spec.applies) == ("bool", "linkedin_runs", "now")
    assert spec.label == "Start netkeep's Chrome when netkeep starts"
    assert "Run at login" in spec.help and "when you log in" in spec.help
    assert "sign in to LinkedIn yourself" in spec.help
    assert "already running, it opens this page in a new window there" in spec.help


async def test_the_setting_is_saved_and_read_back(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    before = next(f for f in (await client.get(SETTINGS_URL)).json()["fields"] if f["key"] == KEY)
    assert (before["value"], before["default"], before["source"]) == (False, False, "default")
    saved = await client.put(SETTINGS_URL, json={"values": {KEY: True}}, headers=CSRF)
    assert saved.status_code == 200, saved.text
    after = next(f for f in (await client.get(SETTINGS_URL)).json()["fields"] if f["key"] == KEY)
    assert (after["value"], after["source"]) == (True, "ui")
    with session_scope(running_app.state.session_factory) as session:
        owner = local_owner(session)
        assert isinstance(owner, User)
        resolved = ui_settings.resolve(session, owner, running_app.state.settings)
    assert resolved.linkedin.start_chrome_at_startup is True


# --- serve's start at startup ----------------------------------------------------------------


async def test_off_starts_nothing(running_app: FastAPI) -> None:
    spawner = _FakeSpawner()
    _ready(running_app, spawner, on=False)
    assert await _start(running_app) is None
    assert spawner.calls == []


async def test_on_starts_chrome_exactly_once_and_logs_the_outcome(
    running_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    spawner = _FakeSpawner()
    _ready(running_app, spawner)
    events = running_app.state.bus.subscribe()
    with caplog.at_level(logging.INFO, logger="netkeep.worker"):
        task_id = await _start(running_app)
    assert task_id is not None
    assert spawner.calls == [False]  # once, and never the first-run sign-in page
    assert "start netkeep's Chrome at startup: started: started" in caplog.text
    seen: list[Event] = []
    while not seen:
        event = await asyncio.wait_for(anext(events), 1)
        if event.type == LAUNCH_EVENT:
            seen.append(event)
    assert seen[0].data["task_id"] == task_id and seen[0].data["status"] == "started"


async def test_a_refusal_is_logged_with_its_case(
    running_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    refused = LaunchResult(
        LaunchStatus.REFUSED, "port 9333 answers, but not ours", refusal=Refusal.NOT_DEDICATED
    )
    spawner = _FakeSpawner(refused)
    _ready(running_app, spawner)
    with caplog.at_level(logging.INFO, logger="netkeep.worker"):
        await _start(running_app)
    assert spawner.calls == [False]
    assert "refused (not_dedicated): port 9333 answers, but not ours" in caplog.text


async def test_no_call_while_a_run_holds_the_browser(
    running_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    spawner = _FakeSpawner()
    _ready(running_app, spawner)
    with caplog.at_level(logging.INFO, logger="netkeep.worker"):
        assert await _start(running_app, run_holds_browser=lambda: True) is None
    assert spawner.calls == []
    assert "a run holds the browser" in caplog.text


async def test_a_real_run_lock_stops_it_too(running_app: FastAPI, tmp_path: Path) -> None:
    """The default check reads the real lock files in the data directory."""
    spawner = _FakeSpawner()
    _ready(running_app, spawner)
    claim = activity_lock.try_claim(activity_lock.account_key(1))
    assert claim is not None
    try:
        assert await worker.start_chrome_at_startup(running_app, sleep=_no_sleep) is None
    finally:
        claim.release()
    assert spawner.calls == []


async def test_a_running_chrome_is_handed_the_ui_window_once(
    running_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """A restart while netkeep's Chrome runs still shows the UI (#679)."""
    handed = LaunchResult(LaunchStatus.HANDED_OFF, "opened the UI", pid=os.getpid())
    spawner = _FakeSpawner(handed)
    _ready(running_app, spawner)
    _hold_profile(os.getpid())  # a live pid
    with caplog.at_level(logging.INFO, logger="netkeep.worker"):
        assert await _start(running_app) is not None
    assert spawner.calls == [False]  # one launcher call, which hands off
    assert f"already running (pid {os.getpid()}): opening the UI in it" in caplog.text
    assert "start netkeep's Chrome at startup: handed_off: opened the UI" in caplog.text


@dataclass
class _Reader:
    """The pid the Chrome on the port names over #675's identity read."""

    pid: int | None
    calls: list[str] = field(default_factory=list)

    async def read_pid(self, cdp_url: str) -> int | None:
        self.calls.append(cdp_url)
        return self.pid


@dataclass
class _Handed:
    """The launched Chrome passes its arguments to the running one and exits 0."""

    launches: list[ChromeLaunch] = field(default_factory=list)
    pid: int = 6464

    def poll(self) -> int | None:
        return 0

    def __call__(self, launch: ChromeLaunch) -> _Handed:
        self.launches.append(launch)
        return self


def _real_launcher(tmp_path: Path, reader: _Reader, spawned: _Handed) -> ChromeLauncher:
    """The real launcher on a held profile, with its probe, port check and spawn faked."""

    async def address() -> str:
        return "http://127.0.0.1:9333"

    async def probe(url: str) -> PortProbe:
        key = activity_lock.SINGLE_ACCOUNT_KEY
        locks = ActivityLocks(tmp_path / "locks")
        return await launcher_probe(url, locks=locks, account=key, reader=reader)

    async def no_sleep(_s: float) -> None:
        return None

    return ChromeLauncher(
        cdp_url=address,
        profile=data_dir() / "chrome-profile",
        app_url=lambda: "http://127.0.0.1:8123/api/v1/session/connect?code=1.ab.cd",
        ui_port=8123,
        probe=probe,
        run_holds_browser=lambda: False,
        pid_alive=lambda pid: pid == os.getpid(),
        spawn=spawned,
        answers=lambda port: True,
        sleep=no_sleep,
        settle_s=0.3,
        platform="darwin",
    )


async def test_the_hand_off_goes_through_the_identity_read(
    running_app: FastAPI, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The real launcher reads the pid, matches the profile's lock, then hands off once."""
    _hold_profile(os.getpid())
    reader, spawned = _Reader(pid=os.getpid()), _Handed()
    _ready(running_app, _FakeSpawner())
    running_app.state.chrome_launcher = _real_launcher(tmp_path, reader, spawned)
    with caplog.at_level(logging.INFO, logger="netkeep.worker"):
        assert await _start(running_app) is not None
    assert reader.calls == ["http://127.0.0.1:9333"]
    [launch] = spawned.launches
    assert launch.port == 9333 and not launch.first_run
    assert launch.app_url.startswith("http://127.0.0.1:8123/")
    assert "start netkeep's Chrome at startup: handed_off" in caplog.text


@pytest.mark.parametrize("pid", [4_000_001, None])
async def test_a_running_chrome_that_isnt_netkeeps_is_refused_and_logged(
    running_app: FastAPI, tmp_path: Path, caplog: pytest.LogCaptureFixture, pid: int | None
) -> None:
    _hold_profile(os.getpid())
    reader, spawned = _Reader(pid=pid), _Handed()
    _ready(running_app, _FakeSpawner())
    running_app.state.chrome_launcher = _real_launcher(tmp_path, reader, spawned)
    with caplog.at_level(logging.INFO, logger="netkeep.worker"):
        assert await _start(running_app) is not None
    assert spawned.launches == []
    assert "start netkeep's Chrome at startup: refused (not_dedicated)" in caplog.text


async def test_a_running_chrome_gets_nothing_while_a_run_holds_the_browser(
    running_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    spawner = _FakeSpawner()
    _ready(running_app, spawner)
    _hold_profile(os.getpid())
    with caplog.at_level(logging.INFO, logger="netkeep.worker"):
        assert await _start(running_app, run_holds_browser=lambda: True) is None
    assert spawner.calls == []
    assert "a run holds the browser" in caplog.text


async def test_a_running_chrome_gets_one_hand_off_while_a_launch_is_under_way(
    running_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    gate = asyncio.Event()
    spawner = _FakeSpawner(gate=gate)
    _ready(running_app, spawner)
    _hold_profile(os.getpid())
    with caplog.at_level(logging.INFO, logger="netkeep.worker"):
        first = await worker.start_chrome_at_startup(
            running_app, sleep=_no_sleep, run_holds_browser=lambda: False
        )
        second = await worker.start_chrome_at_startup(
            running_app, sleep=_no_sleep, run_holds_browser=lambda: False
        )
        gate.set()
        await running_app.state.tasks.join()
    assert first is not None and second is None and spawner.calls == [False]
    assert "a launch is already under way" in caplog.text


async def test_off_hands_nothing_to_a_running_chrome(running_app: FastAPI) -> None:
    spawner = _FakeSpawner()
    _ready(running_app, spawner, on=False)
    _hold_profile(os.getpid())
    assert await _start(running_app) is None
    assert spawner.calls == []


async def test_a_stale_profile_lock_doesnt_stop_it(running_app: FastAPI) -> None:
    spawner = _FakeSpawner()
    _ready(running_app, spawner)
    _hold_profile(4_000_000)
    assert await _start(running_app, pid_alive=lambda pid: False) is not None
    assert spawner.calls == [False]


async def test_no_call_while_a_launch_is_under_way(running_app: FastAPI) -> None:
    gate = asyncio.Event()
    button = _FakeSpawner(gate=gate)
    _ready(running_app, button)
    first = await worker.start_chrome_at_startup(
        running_app, sleep=_no_sleep, run_holds_browser=lambda: False
    )
    assert first is not None
    assert [t.name for t in running_app.state.tasks.active()] == [LAUNCH_TASK]
    second = await worker.start_chrome_at_startup(
        running_app, sleep=_no_sleep, run_holds_browser=lambda: False
    )
    gate.set()
    await running_app.state.tasks.join()
    assert second is None and button.calls == [False]


async def test_the_button_answers_409_while_the_startup_launch_settles(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    gate = asyncio.Event()
    spawner = _FakeSpawner(gate=gate)
    _ready(running_app, spawner)
    assert await worker.start_chrome_at_startup(
        running_app, sleep=_no_sleep, run_holds_browser=lambda: False
    )
    pressed = await client.post("/api/v1/linkedin/browser/start", json={}, headers=CSRF)
    gate.set()
    await running_app.state.tasks.join()
    assert pressed.status_code == 409 and spawner.calls == [False]


async def test_it_waits_until_serve_is_listening(running_app: FastAPI) -> None:
    spawner = _FakeSpawner()
    _ready(running_app, spawner)
    answers = iter([False, False, False, True])
    seen: list[bool] = []

    def listening() -> bool:
        answer = next(answers)
        # Nothing is started before the socket listens.
        seen.append(bool(spawner.calls))
        return answer

    running_app.state.listening = listening
    assert await _start(running_app) is not None
    assert seen == [False, False, False, False] and spawner.calls == [False]


async def test_it_gives_up_when_serve_never_listens(running_app: FastAPI) -> None:
    spawner = _FakeSpawner()
    _ready(running_app, spawner)
    running_app.state.listening = lambda: False
    assert await _start(running_app, wait_s=0.5) is None
    assert spawner.calls == []


async def test_without_a_listening_signal_it_starts_nothing(running_app: FastAPI) -> None:
    """``serve --reload``'s app has no server to watch."""
    spawner = _FakeSpawner()
    _ready(running_app, spawner)
    del running_app.state.listening
    assert await _start(running_app) is None
    assert spawner.calls == []


def test_serve_app_wires_the_startup_hook() -> None:
    app = worker.serve_app(Settings(), ui_base="http://127.0.0.1:8124")
    assert callable(app.state.after_listening)


async def test_the_hook_logs_what_the_start_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(app: FastAPI) -> str | None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(worker, "start_chrome_at_startup", broken)
    app = worker.serve_app(Settings(), ui_base="http://127.0.0.1:8124")
    with caplog.at_level(logging.ERROR, logger="netkeep.worker"):
        await app.state.after_listening()
    assert "could not start netkeep's Chrome at startup" in caplog.text


async def test_the_lifespan_runs_the_hook_as_a_task_and_cancels_it_at_shutdown(
    bare_engine: Engine,
    tmp_path: Path,
    _migrated_template: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup never waits for the hook, and stopping serve cancels a hook still waiting."""
    import conftest

    conftest._copy_template(_migrated_template, tmp_path / "bare")
    monkeypatch.setenv("NETKEEP_FRONTEND_DIST", str(tmp_path / "no-dist"))
    extractor = ServeExtractor(executor=lambda factory, bus: object())  # type: ignore[arg-type,return-value]
    app = create_app(Settings(), engine=bare_engine, extractor=extractor)
    began = asyncio.Event()
    cancelled: list[bool] = []

    async def hook() -> None:
        began.set()
        try:
            await asyncio.Event().wait()  # never listening
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    app.state.after_listening = hook
    lifespan = app.router.lifespan_context(app)
    # A hook awaited inline would hang startup: the timeout fails the test instead.
    await asyncio.wait_for(lifespan.__aenter__(), 5)
    try:
        await asyncio.wait_for(began.wait(), 1)
        assert cancelled == []
    finally:
        await asyncio.wait_for(lifespan.__aexit__(None, None, None), 5)
    await asyncio.sleep(0)
    assert cancelled == [True]


def test_serve_tells_the_startup_hook_when_it_is_listening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cli serve`` sets ``app.state.listening`` to uvicorn's ``started``: without it,
    the start at startup never starts Chrome."""
    import uvicorn
    from typer.testing import CliRunner

    from netkeep import cli as cli_module
    from netkeep.cli import app as cli_app

    application = FastAPI()
    seen: list[bool] = []

    class _Server:
        def __init__(self, config: uvicorn.Config) -> None:
            self.config = config
            self.should_exit = False
            self.started = False

        def run(self) -> None:
            listening = application.state.listening
            seen.append(listening())
            self.started = True
            seen.append(listening())

    monkeypatch.setattr(cli_module, "serve_app", lambda settings, **_: application)
    monkeypatch.setattr(uvicorn, "Server", _Server)
    result = CliRunner().invoke(cli_app, ["serve", "--port", "0"])
    assert result.exit_code == 0, result.output
    assert seen == [False, True]


def test_posture_no_probe_says_whether_netkeeps_chrome_is_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#599: ``netkeep posture --no-probe`` reads the profile's lock, never the port."""
    from typer.testing import CliRunner

    from netkeep import migrations
    from netkeep.cli import app as cli_app
    from netkeep.db import database_url, make_engine, make_session_factory
    from netkeep.scoping import install_scope_guard
    from netkeep.services.users import ensure_local_user

    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEP_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        ensure_local_user(session)
    engine.dispose()
    before = CliRunner().invoke(cli_app, ["posture", "--no-probe", "--details"])
    assert "netkeep's Chrome isn't running" in before.output, before.output
    _hold_profile(os.getpid())
    after = CliRunner().invoke(cli_app, ["posture", "--no-probe", "--details"])
    assert f"netkeep's Chrome is running (pid {os.getpid()})" in after.output, after.output


# --- what the button reads -----------------------------------------------------------------


async def test_health_says_whether_netkeeps_chrome_is_running(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    body = (await client.get(HEALTH_URL)).json()
    assert (body["chrome_running"], body["chrome_pid"]) == (False, None)
    assert (body["browser_held"], body["can_start_chrome"]) == (False, False)
    _hold_profile(os.getpid())
    running_app.state.chrome_launcher = _FakeSpawner()
    body = (await client.get(HEALTH_URL)).json()
    assert (body["chrome_running"], body["chrome_pid"]) == (True, os.getpid())
    assert body["can_start_chrome"] is True


async def test_health_ignores_a_lock_whose_process_is_gone(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold_profile(4_000_001)
    monkeypatch.setattr(activity_lock, "pid_alive", lambda pid: False)
    body = (await client.get(HEALTH_URL)).json()
    assert (body["chrome_running"], body["chrome_pid"]) == (False, None)


async def test_health_says_when_a_run_holds_the_browser(client: httpx.AsyncClient) -> None:
    claim = activity_lock.try_claim(activity_lock.account_key(1))
    assert claim is not None
    try:
        assert (await client.get(HEALTH_URL)).json()["browser_held"] is True
    finally:
        claim.release()


async def test_only_the_owner_is_offered_the_button(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    import factories
    from isolation.harness import FixedUser

    from netkeep.models import UserKind

    running_app.state.chrome_launcher = _FakeSpawner()
    with session_scope(running_app.state.session_factory, write=True) as session:
        other = factories.make_user(session, kind=UserKind.HOSTED).id
    running_app.state.auth = FixedUser(other)
    assert (await client.get(HEALTH_URL)).json()["can_start_chrome"] is False
