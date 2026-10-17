"""``/backups`` is the local owner's alone (#616).

The backups are the whole database's, so a second user gets 403 on both endpoints and
the prune uses the owner's ``backup.keep``, never the requester's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import factories
import httpx
import pytest
from fastapi import FastAPI
from isolation.harness import acting_as
from sqlalchemy import select
from test_runs_serve import HEADERS

from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.models import User, UserKind
from netkeeper.paths import DATA_ENV
from netkeeper.services import ui_settings
from netkeeper.services.backup import backup_name

URL = "/api/v1/backups"
OLD = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The data directory the ``app`` fixture's database lives in."""
    monkeypatch.setenv(DATA_ENV, str(tmp_path / "bare"))
    return tmp_path / "bare" / "backups"


def _files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.sqlite3"))


def _seed(directory: Path, count: int) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = [directory / backup_name(OLD + timedelta(days=n)) for n in range(count)]
    for path in paths:
        path.write_bytes(b"x")
    return paths


def _set_keep(app: FastAPI, user_id: int, keep: int) -> None:
    with session_scope(app.state.session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        ui_settings.write(session, user, Settings(), {"backup.keep": keep})


def _owner_id(app: FastAPI) -> int:
    with session_scope(app.state.session_factory) as session:
        return (
            session.scalars(
                select(User.id).where(User.kind == UserKind.LOCAL).order_by(User.id)
            ).first()
            or 0
        )


def _second_user(app: FastAPI) -> int:
    with session_scope(app.state.session_factory, write=True) as session:
        return factories.make_user(session, kind=UserKind.HOSTED).id


async def test_the_owner_lists_and_creates(client: httpx.AsyncClient, _data_dir: Path) -> None:
    listed = await client.get(URL)
    assert listed.status_code == 200
    created = await client.post(URL, headers=HEADERS)
    assert created.status_code == 201
    assert len(_files(_data_dir)) == 1


@pytest.mark.parametrize("method", ["get", "post"])
async def test_a_second_user_gets_403(
    running_app: FastAPI, client: httpx.AsyncClient, _data_dir: Path, method: str
) -> None:
    seeded = _seed(_data_dir, 3)
    other = _second_user(running_app)
    _set_keep(running_app, other, 1)
    with acting_as(running_app, other):
        response = await client.request(method, URL, headers=HEADERS)
    assert response.status_code == 403
    assert _files(_data_dir) == sorted(seeded)


async def test_a_second_local_user_is_not_the_owner(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    with session_scope(running_app.state.session_factory, write=True) as session:
        other = factories.make_user(session, kind=UserKind.LOCAL).id
    with acting_as(running_app, other):
        assert (await client.get(URL)).status_code == 403


async def test_the_prune_uses_the_owners_keep(
    running_app: FastAPI, client: httpx.AsyncClient, _data_dir: Path
) -> None:
    _seed(_data_dir, 5)
    other = _second_user(running_app)
    _set_keep(running_app, other, 1)
    _set_keep(running_app, _owner_id(running_app), 3)
    response = await client.post(URL, headers=HEADERS)
    assert response.status_code == 201
    assert len(_files(_data_dir)) == 3


def test_get_backups_lists_the_403_in_its_schema(running_app: FastAPI) -> None:
    schema = running_app.openapi()["paths"][URL]
    assert "403" in schema["get"]["responses"]
    assert "403" in schema["post"]["responses"]
