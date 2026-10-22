"""``/login-item`` (#600): owner only, state from the fake launchctl, never a second serve."""

from __future__ import annotations

import os
from pathlib import Path

import factories
import httpx
import pytest
from fastapi import FastAPI
from isolation.harness import acting_as
from test_login_item import FakeLaunchctl, make
from test_runs_serve import HEADERS

from netkeep.db import session_scope
from netkeep.models import UserKind
from netkeep.services.launcher_app import Launcher

URL = "/api/v1/login-item"


def _install(running_app: FastAPI, tmp_path: Path, macos: bool = True) -> FakeLaunchctl:
    launchctl = FakeLaunchctl()
    running_app.state.login_item = make(
        launchctl, tmp_path, tmp_path / "data", lock_held=True, macos=macos
    )
    _install_launcher(running_app, tmp_path, macos=macos)
    return launchctl


def _install_launcher(running_app: FastAPI, tmp_path: Path, *, macos: bool = True) -> Launcher:
    launcher = Launcher(
        data=tmp_path / "data",
        applications_dir=tmp_path / "Applications",
        executable=tmp_path / "bin" / "netkeep",
        macos=macos,
        environ={},
    )
    running_app.state.launcher_app = launcher
    return launcher


async def test_off_then_on_then_off(
    running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    launchctl = _install(running_app, tmp_path)
    first = (await client.get(URL)).json()
    assert first["supported"] is True
    assert first["enabled"] is False
    assert first["state"] == "Off"
    assert first["plist_path"].startswith(str(tmp_path / "LaunchAgents"))

    on = await client.put(URL, json={"enabled": True}, headers=HEADERS)
    assert on.status_code == 200
    assert on.json()["enabled"] is True
    assert on.json()["state"] == "Registered: starts at next login"
    assert on.json()["message"].startswith("Registered. netkeep will start by itself")
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


async def test_repair_adopts_an_old_agent_and_keeps_the_port(
    running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    from test_login_item import write_old_plist

    launchctl = _install(running_app, tmp_path)
    old = write_old_plist(tmp_path, tmp_path / "data", "--port", "8080")
    assert (await client.get(URL)).json()["legacy"] is True
    response = await client.post(f"{URL}/repair", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["legacy"] is False
    assert body["enabled"] is True
    assert not old.exists()
    assert "bootstrap" not in launchctl.verbs()
    plist = next((tmp_path / "LaunchAgents").glob("*.plist"))
    assert b"<string>8080</string>" in plist.read_bytes()


async def test_switching_off_and_on_keeps_the_port(
    running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    from test_login_item import make

    item = make(FakeLaunchctl(), tmp_path, tmp_path / "data", lock_held=True)
    running_app.state.login_item = item
    _install_launcher(running_app, tmp_path)
    item.enable(port=9001)
    await client.put(URL, json={"enabled": True}, headers=HEADERS)
    assert b"<string>9001</string>" in item.plist_path.read_bytes()


# --- the launcher app (#754) ---------------------------------------------------------------


async def test_the_launcher_shows_missing_then_built_after_rebuild(
    running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    launchctl = _install(running_app, tmp_path)
    launcher: Launcher = running_app.state.launcher_app
    first = (await client.get(URL)).json()["launcher"]
    assert first == {
        "supported": True,
        "state": "Not built",
        "exists": False,
        "path": str(launcher.path),
        "runs": None,
    }

    response = await client.post(f"{URL}/launcher", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["launcher"]["state"] == "Built" and body["launcher"]["exists"] is True
    assert body["launcher"]["runs"] == str(tmp_path / "bin" / "netkeep")
    assert body["message"].startswith(f"Built {launcher.path}")
    assert launcher.up_to_date()
    assert launcher.path.parent == tmp_path / "Applications"
    assert [c for c in launchctl.calls if c[0] not in ("print", "print-disabled")] == []

    again = await client.post(f"{URL}/launcher", headers=HEADERS)
    assert again.json()["message"].startswith("Rebuilt ")


async def test_the_launcher_rebuild_refuses_another_app_with_a_409(
    running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _install(running_app, tmp_path)
    launcher: Launcher = running_app.state.launcher_app
    launcher.path.mkdir(parents=True)
    response = await client.post(f"{URL}/launcher", headers=HEADERS)
    assert response.status_code == 409
    assert "isn't netkeep's launcher" in response.json()["detail"]
    assert (await client.get(URL)).json()["launcher"]["state"] == "Another app is in the way"


async def test_the_launcher_off_macos_is_unsupported(
    running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _install(running_app, tmp_path, macos=False)
    assert (await client.get(URL)).json()["launcher"]["supported"] is False
    assert (await client.post(f"{URL}/launcher", headers=HEADERS)).status_code == 409
    assert not (tmp_path / "Applications").exists()


async def test_a_second_user_cannot_rebuild_the_launcher(
    running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _install(running_app, tmp_path)
    with session_scope(running_app.state.session_factory, write=True) as session:
        other = factories.make_user(session, kind=UserKind.HOSTED).id
    with acting_as(running_app, other):
        assert (await client.post(f"{URL}/launcher", headers=HEADERS)).status_code == 403
    assert not (tmp_path / "Applications").exists()


async def test_an_unsearchable_applications_folder_still_shows_the_card(
    running_app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores folder permissions")
    _install(running_app, tmp_path)
    applications = tmp_path / "Applications"
    applications.mkdir()
    applications.chmod(0)
    try:
        response = await client.get(URL)
        rebuilt = await client.post(f"{URL}/launcher", headers=HEADERS)
    finally:
        applications.chmod(0o755)
    assert response.status_code == 200
    launcher = response.json()["launcher"]
    assert launcher["state"].startswith("Couldn't check: ") and launcher["exists"] is False
    assert rebuilt.status_code == 409
    assert "Permission denied" in rebuilt.json()["detail"]
