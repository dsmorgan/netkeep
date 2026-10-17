"""``/login-item`` (#600): owner only, state from the fake launchctl, never a second serve."""

from __future__ import annotations

from pathlib import Path

import factories
import httpx
from fastapi import FastAPI
from isolation.harness import acting_as
from test_login_item import FakeLaunchctl, make
from test_runs_serve import HEADERS

from netkeeper.db import session_scope
from netkeeper.models import UserKind

URL = "/api/v1/login-item"


def _install(running_app: FastAPI, tmp_path: Path, macos: bool = True) -> FakeLaunchctl:
    launchctl = FakeLaunchctl()
    running_app.state.login_item = make(
        launchctl, tmp_path, tmp_path / "data", lock_held=True, macos=macos
    )
    return launchctl


async def test_off_then_on_then_off(
    running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    launchctl = _install(running_app, tmp_path)
    first = (await client.get(URL)).json()
    assert first["supported"] is True
    assert first["enabled"] is False
    assert first["plist_path"].startswith(str(tmp_path / "LaunchAgents"))

    on = await client.put(URL, json={"enabled": True}, headers=HEADERS)
    assert on.status_code == 200
    assert on.json()["enabled"] is True
    assert "next login" in on.json()["message"]
    assert "bootstrap" not in launchctl.verbs()  # this server is already running
    assert (await client.get(URL)).json()["enabled"] is True

    off = await client.put(URL, json={"enabled": False}, headers=HEADERS)
    assert off.json()["enabled"] is False
    assert not list((tmp_path / "LaunchAgents").glob("*.plist"))


async def test_not_macos_is_reported_and_refuses_changes(
    running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    launchctl = _install(running_app, tmp_path, macos=False)
    body = (await client.get(URL)).json()
    assert body["supported"] is False
    assert "macOS" in body["unsupported_reason"]
    refused = await client.put(URL, json={"enabled": True}, headers=HEADERS)
    assert refused.status_code == 409
    assert launchctl.calls == []


async def test_a_launchctl_failure_is_a_409_with_the_reason(
    running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    launchctl = _install(running_app, tmp_path)
    launchctl.fail = "enable"
    response = await client.put(URL, json={"enabled": True}, headers=HEADERS)
    assert response.status_code == 409
    assert "Input/output error" in response.json()["detail"]


async def test_a_second_user_gets_403(
    running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    launchctl = _install(running_app, tmp_path)
    with session_scope(running_app.state.session_factory, write=True) as session:
        other = factories.make_user(session, kind=UserKind.HOSTED).id
    with acting_as(running_app, other):
        assert (await client.get(URL)).status_code == 403
        assert (await client.put(URL, json={"enabled": True}, headers=HEADERS)).status_code == 403
    assert launchctl.calls == []
