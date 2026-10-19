"""The login item (#600): the plist, the label per data directory, state read from the
plist and launchctl, and never a second ``serve``.

``launchctl`` is a fake and the agents directory is under ``tmp_path``: nothing here
touches the real ``~/Library/LaunchAgents`` (``tests/conftest.py`` makes the defaults raise).
"""

from __future__ import annotations

import os
import plistlib
import stat
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from netkeep.cli import app as cli
from netkeep.paths import default_data_dir
from netkeep.services import keychain, login_item
from netkeep.services.login_item import LoginItem, LoginItemError, RunResult, State

UID = 501
EXE = "/opt/nk/bin/netkeep"


class FakeLaunchctl:
    """Remembers what launchd would: which labels are loaded and which are disabled."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.loaded: set[str] = set()
        self.disabled: set[str] = set()
        self.fail: str | None = None

    def __call__(self, args: Sequence[str]) -> RunResult:
        self.calls.append(list(args))
        verb, *rest = args
        if self.fail == verb:
            return RunResult(5, stderr="Input/output error")
        label = rest[-1].rsplit("/", 1)[-1] if rest else ""
        if verb == "print":
            return RunResult(0 if label in self.loaded else 113)
        if verb == "print-disabled":
            lines = [f'\t"{name}" => disabled' for name in sorted(self.disabled)]
            return RunResult(0, "disabled services = {\n" + "\n".join(lines) + "\n}\n")
        if verb == "enable":
            self.disabled.discard(label)
        elif verb == "bootstrap":
            self.loaded.add(Path(rest[-1]).stem)
        elif verb == "bootout":
            self.loaded.discard(label)
        return RunResult(0)

    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls]


@pytest.fixture
def launchctl() -> FakeLaunchctl:
    return FakeLaunchctl()


@pytest.fixture
def data(tmp_path: Path) -> Path:
    return tmp_path / "nk-data"


def make(
    launchctl: FakeLaunchctl,
    tmp_path: Path,
    data: Path,
    *,
    lock_held: bool = False,
    macos: bool = True,
    environ: dict[str, str] | None = None,
    defer: Callable[[Callable[[], None]], None] | None = None,
) -> LoginItem:
    return LoginItem(
        data=data,
        agents_dir=tmp_path / "LaunchAgents",
        runner=launchctl,
        uid=UID,
        executable=Path(EXE),
        macos=macos,
        environ=environ or {},
        lock_held=lambda: lock_held,
        sleep=lambda _s: None,
        defer=defer,
    )


def read_plist(item: LoginItem) -> dict[str, object]:
    parsed: dict[str, object] = plistlib.loads(item.plist_path.read_bytes())
    return parsed


def test_enable_writes_a_plist_for_the_installed_executable_and_the_data_dir(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    item = make(launchctl, tmp_path, data)
    outcome = item.enable()
    plist = read_plist(item)
    assert plist["ProgramArguments"] == [EXE, "serve"]
    env = plist["EnvironmentVariables"]
    assert isinstance(env, dict)
    assert env["NETKEEP_DATA"] == str(data.resolve())
    assert plist["StandardOutPath"] == str(data.resolve() / "logs" / "serve.log")
    assert plist["StandardErrorPath"] == str(data.resolve() / "logs" / "serve.err.log")
    assert plist["RunAtLoad"] is True
    assert outcome.started
    assert outcome.status.enabled
    verbs = launchctl.verbs()
    assert verbs.index("enable") < verbs.index("bootstrap")


def test_modes_hold_whatever_the_umask_and_an_old_log_is_tightened(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    (data / "logs").mkdir(parents=True)
    old = data / "logs" / "serve.err.log"
    old.write_text("from the old setup\n")
    old.chmod(0o644)
    (data / "logs").chmod(0o755)
    previous = os.umask(0o022)  # a umask that would leave everything readable
    try:
        item = make(launchctl, tmp_path, data)
        item.enable()
    finally:
        os.umask(previous)
    assert stat.S_IMODE(item.plist_path.stat().st_mode) == 0o644
    assert item.plist_path.parent == tmp_path / "LaunchAgents"
    assert stat.S_IMODE((data / "logs").stat().st_mode) == 0o700
    assert stat.S_IMODE(old.stat().st_mode) == 0o600
    assert stat.S_IMODE((data / "logs" / "serve.log").stat().st_mode) == 0o600
    assert old.read_text() == "from the old setup\n"


def test_the_plist_is_world_readable_under_a_tight_umask(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    previous = os.umask(0o077)
    try:
        item = make(launchctl, tmp_path, data)
        item.enable()
    finally:
        os.umask(previous)
    assert stat.S_IMODE(item.plist_path.stat().st_mode) == 0o644


def test_host_and_port_pass_through_to_serve(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    item = make(launchctl, tmp_path, data)
    item.enable(host="127.0.0.1", port=8081)
    assert read_plist(item)["ProgramArguments"] == [
        EXE,
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "8081",
    ]


def test_two_data_directories_get_two_labels_and_never_clash(
    launchctl: FakeLaunchctl, tmp_path: Path
) -> None:
    first = make(launchctl, tmp_path, tmp_path / "one")
    second = make(launchctl, tmp_path, tmp_path / "two")
    assert first.label != second.label
    assert first.label.startswith("fun.tnkr.netkeep.")
    first.enable()
    second.enable()
    second.disable()
    assert first.status().enabled
    assert not second.status().enabled
    assert launchctl.loaded == {first.label}


def test_the_default_data_directory_keeps_the_plain_label(
    launchctl: FakeLaunchctl, tmp_path: Path
) -> None:
    item = make(launchctl, tmp_path, default_data_dir())
    assert item.label == "fun.tnkr.netkeep"


def test_the_label_hash_is_the_keychain_services_hash(
    launchctl: FakeLaunchctl, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    monkeypatch.setenv("NETKEEP_DATA", str(other))
    item = make(launchctl, tmp_path, other)
    assert item.label.removeprefix("fun.tnkr.netkeep.") == keychain.service_name().removeprefix(
        "netkeep@"
    )


def test_a_serving_process_registers_the_agent_without_starting_it(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    """A launchd start against a held serve lock exits at once, and KeepAlive would
    relaunch it every ten seconds. This fails if enable() bootstraps under a live serve."""
    item = make(launchctl, tmp_path, data, lock_held=True)
    outcome = item.enable()
    assert "bootstrap" not in launchctl.verbs()
    assert "enable" in launchctl.verbs()
    assert not outcome.started
    assert outcome.status.enabled
    assert not outcome.status.loaded
    assert outcome.message.startswith("Registered. netkeep will start by itself")
    assert outcome.status.state is State.REGISTERED


def test_the_settings_switch_says_registered_and_why_nothing_changes(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    outcome = make(launchctl, tmp_path, data, lock_held=True).enable(serving_here=True)
    assert not outcome.started
    assert outcome.message == (
        "Registered. netkeep will start by itself the next time you log in to this Mac."
        " Nothing changes now, because netkeep is already running: starting another copy"
        " would only fail against the serve lock."
    )


def test_the_settings_switch_never_bootstraps_even_if_the_lock_looks_free(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    item = make(launchctl, tmp_path, data, lock_held=False)
    item.enable(serving_here=True)
    assert "bootstrap" not in launchctl.verbs()


def test_enabling_again_replaces_the_loaded_agent(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    item = make(launchctl, tmp_path, data)
    item.enable()
    launchctl.calls.clear()
    item.enable(port=9000)
    assert launchctl.verbs().count("bootout") == 1
    assert launchctl.verbs().count("bootstrap") == 1
    assert launchctl.verbs().index("bootout") < launchctl.verbs().index("bootstrap")


def test_the_agent_that_is_serving_is_not_booted_out_by_enabling_again(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    item = make(launchctl, tmp_path, data)
    launchctl.loaded.add(item.label)
    running = make(
        launchctl, tmp_path, data, environ={"XPC_SERVICE_NAME": item.label}, lock_held=True
    )
    running.enable(serving_here=True)
    assert "bootout" not in launchctl.verbs()


def test_a_disabled_label_is_cleared_on_enable_and_shows_off_until_then(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    item = make(launchctl, tmp_path, data)
    item.enable()
    launchctl.disabled.add(item.label)
    assert not item.status().enabled
    item.enable()
    assert item.status().enabled


def test_disable_stops_the_agent_and_removes_the_plist_but_not_the_data(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    item = make(launchctl, tmp_path, data)
    item.enable()
    outcome = item.disable()
    assert not item.plist_path.exists()
    assert launchctl.loaded == set()
    assert not outcome.status.enabled
    assert (data / "logs").is_dir()


def test_disable_when_nothing_is_installed_is_a_quiet_no_op(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    item = make(launchctl, tmp_path, data)
    item.disable()
    assert "bootout" not in launchctl.verbs()


def test_the_agent_that_is_serving_is_stopped_after_the_reply(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    deferred: list[Callable[[], None]] = []
    item = make(launchctl, tmp_path, data)
    item.enable()
    running = make(
        launchctl,
        tmp_path,
        data,
        environ={"XPC_SERVICE_NAME": item.label},
        defer=deferred.append,
    )
    outcome = running.disable(serving_here=True)
    assert "bootout" not in launchctl.verbs()  # not before the reply
    assert "stops in a moment" in outcome.message
    deferred[0]()
    assert launchctl.loaded == set()


def test_state_is_read_from_the_plist_and_launchctl_not_remembered(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    item = make(launchctl, tmp_path, data)
    assert not item.status().enabled
    item.enable()
    item.plist_path.unlink()  # removed by hand
    assert not item.status().enabled
    item.enable()
    launchctl.loaded.clear()  # booted out by hand
    status = item.status()
    assert status.enabled
    assert not status.loaded


def test_a_plist_that_runs_another_netkeep_is_flagged(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    item = make(launchctl, tmp_path, data)
    item.enable()
    moved = LoginItem(
        data=data,
        agents_dir=tmp_path / "LaunchAgents",
        runner=launchctl,
        uid=UID,
        executable=Path("/elsewhere/netkeep"),
        macos=True,
        environ={},
    )
    status = moved.status()
    assert status.stale
    assert status.plist_executable == EXE
    assert not item.status().stale


def test_each_state_has_its_words(launchctl: FakeLaunchctl, tmp_path: Path, data: Path) -> None:
    """The Settings card and ``login-item status`` print these exact names."""
    off = make(launchctl, tmp_path, data)
    assert off.status().state is State.OFF
    assert off.status().state.value == "Off"

    off.enable()  # lock free: the agent is started
    assert off.status().state.value == "Running from login agent"
    launchctl.loaded.clear()  # booted out: the plist is there, nothing runs
    assert off.status().state.value == "Registered: starts at next login"
    me = make(launchctl, tmp_path, data, environ={"XPC_SERVICE_NAME": off.label})
    assert me.status().state.value == "Running from login agent"

    moved = LoginItem(
        data=data,
        agents_dir=tmp_path / "LaunchAgents",
        runner=launchctl,
        uid=UID,
        executable=Path("/elsewhere/netkeep"),
        macos=True,
        environ={},
    )
    assert moved.status().state.value == "Points at a different netkeep"
    launchctl.loaded.add(off.label)  # an old checkout's agent is usually loaded: still different
    assert moved.status().loaded
    assert moved.status().state.value == "Points at a different netkeep"

    other = tmp_path / "other-data"
    write_old_plist(tmp_path, other)
    assert make(launchctl, tmp_path, other).status().state.value == "Needs repair"


def test_turning_it_on_with_nothing_running_says_it_started(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    outcome = make(launchctl, tmp_path, data).enable()
    assert outcome.started
    assert outcome.message == (
        "Started. netkeep is starting now, and will start by itself every time you log in"
        " to this Mac."
    )


def test_a_failed_bootstrap_leaves_no_plist_behind(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    item = make(launchctl, tmp_path, data)
    launchctl.fail = "bootstrap"
    with pytest.raises(LoginItemError, match="Input/output error"):
        item.enable()
    assert not item.plist_path.exists()


def test_a_missing_executable_is_an_error_and_writes_nothing(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login_item, "installed_executable", lambda: None)
    item = LoginItem(
        data=data,
        agents_dir=tmp_path / "LaunchAgents",
        runner=launchctl,
        uid=UID,
        executable=None,
        macos=True,
        environ={},
    )
    with pytest.raises(LoginItemError, match="installed netkeep"):
        item.enable()
    assert not (tmp_path / "LaunchAgents").exists()
    assert launchctl.calls == []


def test_the_keychain_and_config_overrides_follow_the_agent(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    item = make(
        launchctl,
        tmp_path,
        data,
        environ={"NETKEEP_KEYCHAIN_SERVICE": "mine", "NETKEEP_CONFIG": "/c/config.toml"},
    )
    item.enable()
    env = read_plist(item)["EnvironmentVariables"]
    assert isinstance(env, dict)
    assert env["NETKEEP_KEYCHAIN_SERVICE"] == "mine"
    assert env["NETKEEP_CONFIG"] == "/c/config.toml"


def test_off_macos_nothing_runs_and_the_reason_is_given(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    item = make(launchctl, tmp_path, data, macos=False)
    status = item.status()
    assert not status.supported
    assert status.reason is not None
    with pytest.raises(login_item.UnsupportedPlatform):
        item.enable()
    with pytest.raises(login_item.UnsupportedPlatform):
        item.disable()
    assert launchctl.calls == []
    assert not (tmp_path / "LaunchAgents").exists()


# -- the CLI ------------------------------------------------------------------------------


@pytest.fixture
def cli_item(
    monkeypatch: pytest.MonkeyPatch, launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> LoginItem:
    item = make(launchctl, tmp_path, data)
    monkeypatch.setattr(login_item, "LoginItem", lambda: item)
    return item


def test_cli_on_status_off(cli_item: LoginItem) -> None:
    runner = CliRunner()
    assert "state: Off" in runner.invoke(cli, ["login-item", "status"]).output
    assert "starts at login: no" in runner.invoke(cli, ["login-item", "status"]).output
    on = runner.invoke(cli, ["login-item", "on", "--port", "8082"])
    assert on.exit_code == 0, on.output
    assert read_plist(cli_item)["ProgramArguments"][-2:] == ["--port", "8082"]  # type: ignore[index]
    assert "starts at login: yes" in runner.invoke(cli, ["login-item", "status"]).output
    assert "state: Running from login agent" in runner.invoke(cli, ["login-item", "status"]).output
    assert runner.invoke(cli, ["login-item", "off"]).exit_code == 0
    assert not cli_item.plist_path.exists()


def test_cli_exits_non_zero_off_macos(
    monkeypatch: pytest.MonkeyPatch, launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    item = make(launchctl, tmp_path, data, macos=False)
    monkeypatch.setattr(login_item, "LoginItem", lambda: item)
    result = CliRunner().invoke(cli, ["login-item", "on"])
    assert result.exit_code == 1
    assert "launchd" in result.output


# -- an agent the removed install script left ----------------------------------------------

OLD_LABEL = "fun.tnkr.netkeeper"  # the old script ran before the rename (#661)
WRAPPER = "/checkout/scripts/serve-launchd.sh"


def write_old_plist(tmp_path: Path, serves: Path | None, *extra: str) -> Path:
    """What install-launchd.sh wrote: the plain label, whichever directory it served."""
    agents = tmp_path / "LaunchAgents"
    agents.mkdir(exist_ok=True)
    document: dict[str, object] = {
        "Label": OLD_LABEL,
        "ProgramArguments": [WRAPPER, *extra],
        "EnvironmentVariables": {"NETKEEPER_DATA": str(serves)} if serves else {},
    }
    path = agents / f"{OLD_LABEL}.plist"
    path.write_bytes(plistlib.dumps(document))
    return path


def test_an_old_agent_for_this_data_directory_shows_as_legacy(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    write_old_plist(tmp_path, data)
    item = make(launchctl, tmp_path, data)
    status = item.status()
    assert status.legacy
    assert not status.enabled  # this label's plist is not there
    other = make(launchctl, tmp_path, tmp_path / "elsewhere")
    assert not other.status().legacy


def test_enable_adopts_an_old_agent_and_keeps_its_port(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    """Without adoption both agents load at the next login, one loses the serve lock,
    and KeepAlive relaunches it every ten seconds for ever."""
    old = write_old_plist(tmp_path, data, "--port", "8080")
    launchctl.loaded.add(OLD_LABEL)
    item = make(launchctl, tmp_path, data)
    item.enable(keep_arguments=True)
    assert not old.exists()
    assert launchctl.loaded == {item.label}
    verbs = launchctl.verbs()
    assert verbs.index("bootout") < verbs.index("bootstrap")
    assert read_plist(item)["ProgramArguments"] == [EXE, "serve", "--port", "8080"]
    assert not item.status().legacy


def test_adopting_leaves_an_old_agent_for_another_data_directory_alone(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    old = write_old_plist(tmp_path, tmp_path / "somewhere-else")
    launchctl.loaded.add(OLD_LABEL)
    make(launchctl, tmp_path, data).enable()
    assert old.exists()
    assert OLD_LABEL in launchctl.loaded


def test_the_default_directorys_label_leaves_an_old_agent_for_another_alone(
    launchctl: FakeLaunchctl, tmp_path: Path
) -> None:
    """Since the rename (#661) the old script's plain label is never this one's."""
    other = tmp_path / "other"
    old = write_old_plist(tmp_path, other)
    before = old.read_bytes()
    item = make(launchctl, tmp_path, default_data_dir())
    item.enable()
    assert old.read_bytes() == before
    assert item.plist_path != old
    assert item.plist_path.exists()


def test_an_old_agent_with_the_plain_label_is_replaced_by_the_new_label(
    launchctl: FakeLaunchctl, tmp_path: Path
) -> None:
    old = write_old_plist(tmp_path, None, "--host", "127.0.0.1")
    launchctl.loaded.add(OLD_LABEL)
    item = make(launchctl, tmp_path, default_data_dir())
    assert item.status().legacy
    item.enable(keep_arguments=True)
    assert read_plist(item)["ProgramArguments"] == [EXE, "serve", "--host", "127.0.0.1"]
    assert not old.exists()
    assert item.label == "fun.tnkr.netkeep"
    assert not item.status().legacy
    assert launchctl.loaded == {"fun.tnkr.netkeep"}


def test_repair_from_the_old_agent_does_not_boot_out_the_server_answering(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    old = write_old_plist(tmp_path, data)
    launchctl.loaded.add(OLD_LABEL)
    item = make(launchctl, tmp_path, data, lock_held=True, environ={"XPC_SERVICE_NAME": OLD_LABEL})
    outcome = item.enable(serving_here=True, keep_arguments=True)
    assert "bootout" not in launchctl.verbs()
    assert "bootstrap" not in launchctl.verbs()
    assert not old.exists()
    assert item.plist_path.exists()
    assert "Repaired" in outcome.message
    assert "off and on" not in outcome.message


def test_a_manual_serve_adopts_an_old_agent_that_is_crash_looping(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    old = write_old_plist(tmp_path, data)
    launchctl.loaded.add(OLD_LABEL)
    item = make(launchctl, tmp_path, data, lock_held=True)
    item.enable(serving_here=True)
    assert launchctl.loaded == set()
    assert not old.exists()
    assert "bootstrap" not in launchctl.verbs()


def test_disable_also_removes_an_old_agent_for_this_data_directory(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    old = write_old_plist(tmp_path, data)
    launchctl.loaded.add(OLD_LABEL)
    make(launchctl, tmp_path, data).disable()
    assert not old.exists()
    assert launchctl.loaded == set()


# -- the wait for a stopping serve -----------------------------------------------------------


def test_a_terminal_reenable_waits_out_a_slow_shutdown_then_starts(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    item = make(launchctl, tmp_path, data)
    item.enable()
    polls = {"n": 0}

    def lock_held() -> bool:
        polls["n"] += 1
        return polls["n"] < 300  # 30 s at the poll interval: well past the old 5 s budget

    slow = LoginItem(
        data=data,
        agents_dir=tmp_path / "LaunchAgents",
        runner=launchctl,
        uid=UID,
        executable=Path(EXE),
        macos=True,
        environ={},
        lock_held=lock_held,
        sleep=lambda _s: None,
    )
    outcome = slow.enable()
    assert outcome.started
    assert launchctl.loaded == {item.label}


def test_a_serve_that_never_lets_go_is_reported_honestly(
    launchctl: FakeLaunchctl, tmp_path: Path, data: Path
) -> None:
    item = make(launchctl, tmp_path, data)
    item.enable()
    stuck = make(launchctl, tmp_path, data, lock_held=True)
    outcome = stuck.enable()
    assert not outcome.started
    assert outcome.message.startswith("Registered. netkeep will start by itself")
    assert "not finished stopping" in outcome.message
    assert "already running" not in outcome.message
    assert launchctl.loaded == set()
    assert outcome.status.enabled  # the plist is in place for the next login
