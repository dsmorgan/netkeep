import shutil
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import factories
import httpx
import keyring
import pytest
from browser_guard import RealBrowserBlocked, is_personal_cdp, refuse_live_port
from fastapi import FastAPI
from gmail_fakes import FakeGoogle, MemoryKeyring
from local_api import LocalKey
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from netkeep import chrome_spawn, cli, migrate_name, migrations, paths
from netkeep.campaigns import gmail_oauth
from netkeep.config import Settings
from netkeep.db import DATABASE_FILENAME, database_url, make_engine, make_session_factory
from netkeep.logging_setup import remove_file_handler
from netkeep.models import Base
from netkeep.scoping import install_scope_guard
from netkeep.services import login_item
from netkeep.web.app import create_app

# The per-test time limit (#210), a small heap for short collections (#478), and pytester
# for the test that shows the limit fails a test.
pytest_plugins = ["time_limit", "heap", "pytester"]


@pytest.fixture(autouse=True)
def _reset_factory_counters() -> None:
    """Every test's first factory rows are ``User 1`` and ``First1 Last1``."""
    factories.reset_counters()


@pytest.fixture(autouse=True)
def _clean_netkeep_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's shell environment, and their data directory, out of every test.

    ``NETKEEP_DATA`` points at a fresh directory rather than being unset: unset, the
    data directory on macOS is the real one under Application Support, and the
    activity lock writes its lock files there the moment a test opens a browser run.
    A test about the default location deletes the variable itself.

    The names before the rename (``NETKEEPER_*``, #661) are cleared too, so a shell's
    ``NETKEEPER_DATA`` never reaches a test through the fallback.
    """
    for name in (
        "NETKEEP_CONFIG",
        "NETKEEP_LOG_LEVEL",
        "NETKEEP_LOG_STDERR_LEVEL",
        "NETKEEP_DATABASE_URL",
        "NETKEEP_FRONTEND_DIST",
        "NETKEEP_KEYCHAIN_SERVICE",
        "NETKEEP_DATA",
    ):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(paths.legacy_env_name(name), raising=False)
    monkeypatch.setenv("NETKEEP_DATA", str(tmp_path / "netkeep-data"))
    # A fake home folder (#661): a test that resolves the default data directory, or the
    # one from before the rename, gets one beside tmp_path, never the real one, where a
    # stray netkeep folder would block the real migration ("both exist").
    home = tmp_path.parent / f"{tmp_path.name}.home"  # beside tmp_path, not in it
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    # The working directory too (#703): the config search reads ``./config.toml`` first,
    # so a test run from a checkout holding one would load the developer's settings. A
    # script a test runs inherits it.
    monkeypatch.chdir(tmp_path)


REAL_HOME = Path.home().resolve()
"""The real home folder, read before any test can fake it."""


@pytest.fixture(autouse=True)
def _no_real_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test runs the rename's automatic migration (#661), and none reaches the real
    home folder through the migration: a test of it fakes ``HOME`` and calls it itself."""

    def guard(path: Path) -> None:
        resolved = path.expanduser().resolve()
        if resolved == REAL_HOME or REAL_HOME in resolved.parents:
            raise AssertionError(f"a migration test reached the real home folder: {path}")

    monkeypatch.setattr(migrate_name, "path_guard", guard)
    monkeypatch.setattr(cli, "_auto_migrate", lambda _command: [])


@pytest.fixture(autouse=True)
def _no_real_launchd(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test runs the real ``launchctl`` or writes to the real ``~/Library/LaunchAgents``
    (#600): a test of the login item hands ``LoginItem`` its own runner and directory."""

    def blocked(*_args: object) -> object:
        raise AssertionError("a test must give LoginItem a fake runner and agents_dir")

    monkeypatch.setattr(login_item, "run_launchctl", blocked)
    monkeypatch.setattr(login_item, "default_agents_dir", blocked)


@pytest.fixture(autouse=True)
def _no_real_chrome_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test starts Chrome or knocks on a real port (#636): a test of the launcher hands
    ``ChromeLauncher`` its own ``spawn`` and ``answers``, and a test of
    ``spawn_chrome`` itself gives the module a fake ``subprocess``."""

    def blocked(*_args: object) -> object:
        raise AssertionError("a test must give ChromeLauncher a fake spawn and answers")

    monkeypatch.setattr(chrome_spawn, "spawn_chrome", blocked)
    monkeypatch.setattr(chrome_spawn, "port_answers", blocked)


@pytest.fixture(autouse=True)
def _close_log_file() -> Iterator[None]:
    """A test that set up logging (a CLI command, ``setup_logging``) leaves no log file
    open on its data directory for the next test to write into (#602)."""
    yield
    remove_file_handler()


@pytest.fixture(autouse=True)
def _no_live_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """No socket this test process opens connects to port 9222 or 8000, on any host
    (#675 review: a mutant reader reached 9222). Raised before the connect, as a
    :class:`RealBrowserBlocked`, so nothing catches it as a failed attach.

    What it covers: ``socket.socket.connect`` and ``connect_ex`` in this process, which
    asyncio, ``websockets``, ``http.client`` and raw sockets all go through. What it
    doesn't: a subprocess (Playwright's driver, a child test process), which
    ``_no_real_browser`` and ``UNREACHABLE_CDP_URL`` cover instead, and a session-scoped
    fixture, which runs before this function-scoped one."""
    import socket

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def connect(self: socket.socket, address: object) -> None:
        refuse_live_port(address)
        real_connect(self, address)  # type: ignore[arg-type]

    def connect_ex(self: socket.socket, address: object) -> int:
        refuse_live_port(address)
        return real_connect_ex(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


@pytest.fixture(autouse=True)
def _no_real_browser(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches a real Chrome unless it opts out, and none ever reaches port 9222.

    The only place netkeep opens a browser connection is
    ``PlaywrightCdpConnector.connect`` (``tests/test_browser_safety.py`` keeps it that
    way), so that is replaced here with a guard that raises
    :class:`RealBrowserBlocked` before Playwright starts -- the CDP socket is opened
    by Playwright's driver process, where a Python socket spy could not see it.
    ``playwright.async_api.async_playwright`` is blocked too, for any route that
    skips the connector. The launcher probe's ``CdpIdentityReader.read`` (#675),
    which opens its own websocket, is guarded the same way.

    A test that must drive a real, isolated Chrome (the opt-in smoke suite under
    ``tests/smoke/``) opts out with ``@pytest.mark.real_cdp``. Even then the connector
    refuses the default ``http://127.0.0.1:9222`` and every other port-9222 address:
    point ``NETKEEP_CDP_URL`` at an isolated Chrome on another port.
    """
    from netkeep.linkedin import browser

    real_connect = browser.PlaywrightCdpConnector.connect
    opted_out = request.node.get_closest_marker("real_cdp") is not None

    async def guarded_connect(self: object, cdp_url: str) -> object:
        if not opted_out:
            raise RealBrowserBlocked(
                f"a test tried to attach to a real Chrome at {cdp_url}. Give it a fake"
                " connector (browser_fakes.FakeConnector) or a provider that fails if used"
            )
        if is_personal_cdp(cdp_url):
            raise RealBrowserBlocked(
                f"a real_cdp test tried to attach to {cdp_url}, a personal Chrome's debug"
                " port. Set NETKEEP_CDP_URL to an isolated Chrome on another port"
            )
        return await real_connect(self, cdp_url)  # type: ignore[arg-type]

    monkeypatch.setattr(browser.PlaywrightCdpConnector, "connect", guarded_connect)

    # #675: the launcher's probe reads the pid over its own websocket, not Playwright,
    # so its reader is guarded the same way. ``read_pid`` goes through ``read``, which
    # also holds #678's front step.
    real_read = browser.CdpIdentityReader.read

    async def guarded_read(self: object, cdp_url: str, **front: Any) -> object:
        if not opted_out:
            raise RealBrowserBlocked(
                f"a test tried to read the pid of a real Chrome at {cdp_url}. Give"
                " launcher_probe a fake reader"
            )
        if is_personal_cdp(cdp_url):
            raise RealBrowserBlocked(
                f"a real_cdp test tried to read the pid of {cdp_url}, a personal Chrome's"
                " debug port. Set NETKEEP_CDP_URL to an isolated Chrome on another port"
            )
        return await real_read(self, cdp_url, **front)  # type: ignore[arg-type]

    monkeypatch.setattr(browser.CdpIdentityReader, "read", guarded_read)
    if not opted_out:
        import playwright.async_api

        def blocked_playwright(*_: object, **__: object) -> None:
            raise RealBrowserBlocked("a test tried to start Playwright without opting in")

        monkeypatch.setattr(playwright.async_api, "async_playwright", blocked_playwright)


#: What ``gmail_oauth.GOOGLE`` is during a test: a scheme urllib cannot open, so a
#: call that forgot its fake fails at once with ``OAuthUnavailable``, offline.
NO_GOOGLE = gmail_oauth.GoogleEndpoints(
    auth_uri="blocked-in-tests://google/auth",
    token_uri="blocked-in-tests://google/token",
    profile_uri="blocked-in-tests://google/profile",
    use_system_proxy=False,
)


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryKeyring]:
    """No test touches a real Keychain, and no test can reach Google (#244)."""
    backend = MemoryKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    monkeypatch.setattr(gmail_oauth, "GOOGLE", NO_GOOGLE)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def fake_google(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGoogle]:
    """Google's token endpoint and Gmail's profile call, faked on loopback, as ``GOOGLE``."""
    fake = FakeGoogle()
    fake.start()
    monkeypatch.setattr(gmail_oauth, "GOOGLE", fake.endpoints)
    yield fake
    fake.stop()


def _template(directory: Path, build: Callable[[Engine], None]) -> Path:
    """A SQLite file under ``directory`` that ``build`` filled, closed and checkpointed.

    Built straight from a path, never through ``database_url``: a session fixture runs
    before ``_clean_netkeep_env`` could clear a ``NETKEEP_DATABASE_URL``.
    """
    file = directory / DATABASE_FILENAME
    engine = make_engine(f"sqlite:///{file}")
    try:
        build(engine)
    finally:
        engine.dispose()  # the last close checkpoints WAL into the file and removes it
    assert not file.with_name(f"{file.name}-wal").exists(), "the template is not one file"
    return file


def _copy_template(template: Path, data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, data_dir / DATABASE_FILENAME)


@pytest.fixture(scope="session")
def _schema_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The models' schema, built once per run (once per xdist worker) (#210).

    ``create_all`` costs about 40 ms and a thousand tests ask for it; a copy of the
    file costs well under one.
    """
    return _template(tmp_path_factory.mktemp("schema-template"), Base.metadata.create_all)


@pytest.fixture(scope="session")
def _migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An empty database the migrations have run on, built once per run (#210).

    The app's startup migrates its database; from empty that costs about 130 ms, and
    on this copy it finds nothing to do. Migrating from empty is tests/test_migrations.py's
    subject, and tests that need it take ``bare_engine``.
    """
    return _template(tmp_path_factory.mktemp("migrated-template"), migrations.upgrade)


@pytest.fixture
def engine(tmp_path: Path, _schema_template: Path) -> Iterator[Engine]:
    """A fresh SQLite database under tmp_path with the schema built from the models.

    Tests that need the schema as the migrations create it use ``migration_engine``
    in tests/test_migrations.py instead.
    """
    _copy_template(_schema_template, tmp_path)
    engine = make_engine(database_url(tmp_path))
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """A factory on ``engine`` with the scope guard installed, as the app has it."""
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    return factory


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A guarded session on ``engine``. Work left uncommitted is discarded at teardown."""
    with session_factory() as session:
        yield session


@pytest.fixture
def bare_engine(tmp_path: Path) -> Iterator[Engine]:
    """A tmp_path SQLite database with no schema, for code that runs the migrations itself."""
    engine = make_engine(database_url(tmp_path / "bare"))
    yield engine
    engine.dispose()


@pytest.fixture
def app(
    bare_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _migrated_template: Path,
) -> FastAPI:
    """The application on ``bare_engine`` with no frontend build, not yet started.

    ``bare_engine``'s file starts as a copy of the migrated template, so the startup
    migration finds nothing to do (#210); everything else startup does, it still does.
    """
    _copy_template(_migrated_template, tmp_path / "bare")
    monkeypatch.setenv("NETKEEP_FRONTEND_DIST", str(tmp_path / "no-dist"))
    return create_app(Settings(), engine=bare_engine)


@pytest.fixture
async def running_app(app: FastAPI) -> AsyncIterator[FastAPI]:
    """``app`` inside its lifespan: migrated, local user present, bus and runner live."""
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client(running_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An in-process HTTP client. State-changing requests need the CSRF header."""
    transport = httpx.ASGITransport(app=running_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1", auth=LocalKey(running_app)
    ) as client:
        yield client


@pytest.fixture
def inside_active_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lets a run started by hand start whatever the wall clock says (#213).

    The API and the CLI refuse a manual run outside ``[linkedin] active_hours``,
    checked against the real clock. A test that starts runs by hand to exercise
    something else takes this, so it passes at 3 a.m. too; the refusal itself is
    tested in tests/test_active_hours_stop.py with its own clock.
    """
    from netkeep.services import runs

    monkeypatch.setattr(runs, "refuse_if_outside_active_hours", lambda *_, **__: None)


class _ProviderThatFailsIfUsed:
    """A browser provider for a test whose run must never reach the browser (#293 review).

    Building it is fine; entering a run on it fails the test. ``mode`` is the attach
    mode, so anything that only reads the provider's mode still works.
    """

    mode = "attach"

    def __init__(self, *_: object, **__: object) -> None:
        pass

    def run(self, *_: object, **__: object) -> object:
        pytest.fail("this test's run reached the browser provider; it must be refused first")


@pytest.fixture
def no_browser_for_cli_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """``netkeep`` CLI commands in this test get a provider that fails the test if used.

    For a test that expects the CLI to refuse *before* any browser work: if that
    refusal ever goes away (a regression, a mutation), the test fails here instead of
    running a real attach.
    """
    from netkeep import cli

    monkeypatch.setattr(cli, "_provider", lambda *_, **__: _ProviderThatFailsIfUsed())
    monkeypatch.setattr(cli, "AttachBrowserProvider", _ProviderThatFailsIfUsed)
