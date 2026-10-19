"""The rename's one-time migration from netkeeper to netkeep (#661).

Every test runs under the fake home folder ``tests/conftest.py`` sets, with the
in-memory keyring and a fake launchctl, and calls the migration itself: conftest turns
the automatic run off, and its ``path_guard`` fails a test that reaches the real home.
"""

from __future__ import annotations

import errno
import json
import os
import plistlib
import sqlite3
import sys
from pathlib import Path

import keyring
import pytest
from gmail_fakes import MemoryKeyring
from test_login_item import EXE, UID, FakeLaunchctl
from typer.testing import CliRunner

import netkeep
from netkeep import cli, migrate_name, paths
from netkeep.linkedin import activity_lock
from netkeep.migrate_name import MARKER_FILENAME, Refused
from netkeep.services import keychain
from netkeep.services.login_item import LoginItem
from netkeep.services.serve_lock import SERVE_LOCK_FILENAME

REAL_AUTO = cli._auto_migrate  # before conftest turns it off for every test
OLD_LABEL = "fun.tnkr.netkeeper"
NEW_LABEL = "fun.tnkr.netkeep"
SECRET = "s3cret-value-never-printed"


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch) -> Path:
    """The fake home on macOS, with the default data directory in use."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("NETKEEP_DATA", raising=False)
    fake = Path.home()
    assert fake.resolve() != Path(os.path.expanduser("~root")).resolve()
    (fake / "Library" / "Application Support").mkdir(parents=True, exist_ok=True)
    return fake


@pytest.fixture
def old(home: Path) -> Path:
    return home / "Library" / "Application Support" / "netkeeper"


@pytest.fixture
def new(home: Path) -> Path:
    return home / "Library" / "Application Support" / "netkeep"


@pytest.fixture
def launchctl() -> FakeLaunchctl:
    return FakeLaunchctl()


@pytest.fixture
def agents(tmp_path: Path) -> Path:
    folder = tmp_path / "LaunchAgents"
    folder.mkdir()
    return folder


@pytest.fixture
def factory(launchctl: FakeLaunchctl, agents: Path) -> migrate_name.LoginItemFactory:
    def make(data: Path) -> LoginItem:
        return LoginItem(
            data=data,
            agents_dir=agents,
            runner=launchctl,
            uid=UID,
            executable=Path(EXE),
            macos=True,
            environ={},
            lock_held=lambda: False,
            sleep=lambda _s: None,
        )

    return make


def make_netkeeper_install(folder: Path, *, users: int = 1) -> None:
    """A data directory as netkeeper left it: a database with users and a mailbox."""
    folder.mkdir(parents=True)
    with sqlite3.connect(folder / "netkeeper.sqlite3") as connection:
        connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE mailboxes (id INTEGER PRIMARY KEY, user_id INTEGER, keychain_ref TEXT)"
        )
        for user_id in range(1, users + 1):
            connection.execute("INSERT INTO users (id) VALUES (?)", (user_id,))
        connection.execute(
            "INSERT INTO mailboxes (id, user_id, keychain_ref) VALUES (1, 1, 'gmail/mailbox/1')"
        )
    (folder / "chrome-profile").mkdir()
    (folder / "chrome-profile" / "Local State").write_text('{"profile": {}}')
    (folder / "locks").mkdir()


def store_old_secrets(service: str = "netkeeper") -> list[str]:
    entries = [
        "install/local-api-key",
        "install/local-api-claimed",
        "1/gmail/oauth_client",
        "1/gmail/mailbox/1",
    ]
    for entry in entries:
        keyring.set_password(service, entry, f"{SECRET}:{entry}")
    return entries


def write_old_agent(agents: Path, data: Path, label: str = OLD_LABEL, **extra_env: str) -> Path:
    path = agents / f"{label}.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": ["/opt/nk/bin/netkeeper", "serve", "--port", "8001"],
                "EnvironmentVariables": {"NETKEEPER_DATA": str(data), **extra_env},
            }
        )
    )
    return path


def login_factory_for(
    launchctl: FakeLaunchctl, agents: Path, environ: dict[str, str]
) -> migrate_name.LoginItemFactory:
    def make(data: Path) -> LoginItem:
        return LoginItem(
            data=data,
            agents_dir=agents,
            runner=launchctl,
            uid=UID,
            executable=Path(EXE),
            macos=True,
            environ=environ,
            lock_held=lambda: False,
            sleep=lambda _s: None,
        )

    return make


# -- the data directory ----------------------------------------------------------------


def test_a_fresh_install_has_nothing_to_migrate(
    old: Path, new: Path, factory: migrate_name.LoginItemFactory
) -> None:
    assert not migrate_name.needed()
    planned = migrate_name.plan(macos=True, login_factory=factory)
    assert not planned.has_work
    assert migrate_name.apply(planned, login_factory=factory) == []
    assert not old.exists()
    assert not new.exists()  # nothing created as a side effect


def test_the_old_folder_moves_whole_and_everything_comes_along(
    old: Path,
    new: Path,
    agents: Path,
    launchctl: FakeLaunchctl,
    factory: migrate_name.LoginItemFactory,
    memory_keyring: MemoryKeyring,
) -> None:
    make_netkeeper_install(old)
    inode = old.stat().st_ino
    entries = store_old_secrets()
    agent = write_old_agent(agents, old)
    launchctl.loaded.add(OLD_LABEL)
    assert migrate_name.needed()

    planned = migrate_name.plan(macos=True, login_factory=factory)
    assert planned.refusals == []
    assert planned.move_from == old
    assert planned.data == new
    assert sorted(planned.copy) == sorted(entries)
    done = migrate_name.apply(planned, login_factory=factory)

    assert not old.exists()
    assert new.stat().st_ino == inode  # renamed, not copied
    assert (new / "netkeeper.sqlite3").is_file()
    for entry in entries:
        assert keyring.get_password("netkeep", entry) == f"{SECRET}:{entry}"
        assert keyring.get_password("netkeeper", entry) == f"{SECRET}:{entry}"  # left in place
    assert not agent.exists()
    new_plist = plistlib.loads((agents / f"{NEW_LABEL}.plist").read_bytes())
    assert new_plist["ProgramArguments"] == [EXE, "serve", "--port", "8001"]
    assert new_plist["EnvironmentVariables"]["NETKEEP_DATA"] == str(new.resolve())
    assert OLD_LABEL not in launchctl.loaded
    assert "bootstrap" not in launchctl.verbs()  # registered for the next login
    marker = json.loads((new / MARKER_FILENAME).read_text())
    assert marker["moved_from"] == str(old)
    assert marker["keychain_copied"] == len(entries)
    assert marker["login_item_from"] == OLD_LABEL
    text = "\n".join(done) + json.dumps(marker)
    assert SECRET not in text
    assert "you can delete them" in text

    # The second start: nothing left to do, and nothing changes.
    assert not migrate_name.needed()
    again = migrate_name.plan(macos=True, login_factory=factory)
    assert not again.has_work
    assert again.copy == []
    assert sorted(again.already) == sorted(entries)


def test_both_folders_existing_is_refused(
    old: Path, new: Path, factory: migrate_name.LoginItemFactory
) -> None:
    make_netkeeper_install(old)
    new.mkdir()
    planned = migrate_name.plan(macos=True, login_factory=factory)
    assert len(planned.refusals) == 1
    assert "both" in planned.refusals[0]
    assert "netkeep migrate" in planned.refusals[0]
    with pytest.raises(Refused):
        migrate_name.apply(planned)
    with pytest.raises(Refused, match="both"):
        migrate_name.auto("serve")
    assert (old / "netkeeper.sqlite3").is_file()
    assert list(new.iterdir()) == []


def test_a_held_serve_lock_in_the_old_folder_is_refused(
    old: Path, new: Path, factory: migrate_name.LoginItemFactory
) -> None:
    make_netkeeper_install(old)
    claim = activity_lock.try_claim_at(old / "locks" / "serve.lock", "serve")
    assert claim is not None
    try:
        planned = migrate_name.plan(macos=True, login_factory=factory)
    finally:
        claim.release()
    assert any("serve.lock" in reason and "Stop it" in reason for reason in planned.refusals)
    with pytest.raises(Refused):
        migrate_name.apply(planned)
    assert old.is_dir()
    assert not new.exists()
    # Released, it goes ahead.
    assert migrate_name.plan(macos=True, login_factory=factory).refusals == []


def test_netkeepers_chrome_holding_the_old_profile_is_refused(
    old: Path, new: Path, factory: migrate_name.LoginItemFactory
) -> None:
    make_netkeeper_install(old)
    (old / "chrome-profile" / "SingletonLock").symlink_to(f"some-host-{os.getpid()}")
    planned = migrate_name.plan(macos=True, login_factory=factory)
    assert any("Chrome" in reason and "Quit" in reason for reason in planned.refusals)
    assert old.is_dir()
    assert not new.exists()


def test_a_stale_chrome_lock_does_not_block_the_move(
    old: Path, new: Path, factory: migrate_name.LoginItemFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_netkeeper_install(old)
    (old / "chrome-profile" / "SingletonLock").symlink_to("some-host-4242")
    monkeypatch.setattr(activity_lock, "pid_alive", lambda pid: False)
    assert migrate_name.plan(macos=True, login_factory=factory).refusals == []


@pytest.mark.parametrize("name", ["NETKEEP_DATABASE_URL", "NETKEEPER_CONFIG"])
def test_a_setting_that_points_inside_the_old_folder_is_refused(
    old: Path, factory: migrate_name.LoginItemFactory, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    make_netkeeper_install(old)
    monkeypatch.setenv(name, f"sqlite:///{old}/netkeeper.sqlite3")
    planned = migrate_name.plan(macos=True, login_factory=factory)
    assert any(name.replace("NETKEEPER_", "NETKEEP_") in reason for reason in planned.refusals)


def test_a_custom_data_directory_stays_where_it_is(
    tmp_path: Path,
    old: Path,
    new: Path,
    agents: Path,
    factory: migrate_name.LoginItemFactory,
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: MemoryKeyring,
) -> None:
    custom = tmp_path / "elsewhere"
    make_netkeeper_install(custom)
    make_netkeeper_install(old)  # someone else's default: left alone too
    monkeypatch.setenv("NETKEEPER_DATA", str(custom))
    digest = paths.data_dir_digest(custom)
    entries = store_old_secrets(f"netkeeper@{digest}")
    agent = write_old_agent(agents, custom, f"{OLD_LABEL}.{digest}")

    planned = migrate_name.plan(macos=True, login_factory=factory)
    assert planned.move_from is None
    assert planned.data == custom
    assert (planned.service_from, planned.service_to) == (
        f"netkeeper@{digest}",
        f"netkeep@{digest}",
    )
    migrate_name.apply(planned, login_factory=factory)

    assert custom.is_dir()
    assert old.is_dir()
    assert not new.exists()
    for entry in entries:
        assert keyring.get_password(f"netkeep@{digest}", entry) == f"{SECRET}:{entry}"
        assert keyring.get_password("netkeep", entry) is None
    assert not agent.exists()
    assert (agents / f"{NEW_LABEL}.{digest}.plist").is_file()
    assert (custom / MARKER_FILENAME).is_file()


def test_the_old_login_items_data_variable_counts_as_the_default(
    old: Path, new: Path, factory: migrate_name.LoginItemFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old agent's job runs with ``NETKEEPER_DATA`` naming the old default."""
    make_netkeeper_install(old)
    monkeypatch.setenv("NETKEEPER_DATA", str(old))
    planned = migrate_name.plan(macos=True, login_factory=factory)
    assert planned.move_from == old
    environ: dict[str, str] = {"NETKEEPER_DATA": str(old)}
    migrate_name.apply(planned, environ, login_factory=factory)
    assert environ == {"NETKEEP_DATA": str(new)}
    assert new.is_dir()


def test_a_restarted_old_job_follows_the_moved_folder(
    old: Path, new: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """launchd restarts the old job with its old environment until the next login."""
    new.mkdir()
    (new / MARKER_FILENAME).write_text("{}")
    monkeypatch.setenv("NETKEEPER_DATA", str(old))
    assert migrate_name.auto("serve").lines == []
    assert os.environ["NETKEEP_DATA"] == str(new)
    assert "NETKEEPER_DATA" not in os.environ
    assert paths.data_dir() == new


def test_a_config_that_names_the_old_folder_is_reported(
    old: Path, new: Path, factory: migrate_name.LoginItemFactory
) -> None:
    make_netkeeper_install(old)
    (old / "config.toml").write_text(f"# kept at {old}\n[web]\nport = 8000\n")
    done = migrate_name.apply(migrate_name.plan(macos=True, login_factory=factory))
    assert any("config.toml still mentions" in line and str(new) in line for line in done)


# -- the Keychain ----------------------------------------------------------------------


def test_a_secret_already_under_the_new_name_is_never_overwritten(
    old: Path, factory: migrate_name.LoginItemFactory, memory_keyring: MemoryKeyring
) -> None:
    make_netkeeper_install(old)
    store_old_secrets()
    keyring.set_password("netkeep", "install/local-api-key", "newer")
    planned = migrate_name.plan(macos=True, login_factory=factory)
    assert "install/local-api-key" in planned.already
    migrate_name.apply(planned, login_factory=factory)
    assert keyring.get_password("netkeep", "install/local-api-key") == "newer"


def test_a_keychain_service_override_has_nothing_to_copy(
    old: Path, factory: migrate_name.LoginItemFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_netkeeper_install(old)
    store_old_secrets()
    monkeypatch.setenv("NETKEEP_KEYCHAIN_SERVICE", "my-own")
    planned = migrate_name.plan(macos=True, login_factory=factory)
    assert planned.service_from is None
    assert planned.copy == []
    assert any("names the service" in line for line in planned.describe())


def test_a_refusing_keychain_leaves_no_marker_so_it_runs_again(
    old: Path, new: Path, factory: migrate_name.LoginItemFactory, memory_keyring: MemoryKeyring
) -> None:
    make_netkeeper_install(old)
    store_old_secrets()
    memory_keyring.broken = True
    planned = migrate_name.plan(macos=True, login_factory=factory)
    assert planned.incomplete
    done = migrate_name.apply(planned, login_factory=factory)
    assert new.is_dir()  # the data still moved
    assert not (new / MARKER_FILENAME).exists()
    assert any("Keychain refused" in line for line in done)
    memory_keyring.broken = False
    assert migrate_name.needed()
    again = migrate_name.plan(macos=True, login_factory=factory)
    assert len(again.copy) == 4
    migrate_name.apply(again, login_factory=factory)
    assert (new / MARKER_FILENAME).exists()


# -- the login item --------------------------------------------------------------------


def test_an_old_login_item_that_was_off_stays_off(
    old: Path,
    agents: Path,
    launchctl: FakeLaunchctl,
    factory: migrate_name.LoginItemFactory,
) -> None:
    make_netkeeper_install(old)
    agent = write_old_agent(agents, old)
    launchctl.disabled.add(OLD_LABEL)
    planned = migrate_name.plan(macos=True, login_factory=factory)
    assert planned.login is not None
    assert not planned.login_on
    done = migrate_name.apply(planned, login_factory=factory)
    assert not agent.exists()
    assert not (agents / f"{NEW_LABEL}.plist").exists()
    assert any("stays off" in line for line in done)


def test_an_old_login_item_for_another_folder_is_left_alone(
    tmp_path: Path, old: Path, agents: Path, factory: migrate_name.LoginItemFactory
) -> None:
    make_netkeeper_install(old)
    agent = write_old_agent(agents, tmp_path / "another", f"{OLD_LABEL}.0123456789ab")
    planned = migrate_name.plan(macos=True, login_factory=factory)
    assert planned.login is None
    migrate_name.apply(planned, login_factory=factory)
    assert agent.exists()


def test_no_login_item_step_off_macos(
    old: Path, agents: Path, factory: migrate_name.LoginItemFactory
) -> None:
    make_netkeeper_install(old)
    write_old_agent(agents, old)
    assert migrate_name.plan(macos=False, login_factory=factory).login is None


# -- the commands ----------------------------------------------------------------------


def test_dry_run_prints_the_plan_and_changes_nothing(
    old: Path,
    new: Path,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    factory: migrate_name.LoginItemFactory,
) -> None:
    make_netkeeper_install(old)
    entries = store_old_secrets()
    before = sorted(p.relative_to(old) for p in old.rglob("*"))
    db_bytes = (old / "netkeeper.sqlite3").read_bytes()
    monkeypatch.setattr(migrate_name, "_default_login_item", factory)
    result = CliRunner().invoke(cli.app, ["migrate", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert f"move the data directory {old} to {new}" in result.output
    assert "copy the Keychain item install/local-api-key from service netkeeper to netkeep" in (
        result.output
    )
    assert "nothing changed" in result.output
    assert SECRET not in result.output
    assert not new.exists()
    assert sorted(p.relative_to(old) for p in old.rglob("*")) == before  # no -wal, no marker
    assert (old / "netkeeper.sqlite3").read_bytes() == db_bytes
    for entry in entries:
        assert keyring.get_password("netkeep", entry) is None


def test_dry_run_shows_a_refusal_and_exits_non_zero(old: Path, new: Path) -> None:
    make_netkeeper_install(old)
    new.mkdir()
    result = CliRunner().invoke(cli.app, ["migrate", "--dry-run"])
    assert result.exit_code == 1
    assert "refused: both" in result.output


def test_migrate_runs_it(
    old: Path, new: Path, monkeypatch: pytest.MonkeyPatch, factory: migrate_name.LoginItemFactory
) -> None:
    make_netkeeper_install(old)
    monkeypatch.setattr(migrate_name, "_default_login_item", factory)
    result = CliRunner().invoke(cli.app, ["migrate"])
    assert result.exit_code == 0, result.output
    assert "moved your data" in result.output
    assert new.is_dir()
    again = CliRunner().invoke(cli.app, ["migrate"])
    assert again.exit_code == 0
    assert "moved" not in again.output


def test_any_command_migrates_first(
    old: Path, new: Path, monkeypatch: pytest.MonkeyPatch, factory: migrate_name.LoginItemFactory
) -> None:
    make_netkeeper_install(old)
    monkeypatch.setattr(cli, "_auto_migrate", REAL_AUTO)
    monkeypatch.setattr(migrate_name, "_default_login_item", factory)
    result = CliRunner().invoke(cli.app, ["config", "show"])
    assert result.exit_code == 0, result.output
    assert "netkeep: moved your data" in result.output
    assert new.is_dir()
    assert not old.exists()


def test_a_refused_automatic_run_stops_the_command(
    old: Path, new: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_netkeeper_install(old)
    new.mkdir()
    monkeypatch.setattr(cli, "_auto_migrate", REAL_AUTO)
    result = CliRunner().invoke(cli.app, ["config", "show"])
    assert result.exit_code == 1
    assert "can't move netkeeper's data yet" in result.output


@pytest.mark.parametrize("command", ["migrate", "openapi", "version"])
def test_some_commands_never_migrate_on_their_own(old: Path, command: str) -> None:
    make_netkeeper_install(old)
    assert migrate_name.auto(command).lines == []
    assert old.is_dir()


def test_the_old_command_name_still_works_with_a_note(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["netkeeper", "version"])
    with pytest.raises(SystemExit) as exited:
        cli.legacy_main()
    assert exited.value.code in (0, None)
    captured = capsys.readouterr()
    assert captured.err.startswith("netkeeper is now netkeep")
    assert captured.err.count("\n") == 1
    assert captured.out.strip() == netkeep.__version__


def test_both_commands_are_installed() -> None:
    import tomllib

    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert pyproject["project"]["name"] == "netkeep"
    assert pyproject["project"]["scripts"] == {
        "netkeep": "netkeep.cli:app",
        "netkeeper": "netkeep.cli:legacy_main",
    }


# -- the guards ------------------------------------------------------------------------


def test_the_guard_refuses_the_real_home_folder() -> None:
    from conftest import REAL_HOME

    with pytest.raises(AssertionError, match="real home"):
        migrate_name.path_guard(REAL_HOME / "Library" / "Application Support" / "netkeeper")
    assert Path.home().resolve() != REAL_HOME


def test_the_names_are_pinned() -> None:
    assert migrate_name.LEGACY_SERVICE == "netkeeper"
    assert keychain.DEFAULT_SERVICE == "netkeep"
    assert MARKER_FILENAME == "migrated-from-netkeeper.json"
    assert paths.legacy_default_data_dir() is None or paths.legacy_default_data_dir() == (
        Path.home() / "Library" / "Application Support" / "netkeeper"
    )


def test_asking_for_help_never_migrates(old: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_netkeeper_install(old)
    monkeypatch.setattr(sys, "argv", ["netkeep", "serve", "--help"])
    assert REAL_AUTO("serve") == []
    assert old.is_dir()


# -- the review's follow-ups ----------------------------------------------------------


def test_a_dry_run_with_an_old_variable_set_creates_nothing_new(
    old: Path,
    new: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory: migrate_name.LoginItemFactory,
) -> None:
    """The old-name warning is a log record: it must not open a log file in the new
    folder, which would leave both folders and block the real run."""
    make_netkeeper_install(old)
    monkeypatch.setattr(paths, "_warned", set())
    monkeypatch.setenv("NETKEEPER_CONFIG", str(tmp_path / "elsewhere.toml"))
    monkeypatch.setattr(migrate_name, "_default_login_item", factory)
    result = CliRunner().invoke(cli.app, ["migrate", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert not new.exists()
    real = CliRunner().invoke(cli.app, ["migrate"])
    assert real.exit_code == 0, real.output
    assert new.is_dir() and not old.exists()


def test_an_unreadable_database_leaves_the_gmail_secrets_for_a_retry(
    old: Path, new: Path, factory: migrate_name.LoginItemFactory, memory_keyring: MemoryKeyring
) -> None:
    make_netkeeper_install(old)
    entries = store_old_secrets()
    good = (old / "netkeeper.sqlite3").read_bytes()
    (old / "netkeeper.sqlite3").write_bytes(b"this is not a database" * 100)
    planned = migrate_name.plan(macos=True, login_factory=factory)
    assert planned.incomplete
    assert sorted(planned.copy) == ["install/local-api-claimed", "install/local-api-key"]
    done = migrate_name.apply(planned, login_factory=factory)
    assert any("could not read the database" in line for line in done)
    assert not (new / MARKER_FILENAME).exists()
    assert keyring.get_password("netkeep", "1/gmail/oauth_client") is None
    # Readable again: the next start copies the rest and finishes.
    (new / "netkeeper.sqlite3").write_bytes(good)
    assert migrate_name.needed()
    again = migrate_name.plan(macos=True, login_factory=factory)
    assert not again.incomplete
    migrate_name.apply(again, login_factory=factory)
    for entry in entries:
        assert keyring.get_password("netkeep", entry) == f"{SECRET}:{entry}"
    assert (new / MARKER_FILENAME).exists()


def test_a_database_never_migrated_has_no_users_and_is_complete(
    old: Path, factory: migrate_name.LoginItemFactory
) -> None:
    old.mkdir()
    sqlite3.connect(old / "netkeeper.sqlite3").close()  # an empty file: no tables
    planned = migrate_name.plan(macos=True, login_factory=factory)
    assert not planned.incomplete


def test_the_new_login_item_keeps_the_old_ones_config_and_keychain_settings(
    tmp_path: Path,
    old: Path,
    new: Path,
    agents: Path,
    factory: migrate_name.LoginItemFactory,
) -> None:
    """The maintainer's case: the old agent named a config in the repo checkout. A
    terminal without that variable must not drop it from the new agent."""
    make_netkeeper_install(old)
    checkout_config = tmp_path / "code" / "netkeeper" / "config.toml"
    write_old_agent(
        agents,
        old,
        NETKEEPER_CONFIG=str(checkout_config),
        NETKEEPER_KEYCHAIN_SERVICE="my-service",
    )
    migrate_name.apply(migrate_name.plan(macos=True, login_factory=factory), login_factory=factory)
    env = plistlib.loads((agents / f"{NEW_LABEL}.plist").read_bytes())["EnvironmentVariables"]
    assert env["NETKEEP_CONFIG"] == str(checkout_config)
    assert env["NETKEEP_KEYCHAIN_SERVICE"] == "my-service"
    assert env["NETKEEP_DATA"] == str(new.resolve())
    assert not any(name.startswith("NETKEEPER_") for name in env)


def test_a_config_inside_the_old_folder_is_pointed_at_the_new_one(
    old: Path, new: Path, agents: Path, factory: migrate_name.LoginItemFactory
) -> None:
    make_netkeeper_install(old)
    write_old_agent(agents, old, NETKEEPER_CONFIG=str(old / "config.toml"))
    migrate_name.apply(migrate_name.plan(macos=True, login_factory=factory), login_factory=factory)
    env = plistlib.loads((agents / f"{NEW_LABEL}.plist").read_bytes())["EnvironmentVariables"]
    assert env["NETKEEP_CONFIG"] == str(new.resolve() / "config.toml")


def test_the_serve_lock_is_held_through_the_move(
    old: Path, new: Path, launchctl: FakeLaunchctl, agents: Path
) -> None:
    """A serve starting during the migration, on either path, is refused."""
    make_netkeeper_install(old)
    write_old_agent(agents, old)
    seen: list[object] = []
    inner = login_factory_for(launchctl, agents, {})

    def during(data: Path) -> LoginItem:  # called while the login item moves
        seen.append(activity_lock.try_claim_at(new / "locks" / SERVE_LOCK_FILENAME, "serve"))
        return inner(data)

    planned = migrate_name.plan(macos=True, login_factory=inner)
    migrate_name.apply(planned, login_factory=during)
    for claim in seen[1:]:  # the hand-over check runs after the release
        if isinstance(claim, activity_lock.Claim):
            claim.release()
    assert seen[0] is None
    after = activity_lock.try_claim_at(new / "locks" / SERVE_LOCK_FILENAME, "serve")
    assert after is not None  # released at the end
    after.release()


def test_a_second_migration_holding_the_lock_is_refused(
    old: Path, new: Path, factory: migrate_name.LoginItemFactory
) -> None:
    make_netkeeper_install(old)
    planned = migrate_name.plan(macos=True, login_factory=factory)
    other = activity_lock.try_claim_at(old / "locks" / SERVE_LOCK_FILENAME, "serve")
    assert other is not None
    try:
        with pytest.raises(Refused, match="holds"):
            migrate_name.apply(planned, login_factory=factory)
    finally:
        other.release()
    assert old.is_dir() and not new.exists()


def test_the_folder_appearing_after_the_plan_is_refused(
    old: Path, new: Path, factory: migrate_name.LoginItemFactory
) -> None:
    make_netkeeper_install(old)
    planned = migrate_name.plan(macos=True, login_factory=factory)
    new.mkdir()
    with pytest.raises(Refused, match="both"):
        migrate_name.apply(planned, login_factory=factory)
    assert (old / "netkeeper.sqlite3").is_file()


@pytest.mark.parametrize("code", [errno.EXDEV, errno.EBUSY, errno.ENOENT])
def test_a_failed_rename_is_a_refusal_and_moves_nothing(
    old: Path,
    new: Path,
    factory: migrate_name.LoginItemFactory,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
) -> None:
    make_netkeeper_install(old)
    planned = migrate_name.plan(macos=True, login_factory=factory)

    def refuse(self: Path, target: Path) -> Path:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(Path, "rename", refuse)
    with pytest.raises(Refused, match="nothing was moved") as refused:
        migrate_name.apply(planned, login_factory=factory)
    if code == errno.EXDEV:
        assert "different volumes" in str(refused.value)
    monkeypatch.undo()
    assert old.is_dir() and not new.exists()
    claim = activity_lock.try_claim_at(old / "locks" / SERVE_LOCK_FILENAME, "serve")
    assert claim is not None  # the lock was let go
    claim.release()


def test_the_old_agents_own_job_hands_over_to_the_new_one(
    old: Path, new: Path, agents: Path, launchctl: FakeLaunchctl
) -> None:
    """Under ``fun.tnkr.netkeeper`` (installed without the bootout), the old job's
    definition points into the moved folder: it starts the new agent and stops itself."""
    make_netkeeper_install(old)
    write_old_agent(agents, old)
    launchctl.loaded.add(OLD_LABEL)
    as_old_job = login_factory_for(launchctl, agents, {"XPC_SERVICE_NAME": OLD_LABEL})
    planned = migrate_name.plan(macos=True, login_factory=as_old_job)
    done = migrate_name.apply(planned, login_factory=as_old_job)
    assert planned.handed_over
    assert OLD_LABEL not in launchctl.loaded
    assert NEW_LABEL in launchctl.loaded
    verbs = launchctl.verbs()
    assert verbs.index("bootstrap") < len(verbs) - 1 - verbs[::-1].index("bootout")
    assert any("handed over" in line for line in done)
    assert (new / MARKER_FILENAME).exists()  # finished before handing over


def test_the_cli_ends_after_a_hand_over(monkeypatch: pytest.MonkeyPatch) -> None:
    import typer

    monkeypatch.setattr(
        migrate_name, "auto", lambda *_a, **_k: migrate_name.AutoResult(["x"], handed_over=True)
    )
    with pytest.raises(typer.Exit) as exited:
        REAL_AUTO("serve")
    assert exited.value.exit_code == 0


def test_a_restart_never_relies_on_the_old_agents_definition() -> None:
    from netkeep.services import restart

    assert restart.mode({"XPC_SERVICE_NAME": OLD_LABEL}) == "exec"


def test_a_custom_data_directory_is_never_migrated_automatically(
    tmp_path: Path,
    old: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: MemoryKeyring,
) -> None:
    """Other agents run commands with a scratch NETKEEPER_DATA: no Keychain read, no marker."""
    custom = tmp_path / "scratch"
    make_netkeeper_install(custom)
    make_netkeeper_install(old)
    monkeypatch.setenv("NETKEEPER_DATA", str(custom))
    memory_keyring.broken = True  # any Keychain access would raise
    assert not migrate_name.needed()
    assert migrate_name.auto("serve").lines == []
    assert not (custom / MARKER_FILENAME).exists()
    assert old.is_dir()


def test_a_folder_that_came_back_after_the_migration_is_named_as_unused(
    old: Path, new: Path, factory: migrate_name.LoginItemFactory
) -> None:
    make_netkeeper_install(old)
    new.mkdir()
    (new / MARKER_FILENAME).write_text("{}")
    refusals = migrate_name.plan(macos=True, login_factory=factory).refusals
    assert len(refusals) == 1
    assert "already moved" in refusals[0] and "don't move the new one" in refusals[0]


@pytest.mark.parametrize(
    ("value", "inside"),
    [
        ("~/Library/Application Support/netkeeper/config.toml", True),
        ("sqlite:///~/Library/Application%20Support/netkeeper/netkeeper.sqlite3", True),
        ("~/Library/Application Support/netkeeper-backup/config.toml", False),
        ("~/Library/Application Support/netkeep/config.toml", False),
        ("postgresql://u@localhost/netkeeper", False),
    ],
)
def test_a_setting_inside_the_old_folder_is_matched_by_path(
    old: Path, value: str, inside: bool
) -> None:
    assert migrate_name._names_inside(value, old) is inside


def test_copying_a_secret_never_overwrites_the_new_one(memory_keyring: MemoryKeyring) -> None:
    keyring.set_password("netkeeper", "install/local-api-key", "old")
    keyring.set_password("netkeep", "install/local-api-key", "newer")
    assert not keychain.copy_secret("install/local-api-key", source="netkeeper", target="netkeep")
    assert keyring.get_password("netkeep", "install/local-api-key") == "newer"
    keyring.set_password("netkeeper", "1/gmail/oauth_client", "c")
    assert keychain.copy_secret("1/gmail/oauth_client", source="netkeeper", target="netkeep")
    assert keyring.get_password("netkeep", "1/gmail/oauth_client") == "c"


# -- the re-review's follow-ups --------------------------------------------------------


@pytest.mark.parametrize("spelling", ["exact", "tilde", "dotted"])
def test_naming_the_new_default_still_moves_the_old_folder(
    old: Path,
    new: Path,
    factory: migrate_name.LoginItemFactory,
    monkeypatch: pytest.MonkeyPatch,
    spelling: str,
) -> None:
    """``NETKEEP_DATA`` set to .../netkeep before the move is the default, not a custom
    folder: the move is checked and done, never skipped onto an empty folder."""
    make_netkeeper_install(old)
    named = {
        "exact": str(new),
        "tilde": "~/Library/Application Support/netkeep",
        "dotted": str(new.parent / "." / "netkeep"),
    }[spelling]
    monkeypatch.setenv("NETKEEP_DATA", named)
    assert migrate_name.needed()
    planned = migrate_name.plan(macos=True, login_factory=factory)
    assert planned.move_from == old
    assert planned.data == new
    migrate_name.apply(planned, login_factory=factory)
    assert new.is_dir() and not old.exists()


def test_naming_the_new_default_while_both_exist_is_refused(
    old: Path, new: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_netkeeper_install(old)
    new.mkdir()
    monkeypatch.setenv("NETKEEP_DATA", str(new))
    with pytest.raises(Refused, match="both"):
        migrate_name.auto("serve")


def test_a_migration_that_loses_the_race_never_recreates_the_old_folder(
    old: Path, new: Path, factory: migrate_name.LoginItemFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another migration's rename lands between the folder check and the lock claim."""
    make_netkeeper_install(old)
    (old / "locks").rmdir()
    planned = migrate_name.plan(macos=True, login_factory=factory)
    real_claim = activity_lock.try_claim_at

    def winner_moves_it_first(path: Path, account: str, **kwargs: bool) -> object:
        old.rename(new)  # the other migration wins
        return real_claim(path, account, **kwargs)

    monkeypatch.setattr(activity_lock, "try_claim_at", winner_moves_it_first)
    with pytest.raises(Refused, match="moved while netkeep was migrating"):
        migrate_name.apply(planned, login_factory=factory)
    assert not old.exists()  # not even an empty folder holding a lock file
    assert new.is_dir()


def test_the_new_agent_starts_before_the_old_job_stops(
    agents: Path, launchctl: FakeLaunchctl, new: Path
) -> None:
    """Under real launchd, booting out the old job ends this process: the new agent
    must already be bootstrapped by then."""
    item = login_factory_for(launchctl, agents, {"XPC_SERVICE_NAME": OLD_LABEL})(new)
    agents.mkdir(exist_ok=True)
    item.plist_path.write_bytes(plistlib.dumps({"Label": NEW_LABEL}))
    launchctl.loaded.add(OLD_LABEL)
    assert item.replace_old_job() is not None
    calls = launchctl.calls
    bootstrap = next(i for i, c in enumerate(calls) if c[0] == "bootstrap")
    bootout = next(i for i, c in enumerate(calls) if c[0] == "bootout")
    assert calls[bootstrap][-1] == str(item.plist_path)
    assert calls[bootout][-1].endswith(f"/{OLD_LABEL}")
    assert bootstrap < bootout
    assert launchctl.loaded == {NEW_LABEL}


def test_a_failed_bootstrap_never_stops_the_old_job(
    agents: Path, launchctl: FakeLaunchctl, new: Path
) -> None:
    from netkeep.services.login_item import LoginItemError

    item = login_factory_for(launchctl, agents, {"XPC_SERVICE_NAME": OLD_LABEL})(new)
    item.plist_path.write_bytes(plistlib.dumps({"Label": NEW_LABEL}))
    launchctl.loaded.add(OLD_LABEL)
    launchctl.fail = "bootstrap"
    with pytest.raises(LoginItemError):
        item.replace_old_job()
    assert "bootout" not in launchctl.verbs()
    assert OLD_LABEL in launchctl.loaded


def test_a_retry_skips_while_a_serve_holds_the_lock(
    new: Path, memory_keyring: MemoryKeyring
) -> None:
    """The new folder without its marker (a Keychain step left for later) and a serve
    running: a command started meanwhile neither refuses nor touches the Keychain."""
    new.mkdir()
    serve = activity_lock.try_claim_at(new / "locks" / SERVE_LOCK_FILENAME, "serve")
    assert serve is not None
    memory_keyring.broken = True
    try:
        assert migrate_name.needed()
        assert migrate_name.auto("status").lines == []
    finally:
        serve.release()
    assert not (new / MARKER_FILENAME).exists()
