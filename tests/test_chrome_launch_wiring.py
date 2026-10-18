"""How the Chrome launcher is reached (#636): ``serve``'s wiring, the route, ``netkeeper open``.

The launcher's own checks and arguments are in ``tests/test_chrome_spawn.py``; this file
covers its callers. Fakes only: no test starts Chrome or knocks on a port.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

import factories
import httpx
import pytest
from browser_fakes import FakeConnector
from fastapi import FastAPI
from isolation.harness import FixedUser
from typer.testing import CliRunner

from netkeeper import chrome_spawn
from netkeeper.chrome_spawn import ChromeLaunch, ChromeLauncher
from netkeeper.cli import app as cli
from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.linkedin import activity_lock
from netkeeper.linkedin.browser import ActivityLocks, BrowserUnavailable
from netkeeper.models import UserKind
from netkeeper.paths import data_dir
from netkeeper.services import runs
from netkeeper.services.chrome_launch import (
    LAUNCH_EVENT,
    LaunchResult,
    LaunchStatus,
    PortProbe,
    Refusal,
)
from netkeeper.services.events import Event
from netkeeper.worker import launcher_probe, serve_app

CSRF = {"X-Netkeeper-Client": "1"}
START = "/api/v1/linkedin/browser/start"
CDP_URL = "http://127.0.0.1:9333"


# --- the browser locks the launcher reads -------------------------------------------------


def test_any_browser_lock_held_sees_every_accounts_lock(tmp_path: Path) -> None:
    assert not runs.any_browser_lock_held(tmp_path / "none-yet")
    claim = activity_lock.try_claim(activity_lock.account_key(7), tmp_path)
    assert claim is not None
    try:
        assert runs.any_browser_lock_held(tmp_path)
    finally:
        claim.release()
    assert not runs.any_browser_lock_held(tmp_path)  # the file stays; nobody holds it


# --- the probe: the attach's connector, under the lock --------------------------------------


async def test_the_probe_reads_the_pid_and_detaches(tmp_path: Path) -> None:
    connector = FakeConnector(browser_pid=4242)
    locks = ActivityLocks(tmp_path)
    probe = await launcher_probe(
        CDP_URL, locks=locks, account=activity_lock.SINGLE_ACCOUNT_KEY, connector=connector
    )
    assert probe == PortProbe(busy=False, browser_pid=4242)
    assert connector.connect_calls == [CDP_URL] and connector.detaches == 1


async def test_the_probe_opens_no_second_client_while_a_run_holds_the_lock(
    tmp_path: Path,
) -> None:
    connector = FakeConnector()
    held = activity_lock.try_claim(activity_lock.SINGLE_ACCOUNT_KEY, tmp_path)
    assert held is not None
    try:
        probe = await launcher_probe(
            CDP_URL,
            locks=ActivityLocks(tmp_path),
            account=activity_lock.SINGLE_ACCOUNT_KEY,
            connector=connector,
        )
    finally:
        held.release()
    assert probe == PortProbe(busy=True)
    assert connector.connect_calls == []


async def test_a_port_that_isnt_chrome_names_no_pid(tmp_path: Path) -> None:
    connector = FakeConnector(error=BrowserUnavailable("not Chrome"))
    probe = await launcher_probe(
        CDP_URL,
        locks=ActivityLocks(tmp_path),
        account=activity_lock.SINGLE_ACCOUNT_KEY,
        connector=connector,
    )
    assert probe == PortProbe(busy=False, browser_pid=None)


# --- serve's wiring ---------------------------------------------------------------------


def test_serve_puts_the_launcher_on_app_state_and_starts_nothing() -> None:
    app = serve_app(Settings(), ui_base="http://127.0.0.1:8124")
    launcher = app.state.chrome_launcher
    assert isinstance(launcher, ChromeLauncher)
    assert launcher.profile == data_dir() / "chrome-profile"


# --- the route --------------------------------------------------------------------------


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


async def test_without_a_launcher_the_route_answers_503(client: httpx.AsyncClient) -> None:
    response = await client.post(START, json={}, headers=CSRF)
    assert response.status_code == 503
    assert "no Chrome launcher" in response.json()["detail"]


@pytest.mark.parametrize("first_run", [False, True])
async def test_the_route_submits_the_launch_and_reports_it_as_an_event(
    client: httpx.AsyncClient, running_app: FastAPI, first_run: bool
) -> None:
    spawner = _FakeSpawner(LaunchResult(LaunchStatus.REFUSED, "no", refusal=Refusal.NOT_DEDICATED))
    running_app.state.chrome_launcher = spawner
    events = running_app.state.bus.subscribe()
    body = {"first_run": True} if first_run else {}
    response = await client.post(START, json=body, headers=CSRF)
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    await running_app.state.tasks.join()
    assert spawner.calls == [first_run]
    seen: list[Event] = []
    while not seen:
        event = await asyncio.wait_for(anext(events), 1)
        if event.type == LAUNCH_EVENT:
            seen.append(event)
    assert seen[0].data == {
        "task_id": task_id,
        "status": "refused",
        "refusal": "not_dedicated",
        "message": "no",
        "pid": None,
    }


async def test_one_launch_at_a_time(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    gate = asyncio.Event()
    running_app.state.chrome_launcher = _FakeSpawner(gate=gate)
    first = await client.post(START, json={}, headers=CSRF)
    second = await client.post(START, json={}, headers=CSRF)
    gate.set()
    await running_app.state.tasks.join()
    assert first.status_code == 202 and second.status_code == 409


async def test_only_the_owner_starts_chrome(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    spawner = _FakeSpawner()
    running_app.state.chrome_launcher = spawner
    with session_scope(running_app.state.session_factory, write=True) as session:
        other = factories.make_user(session, kind=UserKind.HOSTED).id
    running_app.state.auth = FixedUser(other)
    response = await client.post(START, json={}, headers=CSRF)
    assert response.status_code == 403 and spawner.calls == []


async def test_the_route_refuses_unknown_fields(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    running_app.state.chrome_launcher = _FakeSpawner()
    response = await client.post(START, json={"url": "https://example.com/"}, headers=CSRF)
    assert response.status_code == 422


# --- netkeeper open ---------------------------------------------------------------------


@dataclass
class _Spawned:
    launches: list[ChromeLaunch] = field(default_factory=list)
    exits: int | None = 0

    def __call__(self, launch: ChromeLaunch) -> object:
        self.launches.append(launch)
        exits = self.exits

        class _P:
            pid = 99

            def poll(self) -> int | None:
                return exits

        return _P()


def _config(tmp_path: Path) -> list[str]:
    config = tmp_path / "config.toml"
    config.write_text(f'[linkedin]\ncdp_url = "{CDP_URL}"\n')
    return ["--config", str(config)]


def test_netkeeper_open_opens_the_ui_in_netkeepers_chrome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spawned = _Spawned()
    monkeypatch.setattr(chrome_spawn, "spawn_chrome", spawned)
    monkeypatch.setattr(chrome_spawn, "port_answers", lambda port: False)
    result = CliRunner().invoke(cli, [*_config(tmp_path), "open", "--port", "8125"])
    assert result.exit_code == 0, result.output
    [launch] = spawned.launches
    assert launch.port == 9333 and not launch.first_run
    assert launch.profile == data_dir() / "chrome-profile"
    assert launch.app_url.startswith("http://127.0.0.1:8125/api/v1/session/connect?code=")
    # The printed fallback link is a second, separate code.
    [link] = re.findall(r"http://\S+", result.output)
    assert link != launch.app_url and link.startswith("http://127.0.0.1:8125/")


def test_netkeeper_open_says_why_it_refused_and_still_prints_the_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spawned = _Spawned()
    monkeypatch.setattr(chrome_spawn, "spawn_chrome", spawned)
    monkeypatch.setattr(chrome_spawn, "port_answers", lambda port: False)
    held = activity_lock.try_claim(activity_lock.SINGLE_ACCOUNT_KEY)
    assert held is not None
    try:
        result = CliRunner().invoke(cli, [*_config(tmp_path), "open"])
    finally:
        held.release()
    assert result.exit_code == 1
    assert "a run holds the browser" in result.output
    assert re.findall(r"http://\S+/api/v1/session/connect\S+", result.output)
    assert spawned.launches == []


def test_netkeeper_open_link_only_starts_nothing() -> None:
    # conftest's blocked spawn_chrome and port_answers would raise if either ran.
    result = CliRunner().invoke(cli, ["open", "--link-only"])
    assert result.exit_code == 0, result.output
    assert len(re.findall(r"http://\S+", result.output)) == 1
