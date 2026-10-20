"""``netkeep update`` (#685, ADR 0011 rules 1, 8 and 9): upgrade in place, with a backup
and a rollback that runs by itself only before the new version's scheduler started.

Everything is a fake: GitHub's release downloads, ``uv``, the exec into the next
version, ``launchctl`` and the login item (its agents folder under ``tmp_path``), the
serve lock, the backup and restore, and the health check. A :class:`World` plays all of
them and remembers which netkeep is installed, so each test runs the three processes
(``update``, ``--finish`` on the new version, ``--rollback`` on the old one) in order,
as the execs would. Nothing here downloads, runs ``uv``, execs, or writes outside
``tmp_path``; the ``_confined`` fixture checks the real ``PATH`` and home are untouched.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import plistlib
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import pytest
import typer
from fastapi import FastAPI
from sqlalchemy import Engine
from typer.testing import CliRunner

from netkeep import __version__, cli
from netkeep.linkedin.activity_lock import Holder, LockState
from netkeep.services import post_install, update, uv_tool
from netkeep.services.login_item import LoginItem, RunResult
from netkeep.services.scheduled_runs import ServeExtractor
from netkeep.web.app import create_app

UID = 501
OLD = "1.2.0"
NEW = "1.3.0"
AGENT_PID = 4242
MANUAL_PID = 999
BASE = "http://127.0.0.1:18123"


# -- confinement -----------------------------------------------------------------------


def _snapshot(directories: Sequence[Path]) -> dict[str, tuple[str, ...]]:
    found: dict[str, tuple[str, ...]] = {}
    for directory in directories:
        try:
            found[str(directory)] = tuple(sorted(os.listdir(directory)))
        except OSError:
            found[str(directory)] = ()
    return found


@pytest.fixture(autouse=True)
def _confined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A scratch HOME and install folders, and a check that nothing on the real PATH (or
    the real home's uv and LaunchAgents folders) changed. ``uv_tool``'s real process
    starts raise, so a test that missed a fake fails instead of running uv."""
    real_home = Path.home()
    watched = [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p] + [
        real_home / ".local" / "bin",
        real_home / "Library" / "LaunchAgents",
    ]
    before = _snapshot(watched)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("UV_INSTALL_DIR", "UV_TOOL_BIN_DIR", "UV_TOOL_DIR", "XDG_BIN_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "CARGO_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))

    def no_process(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("a test reached a real process start in uv_tool")

    monkeypatch.setattr("netkeep.services.uv_tool.subprocess.run", no_process)
    monkeypatch.setattr("netkeep.services.uv_tool.os.execv", no_process)
    yield
    assert _snapshot(watched) == before, "a test changed a folder on the real PATH"


# -- the world -------------------------------------------------------------------------


class Exec(Exception):
    """Raised where the real exec would replace the process."""

    def __init__(self, executable: Path, arguments: Sequence[str]) -> None:
        super().__init__(f"{executable} {' '.join(arguments)}")
        self.executable = executable
        self.arguments = list(arguments)


@dataclass
class FakeClaim:
    lock: FakeLock
    released: bool = False

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.lock.holder = None


@dataclass
class FakeLock:
    """The serve lock: held by a pid, or free."""

    holder: int | None = None
    claims: int = 0
    stays_held: bool = False
    """The holder never lets go, even when its login item stops."""

    def state(self) -> LockState:
        holder = None if self.holder is None else Holder(self.holder, "netkeep serve", None)
        return LockState(
            account="serve", path=Path("serve.lock"), held=holder is not None, holder=holder
        )

    def claim(self) -> FakeClaim:
        if self.holder is not None:
            raise RuntimeError(f"pid {self.holder} holds the serve lock")
        self.holder = os.getpid()
        self.claims += 1
        return FakeClaim(self)


@dataclass
class Release:
    constraints: bytes
    sums: bytes


def _release(version: str, *, tamper: bool = False) -> Release:
    constraints = f"fastapi==0.121.0\n# netkeep {version}\n".encode()
    digest = hashlib.sha256(constraints).hexdigest()
    sums = f"{'0' * 64}  netkeep-{version}-py3-none-any.whl\n{digest}  constraints.txt\n"
    if tamper:
        constraints += b"evil==6.6.6\n"
    return Release(constraints, sums.encode())


@dataclass
class World:
    """Every fake an update reaches, and what netkeep is installed."""

    tmp: Path
    installed: str = OLD
    latest: str = NEW
    releases: dict[str, Release] = field(default_factory=dict)
    lock: FakeLock = field(default_factory=FakeLock)
    loaded: set[str] = field(default_factory=set)
    agent_pid: int | None = None
    """launchd's job pid while the login item is loaded."""
    uv_exit: dict[str, int] = field(default_factory=dict)
    """The exit status ``uv tool install netkeep==<version>`` returns; 0 by default."""
    uv_calls: list[list[str]] = field(default_factory=list)
    uv_locked: list[bool] = field(default_factory=list)
    """For each uv call, whether the update held the serve lock."""
    fetched: list[str] = field(default_factory=list)
    execs: list[list[str]] = field(default_factory=list)
    serve_answers: dict[str, bool] = field(default_factory=dict)
    """Whether a version's ``serve`` answers its health check; True by default."""
    scheduler_runs: dict[str, bool] = field(default_factory=dict)
    """Whether a version's ``serve`` starts its scheduler; True by default."""
    running: str | None = None
    restored: list[Path] = field(default_factory=list)
    backups: list[Path] = field(default_factory=list)
    has_database: bool = True
    launchctl: list[list[str]] = field(default_factory=list)
    output: list[str] = field(default_factory=list)
    clock_s: float = 0.0
    tool_install: bool = True

    def __post_init__(self) -> None:
        self.releases.setdefault(OLD, _release(OLD))
        self.releases.setdefault(NEW, _release(NEW))
        self.data = self.tmp / "netkeep-data"
        self.data.mkdir(exist_ok=True)
        self.bin = self.tmp / "uv-bin"
        self.bin.mkdir(exist_ok=True)
        (self.bin / "netkeep").write_text("#!/bin/sh\n")

    # -- launchd and the agent's serve -------------------------------------------------

    @property
    def label(self) -> str:
        return LoginItem(data=self.data, agents_dir=self.tmp / "agents", runner=self.run).label

    def install_login_item(self, *, running: bool = True) -> None:
        agents = self.tmp / "agents"
        agents.mkdir(exist_ok=True)
        item = self.item()
        (agents / f"{item.label}.plist").write_bytes(item.render_plist())
        if running:
            self._start_agent()

    def _start_agent(self) -> None:
        self.loaded.add(self.label)
        self.agent_pid = AGENT_PID
        self.lock.holder = AGENT_PID
        self.running = self.installed
        if self.scheduler_runs.get(self.installed, True):
            update.mark_scheduler_starting(self.installed, self.data)

    def run(self, args: Sequence[str]) -> RunResult:
        self.launchctl.append(list(args))
        verb, *rest = args
        label = rest[-1].rsplit("/", 1)[-1] if rest else ""
        if verb == "print":
            if label not in self.loaded:
                return RunResult(113)
            pid = f"\tpid = {self.agent_pid}\n" if self.agent_pid is not None else ""
            return RunResult(0, f"{label} = {{\n\tstate = running\n{pid}}}\n")
        if verb == "print-disabled":
            return RunResult(0, "disabled services = {\n}\n")
        if verb == "bootstrap":
            self._start_agent()
        elif verb == "bootout":
            self.loaded.discard(label)
            self.agent_pid = None
            self.running = None
            if self.lock.holder == AGENT_PID and not self.lock.stays_held:
                self.lock.holder = None
        return RunResult(0)

    def item(self) -> LoginItem:
        return LoginItem(
            data=self.data,
            agents_dir=self.tmp / "agents",
            runner=self.run,
            uid=UID,
            executable=self.bin / "netkeep",
            macos=True,
            environ={},
            lock_held=lambda: self.lock.holder is not None,
            sleep=lambda _s: None,
        )

    # -- the seams ---------------------------------------------------------------------

    def fetch(self, url: str, limit: int) -> bytes:
        self.fetched.append(url)
        assert url.startswith("https://"), url
        if url == update.LATEST_URL:
            return json.dumps({"tag_name": f"v{self.latest}"}).encode()
        for version, release in self.releases.items():
            base = f"{update.DOWNLOAD_URL}/v{version}"
            if url == f"{base}/constraints.txt":
                return release.constraints
            if url == f"{base}/SHA256SUMS":
                return release.sums
        raise update.UpdateError(f"could not download {url}: HTTP 404")

    def uv(self, arguments: list[str]) -> int:
        self.uv_calls.append(list(arguments))
        self.uv_locked.append(self.lock.holder == os.getpid())
        assert self.running is None, "uv ran while a serve was running"
        spec = next(a for a in arguments if a.startswith("netkeep=="))
        version = spec.removeprefix("netkeep==")
        status = self.uv_exit.get(version, 0)
        constraints = Path(arguments[arguments.index("-c") + 1])
        assert constraints.read_bytes() == self.releases[version].constraints
        self.installed = version if status == 0 else "broken"
        return status

    def exec_netkeep(self, executable: Path, arguments: list[str]) -> NoReturn:
        self.execs.append(list(arguments))
        raise Exec(executable, arguments)

    def serve_version(self, base: str) -> str | None:
        assert base == BASE
        if self.running is None or not self.serve_answers.get(self.running, True):
            return None
        return self.running

    def backup(self) -> Path | None:
        if not self.has_database:
            return None
        path = self.data / "backups" / f"netkeep-2026101{len(self.backups)}T000000Z.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("backup")
        self.backups.append(path)
        return path

    def restore(self, source: Path) -> list[str]:
        assert self.running is None, "restored under a running serve"
        assert self.lock.holder == os.getpid(), "restored without holding the serve lock"
        self.restored.append(source)
        return [f"restored {source.name}"]

    def sleep(self, seconds: float) -> None:
        self.clock_s += seconds

    def seams(self) -> update.Seams:
        return update.Seams(
            fetch=self.fetch,
            uv=self.uv,
            tool_bin=lambda: self.bin,
            exec_netkeep=self.exec_netkeep,
            serve_version=self.serve_version,
            base_url=lambda host, port: BASE,
            lock_state=self.lock.state,
            claim_lock=self.lock.claim,  # type: ignore[arg-type]
            backup=self.backup,
            restore=self.restore,
            tool_install=self.tool_install,
            current_version=self.installed,
            sleep=self.sleep,
            clock=lambda: self.clock_s,
        )

    def say(self, line: str) -> None:
        self.output.append(line)

    @property
    def text(self) -> str:
        return "\n".join(self.output)

    # -- the processes -----------------------------------------------------------------

    def run_update(self, *, version: str | None = None) -> int:
        """``netkeep update``, then every step its execs hand over to, in order. The exit
        status of the last."""
        step: Callable[[], int] = lambda: update.start(  # noqa: E731
            self.item(), self.seams(), self.say, version=version
        )
        while True:
            try:
                return step()
            except Exec as handed:
                assert handed.executable == self.bin / "netkeep"
                flag = handed.arguments[1]
                assert self.installed != "broken"
                if flag == "--finish":
                    step = lambda: update.finish(self.item(), self.seams(), self.say)  # noqa: E731
                else:
                    assert flag == "--rollback"
                    step = lambda: update.rollback(self.item(), self.seams(), self.say)  # noqa: E731

    def state(self) -> update.UpdateState | None:
        return update.read_state(self.data)


def _state(
    from_version: str,
    to_version: str,
    data: Path,
    backup: str | None,
    was_on: bool,
    phase: str,
    **more: Any,
) -> update.UpdateState:
    """A state as ``netkeep update`` writes it, with the constraints paths it keeps."""
    return update.UpdateState(
        from_version,
        to_version,
        str(update.constraints_path(from_version, data)),
        str(update.constraints_path(to_version, data)),
        backup,
        was_on,
        phase,
        **more,
    )


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


# -- the happy path --------------------------------------------------------------------


def test_an_update_backs_up_installs_and_hands_over(world: World) -> None:
    world.install_login_item()
    assert world.run_update() == 0, world.text
    assert world.installed == NEW
    assert world.running == NEW
    assert world.uv_calls == [
        [
            "tool",
            "install",
            "--python",
            "3.12",
            "--managed-python",
            "-c",
            str(update.constraints_path(NEW, world.data)),
            f"netkeep=={NEW}",
            "--reinstall",
        ]
    ]
    assert world.execs == [["update", "--finish"]]
    assert len(world.backups) == 1
    assert world.restored == []
    assert world.state() is None, "a finished update leaves no state"
    assert not (update.update_dir(world.data) / update.SCHEDULER_MARKER).exists()
    # Both versions' constraints files are kept in the data directory (rule 8).
    for version in (OLD, NEW):
        kept = update.constraints_path(version, world.data)
        assert kept.read_bytes() == world.releases[version].constraints
    assert all(url.startswith("https://") for url in world.fetched)
    assert f"Done: netkeep {NEW} is running from the login item." in world.output


def test_the_login_item_is_stopped_before_the_backup_and_the_install(world: World) -> None:
    world.install_login_item()
    order: list[str] = []
    backup, uv = world.backup, world.uv

    def recording_backup() -> Path | None:
        order.append(f"backup running={world.running}")
        return backup()

    def recording_uv(arguments: list[str]) -> int:
        order.append(f"uv running={world.running} lock={world.lock.holder == os.getpid()}")
        return uv(arguments)

    world.backup = recording_backup  # type: ignore[method-assign]
    world.uv = recording_uv  # type: ignore[method-assign]
    assert world.run_update() == 0, world.text
    assert order == ["backup running=None", "uv running=None lock=True"]
    verbs = [call[0] for call in world.launchctl if call[0] in ("bootout", "bootstrap")]
    assert verbs == ["bootout", "bootstrap"]


def test_an_explicit_version_is_installed(world: World) -> None:
    world.releases["1.4.0"] = _release("1.4.0")
    world.install_login_item()
    assert world.run_update(version="1.4.0") == 0, world.text
    assert world.installed == "1.4.0"
    assert update.LATEST_URL not in world.fetched


def test_a_login_item_that_was_off_stays_off(world: World) -> None:
    assert world.run_update() == 0, world.text
    assert world.installed == NEW
    assert world.running is None
    assert not any(call[0] == "bootstrap" for call in world.launchctl)
    assert "Start it with: netkeep serve" in world.output
    assert world.state() is None


def test_nothing_to_do_when_up_to_date(world: World) -> None:
    world.latest = OLD
    world.install_login_item()
    assert world.run_update() == 0
    assert world.uv_calls == [] and world.backups == []
    assert world.running == OLD
    assert not any(call[0] == "bootout" for call in world.launchctl)


def test_an_older_version_is_never_installed(world: World) -> None:
    world.releases["1.1.0"] = _release("1.1.0")
    world.install_login_item()
    assert world.run_update(version="1.1.0") == 0
    assert world.uv_calls == [] and world.running == OLD


# -- refusals that change nothing ------------------------------------------------------


def test_a_manual_serve_is_refused(world: World) -> None:
    world.install_login_item(running=False)
    world.lock.holder = MANUAL_PID
    assert world.run_update() == 1
    assert "isn't the login item's serve" in world.text
    assert world.uv_calls == [] and world.backups == []
    assert not any(call[0] in ("bootout", "bootstrap") for call in world.launchctl)
    assert world.lock.holder == MANUAL_PID
    assert world.state() is None


def test_a_serve_beside_the_running_login_item_is_refused(world: World) -> None:
    """The lock is held, the agent is loaded, but by another pid: a manual serve won."""
    world.install_login_item()
    world.lock.holder = MANUAL_PID
    assert world.run_update() == 1
    assert world.uv_calls == [] and world.running == OLD
    assert not any(call[0] == "bootout" for call in world.launchctl)


def test_a_held_lock_with_no_holder_note_is_refused(world: World) -> None:
    world.install_login_item()
    world.lock.state = lambda: LockState(  # type: ignore[method-assign]
        account="serve", path=Path("serve.lock"), held=True, holder=None
    )
    assert world.run_update() == 1
    assert world.uv_calls == []


def test_a_serve_that_wont_let_go_aborts_and_restarts_the_login_item(world: World) -> None:
    world.install_login_item()
    world.lock.stays_held = True
    assert world.run_update() == 1
    assert "still holds the serve lock" in world.text
    assert world.uv_calls == [] and world.backups == []
    # The login item is still registered; it can't start while that serve holds the lock.
    assert world.item().plist_path.exists()
    assert "started the login item again" in world.text


def test_a_failed_backup_installs_nothing_and_restarts_the_login_item(world: World) -> None:
    world.install_login_item()

    def broken() -> Path | None:
        raise OSError("disk full")

    world.backup = broken  # type: ignore[method-assign]
    assert world.run_update() == 1
    assert "the backup failed: disk full" in world.text
    assert world.uv_calls == []
    assert world.running == OLD
    assert world.lock.holder == AGENT_PID, "the update let go of the lock"
    assert world.state() is None


def test_a_checksum_mismatch_installs_nothing(world: World) -> None:
    world.releases[NEW] = _release(NEW, tamper=True)
    world.install_login_item()
    assert world.run_update() == 1
    assert "but its SHA256SUMS says" in world.text
    assert world.uv_calls == [] and world.backups == []
    assert world.running == OLD, "serve was never stopped"
    assert not update.constraints_path(NEW, world.data).exists()


@pytest.mark.parametrize(
    "sums",
    [
        b"",
        b"abc  constraints.txt\n",
        b"%s  constraints.txt\n%s  constraints.txt\n" % (b"a" * 64, b"b" * 64),
        b"%s  other.txt\n" % (b"a" * 64),
    ],
)
def test_sha256sums_needs_one_valid_line(world: World, sums: bytes) -> None:
    world.releases[NEW] = Release(world.releases[NEW].constraints, sums)
    assert world.run_update() == 1
    assert "SHA256SUMS has no single valid checksum" in world.text
    assert world.uv_calls == []


def test_a_missing_release_for_the_installed_version_is_refused(world: World) -> None:
    """Without the old version's constraints there is nothing to roll back to."""
    del world.releases[OLD]
    world.install_login_item()
    assert world.run_update() == 1
    assert "HTTP 404" in world.text
    assert world.uv_calls == [] and world.running == OLD


def test_a_checkout_is_refused(world: World) -> None:
    world.tool_install = False
    world.install_login_item()
    assert world.run_update() == 1
    assert "git pull, then make install" in world.text
    assert world.fetched == [] and world.uv_calls == []


def test_an_unfinished_update_is_refused(world: World) -> None:
    world.install_login_item()
    update.write_state(
        _state(OLD, NEW, world.data, None, True, "stopped", reason="it broke."),
        world.data,
    )
    assert world.run_update() == 1
    assert "didn't finish (stopped). it broke." in world.text
    assert world.fetched == [] and world.uv_calls == []


# -- rollback ---------------------------------------------------------------------------


def test_an_upgrade_failure_reinstalls_the_old_version(world: World) -> None:
    world.install_login_item()
    world.uv_exit[NEW] = 1
    assert world.run_update() == 1
    assert [call[-2] for call in world.uv_calls] == [f"netkeep=={NEW}", f"netkeep=={OLD}"]
    assert world.uv_calls[1][world.uv_calls[1].index("-c") + 1] == str(
        update.constraints_path(OLD, world.data)
    )
    assert world.execs == [["update", "--rollback"]]
    assert world.installed == OLD and world.running == OLD
    assert world.restored == [], "the new version never ran, so the database is as it was"
    assert world.state() is None
    assert f"Rolled back: netkeep {OLD} is running again" in world.text


def test_a_failed_reinstall_after_an_upgrade_failure_explains(world: World) -> None:
    world.install_login_item()
    world.uv_exit[NEW] = 1
    world.uv_exit[OLD] = 1
    assert world.run_update() == 1
    assert world.execs == []
    assert "Run this, then netkeep update --rollback:" in world.text
    state = world.state()
    assert state is not None and state.phase == "rolling-back"
    # Once the person reinstalls by hand, the --rollback it names finishes the job.
    world.uv_exit[OLD] = 0
    world.uv(update.install_command(OLD, Path(state.from_constraints)))
    assert update.rollback(world.item(), world.seams(), world.say) == 1
    assert world.running == OLD and world.state() is None


def test_a_health_failure_before_the_scheduler_rolls_back(world: World) -> None:
    world.install_login_item()
    world.serve_answers[NEW] = False
    world.scheduler_runs[NEW] = False
    assert world.run_update() == 1
    assert world.execs == [["update", "--finish"], ["update", "--rollback"]]
    assert world.installed == OLD and world.running == OLD
    assert world.restored == world.backups, "the backup from before the update is restored"
    assert world.state() is None
    assert "failed before its scheduler started" in world.text
    assert f"Rolled back: netkeep {OLD} is running again" in world.text


def test_a_new_serve_answering_with_the_old_version_fails_the_check(world: World) -> None:
    world.install_login_item()
    world.scheduler_runs[NEW] = False
    original = world.serve_version
    world.serve_version = lambda base: OLD if original(base) == NEW else original(base)  # type: ignore[method-assign]
    assert world.run_update() == 1
    assert f"with version {OLD}, not {NEW}" in world.text
    assert world.installed == OLD


def test_a_health_failure_after_the_scheduler_stops_and_explains(world: World) -> None:
    world.install_login_item()
    world.serve_answers[NEW] = False
    assert world.run_update() == 1
    assert world.execs == [["update", "--finish"]], "no automatic rollback"
    assert world.installed == NEW
    assert world.restored == []
    assert [call[-2] for call in world.uv_calls] == [f"netkeep=={NEW}"]
    assert world.running is None, "the version that failed is stopped"
    assert not world.item().plist_path.exists(), "and doesn't start again at the next login"
    state = world.state()
    assert state is not None and state.phase == "stopped"
    assert "it may have sent messages" in world.text
    assert f"netkeep restore {world.backups[0].name}" in world.text
    assert f"netkeep=={OLD}" in world.text


def test_an_unreadable_scheduler_marker_counts_as_started(world: World) -> None:
    world.install_login_item()
    world.serve_answers[NEW] = False
    world.scheduler_runs[NEW] = False
    original = world._start_agent

    def start_and_scribble() -> None:
        original()
        if world.installed == NEW:
            (update.update_dir(world.data) / update.SCHEDULER_MARKER).write_text("{not json")

    world._start_agent = start_and_scribble  # type: ignore[method-assign]
    assert world.run_update() == 1
    assert world.restored == [] and world.installed == NEW


def test_an_old_marker_is_cleared_before_the_install(world: World) -> None:
    """The old version's serve wrote a marker during an earlier attempt: it never counts
    for the new version."""
    world.install_login_item()
    update.update_dir(world.data).mkdir(parents=True, exist_ok=True)
    (update.update_dir(world.data) / update.SCHEDULER_MARKER).write_text(
        json.dumps({"version": NEW})
    )
    world.serve_answers[NEW] = False
    world.scheduler_runs[NEW] = False
    assert world.run_update() == 1
    assert world.installed == OLD and world.restored == world.backups


def test_a_rollback_whose_health_check_fails_explains(world: World) -> None:
    world.install_login_item()
    world.serve_answers[NEW] = False
    world.serve_answers[OLD] = False
    world.scheduler_runs[NEW] = False
    assert world.run_update() == 1
    assert world.installed == OLD
    assert "the rollback didn't finish" in world.text
    state = world.state()
    assert state is not None and state.phase == "stopped"


# -- --finish and --rollback on their own ----------------------------------------------


def test_finish_runs_only_on_the_new_version(world: World) -> None:
    world.install_login_item()
    with pytest.raises(Exec) as handed:
        update.start(world.item(), world.seams(), world.say)
    assert handed.value.arguments == ["update", "--finish"]
    state = world.state()
    assert state is not None and state.phase == "finishing"
    # The old code, still loaded, refuses the new version's step.
    wrong = dataclasses.replace(world.seams(), current_version=OLD)
    assert update.finish(world.item(), wrong, world.say) == 1
    assert f"but the update installed {NEW}" in world.text
    assert world.running is None
    assert update.finish(world.item(), world.seams(), world.say) == 0
    assert world.running == NEW


def test_finish_and_rollback_with_nothing_pending(world: World) -> None:
    assert update.finish(world.item(), world.seams(), world.say) == 1
    assert update.rollback(world.item(), world.seams(), world.say) == 1
    assert world.uv_calls == [] and world.launchctl == []


def test_a_missing_netkeep_after_install_says_what_to_run(world: World) -> None:
    world.install_login_item()
    (world.bin / "netkeep").unlink()
    with pytest.raises(update.UpdateError, match="Run netkeep update --finish yourself"):
        update.start(world.item(), world.seams(), world.say)


# -- --check -----------------------------------------------------------------------------


def test_check_reports_a_newer_version_and_changes_nothing(world: World) -> None:
    world.install_login_item()
    assert update.check(world.seams(), world.say) == 0
    assert f"netkeep {NEW} is available; this is {OLD}." in world.output
    assert world.fetched == [update.LATEST_URL]
    assert world.uv_calls == [] and world.backups == []
    assert world.launchctl == [], "check never touches launchd"
    assert not update.update_dir(world.data).exists()


def test_check_when_up_to_date(world: World) -> None:
    world.latest = OLD
    assert update.check(world.seams(), world.say) == 0
    assert f"netkeep {OLD} is up to date" in world.text


@pytest.mark.parametrize("tag", ["1.3.0", "v1.3", "v1.3.0-rc1", None])
def test_a_latest_tag_that_isnt_vxyz_is_refused(world: World, tag: str | None) -> None:
    world.fetch = lambda url, limit: json.dumps({"tag_name": tag}).encode()  # type: ignore[method-assign]
    if tag == "1.3.0":
        assert update.check(world.seams(), world.say) == 0  # a bare X.Y.Z is fine
        return
    assert update.check(world.seams(), world.say) == 1
    assert "tag isn't vX.Y.Z" in world.text


# -- the scheduler marker ---------------------------------------------------------------


def test_serve_writes_the_marker_only_during_an_update(tmp_path: Path) -> None:
    update.mark_scheduler_starting(NEW, tmp_path)
    assert not update.update_dir(tmp_path).exists()
    update.write_state(_state(OLD, NEW, tmp_path, None, True, "finishing"), tmp_path)
    update.mark_scheduler_starting(NEW, tmp_path)
    assert update.scheduler_started(tmp_path)


def test_a_marker_from_any_version_counts_as_started(tmp_path: Path) -> None:
    """Whichever netkeep wrote it (a checkout's serve, another install), a scheduler ran on
    this data directory during the update, and a restore would forget what it sent."""
    update.write_state(_state(OLD, NEW, tmp_path, None, True, "finishing"), tmp_path)
    update.mark_scheduler_starting("9.9.9", tmp_path)
    assert update.scheduler_started(tmp_path)


async def test_the_lifespan_marks_before_the_scheduler_starts(
    app: FastAPI, bare_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``serve`` records the marker before anything that can send has started."""
    data = Path(os.environ["NETKEEP_DATA"])
    update.write_state(_state(OLD, __version__, data, None, True, "finishing"), data)
    seen: list[bool] = []

    class _Stopped:
        executor = None
        scheduler = None

        def stop(self) -> None:
            return None

    def scheduler(*_args: Any, **_kwargs: Any) -> _Stopped:
        seen.append(update.scheduler_started(data))
        return _Stopped()

    class _Monitor:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr("netkeep.web.app.start_serve_scheduler", scheduler)
    monkeypatch.setattr("netkeep.web.app.MailboxMonitor", _Monitor)
    served = create_app(engine=bare_engine, extractor=ServeExtractor(executor=_no_executor))
    async with served.router.lifespan_context(served):
        pass
    assert seen == [True]


def _no_executor(factory: Any, bus: Any) -> Any:
    raise AssertionError("the recorded scheduler never builds an executor")


# -- downloads ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status: int, body: bytes = b"", location: str | None = None) -> None:
        self.status = status
        self._body = body
        self._location = location

    def getheader(self, name: str) -> str | None:
        return self._location if name == "Location" else None

    def read(self, limit: int) -> bytes:
        chunk, self._body = self._body[:limit], self._body[limit:]
        return chunk


class _Connections:
    """Stands in for ``http.client.HTTPSConnection``: one scripted answer per host."""

    def __init__(self, answers: dict[str, _Response]) -> None:
        self.answers = answers
        self.opened: list[tuple[str, int]] = []
        self.paths: list[str] = []

    def __call__(self, host: str, port: int, **_kwargs: object) -> _Connections._One:
        self.opened.append((host, port))
        return _Connections._One(self, host)

    class _One:
        def __init__(self, outer: _Connections, host: str) -> None:
            self.outer = outer
            self.host = host

        def request(self, method: str, path: str, headers: dict[str, str]) -> None:
            assert method == "GET"
            assert headers["User-Agent"].startswith("netkeep-update")
            self.outer.paths.append(path)

        def getresponse(self) -> _Response:
            return self.outer.answers[self.host]

        def close(self) -> None:
            pass


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/dsmorgan/netkeep/releases/download/v1.3.0/constraints.txt",
        "https://example.invalid/constraints.txt",
        "https://github.com:8443/x",
        "ftp://github.com/x",
    ],
)
def test_a_download_is_https_from_github_only(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    connections = _Connections({})
    monkeypatch.setattr("netkeep.services.update.http.client.HTTPSConnection", connections)
    with pytest.raises(update.UpdateError, match="only HTTPS from GitHub"):
        update.https_get(url, max_bytes=10)
    assert connections.opened == []


def test_a_redirect_is_followed_only_to_an_allowed_https_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections = _Connections(
        {
            "github.com": _Response(
                302, location="https://release-assets.githubusercontent.com/a?sig=1"
            ),
            "release-assets.githubusercontent.com": _Response(200, b"pins"),
        }
    )
    monkeypatch.setattr("netkeep.services.update.http.client.HTTPSConnection", connections)
    assert update.https_get("https://github.com/x", max_bytes=10) == b"pins"
    assert connections.opened == [
        ("github.com", 443),
        ("release-assets.githubusercontent.com", 443),
    ]
    assert connections.paths == ["/x", "/a?sig=1"]


@pytest.mark.parametrize(
    "location", ["http://release-assets.githubusercontent.com/a", "https://example.invalid/a"]
)
def test_a_redirect_elsewhere_is_refused(location: str, monkeypatch: pytest.MonkeyPatch) -> None:
    connections = _Connections({"github.com": _Response(302, location=location)})
    monkeypatch.setattr("netkeep.services.update.http.client.HTTPSConnection", connections)
    with pytest.raises(update.UpdateError, match="only HTTPS from GitHub"):
        update.https_get("https://github.com/x", max_bytes=10)
    assert connections.opened == [("github.com", 443)]


def test_a_download_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    connections = _Connections({"github.com": _Response(200, b"x" * 11)})
    monkeypatch.setattr("netkeep.services.update.http.client.HTTPSConnection", connections)
    with pytest.raises(update.UpdateError, match="larger than 10 bytes"):
        update.https_get("https://github.com/x", max_bytes=10)


def test_an_http_error_is_an_update_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connections = _Connections({"github.com": _Response(404)})
    monkeypatch.setattr("netkeep.services.update.http.client.HTTPSConnection", connections)
    with pytest.raises(update.UpdateError, match="HTTP 404"):
        update.https_get("https://github.com/x", max_bytes=10)


def test_the_install_command_is_rule_1s() -> None:
    assert update.install_command("1.3.0", Path("/c.txt")) == [
        "tool",
        "install",
        "--python",
        "3.12",
        "--managed-python",
        "-c",
        "/c.txt",
        "netkeep==1.3.0",
        "--reinstall",
    ]
    assert {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    } == update.ALLOWED_HOSTS
    assert (update.SERVE_WAIT_S, update._LOCK_WAIT_S) == (90.0, 60.0)


# -- uv_tool's guards --------------------------------------------------------------------


def test_uv_tool_runs_only_uv_tool_commands() -> None:
    with pytest.raises(ValueError, match="only uv tool commands"):
        uv_tool.run_uv(["pip", "install", "x"])
    with pytest.raises(ValueError, match="only uv tool commands"):
        uv_tool.run_uv([])


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("sh", ["update", "--finish"]),
        ("netkeep", ["serve"]),
        ("netkeep", ["update", "--finish", "--extra"]),
    ],
)
def test_uv_tool_execs_only_an_update_step(tmp_path: Path, name: str, arguments: list[str]) -> None:
    with pytest.raises(ValueError, match="execs only"):
        uv_tool.exec_netkeep(tmp_path / name, arguments)


def test_uv_tool_execs_the_next_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Path, list[str]]] = []

    def fake_execv(path: Path, argv: list[str]) -> None:
        calls.append((path, argv))

    monkeypatch.setattr("netkeep.services.uv_tool.os.execv", fake_execv)
    with pytest.raises(RuntimeError, match="exec returned"):
        uv_tool.exec_netkeep(tmp_path / "netkeep", ["update", "--rollback"])
    assert calls == [(tmp_path / "netkeep", [str(tmp_path / "netkeep"), "update", "--rollback"])]


def test_uv_tool_finds_uv_on_path_or_in_local_bin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert uv_tool.find_uv() is None
    assert uv_tool.run_uv(["tool", "dir"]).returncode == 127
    local = Path.home() / ".local" / "bin" / "uv"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    local.chmod(0o755)
    assert uv_tool.find_uv() == local


def test_running_from_a_tool_install(tmp_path: Path) -> None:
    tool = tmp_path / "tools" / "netkeep"
    package = tool / "lib" / "python3.12" / "site-packages" / "netkeep"
    package.mkdir(parents=True)
    assert not update.running_from_tool_install(tool, package)  # no receipt
    (tool / "uv-receipt.toml").write_text("[tool]\n")
    assert update.running_from_tool_install(tool, package)
    checkout = tmp_path / "checkout" / "netkeep"
    checkout.mkdir(parents=True)
    assert not update.running_from_tool_install(tool, checkout)  # an editable checkout


# -- the CLI -----------------------------------------------------------------------------


def test_the_cli_refuses_a_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """This test suite runs from a checkout's .venv: the real check says so."""

    def no_download(url: str, *, max_bytes: int, accept: str = "*/*") -> bytes:
        raise AssertionError(f"downloaded {url}")

    monkeypatch.setattr(update, "https_get", no_download)
    result = CliRunner().invoke(cli.app, ["update"])
    assert result.exit_code == 1
    assert "git pull, then make install" in result.output


def test_the_cli_check(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_download(url: str, *, max_bytes: int, accept: str = "*/*") -> bytes:
        assert url == update.LATEST_URL
        return json.dumps({"tag_name": "v999.0.0"}).encode()

    monkeypatch.setattr(update, "https_get", fake_download)
    result = CliRunner().invoke(cli.app, ["update", "--check"])
    assert result.exit_code == 0, result.output
    assert f"netkeep 999.0.0 is available; this is {__version__}." in result.output


def test_serve_version_reads_the_health_version(monkeypatch: pytest.MonkeyPatch) -> None:
    answers: list[dict[str, object] | None] = [
        {"status": "ok", "version": NEW},
        {"status": "ok"},
        None,
    ]
    monkeypatch.setattr(post_install, "health", lambda base, timeout_s=2.0: answers.pop(0))
    assert post_install.serve_version(BASE) == NEW
    assert post_install.serve_version(BASE) is None
    assert post_install.serve_version(BASE) is None


@pytest.mark.parametrize("marker", ["{}", "[]", '"x"', "{not json"])
def test_a_marker_that_names_no_version_counts_as_started(tmp_path: Path, marker: str) -> None:
    """The safe reading: a marker serve wrote, whatever it holds, means don't restore."""
    update.update_dir(tmp_path).mkdir(parents=True)
    (update.update_dir(tmp_path) / update.SCHEDULER_MARKER).write_text(marker)
    assert update.scheduler_started(tmp_path)


# -- review fixes (#692): no restore after any scheduler may have run -----------------


def test_rollback_refuses_to_restore_once_a_scheduler_ran(world: World) -> None:
    """The review's path: --finish fails before the scheduler, the old version's reinstall
    fails, the new version starts from the login item at the next login and runs its
    scheduler, then the person reinstalls the old version and runs --rollback."""
    world.install_login_item()
    world.serve_answers[NEW] = False
    world.scheduler_runs[NEW] = False
    world.uv_exit[OLD] = 1
    assert world.run_update() == 1
    state = world.state()
    assert state is not None and state.phase == "rolling-back" and state.new_version_ran
    # The next login: launchd starts the netkeep that is installed, whose scheduler runs.
    world.installed = NEW
    world.scheduler_runs[NEW] = True
    world.run(["bootstrap", f"gui/{UID}", str(world.item().plist_path)])
    assert update.scheduler_started(world.data)
    world.run(["bootout", f"gui/{UID}/{world.label}"])
    # The person follows the printed steps: reinstall the old version, then --rollback.
    world.uv_exit[OLD] = 0
    world.uv(update.install_command(OLD, Path(state.from_constraints)))
    assert update.rollback(world.item(), world.seams(), world.say) == 1
    assert world.restored == [], "no restore over what the new version's scheduler sent"
    assert "a serve started its scheduler on this data directory" in world.text
    assert not world.item().plist_path.exists()
    stopped = world.state()
    assert stopped is not None and stopped.phase == "stopped"


def test_a_second_rollback_never_restores_again(world: World) -> None:
    """A crash during the health wait after the restore: the restored version's serve may
    be sending. Running --rollback again refuses."""
    world.install_login_item()
    world.serve_answers[NEW] = False
    world.scheduler_runs[NEW] = False

    class Crash(Exception):
        pass

    original = world.serve_version

    def crash_on_old(base: str) -> str | None:
        if world.running == OLD:
            raise Crash
        return original(base)

    world.serve_version = crash_on_old  # type: ignore[method-assign]
    with pytest.raises(Crash):
        world.run_update()
    assert world.restored == world.backups
    state = world.state()
    assert state is not None and state.restored, "the restore is recorded before serve starts"
    world.serve_version = original  # type: ignore[method-assign]
    assert update.rollback(world.item(), world.seams(), world.say) == 1
    assert world.restored == world.backups, "restored once, never twice"
    assert "already restored the backup" in world.text


def test_the_restore_is_recorded_before_it_is_made(world: World) -> None:
    world.install_login_item()
    world.serve_answers[NEW] = False
    world.scheduler_runs[NEW] = False
    recorded: list[bool] = []
    restore = world.restore

    def recording(source: Path) -> list[str]:
        state = world.state()
        recorded.append(state is not None and state.restored)
        return restore(source)

    world.restore = recording  # type: ignore[method-assign]
    assert world.run_update() == 1
    assert recorded == [True]


def test_a_marker_from_another_version_stops_the_rollback(world: World) -> None:
    world.install_login_item()
    world.serve_answers[NEW] = False
    world.scheduler_runs[NEW] = False
    original = world._start_agent

    def another_serve_marks() -> None:
        original()
        if world.installed == NEW:
            update.mark_scheduler_starting("9.9.9", world.data)

    world._start_agent = another_serve_marks  # type: ignore[method-assign]
    assert world.run_update() == 1
    assert world.restored == [] and world.installed == NEW
    assert "may have sent messages" in world.text


def test_a_serve_that_wont_stop_after_a_failed_check_stops_and_explains(world: World) -> None:
    world.install_login_item()
    world.serve_answers[NEW] = False
    world.scheduler_runs[NEW] = False
    original = world._start_agent

    def stubborn() -> None:
        original()
        if world.installed == NEW:
            world.lock.stays_held = True

    world._start_agent = stubborn  # type: ignore[method-assign]
    assert world.run_update() == 1
    assert "its serve wouldn't stop" in world.text
    assert [call[-2] for call in world.uv_calls] == [f"netkeep=={NEW}"], "no reinstall"
    assert world.restored == []
    state = world.state()
    assert state is not None and state.phase == "stopped"


def test_rollback_runs_only_on_the_old_version(world: World) -> None:
    world.install_login_item()
    world.installed = NEW
    update.write_state(
        _state(
            OLD, NEW, world.data, str(world.backup()), True, "rolling-back", new_version_ran=True
        ),
        world.data,
    )
    assert update.rollback(world.item(), world.seams(), world.say) == 1
    assert f"but the rollback reinstalled {OLD}" in world.text
    assert world.restored == []


def test_the_lock_is_held_across_every_install(world: World) -> None:
    world.install_login_item()
    world.uv_exit[NEW] = 1
    assert world.run_update() == 1
    assert world.uv_locked == [True, True]
    (world.tmp / "second").mkdir()
    world2 = World(world.tmp / "second")
    world2.install_login_item()
    world2.serve_answers[NEW] = False
    world2.scheduler_runs[NEW] = False
    assert world2.run_update() == 1
    assert world2.uv_locked == [True, True], "the old version's reinstall holds the lock too"
    assert world2.lock.holder == AGENT_PID


def test_finish_accepts_an_install_it_couldnt_record(world: World) -> None:
    """uv finished, but writing "finishing" failed: the new version still finishes."""
    world.install_login_item()
    update.write_state(_state(OLD, NEW, world.data, None, True, "installing"), world.data)
    world.run(["bootout", f"gui/{UID}/{world.label}"])
    world.installed = NEW
    assert update.finish(world.item(), world.seams(), world.say) == 0
    assert world.running == NEW


def test_a_state_write_failure_aborts_cleanly(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.install_login_item()

    def refuse(_state: update.UpdateState, _data: Path | None = None) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(update, "write_state", refuse)
    assert world.run_update() == 1
    assert "could not record the update" in world.text
    assert "No space left on device" in world.text
    assert world.uv_calls == []
    assert world.running == OLD, "the login item is started again"
    assert world.lock.holder == AGENT_PID


def test_state_and_marker_are_written_durably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each write fsyncs the file before the rename and the folder after it."""
    events: list[str] = []
    real_fsync, real_replace = os.fsync, Path.replace

    def fsync(fd: int) -> None:
        events.append("fsync-dir" if os.path.isdir(f"/dev/fd/{fd}") else "fsync-file")
        real_fsync(fd)

    def replace(self: Path, target: Path) -> Path:
        events.append(f"rename {Path(target).name}")
        return real_replace(self, target)

    monkeypatch.setattr("os.fsync", fsync)
    monkeypatch.setattr(Path, "replace", replace)
    update.write_state(_state(OLD, NEW, tmp_path, None, True, "finishing"), tmp_path)
    assert events[-3:] == ["fsync-file", f"rename {update.STATE_FILE}", "fsync-dir"]
    events.clear()
    update.mark_scheduler_starting(NEW, tmp_path)
    assert events == ["fsync-file", f"rename {update.SCHEDULER_MARKER}", "fsync-dir"]


def test_constraints_are_written_durably_and_never_half_written(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback reinstalls the old version from its constraints file (#705): the
    download is fsynced before its rename and the folder after it, and a failed write
    leaves the file that was there, with no temporary file behind."""
    target = update.constraints_path(OLD, world.data)
    target.parent.mkdir(parents=True)
    target.write_text("kept\n")
    events: list[str] = []
    real_fsync, real_replace = os.fsync, Path.replace

    def fsync(fd: int) -> None:
        events.append("fsync-dir" if os.path.isdir(f"/dev/fd/{fd}") else "fsync-file")
        real_fsync(fd)

    def failing_replace(self: Path, _target: Path) -> Path:
        events.append("rename")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("os.fsync", fsync)
    monkeypatch.setattr(Path, "replace", failing_replace)
    with pytest.raises(OSError, match="No space left"):
        update.download_constraints(world.seams(), OLD, world.data)
    assert target.read_text() == "kept\n"
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]
    assert events == ["fsync-file", "rename"]

    events.clear()

    def replace(self: Path, target_path: Path) -> Path:
        events.append(f"rename {Path(target_path).name}")
        return real_replace(self, target_path)

    monkeypatch.setattr(Path, "replace", replace)
    assert update.download_constraints(world.seams(), OLD, world.data) == target
    assert events == ["fsync-file", f"rename {target.name}", "fsync-dir"]
    assert target.read_bytes() != b"kept\n"
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]


async def test_a_marker_that_cant_be_written_stops_serve(
    app: FastAPI, bare_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = Path(os.environ["NETKEEP_DATA"])
    update.write_state(_state(OLD, __version__, data, None, True, "finishing"), data)
    started: list[bool] = []

    def scheduler(*_args: Any, **_kwargs: Any) -> Any:
        started.append(True)
        raise AssertionError("the scheduler started without its marker")

    def unwritable(_path: Path, _text: str) -> None:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr("netkeep.web.app.start_serve_scheduler", scheduler)
    monkeypatch.setattr(update, "_write_durably", unwritable)
    served = create_app(engine=bare_engine, extractor=ServeExtractor(executor=_no_executor))
    with pytest.raises(OSError, match="Read-only file system"):
        async with served.router.lifespan_context(served):
            pass
    assert started == []


@pytest.mark.parametrize(
    "change",
    [
        {"from_version": "1.2.0; rm -rf"},
        {"to_version": "latest"},
        {"phase": "anything"},
        {"login_item_was_on": "yes"},
        {"restored": None},
    ],
)
def test_read_state_is_strict(tmp_path: Path, change: dict[str, object]) -> None:
    update.write_state(_state(OLD, NEW, tmp_path, None, True, "finishing"), tmp_path)
    path = update.update_dir(tmp_path) / update.STATE_FILE
    raw = json.loads(path.read_text())
    path.write_text(json.dumps(raw | change))
    with pytest.raises(update.UpdateError, match="isn't an update netkeep knows"):
        update.read_state(tmp_path)


@pytest.mark.parametrize(
    "missing",
    ["from_version", "phase", "login_item_was_on", "restored", "new_version_ran", "reason"],
)
def test_a_state_missing_a_frozen_field_is_refused(tmp_path: Path, missing: str) -> None:
    """Every frozen field must be there, defaulted ones too: a state without ``restored``
    must never read as "not restored yet" and let a second rollback restore."""
    update.write_state(_state(OLD, NEW, tmp_path, None, True, "finishing"), tmp_path)
    path = update.update_dir(tmp_path) / update.STATE_FILE
    raw = json.loads(path.read_text())
    del raw[missing]
    path.write_text(json.dumps(raw))
    with pytest.raises(update.UpdateError, match="isn't an update netkeep knows"):
        update.read_state(tmp_path)


def test_the_state_fields_and_phases_are_frozen() -> None:
    """The compatibility rule (#705): every version reads every other's state.json, so no
    release renames, removes or retypes a field or a phase. Only additions with a default
    are allowed, and adding one updates this pin."""
    assert {
        "from_version": (str,),
        "to_version": (str,),
        "from_constraints": (str,),
        "to_constraints": (str,),
        "backup": (str, type(None)),
        "login_item_was_on": (bool,),
        "phase": (str,),
        "new_version_ran": (bool,),
        "restored": (bool,),
        "reason": (str,),
        "started_at": (str,),
    } == update._STATE_TYPES
    assert {"installing", "finishing", "rolling-back", "stopped"} == update.PHASES
    names = {f.name for f in dataclasses.fields(update.UpdateState)}
    assert names == set(update._STATE_TYPES) | {"carried"}


def _add_unknown_keys(data: Path, keys: dict[str, object]) -> None:
    """Make the state file look like a newer netkeep wrote it, with fields this one
    doesn't know."""
    path = update.update_dir(data) / update.STATE_FILE
    path.write_text(json.dumps(json.loads(path.read_text()) | keys))


NEWER_KEYS: dict[str, object] = {"added_later": {"note": "from a newer netkeep"}, "count": 3}


def test_unknown_keys_are_kept_and_written_back(tmp_path: Path) -> None:
    update.write_state(_state(OLD, NEW, tmp_path, None, True, "finishing"), tmp_path)
    _add_unknown_keys(tmp_path, NEWER_KEYS)
    state = update.read_state(tmp_path)
    assert state is not None and state.carried == NEWER_KEYS
    update.write_state(dataclasses.replace(state, phase="rolling-back"), tmp_path)
    raw = json.loads((update.update_dir(tmp_path) / update.STATE_FILE).read_text())
    assert raw["phase"] == "rolling-back"
    assert {name: raw[name] for name in NEWER_KEYS} == NEWER_KEYS


def test_a_rollback_reads_a_state_a_newer_version_wrote(world: World) -> None:
    """The new version's --finish wrote fields the old version doesn't know: the old
    version's --rollback still restores (no scheduler ran) and records the restore first,
    keeping those fields."""
    world.install_login_item(running=False)
    backup = world.backup()
    update.write_state(
        _state(OLD, NEW, world.data, str(backup), True, "rolling-back", new_version_ran=True),
        world.data,
    )
    _add_unknown_keys(world.data, NEWER_KEYS)
    path = update.update_dir(world.data) / update.STATE_FILE
    recorded: list[dict[str, object]] = []
    restore = world.restore

    def recording(source: Path) -> list[str]:
        recorded.append(json.loads(path.read_text()))
        return restore(source)

    world.restore = recording  # type: ignore[method-assign]
    assert update.rollback(world.item(), world.seams(), world.say) == 1
    assert "isn't an update netkeep knows" not in world.text
    assert world.restored == [backup]
    assert recorded[0]["restored"] is True
    assert {name: recorded[0][name] for name in NEWER_KEYS} == NEWER_KEYS
    assert f"Rolled back: netkeep {OLD} is running again" in world.text


def test_unknown_keys_never_let_a_rollback_restore_after_a_scheduler_ran(
    world: World,
) -> None:
    """PR #692's rule holds whatever a newer version wrote: once any scheduler may have
    run, nothing restores."""
    world.install_login_item(running=False)
    update.write_state(
        _state(
            OLD, NEW, world.data, str(world.backup()), True, "rolling-back", new_version_ran=True
        ),
        world.data,
    )
    update.mark_scheduler_starting(NEW, world.data)
    _add_unknown_keys(world.data, NEWER_KEYS | {"scheduler_ran": False})
    assert update.rollback(world.item(), world.seams(), world.say) == 1
    assert world.restored == []
    assert "started its scheduler" in world.text
    state = world.state()
    assert state is not None and state.phase == "stopped"
    assert state.carried["scheduler_ran"] is False


def test_a_rollback_refuses_a_state_without_restored(world: World) -> None:
    """A newer version that dropped ``restored`` (breaking the frozen rule) stops the
    older rollback; it never restores a second time on the default."""
    world.install_login_item(running=False)
    update.write_state(
        _state(
            OLD,
            NEW,
            world.data,
            str(world.backup()),
            True,
            "rolling-back",
            new_version_ran=True,
            restored=True,
        ),
        world.data,
    )
    path = update.update_dir(world.data) / update.STATE_FILE
    raw = json.loads(path.read_text())
    del raw["restored"]
    path.write_text(json.dumps(raw))
    assert update.rollback(world.item(), world.seams(), world.say) == 1
    assert "isn't an update netkeep knows" in world.text
    assert world.restored == []


def test_clear_state_removes_what_an_interrupted_write_left(tmp_path: Path) -> None:
    update.write_state(_state(OLD, NEW, tmp_path, None, True, "finishing"), tmp_path)
    directory = update.update_dir(tmp_path)
    leftovers = [f".{update.STATE_FILE}.abc123.tmp", f".constraints-{OLD}.txt.def456.tmp"]
    for name in leftovers:
        (directory / name).write_text("half")
    marker_temporary = directory / f".{update.SCHEDULER_MARKER}.ghi789.tmp"
    marker_temporary.write_text("a serve is writing this")
    kept = update.constraints_path(OLD, tmp_path)
    kept.write_text("kept\n")
    update.clear_state(tmp_path)
    assert sorted(p.name for p in directory.iterdir()) == sorted([kept.name, marker_temporary.name])


def test_unknown_keys_never_let_a_second_rollback_restore(world: World) -> None:
    world.install_login_item(running=False)
    update.write_state(
        _state(
            OLD,
            NEW,
            world.data,
            str(world.backup()),
            True,
            "rolling-back",
            new_version_ran=True,
            restored=True,
        ),
        world.data,
    )
    _add_unknown_keys(world.data, NEWER_KEYS)
    assert update.rollback(world.item(), world.seams(), world.say) == 1
    assert world.restored == []
    assert "already restored the backup" in world.text


def test_a_phase_a_newer_version_added_is_refused(world: World) -> None:
    """A phase this version doesn't know is never guessed at: the rollback refuses and
    restores nothing."""
    world.install_login_item(running=False)
    update.write_state(
        _state(
            OLD, NEW, world.data, str(world.backup()), True, "rolling-back", new_version_ran=True
        ),
        world.data,
    )
    _add_unknown_keys(world.data, {"phase": "verifying"})
    assert update.rollback(world.item(), world.seams(), world.say) == 1
    assert "isn't an update netkeep knows" in world.text
    assert world.restored == []


def test_check_validates_its_version(world: World) -> None:
    assert update.check(world.seams(), world.say, version="junk") == 1
    assert "--version takes X.Y.Z" in world.text
    assert world.fetched == []


def test_a_bad_port_is_an_update_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connections = _Connections({})
    monkeypatch.setattr("netkeep.services.update.http.client.HTTPSConnection", connections)
    with pytest.raises(update.UpdateError, match="only HTTPS from GitHub"):
        update.https_get("https://github.com:99999/x", max_bytes=10)
    assert connections.opened == []


def test_a_slow_download_runs_out_of_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that drips the body one byte at a time can't hold the download."""
    now = [0.0]

    class Drip(_Response):
        def read(self, limit: int) -> bytes:
            now[0] += 50.0
            return b"x"

    connections = _Connections({"github.com": Drip(200)})
    monkeypatch.setattr("netkeep.services.update.http.client.HTTPSConnection", connections)
    with pytest.raises(update.UpdateError, match="longer than 120 s"):
        update.https_get("https://github.com/x", max_bytes=1000, clock=lambda: now[0])


@pytest.mark.parametrize("field_name", ["from_constraints", "to_constraints"])
def test_a_constraints_path_outside_the_update_folder_is_refused(
    tmp_path: Path, field_name: str
) -> None:
    """The old version's reinstall uses the constraints file the update kept, never one
    a changed state.json points at."""
    update.write_state(_state(OLD, NEW, tmp_path, None, True, "rolling-back"), tmp_path)
    path = update.update_dir(tmp_path) / update.STATE_FILE
    raw = json.loads(path.read_text())
    raw[field_name] = str(tmp_path / "elsewhere" / "constraints.txt")
    path.write_text(json.dumps(raw))
    with pytest.raises(update.UpdateError, match="isn't an update netkeep knows"):
        update.read_state(tmp_path)


# -- other commands while an update is in progress ---------------------------------------


@pytest.fixture
def in_progress() -> Path:
    data = Path(os.environ["NETKEEP_DATA"])
    update.write_state(_state(OLD, NEW, data, None, True, "finishing"), data)
    return data


@pytest.mark.parametrize(
    "args",
    [
        ["campaigns", "linkedin", "prefill", "1"],
        ["--config", "/nonexistent.toml", "tags", "run-rules"],
        ["--config", "update", "tags", "run-rules"],
        ["db", "upgrade"],
        ["backup", "create"],
        # #692 re-review: a --help that is an option's value, or after --, isn't help.
        ["campaigns", "create", "x", "--mailbox", "--help"],
        ["import", "csv", "--", "--help"],
    ],
)
def test_other_commands_refuse_during_an_update(in_progress: Path, args: list[str]) -> None:
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 1
    assert "an update is in progress: run `netkeep update --finish`" in result.output
    assert str(update.update_dir(in_progress) / update.STATE_FILE) in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["version"],
        ["login-item", "status"],
        ["db", "current"],
        ["backup", "list"],
        ["config", "show"],
        ["update", "--finish"],
        ["update", "--rollback"],
        ["campaigns", "linkedin", "prefill", "--help"],
        ["serve", "--help"],
    ],
)
def test_the_update_and_read_only_commands_still_run(in_progress: Path, args: list[str]) -> None:
    result = CliRunner().invoke(cli.app, args)
    assert "an update is in progress" not in result.output


def test_update_check_runs_during_an_update(
    in_progress: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        update,
        "https_get",
        lambda url, *, max_bytes, accept="*/*": json.dumps({"tag_name": "v999.0.0"}).encode(),
    )
    result = CliRunner().invoke(cli.app, ["update", "--check"])
    assert result.exit_code == 0, result.output
    assert "999.0.0 is available" in result.output


def test_version_prints_during_an_update(in_progress: Path) -> None:
    result = CliRunner().invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_no_refusal_without_an_update(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli.app, ["campaigns", "linkedin", "prefill", "1"])
    assert "an update is in progress" not in result.output


def test_command_words() -> None:
    words = cli._command_words
    assert words(["--config", "c.toml", "db", "current", "--x", "y"]) == [(2, "db"), (3, "current")]
    assert words(["update", "--finish"]) == [(0, "update")]
    assert words(["--config=c.toml", "serve"]) == [(1, "serve")]
    assert words(["import", "--", "csv"]) == [(0, "import")]


def _hook_allows(args: list[str]) -> bool:
    """Whether the pre-command hook lets ``args`` through, with an update in progress."""
    root = typer.main.get_command(cli.app)
    ctx = root.make_context("netkeep", [], resilient_parsing=True)
    try:
        cli._refuse_during_an_update(ctx, args)  # type: ignore[arg-type]
    except typer.Exit:
        return False
    return True


def test_the_login_agents_own_serve_passes_the_hook(world: World, in_progress: Path) -> None:
    """#692 re-review: the agent's exact ProgramArguments, with --host and --port."""
    agents = world.tmp / "agents"
    agents.mkdir(exist_ok=True)
    item = world.item()
    document = plistlib.loads(item.render_plist(host="127.0.0.1", port=8123))
    arguments = document["ProgramArguments"]
    assert arguments[1:] == ["serve", "--host", "127.0.0.1", "--port", "8123"]
    assert _hook_allows(arguments[1:])
    assert _hook_allows(plistlib.loads(item.render_plist())["ProgramArguments"][1:])


def test_every_printed_recovery_step_passes_the_hook(world: World, in_progress: Path) -> None:
    """Each netkeep command the update prints, and each the troubleshooting guide's
    Updating section gives, runs while state.json is still there."""
    world.install_login_item()
    world.serve_answers[NEW] = False
    assert world.run_update() == 1  # stop and explain
    printed = re.findall(
        r"\bnetkeep ((?:update|restore|login-item|serve|version)[^.,;:]*)", world.text
    )
    guide = (Path(__file__).parent.parent / "docs" / "guide" / "troubleshooting.md").read_text()
    section = guide[guide.index("## Updating") : guide.index("## Run at login does nothing")]
    documented = re.findall(r"`netkeep ([^`]+)`", section)
    steps = {step.strip() for step in printed + documented}
    assert {"login-item on", "update --finish", "update --rollback"} <= steps
    assert any(step.startswith("restore ") for step in steps)
    for step in sorted(steps):
        assert _hook_allows(step.replace("<backup>", "b.sqlite3").split()), step


@pytest.mark.parametrize(
    "args",
    [
        ["campaigns", "linkedin", "prefill", "--help"],
        ["campaigns", "linkedin", "--help"],
        ["tags", "run-rules", "--help"],
    ],
)
def test_real_help_passes_the_hook(in_progress: Path, args: list[str]) -> None:
    assert _hook_allows(args)


@pytest.mark.parametrize(
    "args",
    [
        ["serve"],
        ["--config", "/c.toml", "serve", "--port", "8123"],
        ["restore", "netkeep-20261010T000000Z.sqlite3"],
        ["login-item", "on"],
        ["login-item", "off"],
        ["login-item", "status"],
    ],
)
def test_serve_and_the_recovery_commands_pass_the_hook(in_progress: Path, args: list[str]) -> None:
    """Checked through the hook alone: running them would start a server or touch launchd."""
    assert _hook_allows(args)


@pytest.mark.parametrize(
    "args",
    [
        ["campaigns", "create", "x", "--mailbox", "--help"],
        ["import", "csv", "--", "--help"],
        ["tags", "run-rules"],
    ],
)
def test_the_hook_refuses_what_isnt_help(in_progress: Path, args: list[str]) -> None:
    assert not _hook_allows(args)


def test_finish_passes_when_the_agents_serve_runs_through_the_cli(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: launchd's bootstrap runs the plist's ProgramArguments through the real
    CLI, hook included, with state.json present. Only the server itself is a fake."""
    served: list[list[str]] = []

    class FakeServer:
        started = True
        should_exit = False

        def __init__(self, _config: object) -> None:
            pass

        def run(self) -> None:
            World._start_agent(world)

    class FakeApp:
        class state:
            pass

    monkeypatch.setattr("netkeep.cli.serve_app", lambda *_a, **_k: FakeApp())
    monkeypatch.setattr("netkeep.cli.uvicorn.Server", FakeServer)
    monkeypatch.setattr("netkeep.cli.uvicorn.Config", lambda *_a, **_k: None)
    monkeypatch.setattr("netkeep.cli.serve_lock.state", world.lock.state)

    def through_the_cli() -> None:
        document = plistlib.loads(world.item().plist_path.read_bytes())
        arguments = document["ProgramArguments"][1:]
        served.append(arguments)
        result = CliRunner().invoke(
            cli.app, arguments, env=document["EnvironmentVariables"], catch_exceptions=False
        )
        assert result.exit_code == 0, result.output

    world.install_login_item()
    world._start_agent = through_the_cli  # type: ignore[method-assign]
    assert world.run_update() == 0, world.text
    assert served and served[-1][0] == "serve"
    assert world.running == NEW
    assert world.state() is None
