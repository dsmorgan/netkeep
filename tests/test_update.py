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
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import pytest
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
        assert self.lock.holder is None or self.lock.holder == os.getpid()
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
        update.UpdateState(OLD, NEW, "a", "b", None, True, "stopped", reason="it broke."),
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
    update.write_state(update.UpdateState(OLD, NEW, "a", "b", None, True, "finishing"), tmp_path)
    update.mark_scheduler_starting(NEW, tmp_path)
    assert update.scheduler_started(NEW, tmp_path)
    assert not update.scheduler_started("9.9.9", tmp_path)


async def test_the_lifespan_marks_before_the_scheduler_starts(
    app: FastAPI, bare_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``serve`` records the marker before anything that can send has started."""
    data = Path(os.environ["NETKEEP_DATA"])
    update.write_state(
        update.UpdateState(OLD, __version__, "a", "b", None, True, "finishing"), data
    )
    seen: list[bool] = []

    class _Stopped:
        executor = None
        scheduler = None

        def stop(self) -> None:
            return None

    def scheduler(*_args: Any, **_kwargs: Any) -> _Stopped:
        seen.append(update.scheduler_started(__version__, data))
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
        return self._body[:limit]


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
    assert update.scheduler_started(NEW, tmp_path)
